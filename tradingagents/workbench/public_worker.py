"""Hosted worker transport and explicit injection helpers."""

import json
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

import httpx

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.azure_client import _TRUSTED_AZURE_AI_HOST_SUFFIXES
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
from tradingagents.workbench.contracts import validate_request
from tradingagents.workbench.public_transport import (
    TransportPolicyError,
    create_pinned_http_client,
    validate_gateway_http_client,
)

BACKEND_DISPATCH_PATH = "/api/internal/worker/provider-dispatch"
BACKEND_EVENTS_PATH = "/api/internal/worker/events"
CAPABILITY_HEADER = "x-tawb-run-capability"
SDK_DUMMY_KEY = "tawb-backend-dispatch"
_MAX_SDK_REQUEST_BYTES = 1024 * 1024
_MAX_BACKEND_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_BACKEND_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_BACKEND_READ_DEADLINE_SECONDS = 60.0
_BACKEND_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=20.0, write=20.0, pool=5.0)
_HOSTED_ERRORS = MappingProxyType({
    "invalid_request": (400, "Hosted worker provider request is invalid."),
    "invalid_response": (502, "Hosted worker provider response is invalid."),
    "client_unavailable": (503, "Hosted worker provider client is unavailable."),
    "revoked": (403, "Hosted worker run authorization is unavailable."),
    "credentials_unavailable": (409, "Hosted worker provider credentials are unavailable."),
    "budget_exhausted": (429, "Hosted worker provider budget is exhausted."),
    "rate_limited": (429, "Hosted worker provider rate limit was reached."),
    "storage_unavailable": (503, "Hosted worker provider storage is unavailable."),
    "upstream_error": (502, "Hosted worker upstream request failed."),
    "timeout": (504, "Hosted worker provider request timed out."),
})
_CURRENT_HOSTED_CONTEXT = ContextVar("tawb_hosted_worker_context", default=None)
_HOSTED_CONFIG_ERROR = "Hosted worker configuration is invalid."
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[/:][A-Za-z0-9][A-Za-z0-9._-]*)*")
_AZURE_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RUN_CAPABILITY_RE = re.compile(
    r"(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.[A-Za-z0-9_-]{43}"
)
_OPENAI_NATIVE_BASE_URL = "https://api.openai.com/v1"
_GOOGLE_NATIVE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_ANTHROPIC_NATIVE_BASE_URL = "https://api.anthropic.com"
_HOSTED_MARKERS = frozenset({
    "TAWB_WORKER_MODE",
    "TAWB_BACKEND_BASE_URL",
    "TAWB_RUN_CAPABILITY",
    "TAWB_PUBLIC_CONFIG",
})


def _https_origin(value: str) -> str:
    parsed = _public_https(value)
    if parsed.path not in ("", "/"):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return f"https://{parsed.hostname}"


def _public_https(value):
    if type(value) is not str or len(value) > 2048 or not re.fullmatch(r"[\x21-\x7e]+", value):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise RuntimeError(_HOSTED_CONFIG_ERROR) from None
    labels = host.split(".")
    if (
        parsed.scheme != "https"
        or value != f"https://{host}{parsed.path}"
        or port is not None
        or len(host) > 253
        or len(labels) < 2
        or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
        or labels[-1] in {"localhost", "local", "internal", "lan", "home"}
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return parsed


def _native_base_url(provider: str, provider_config: MappingProxyType) -> str | None:
    if provider == "openai":
        return provider_config.get("base_url") or _OPENAI_NATIVE_BASE_URL
    if provider == "anthropic":
        return provider_config.get("base_url") or _ANTHROPIC_NATIVE_BASE_URL
    if provider == "google":
        return provider_config.get("base_url") or _GOOGLE_NATIVE_BASE_URL
    if provider == "azure":
        base_url = provider_config.get("base_url")
        return base_url if type(base_url) is str else None
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    if spec is not None:
        return provider_config.get("base_url") or spec.base_url
    return None


def _model_name(value: object, *, azure: bool = False) -> str:
    pattern = _AZURE_MODEL_RE if azure else _MODEL_RE
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value == "custom"
        or ".." in value
        or not pattern.fullmatch(value)
    ):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return value


def _provider_name(value: object) -> str:
    allowed = {name for name, env in PROVIDER_API_KEY_ENV.items() if env is not None}
    if type(value) is not str or value not in allowed:
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return value


def _validated_capability(value: object, environ: dict) -> str:
    if type(value) is not str or not _RUN_CAPABILITY_RE.fullmatch(value):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    match = _RUN_CAPABILITY_RE.fullmatch(value)
    run_id = environ.get("TRADINGAGENTS_RUN_ID")
    if "TRADINGAGENTS_RUN_ID" in environ and (type(run_id) is not str or match.group("run_id") != run_id):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return value


def _data_key_flags(value: object) -> MappingProxyType:
    if type(value) not in (dict, MappingProxyType) or set(value) != {"alpha_vantage", "fred"}:
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    if any(type(flag) is not bool for flag in value.values()):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return MappingProxyType(dict(value))


def _provider_config(mode: str, provider: str, value: object) -> MappingProxyType:
    if value is None:
        value = {}
    if type(value) not in (dict, MappingProxyType):
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    if mode == "managed_identity":
        if value:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        return MappingProxyType({})
    if provider == "azure":
        if set(value) != {"base_url", "api_version"}:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        base_url = value.get("base_url")
        api_version = value.get("api_version")
        parsed = _public_https(base_url)
        if parsed.path not in ("", "/") or not any(parsed.hostname.endswith(suffix) for suffix in _TRUSTED_AZURE_AI_HOST_SUFFIXES):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        if type(api_version) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?", api_version):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        return MappingProxyType({"base_url": str(base_url), "api_version": api_version})
    if set(value) - {"base_url"}:
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    if provider == "openai_compatible":
        parsed = _public_https(value.get("base_url"))
        if parsed.path not in ("", "/", "/v1", "/v1/"):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
    elif "base_url" in value:
        expected = {
            "openai": _OPENAI_NATIVE_BASE_URL,
            "google": "https://generativelanguage.googleapis.com",
            "anthropic": _ANTHROPIC_NATIVE_BASE_URL,
        }.get(provider) or _native_base_url(provider, MappingProxyType({}))
        if type(value["base_url"]) is not str or value["base_url"] != expected:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
    return MappingProxyType(dict(value))


def _route_error() -> RuntimeError:
    return RuntimeError("Hosted worker provider request is invalid.")


def _parsed_https_url(value: str):
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise _route_error() from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise _route_error()
    return parsed


def _exact_query(query: str, *, allowed: tuple[str, str] | None = None):
    if not query:
        if allowed is None:
            return
        raise _route_error()
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _route_error() from None
    if allowed is None or pairs != [allowed]:
        raise _route_error()


def _dispatch_path(config: "HostedWorkerConfig", request: httpx.Request) -> str:
    request_url = _parsed_https_url(str(request.url))
    if request_url.hostname == "worker.invalid":
        if request_url.path.startswith("/data/"):
            _exact_query(request_url.query)
            return request_url.path
        raise _route_error()
    mode = config.mode
    if mode == "managed_identity":
        expected = _parsed_https_url(_OPENAI_NATIVE_BASE_URL)
        if request_url.hostname != expected.hostname or request_url.path not in {"/v1/responses", "/v1/responses/parse"}:
            raise _route_error()
        _exact_query(request_url.query)
        return "/responses"
    if config.provider == "anthropic":
        expected = _parsed_https_url(_ANTHROPIC_NATIVE_BASE_URL)
        if request_url.hostname != expected.hostname or request_url.path != "/v1/messages":
            raise _route_error()
        _exact_query(request_url.query)
        return "/v1/messages"
    if config.provider == "google":
        prefix = _parsed_https_url(_GOOGLE_NATIVE_BASE_URL)
        suffix = ":generateContent"
        if request_url.hostname != prefix.hostname or not request_url.path.startswith("/v1beta/models/") or not request_url.path.endswith(suffix):
            raise _route_error()
        model = request_url.path[len("/v1beta/models/"):-len(suffix)]
        if model not in {config.quick_model, config.deep_model}:
            raise _route_error()
        _exact_query(request_url.query)
        return request_url.path
    if config.provider == "azure":
        base_url = _native_base_url("azure", config.provider_config)
        if base_url is None:
            raise _route_error()
        expected = _parsed_https_url(base_url)
        if request_url.hostname != expected.hostname or request_url.path not in {
            f"/openai/deployments/{config.quick_model}/chat/completions",
            f"/openai/deployments/{config.deep_model}/chat/completions",
        }:
            raise _route_error()
        _exact_query(request_url.query, allowed=("api-version", config.provider_config["api_version"]))
        return request_url.path
    base_url = _native_base_url(config.provider, config.provider_config)
    if base_url is None:
        raise _route_error()
    expected = _parsed_https_url(base_url)
    prefix = expected.path.rstrip("/")
    if request_url.hostname != expected.hostname:
        raise _route_error()
    if config.provider == "openai":
        candidates = {prefix + "/responses": "/responses", prefix + "/responses/parse": "/responses", prefix + "/chat/completions": "/chat/completions"}
    else:
        candidates = {prefix + "/chat/completions": "/chat/completions"}
    mapped = candidates.get(request_url.path)
    if mapped is None:
        raise _route_error()
    _exact_query(request_url.query)
    return mapped


@dataclass(frozen=True, repr=False)
class HostedWorkerConfig:
    backend_base_url: str
    run_capability: str = field(repr=False)
    mode: str
    provider: str
    quick_model: str | None
    deep_model: str | None
    asset_type: str
    data_vendors: MappingProxyType
    provider_config: MappingProxyType
    data_key_configured: MappingProxyType = field(default_factory=lambda: _data_key_flags({"alpha_vantage": False, "fred": False}))

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in {"managed_identity", "api_key"}:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        _provider_name(self.provider)
        if self.mode == "managed_identity" and self.provider != "azure":
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        _validated_capability(self.run_capability, {})
        azure = self.mode == "managed_identity" or self.provider == "azure"
        _model_name(self.quick_model, azure=azure)
        _model_name(self.deep_model, azure=azure)
        if type(self.data_vendors) not in (dict, MappingProxyType):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        try:
            request = validate_request({"asset_type": self.asset_type, "data_vendors": dict(self.data_vendors)})
        except (TypeError, ValueError):
            raise RuntimeError(_HOSTED_CONFIG_ERROR) from None
        object.__setattr__(self, "backend_base_url", _https_origin(self.backend_base_url))
        object.__setattr__(self, "data_vendors", MappingProxyType(dict(request["data_vendors"])))
        object.__setattr__(self, "provider_config", _provider_config(self.mode, self.provider, self.provider_config))
        object.__setattr__(self, "data_key_configured", _data_key_flags(self.data_key_configured))

    @classmethod
    def from_environment(cls, environ: dict) -> "HostedWorkerConfig":
        if environ.get("TAWB_WORKER_MODE") != "hosted" or "TRADINGAGENTS_RUN_ID" not in environ:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        capability = _validated_capability(environ.get("TAWB_RUN_CAPABILITY"), environ)
        raw = environ.get("TAWB_PUBLIC_CONFIG")
        if type(raw) is not str:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        try:
            raw_bytes = raw.encode("utf-8")
            if len(raw_bytes) > 16 * 1024:
                raise ValueError
            payload = _strict_json_object(raw_bytes)
        except (ValueError, RecursionError):
            raise RuntimeError(_HOSTED_CONFIG_ERROR) from None
        if type(payload) is not dict:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        data_key_configured = _data_key_flags(payload.pop("dataKeyConfigured", {"alpha_vantage": False, "fred": False}))
        mode = payload.get("mode")
        if type(mode) is not str or mode not in {"api_key", "managed_identity"}:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        provider = payload.get("provider") if mode == "api_key" else "azure"
        if mode == "managed_identity":
            if set(payload) != {"mode", "quick_model", "deep_model", "asset_type", "data_vendors"}:
                raise RuntimeError(_HOSTED_CONFIG_ERROR)
        elif set(payload) not in (
            {"mode", "provider", "quick_model", "deep_model", "asset_type", "data_vendors"},
            {"mode", "provider", "quick_model", "deep_model", "asset_type", "data_vendors", "provider_config"},
        ):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        provider = _provider_name(provider)
        try:
            request = validate_request(
                {
                    "asset_type": payload["asset_type"],
                    "data_vendors": payload["data_vendors"],
                }
            )
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(_HOSTED_CONFIG_ERROR) from None
        quick_model = _model_name(payload.get("quick_model"), azure=(mode == "managed_identity" or provider == "azure"))
        deep_model = _model_name(payload.get("deep_model"), azure=(mode == "managed_identity" or provider == "azure"))
        provider_config = _provider_config(mode, provider, payload.get("provider_config"))
        return cls(
            backend_base_url=_https_origin(environ.get("TAWB_BACKEND_BASE_URL")),
            run_capability=capability,
            mode=mode,
            provider=provider,
            quick_model=quick_model,
            deep_model=deep_model,
            asset_type=request["asset_type"],
            data_vendors=MappingProxyType(dict(request["data_vendors"])),
            provider_config=provider_config,
            data_key_configured=data_key_configured,
        )


def _google_json_schema(schema):
    if type(schema) is not dict:
        raise ValueError
    result = dict(schema)
    kind = result.get("type")
    if type(kind) is str and kind in {"STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT", "NULL"}:
        result["type"] = kind.lower()
    if "properties" in result:
        result["properties"] = {name: _google_json_schema(value) for name, value in result["properties"].items()}
    if "items" in result:
        result["items"] = _google_json_schema(result["items"])
    for keyword in ("anyOf", "allOf", "oneOf"):
        if keyword in result:
            result[keyword] = [_google_json_schema(value) for value in result[keyword]]
    return result


class GatewayDispatchTransport(httpx.BaseTransport):
    def __init__(self, config: HostedWorkerConfig, backend_client: httpx.Client):
        self._config = config
        self._backend = backend_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._config.provider == "google":
            failure = False
            try:
                if len(request.content) > _MAX_SDK_REQUEST_BYTES:
                    raise ValueError
                body = _strict_json_object(request.content)
                if body.get("safetySettings") == []:
                    del body["safetySettings"]
                generation = body.get("generationConfig")
                if type(generation) is dict and type(generation.get("candidateCount")) is int and generation["candidateCount"] == 1:
                    del generation["candidateCount"]
                for tool in body.get("tools", []):
                    for declaration in tool.get("functionDeclarations", []):
                        if "parameters" in declaration and "parameters_json_schema" not in declaration:
                            declaration["parameters_json_schema"] = _google_json_schema(declaration.pop("parameters"))
                request = httpx.Request(request.method, request.url, content=_bounded_json_bytes(body, _MAX_SDK_REQUEST_BYTES))
            except Exception:
                failure = True
            if failure:
                raise HostedProviderError("invalid_request")
        return _dispatch_backend_request(self._config, self._backend, request)

    def close(self):
        return None


def _post_telemetry_events(config, backend, lines):
    try:
        if (type(lines) is not list or not lines or len(lines) > 1000
                or any(type(line) is not str for line in lines)):
            return
        content = _bounded_json_bytes({"lines": lines}, _MAX_BACKEND_REQUEST_BYTES)
        request = httpx.Request(
            "POST", config.backend_base_url + BACKEND_EVENTS_PATH,
            headers={CAPABILITY_HEADER: config.run_capability, "content-type": "application/json",
                     "accept": "application/json", "accept-encoding": "identity"},
            content=content, extensions={"timeout": httpx.Timeout(2.0).as_dict()},
        )
        response = _send_backend_request(backend, request)
        raw = _read_backend_response_bytes(response, monotonic() + 2.0)
        if response.status_code < 200 or response.status_code >= 300:
            return
        payload = _strict_json_object(raw)
        if set(payload) != {"accepted"} or payload["accepted"] is not True:
            return
    except BaseException:
        return


class _TelemetryContext:
    def __init__(self, config):
        self.config = config
        self._backend = create_pinned_http_client()

    def post_events(self, lines):
        _post_telemetry_events(self.config, self._backend, lines)

    def close(self):
        self._backend.close()


class HostedWorkerContext:
    def __init__(self, config: HostedWorkerConfig, *, http_client: httpx.Client | None = None):
        self.config = config
        self._closed = False
        failure = False
        try:
            self._backend = create_pinned_http_client() if http_client is None else validate_gateway_http_client(http_client)
        except Exception:
            failure = True
        if failure:
            raise HostedProviderError("client_unavailable")
        self._transport_client = httpx.Client(
            transport=GatewayDispatchTransport(config, self._backend),
            follow_redirects=False,
            trust_env=False,
            timeout=_BACKEND_TIMEOUT,
        )

    def close(self):
        if self._closed:
            return None
        self._closed = True
        failure = False
        try:
            try:
                self._transport_client.close()
            finally:
                self._backend.close()
        except Exception:
            failure = True
        if failure:
            raise HostedProviderError("upstream_error")

    def _require_open(self):
        if self._closed:
            raise HostedProviderError("upstream_error")

    def telemetry_context(self):
        self._require_open()
        return _TelemetryContext(self.config)

    def create_transport(self) -> GatewayDispatchTransport:
        self._require_open()
        return GatewayDispatchTransport(self.config, self._backend)

    @contextmanager
    def activate(self):
        from tradingagents.dataflows.config import config_scope

        token = _CURRENT_HOSTED_CONTEXT.set(self)
        try:
            with config_scope({"data_vendors": dict(self.config.data_vendors), "tool_vendors": {}}):
                yield self
        finally:
            _CURRENT_HOSTED_CONTEXT.reset(token)

    def dispatch(self, method: str, path: str, body: dict | None = None):
        self._require_open()
        failure = False
        try:
            content = _bounded_json_bytes({} if body is None else body, _MAX_SDK_REQUEST_BYTES)
            request = httpx.Request(method, "https://worker.invalid" + path, content=content)
        except Exception:
            failure = True
        if failure:
            raise HostedProviderError("invalid_request")
        response = _dispatch_backend_request(self.config, self._backend, request)
        if response.status_code >= 400:
            fred_body = _fred_bad_request_body(path, response.status_code, response.json())
            if fred_body is not None:
                import requests

                from tradingagents.dataflows.fred import _parse_response

                native_response = requests.Response()
                native_response.status_code = 400
                native_response._content = json.dumps(fred_body).encode("utf-8")
                return _parse_response(native_response)
            raise HostedProviderError("upstream_error")
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text

    def post_events(self, lines: list[str]) -> None:
        self._require_open()
        _post_telemetry_events(self.config, self._backend, lines)

    def create_llm_client(self, provider: str, model: str, base_url: str | None = None, **kwargs):
        _provider_name(provider)
        _model_name(model, azure=self.config.mode == "managed_identity" or provider == "azure")
        if provider != self.config.provider or model not in {self.config.quick_model, self.config.deep_model}:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        configured_base_url = _native_base_url(provider, self.config.provider_config)
        if base_url is not None and base_url != configured_base_url:
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        llm_kwargs = dict(kwargs)
        llm_kwargs["max_retries"] = 0
        if not any(llm_kwargs.get(key) is not None for key in ("max_tokens", "max_output_tokens", "max_completion_tokens")):
            llm_kwargs["max_output_tokens" if provider == "google" else "max_tokens"] = 1024
        if self.config.mode == "managed_identity":
            llm_kwargs["http_client"] = self._transport_client
            llm_kwargs["api_key"] = SDK_DUMMY_KEY
            return create_llm_client("openai", model, base_url=_OPENAI_NATIVE_BASE_URL, **llm_kwargs)
        if provider == "google":
            client_args = dict(llm_kwargs.get("client_args") or {})
            client_args["transport"] = self.create_transport()
            llm_kwargs["client_args"] = client_args
            llm_kwargs["google_api_key"] = SDK_DUMMY_KEY
        elif provider == "azure":
            llm_kwargs["http_client"] = self._transport_client
            llm_kwargs["auth_mode"] = "api_key"
            llm_kwargs["azure_endpoint"] = configured_base_url
            llm_kwargs["azure_deployment"] = model
            llm_kwargs["api_version"] = self.config.provider_config.get("api_version")
            llm_kwargs["api_key"] = SDK_DUMMY_KEY
        else:
            llm_kwargs["http_client"] = self._transport_client
            llm_kwargs["api_key"] = SDK_DUMMY_KEY
        return create_llm_client(provider, model, base_url=base_url or configured_base_url, **llm_kwargs)


def hosted_worker_context_from_environment(environ: dict) -> HostedWorkerContext | None:
    if "TAWB_WORKER_MODE" not in environ:
        if _HOSTED_MARKERS & set(environ):
            raise RuntimeError(_HOSTED_CONFIG_ERROR)
        return None
    return HostedWorkerContext(HostedWorkerConfig.from_environment(environ))


def current_hosted_worker_context() -> HostedWorkerContext | None:
    return _CURRENT_HOSTED_CONTEXT.get()


class HostedProviderError(RuntimeError):
    def __init__(self, code="upstream_error"):
        self.code = code if type(code) is str and code in _HOSTED_ERRORS else "upstream_error"
        self.status_code, message = _HOSTED_ERRORS[self.code]
        super().__init__(message)


def _strict_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _strict_json_object(raw: bytes) -> dict:
    text = raw.decode("utf-8")
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError
        elif character in "]}":
            depth -= 1
    value = json.loads(text, object_pairs_hook=_strict_json_pairs,
                       parse_constant=_reject_json_constant, parse_float=_finite_json_float)
    if type(value) is not dict:
        raise ValueError
    return value


def _bounded_json_bytes(value, limit):
    raw = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    for part in encoder.iterencode(value):
        encoded = part.encode("ascii")
        if len(raw) + len(encoded) > limit:
            raise ValueError
        raw.extend(encoded)
    return bytes(raw)


def _check_read_deadline(deadline):
    if monotonic() >= deadline:
        raise HostedProviderError("timeout")


def _read_backend_response_bytes(response: httpx.Response, deadline: float) -> bytes:
    raw = bytearray()
    try:
        _check_read_deadline(deadline)
        if not 200 <= response.status_code < 300 and response.status_code != 429:
            raise _backend_status_error(response.status_code, b"")
        invalid_code = "rate_limited" if response.status_code == 429 else "invalid_response"
        if response.headers.get("content-encoding") not in (None, "identity"):
            raise HostedProviderError(invalid_code)
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HostedProviderError(invalid_code)
        if any(len(response.headers.get_list(name)) > 1 for name in ("content-type", "content-encoding", "content-length")):
            raise HostedProviderError(invalid_code)
        content_length = response.headers.get("content-length")
        if content_length is not None and (
            not re.fullmatch(r"[0-9]{1,20}", content_length) or int(content_length) > _MAX_BACKEND_RESPONSE_BYTES
        ):
            raise HostedProviderError(invalid_code)
        source = (response.content,) if response.is_stream_consumed else response.iter_raw()
        iterator = iter(source)
        while True:
            _check_read_deadline(deadline)
            chunk = next(iterator, None)
            _check_read_deadline(deadline)
            if chunk is None:
                break
            if len(raw) + len(chunk) > _MAX_BACKEND_RESPONSE_BYTES:
                raise HostedProviderError(invalid_code)
            raw.extend(chunk)
    finally:
        response.close()
    return bytes(raw)


def _backend_status_error(status, raw):
    code = {403: "revoked", 409: "credentials_unavailable", 429: "rate_limited",
            503: "storage_unavailable"}.get(status, "upstream_error")
    if status == 429:
        try:
            payload = _strict_json_object(raw)
            codes = [payload["code"]] if "code" in payload else []
            if type(payload.get("error")) is dict and "code" in payload["error"]:
                codes.append(payload["error"]["code"])
            if codes and all(value == "budget_exhausted" for value in codes):
                code = "budget_exhausted"
        except Exception:
            pass
    return HostedProviderError(code)


def _redact_capability(value, capability):
    if type(value) is str:
        return value.replace(capability, "[redacted]") if capability else value
    if type(value) is list:
        return [_redact_capability(item, capability) for item in value]
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            clean_key = _redact_capability(key, capability)
            if clean_key in result:
                raise HostedProviderError("invalid_response")
            result[clean_key] = _redact_capability(item, capability)
        return result
    return value


def _fred_bad_request_body(path, status, body):
    if (
        path not in {"/data/fred/series", "/data/fred/series/observations"}
        or status != 400
        or type(body) is not dict
    ):
        return None
    message = body.get("error_message")
    if (
        type(message) is not str
        or not re.fullmatch(r"[\x20-\x7e]{1,512}", message)
        or not message.strip()
        or "://" in message
    ):
        return None
    return {"error_message": message}


def _send_backend_request(backend_client: httpx.Client, request: httpx.Request) -> httpx.Response:
    validate_gateway_http_client(backend_client)
    transport = getattr(backend_client, "_transport", None)
    if transport is None or not hasattr(transport, "handle_request"):
        raise HostedProviderError("client_unavailable")
    return transport.handle_request(request)


def _dispatch_backend_request(config: HostedWorkerConfig, backend_client: httpx.Client, request: httpx.Request) -> httpx.Response:
    failure_code = "invalid_request"
    try:
        if len(request.content) > _MAX_SDK_REQUEST_BYTES:
            raise HostedProviderError("invalid_request")
        body = _strict_json_object(request.content)
        path = _dispatch_path(config, request)
        content = _bounded_json_bytes({
            "method": request.method,
            "path": path,
            "headers": {},
            "body": body,
        }, _MAX_BACKEND_REQUEST_BYTES)
        backend_request = httpx.Request(
            "POST", config.backend_base_url + BACKEND_DISPATCH_PATH,
            headers={CAPABILITY_HEADER: config.run_capability, "content-type": "application/json",
                     "accept": "application/json", "accept-encoding": "identity"},
            content=content, extensions={"timeout": _BACKEND_TIMEOUT.as_dict()},
        )
        failure_code = "upstream_error"
        deadline = monotonic() + _BACKEND_READ_DEADLINE_SECONDS
        response = _send_backend_request(backend_client, backend_request)
        raw = _read_backend_response_bytes(response, deadline)
        if not 200 <= response.status_code < 300:
            raise _backend_status_error(response.status_code, raw)
        failure_code = "invalid_response"
        payload = _strict_json_object(raw)
        if set(payload) != {"status_code", "body"}:
            raise HostedProviderError("invalid_response")
        status = payload["status_code"]
        if type(status) is not int or not 200 <= status <= 599 or type(payload["body"]) not in (dict, str):
            raise HostedProviderError("invalid_response")
        result_body = _redact_capability(payload["body"], config.run_capability)
        if status >= 300:
            result_body = _fred_bad_request_body(path, status, result_body) or {
                "error": {"message": _HOSTED_ERRORS["upstream_error"][1], "code": "upstream_error", "type": "upstream_error"},
            }
        _check_read_deadline(deadline)
        if type(result_body) is dict:
            return httpx.Response(status, json=result_body, request=request)
        return httpx.Response(status, text=result_body, request=request)
    except HostedProviderError as error:
        failure_code = error.code
    except httpx.TimeoutException:
        failure_code = "timeout"
    except TransportPolicyError:
        failure_code = "client_unavailable"
    except Exception:
        pass
    raise HostedProviderError(failure_code)


def hosted_public_config_for_settings(*, mode: str, provider: str, quick_model: str | None,
                                      deep_model: str | None, asset_type: str,
                                      data_vendors: dict, provider_config: dict | None = None,
                                      data_key_configured: dict | None = None) -> dict:
    if type(mode) is not str or mode not in {"api_key", "managed_identity"}:
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    _provider_name(provider)
    if mode == "managed_identity" and provider != "azure":
        raise RuntimeError(_HOSTED_CONFIG_ERROR)
    _model_name(quick_model, azure=mode == "managed_identity" or provider == "azure")
    _model_name(deep_model, azure=mode == "managed_identity" or provider == "azure")
    provider_config = dict(_provider_config(mode, provider, provider_config))
    validated = validate_request({"asset_type": asset_type, "data_vendors": data_vendors})
    payload = {
        "mode": mode,
        "quick_model": quick_model,
        "deep_model": deep_model,
        "asset_type": validated["asset_type"],
        "data_vendors": validated["data_vendors"],
        "dataKeyConfigured": dict(_data_key_flags(
            {"alpha_vantage": False, "fred": False} if data_key_configured is None else data_key_configured,
        )),
    }
    if mode != "managed_identity":
        payload.update(provider=provider, provider_config=provider_config)
    return payload
