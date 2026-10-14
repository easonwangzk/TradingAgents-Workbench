# Modified for public distribution: installation identifiers replaced with examples.
import json
import math
import traceback
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tests.test_public_accounts import ORIGIN, OWNER_PASSWORD
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.contracts import default_request, validate_request
from tradingagents.workbench.public_store import EncryptedStore

FIXTURE_NOW = 1800000000
FIXTURE_RUN_CAPABILITY = "11111111-1111-4111-8111-111111111111." + ("a" * 43)


class BackendStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.iterations = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def bounded_worker_config():
    from tradingagents.workbench.public_worker import HostedWorkerConfig

    return HostedWorkerConfig.from_environment({
        "TAWB_WORKER_MODE": "hosted",
        "TAWB_BACKEND_BASE_URL": "https://workbench.example",
        "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
        "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
        "TAWB_PUBLIC_CONFIG": json.dumps({
            "mode": "api_key", "provider": "openai",
            "quick_model": "gpt-5", "deep_model": "gpt-5",
            "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
        }),
    })


def test_bounded_backend_content_length_rejected_without_iteration(bounded_worker_config):
    from tradingagents.workbench.public_worker import _dispatch_backend_request

    stream = BackendStream([b'{"status_code":200,"body":{}}'])
    response = httpx.Response(200, headers={
        "content-type": "application/json", "content-length": str(16 * 1024 * 1024 + 1),
    }, stream=stream)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: response)) as backend,
        pytest.raises(RuntimeError) as caught,
    ):
        _dispatch_backend_request(bounded_worker_config, backend, httpx.Request(
            "POST", "https://api.openai.com/v1/responses", json={"model": "gpt-5"},
        ))
    assert type(caught.value).__name__ == "HostedProviderError"
    assert caught.value.code == "invalid_response"
    assert caught.value.status_code == 502
    assert stream.iterations == 0
    assert stream.closed and response.is_closed


def _bounded_dispatch(config, handler, *, content=b'{}', url="https://api.openai.com/v1/responses"):
    from tradingagents.workbench.public_worker import _dispatch_backend_request

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as backend:
        return _dispatch_backend_request(config, backend, httpx.Request("POST", url, content=content))


def _assert_hosted_error(error, code, status_code):
    from tradingagents.workbench.public_worker import HostedProviderError

    assert type(error) is HostedProviderError
    assert error.code == code
    assert error.status_code == status_code
    assert error.__cause__ is None and error.__context__ is None
    assert not hasattr(error, "request") and not hasattr(error, "response")
    visible = str(error) + repr(error) + repr(vars(error)) + "".join(traceback.format_exception(error))
    assert FIXTURE_RUN_CAPABILITY not in visible
    assert "backend-secret-echo" not in visible


def test_bounded_backend_chunked_oversize_stops_and_closes(bounded_worker_config):
    stream = BackendStream([b" " * (1024 * 1024)] * 17 + [b"never-read"])
    response = httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "invalid_response", 502)
    assert stream.iterations == 17
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("headers", [
    {"content-type": "application/json", "content-encoding": "gzip"},
    {"content-type": "application/json", "content-encoding": "br"},
    {"content-type": "application/json", "content-encoding": "identity, identity"},
    {"content-type": "text/html"},
    {},
    [("content-type", "application/json"), ("content-type", "application/json")],
    {"content-type": "application/json", "content-length": "-1"},
    {"content-type": "application/json", "content-length": "1.5"},
    {"content-type": "application/json", "content-length": "+5"},
    [("content-type", "application/json"), ("content-length", "5"), ("content-length", "5")],
])
def test_bounded_backend_invalid_headers_never_iterate(bounded_worker_config, headers):
    stream = BackendStream([b'{"status_code":200,"body":{}}'])
    response = httpx.Response(200, headers=headers, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "invalid_response", 502)
    assert stream.iterations == 0
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("raw", [
    b'[]', b'null', b'{', b'\xff',
    b'{"status_code":true,"body":{}}',
    b'{"status_code":"200","body":{}}',
    b'{"status_code":200.0,"body":{}}',
    b'{"status_code":199,"body":{}}',
    b'{"status_code":600,"body":{}}',
    b'{"status_code":200,"body":[]}',
    b'{"status_code":200,"body":null}',
    b'{"status_code":200,"body":1}',
    b'{"status_code":200,"body":true}',
    b'{"status_code":200,"body":{},"extra":0}',
    b'{"status_code":200}',
    b'{"status_code":200,"status_code":200,"body":{}}',
    b'{"status_code":200,"body":{"nested":{"key":1,"key":2}}}',
    b'{"status_code":200,"body":{"value":NaN}}',
    b'{"status_code":200,"body":{"value":Infinity}}',
    b'{"status_code":200,"body":{"value":-Infinity}}',
    b'{"status_code":200,"body":{"value":1e9999}}',
    b'{"status_code":200,"body":{"value":' + b'[' * 63 + b'0' + b']' * 63 + b'}}',
    b'{"status_code":200,"body":{"value":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}}',
], ids=lambda raw: f"bytes-{len(raw)}")
def test_bounded_backend_strict_envelope(bounded_worker_config, raw):
    stream = BackendStream([raw])
    response = httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "invalid_response", 502)
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("raw", [
    b'', b'[]', b'null', b'{', b'\xff',
    b'{"key":1,"key":2}', b'{"nested":{"key":1,"key":2}}',
    b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}', b'{"value":1e9999}',
    b'{"value":' + b'[' * 64 + b'0' + b']' * 64 + b'}',
    b'{"value":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
    b'{"text":"' + b'a' * (1024 * 1024 - 10) + b'"}',
], ids=lambda raw: f"bytes-{len(raw)}")
def test_bounded_backend_invalid_sdk_json_makes_zero_http_calls(bounded_worker_config, raw):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status_code": 200, "body": {}})

    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, respond, content=raw)
    _assert_hosted_error(caught.value, "invalid_request", 400)
    assert calls == []


def test_bounded_backend_large_serialized_envelope_makes_zero_http_calls(bounded_worker_config):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status_code": 200, "body": {}})

    raw = json.dumps({"text": "\U00010000" * 200000}, ensure_ascii=False).encode("utf-8")
    assert len(raw) < 1024 * 1024
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, respond, content=raw)
    _assert_hosted_error(caught.value, "invalid_request", 400)
    assert calls == []


def test_bounded_backend_explicit_request_overrides_infinite_client_timeout(bounded_worker_config, monkeypatch):
    from tradingagents.workbench.public_worker import _dispatch_backend_request

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"status_code": 200, "body": {"ok": True}})

    with httpx.Client(transport=httpx.MockTransport(respond), timeout=None) as backend:
        def merged_request_forbidden(*args, **kwargs):
            pytest.fail("Dispatch must build its own request")

        monkeypatch.setattr(backend, "build_request", merged_request_forbidden)
        result = _dispatch_backend_request(bounded_worker_config, backend, httpx.Request(
            "POST", "https://api.openai.com/v1/responses",
            headers={"authorization": "Bearer sdk-secret", "cookie": "sdk-secret", "x-custom": "sdk-secret"},
            content=b'{"prompt":"ordinary sk-user-text stays untouched"}',
        ))
    assert result.json() == {"ok": True}
    assert len(requests) == 1
    sent = requests[0]
    assert str(sent.url) == "https://workbench.example/api/internal/worker/provider-dispatch"
    assert sent.method == "POST"
    assert set(sent.headers) == {"host", "content-length", "content-type", "accept", "accept-encoding", "x-tawb-run-capability"}
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["accept"] == "application/json"
    assert sent.headers["x-tawb-run-capability"] == FIXTURE_RUN_CAPABILITY
    assert all(isinstance(value, (int, float)) and math.isfinite(value) and 0 < value <= 20 for value in sent.extensions["timeout"].values())
    assert sent.extensions["timeout"].keys() == {"connect", "read", "write", "pool"}
    assert _json_request_body(sent) == {"method": "POST", "path": "/responses", "headers": {}, "body": {"prompt": "ordinary sk-user-text stays untouched"}}


@pytest.mark.parametrize("mutation", ["headers", "auth", "cookies", "hooks", "redirects", "params", "base_url", "mounts"])
def test_bounded_backend_revalidates_mutated_client_before_each_request(bounded_worker_config, mutation):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    calls = []
    hooks = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status_code": 200, "body": {"ok": True}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as backend:
        context = HostedWorkerContext(bounded_worker_config, http_client=backend)
        assert context.dispatch("POST", "/data/fred/series") == {"ok": True}
        if mutation == "headers":
            backend.headers["x-default-secret"] = "backend-secret-echo"
        elif mutation == "auth":
            backend.auth = ("username", "backend-secret-echo")
        elif mutation == "cookies":
            backend.cookies.set("session", "backend-secret-echo")
        elif mutation == "hooks":
            backend.event_hooks["request"].append(lambda request: hooks.append(request))
        elif mutation == "redirects":
            backend.follow_redirects = True
        elif mutation == "params":
            backend.params = {"token": "backend-secret-echo"}
        elif mutation == "base_url":
            backend.base_url = "https://other.example"
        elif mutation == "mounts":
            backend._mounts[httpx._utils.URLPattern("all://")] = httpx.MockTransport(respond)
        with pytest.raises(RuntimeError) as caught:
            context.dispatch("POST", "/data/fred/series")
    _assert_hosted_error(caught.value, "client_unavailable", 503)
    assert len(calls) == 1 and hooks == []


def test_bounded_backend_rejects_changed_pinned_trust_env(bounded_worker_config, monkeypatch):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    context = HostedWorkerContext(bounded_worker_config)
    try:
        assert context._backend.trust_env is False
        assert context._backend._transport._pool._retries == 0
        monkeypatch.setattr(context._backend._transport, "handle_request", lambda request: pytest.fail("No network allowed"))
        context._backend._trust_env = True
        with pytest.raises(RuntimeError) as caught:
            context.dispatch("POST", "/data/fred/series")
        _assert_hosted_error(caught.value, "client_unavailable", 503)
    finally:
        context.close()


@pytest.mark.parametrize("status,payload,code,expected_status", [
    (403, {"error": "backend-secret-echo"}, "revoked", 403),
    (409, {"error": "backend-secret-echo"}, "credentials_unavailable", 409),
    (429, {"error": {"code": "budget_exhausted", "message": "backend-secret-echo"}}, "budget_exhausted", 429),
    (429, {"code": "budget_exhausted"}, "budget_exhausted", 429),
    (429, {"error": "budget_exhausted"}, "rate_limited", 429),
    (429, {"error": {"message": "budget_exhausted"}}, "rate_limited", 429),
    (429, {"code": "other", "error": {"code": "budget_exhausted"}}, "rate_limited", 429),
    (503, {"error": "backend-secret-echo"}, "storage_unavailable", 503),
    (502, {"error": "backend-secret-echo"}, "upstream_error", 502),
    (500, {"code": "budget_exhausted"}, "upstream_error", 502),
    (302, {"error": "backend-secret-echo"}, "upstream_error", 502),
])
def test_bounded_backend_http_errors_are_typed_not_echoed(bounded_worker_config, status, payload, code, expected_status):
    stream = BackendStream([json.dumps(payload).encode()])
    response = httpx.Response(status, headers={"content-type": "application/json", "location": "https://other.example"}, stream=stream)
    calls = []

    def respond(request):
        calls.append(request)
        return response

    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, respond)
    _assert_hosted_error(caught.value, code, expected_status)
    assert len(calls) == 1 and stream.closed and response.is_closed


@pytest.mark.parametrize("failure_kind", ["send_timeout", "read_timeout", "send_error", "read_error", "close_error"])
def test_bounded_backend_transport_failures_have_no_request_or_exception_chain(bounded_worker_config, failure_kind):
    calls = []

    class FailingStream(BackendStream):
        def __iter__(self):
            if failure_kind == "read_timeout":
                raise httpx.ReadTimeout(FIXTURE_RUN_CAPABILITY, request=calls[0])
            if failure_kind == "read_error":
                raise RuntimeError(FIXTURE_RUN_CAPABILITY)
            yield from super().__iter__()

        def close(self):
            super().close()
            if failure_kind == "close_error":
                raise RuntimeError(FIXTURE_RUN_CAPABILITY)

    stream = FailingStream([b'{"status_code":200,"body":{}}'])
    response = httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    def respond(request):
        calls.append(request)
        if failure_kind == "send_timeout":
            raise httpx.ReadTimeout(FIXTURE_RUN_CAPABILITY, request=request)
        if failure_kind == "send_error":
            raise httpx.HTTPStatusError(FIXTURE_RUN_CAPABILITY, request=request, response=response)
        return response

    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, respond)
    _assert_hosted_error(caught.value, "timeout" if "timeout" in failure_kind else "upstream_error", 504 if "timeout" in failure_kind else 502)
    assert len(calls) == 1
    if not failure_kind.startswith("send"):
        assert stream.closed and response.is_closed


def test_bounded_backend_absolute_deadline_stops_trickle(bounded_worker_config, monkeypatch):
    from tradingagents.workbench import public_worker

    clock = [0.0]
    monkeypatch.setattr(public_worker, "monotonic", lambda: clock[0], raising=False)

    def trickle():
        while True:
            clock[0] += 10
            if clock[0] > 100:
                raise AssertionError("Read deadline not enforced")
            yield b" "

    stream = BackendStream(trickle())
    response = httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "timeout", 504)
    assert clock[0] <= 90
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 599])
def test_bounded_backend_native_status_preserved_with_safe_sdk_body(bounded_worker_config, status):
    from openai import APIStatusError, OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerContext

    calls = []
    responses = []

    def respond(request):
        calls.append(request)
        response = httpx.Response(200, json={"status_code": status, "body": {
            "error": {"message": FIXTURE_RUN_CAPABILITY, "type": "backend-secret-echo"},
            "model": FIXTURE_RUN_CAPABILITY, "debug": "backend-secret-echo",
        }})
        responses.append(response)
        return response

    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        llm = context.create_llm_client("openai", "gpt-5", max_retries=9).get_llm()
        assert llm.max_retries == 0
        sdk = OpenAI(api_key="dummy", max_retries=llm.max_retries, http_client=llm.http_client)
        with pytest.raises(APIStatusError) as caught:
            sdk.responses.create(model="gpt-5", input="hi")
        assert caught.value.status_code == status
        visible = repr(caught.value) + str(caught.value) + repr(caught.value.body) + caught.value.response.text + "".join(traceback.format_exception(caught.value))
        assert FIXTURE_RUN_CAPABILITY not in visible and "backend-secret-echo" not in visible
        assert len(calls) == 1 and all(response.is_closed for response in responses)
    finally:
        context.close()


@pytest.mark.parametrize("body", [
    "prefix " + FIXTURE_RUN_CAPABILITY,
    {"model": FIXTURE_RUN_CAPABILITY, "nested": [{FIXTURE_RUN_CAPABILITY: "echo " + FIXTURE_RUN_CAPABILITY}]},
])
def test_bounded_backend_success_redacts_capability_including_nested_keys(bounded_worker_config, body):
    response = httpx.Response(200, json={"status_code": 200, "body": body})
    result = _bounded_dispatch(bounded_worker_config, lambda request: response)
    assert FIXTURE_RUN_CAPABILITY not in result.text
    assert "[redacted]" in result.text
    assert result.status_code == 200 and response.is_closed


def test_bounded_backend_accepts_escaped_large_body_and_exact_sdk_limit(bounded_worker_config):
    body = "\\" * (4 * 1024 * 1024 + 1)
    raw = json.dumps({"status_code": 200, "body": body}).encode()
    assert 8 * 1024 * 1024 < len(raw) < 16 * 1024 * 1024
    stream = BackendStream([raw[:1024], raw[1024:]])
    response = httpx.Response(200, headers={"content-type": "application/json; charset=utf-8", "content-encoding": "identity"}, stream=stream)
    sdk_body = b'{"text":"' + b'a' * (1024 * 1024 - 11) + b'"}'
    assert len(sdk_body) == 1024 * 1024
    result = _bounded_dispatch(bounded_worker_config, lambda request: response, content=sdk_body)
    assert result.text == body
    assert stream.closed and response.is_closed


def test_bounded_backend_data_error_does_not_fall_back(bounded_worker_config, monkeypatch):
    from tradingagents.dataflows import alpha_vantage_common, fred
    from tradingagents.workbench.public_worker import HostedWorkerContext

    monkeypatch.setattr(alpha_vantage_common.requests, "get", lambda *args, **kwargs: pytest.fail("No fallback network allowed"))
    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": FIXTURE_RUN_CAPABILITY}),
    )))
    try:
        with context.activate():
            for operation in (
                lambda: alpha_vantage_common._make_api_request("GLOBAL_QUOTE", {"symbol": "MSFT"}),
                lambda: fred._request("series", {"series_id": "DGS10"}),
            ):
                with pytest.raises(RuntimeError) as caught:
                    operation()
                _assert_hosted_error(caught.value, "revoked", 403)
        assert FIXTURE_RUN_CAPABILITY not in repr(context) + repr(context.config)
    finally:
        context.close()


@pytest.mark.parametrize("body", [
    {"error_message": {"nested": "backend-secret-echo"}},
    {"error_message": ["backend-secret-echo"]},
    {"error_message": None}, {"error_message": True}, {"error_message": 400},
    {"error_message": ""}, {"error_message": " "},
    {"error_message": "Bad request\nbackend-secret-echo"},
    {"error_message": "Bad request\x00"},
    {"error_message": "a" * 513},
    {"error_message": "https://workbench.example/backend-secret-echo"},
    {"error": {"error_message": "backend-secret-echo"}},
    "backend-secret-echo",
])
def test_hosted_fred_rejects_unvalidated_error_message(bounded_worker_config, body):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status_code": 400, "body": body}),
    )))
    try:
        with pytest.raises(RuntimeError) as caught:
            context.dispatch("POST", "/data/fred/series", {"params": {"series_id": "NOSUCHSERIES"}})
        _assert_hosted_error(caught.value, "upstream_error", 502)
    finally:
        context.close()


@pytest.mark.parametrize("path,status", [
    ("/data/fred/arbitrary", 400), ("/data/fred/series/", 400),
    ("/data/alpha_vantage", 400), ("/data/fred/series", 401),
    ("/data/fred/series", 500),
])
def test_hosted_fred_error_exception_is_route_and_status_scoped(bounded_worker_config, path, status):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "status_code": status, "body": {"error_message": "Bad Request. The series does not exist."},
        }),
    )))
    try:
        with pytest.raises(RuntimeError) as caught:
            context.dispatch("POST", path)
        _assert_hosted_error(caught.value, "upstream_error", 502)
    finally:
        context.close()


def test_hosted_data_success_keeps_native_text(bounded_worker_config):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    text = "timestamp,close\n2026-09-01,100\n"
    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status_code": 200, "body": text}),
    )))
    try:
        assert context.dispatch("POST", "/data/alpha_vantage") == text
    finally:
        context.close()


@pytest.mark.parametrize("failing_clients", [("transport",), ("backend",), ("transport", "backend")])
def test_hosted_context_close_attempts_both_once_without_fallback(bounded_worker_config, monkeypatch, failing_clients):
    from tradingagents.dataflows import fred
    from tradingagents.workbench.public_worker import HostedWorkerContext

    closed = []

    def close_client(name):
        closed.append(name)
        if name in failing_clients:
            raise RuntimeError("backend-secret-echo " + FIXTURE_RUN_CAPABILITY)

    backend_transport = httpx.MockTransport(lambda request: pytest.fail("Closed context sent a request"))
    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=backend_transport))
    monkeypatch.setattr(backend_transport, "close", lambda: close_client("backend"))
    monkeypatch.setattr(context._transport_client._transport, "close", lambda: close_client("transport"))
    monkeypatch.setenv("FRED_API_KEY", "fixture-local-key-must-not-be-used")
    monkeypatch.setattr(fred.requests, "get", lambda *args, **kwargs: pytest.fail("Hosted cleanup triggered local fallback"))
    with pytest.raises(RuntimeError) as caught:
        context.close()
    _assert_hosted_error(caught.value, "upstream_error", 502)
    assert context._transport_client.is_closed and context._backend.is_closed
    context.close()
    assert closed == ["transport", "backend"]
    with context.activate(), pytest.raises(RuntimeError) as caught:
        fred.get_macro_data("DGS10", "2026-09-01")
    _assert_hosted_error(caught.value, "upstream_error", 502)
    assert closed == ["transport", "backend"]


def test_bounded_backend_config_repr_hides_copied_secret(bounded_worker_config):
    from tradingagents.workbench.public_worker import HostedWorkerContext

    config = replace(bounded_worker_config)
    object.__setattr__(config, "quick_model", FIXTURE_RUN_CAPABILITY)
    object.__setattr__(config, "provider_config", MappingProxyType({"base_url": "https://relay.example/" + FIXTURE_RUN_CAPABILITY}))
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("No network allowed"),
    )))
    try:
        assert FIXTURE_RUN_CAPABILITY not in repr(config) + str(config) + repr(context)
    finally:
        context.close()


@pytest.mark.parametrize("status,code,expected_status", [
    (403, "revoked", 403), (409, "credentials_unavailable", 409),
    (503, "storage_unavailable", 503), (502, "upstream_error", 502),
    (302, "upstream_error", 502),
])
def test_bounded_backend_http_status_rejects_without_reading_error_body(bounded_worker_config, status, code, expected_status):
    stream = BackendStream([FIXTURE_RUN_CAPABILITY.encode()])
    response = httpx.Response(status, headers={"content-type": "text/html"}, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, code, expected_status)
    assert stream.iterations == 0
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("raw", [
    b'{"code":"budget_exhausted","code":"budget_exhausted"}',
    b'{"code":"budget_exhausted","value":NaN}',
    b'{"error":"budget_exhausted"}',
    b'budget_exhausted',
])
def test_bounded_backend_untrusted_budget_code_is_rate_limited(bounded_worker_config, raw):
    response = httpx.Response(429, headers={"content-type": "application/json"}, stream=BackendStream([raw]))
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "rate_limited", 429)
    assert response.is_closed


@pytest.mark.parametrize("headers", [
    {"content-type": "text/html"},
    {"content-type": "application/json", "content-encoding": "gzip"},
    {"content-type": "application/json", "content-length": str(16 * 1024 * 1024 + 1)},
])
def test_bounded_backend_untrusted_budget_headers_are_rate_limited(bounded_worker_config, headers):
    stream = BackendStream([b'{"code":"budget_exhausted"}'])
    response = httpx.Response(429, headers=headers, stream=stream)
    with pytest.raises(RuntimeError) as caught:
        _bounded_dispatch(bounded_worker_config, lambda request: response)
    _assert_hosted_error(caught.value, "rate_limited", 429)
    assert stream.iterations == 0
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("kind,code,status", [("revoked", "revoked", 403), ("timeout", "timeout", 504)])
def test_bounded_backend_sdk_transport_error_has_safe_cause_and_no_retry(bounded_worker_config, kind, code, status):
    from openai import APIConnectionError, OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerContext

    calls = []

    def respond(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout(FIXTURE_RUN_CAPABILITY, request=request)
        return httpx.Response(403, json={"error": FIXTURE_RUN_CAPABILITY})

    context = HostedWorkerContext(bounded_worker_config, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        llm = context.create_llm_client("openai", "gpt-5", max_retries=9).get_llm()
        sdk = OpenAI(api_key="dummy", max_retries=llm.max_retries, http_client=llm.http_client)
        with pytest.raises(APIConnectionError) as caught:
            sdk.responses.create(model="gpt-5", input="hi")
        _assert_hosted_error(caught.value.__cause__, code, status)
        assert FIXTURE_RUN_CAPABILITY not in str(caught.value) + repr(caught.value) + "".join(traceback.format_exception(caught.value))
        assert len(calls) == 1
    finally:
        context.close()


HOSTED_KEY_PROVIDERS = (
    "openai", "anthropic", "google", "azure", "xai", "deepseek", "openrouter",
    "qwen-cn", "qwen", "glm-cn", "glm", "minimax", "minimax-cn", "mistral",
    "kimi", "groq", "nvidia", "openai_compatible",
)


def _config_from_public_settings(public, capability, *, mi_model="gpt-5.6-sol"):
    from tradingagents.workbench.public_worker import (
        HostedWorkerConfig,
        hosted_public_config_for_settings,
    )

    profile = public["providers"].get(public["provider"], {})
    payload = hosted_public_config_for_settings(
        mode=public["mode"], provider=public["provider"] or "azure",
        quick_model=public["quick_model"] or mi_model,
        deep_model=public["deep_model"] or mi_model,
        asset_type=public["asset_type"], data_vendors=public["data_vendors"],
        provider_config={key: profile[key] for key in ("base_url", "api_version") if key in profile},
        data_key_configured=public["data_key_configured"],
    )
    return payload, HostedWorkerConfig.from_environment({
        "TAWB_WORKER_MODE": "hosted", "TAWB_BACKEND_BASE_URL": "https://workbench.example",
        "TAWB_RUN_CAPABILITY": capability, "TRADINGAGENTS_RUN_ID": capability.split(".")[0],
        "TAWB_PUBLIC_CONFIG": json.dumps(payload),
    })


@pytest.mark.parametrize("provider", (*HOSTED_KEY_PROVIDERS, "managed_identity"))
def test_settings_factory_roundtrip_all_hosted_providers(provider):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_settings import PROVIDERS, UserSettings

    assert set(PROVIDERS) == set(HOSTED_KEY_PROVIDERS)
    store = EncryptedStore(MemoryBackend(), b"w" * 32)
    owner = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW).bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    update = {"mode": "managed_identity"} if provider == "managed_identity" else _provider_payload(provider)
    update["data_keys"] = {"fred": "fixture-fred-key"}
    settings.update(owner.principal, update, expected_version=None)
    payload, config = _config_from_public_settings(settings.get(owner.principal), FIXTURE_RUN_CAPABILITY)
    assert config.provider == ("azure" if provider == "managed_identity" else provider)
    assert config.mode == ("managed_identity" if provider == "managed_identity" else "api_key")
    assert dict(config.data_key_configured) == {"alpha_vantage": False, "fred": True}
    assert payload["dataKeyConfigured"] == {"alpha_vantage": False, "fred": True}
    assert "fixture-fred-key" not in json.dumps(payload)
    assert "api_key" not in config.provider_config
    if provider == "managed_identity":
        assert "provider" not in payload and "provider_config" not in payload
        assert config.quick_model == config.deep_model == "gpt-5.6-sol"
    else:
        assert config.quick_model == update["quick_model"]
        assert config.deep_model == update["deep_model"]


@pytest.mark.parametrize("changes", [
    {"provider": "ollama"}, {"provider": "bedrock"}, {"provider": True},
    {"mode": "local"}, {"mode": "managed_identity", "provider": "openai"},
    {"quick_model": None}, {"deep_model": "custom"}, {"quick_model": "../model"},
    {"run_capability": "bad"}, {"backend_base_url": "https://user:secret@workbench.example"},
    {"backend_base_url": "http://workbench.example"}, {"backend_base_url": "https://127.0.0.1"},
    {"asset_type": "invalid"}, {"data_vendors": {"core_stock_apis": "bad"}},
    {"provider_config": {"api_key": "secret"}},
    {"provider_config": {"base_url": "https://evil.example/v1"}},
    {"provider": "openai_compatible", "provider_config": {}},
    {"provider": "openai_compatible", "provider_config": {"base_url": "http://relay.example/v1"}},
    {"provider": "openai_compatible", "provider_config": {"base_url": "https://relay.example/v1?key=secret"}},
    {"provider": "azure", "provider_config": {"base_url": "https://evil.example", "api_version": "2025-03-01-preview"}},
    {"provider": "azure", "provider_config": {"base_url": "https://fixture.openai.azure.com/", "api_version": "bad"}},
    {"data_key_configured": {"alpha_vantage": "key", "fred": True}},
    {"data_key_configured": {"alpha_vantage": False, "fred": 1}},
    {"data_key_configured": {"alpha_vantage": False}},
    {"data_key_configured": {"alpha_vantage": False, "fred": True, "api_key": "secret"}},
])
def test_hosted_constructor_validates_config_before_clients(bounded_worker_config, changes):
    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        replace(bounded_worker_config, **changes)


@pytest.mark.parametrize("run_id", [None, True, 123, "", str(uuid4()), "11111111-1111-4111-8111-11111111111A"])
def test_hosted_environment_binds_exact_run_uuid(run_id):
    from tradingagents.workbench.public_worker import HostedWorkerConfig

    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        HostedWorkerConfig.from_environment({
            "TAWB_WORKER_MODE": "hosted", "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY, "TRADINGAGENTS_RUN_ID": run_id,
            "TAWB_PUBLIC_CONFIG": json.dumps({
                "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
                "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
            }),
        })


@pytest.mark.parametrize("flags", [None, {}, {"fred": True}, {"alpha_vantage": False, "fred": "secret"},
                                      {"alpha_vantage": 0, "fred": True}, {"alpha_vantage": False, "fred": True, "key": "secret"}])
def test_hosted_environment_rejects_nonboolean_flags(flags):
    from tradingagents.workbench.public_worker import HostedWorkerConfig

    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        HostedWorkerConfig.from_environment({
            "TAWB_WORKER_MODE": "hosted", "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
            "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps({
                "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
                "asset_type": "stock", "data_vendors": default_request()["data_vendors"], "dataKeyConfigured": flags,
            }),
        })


@pytest.fixture
def hosted_gateway(monkeypatch):
    import socket

    import requests

    from tradingagents.dataflows import y_finance
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings
    from tradingagents.workbench.public_worker import HostedWorkerContext

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected real network access")

    def offline_data(*args, **kwargs):
        raise requests.Timeout("offline fixture")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(requests, "get", offline_data)
    monkeypatch.setattr(y_finance.yf, "Ticker", lambda *args: SimpleNamespace(history=offline_data))
    for name in {*PROVIDER_API_KEY_ENV.values(), "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "AZURE_OPENAI_ENDPOINT", "GEMINI_API_KEY"} - {None}:
        monkeypatch.delenv(name, raising=False)
    contexts = []

    def build(provider="openai", *, mode="api_key", data_keys=None, data_vendors=None, respond=None, installation=None):
        store = EncryptedStore(MemoryBackend(), b"w" * 32)
        owner = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW).bootstrap(OWNER_PASSWORD)
        settings = UserSettings(store, approved_origins=("https://relay.example.com",))
        update = {"mode": mode} if mode == "managed_identity" else _provider_payload(provider)
        if provider == "openai" and mode == "api_key":
            update["deep_model"] = "gpt-5-mini"
        if data_keys is not None:
            update["data_keys"] = data_keys
        if data_vendors is not None:
            update["data_vendors"] = data_vendors
        public = settings.update(owner.principal, update, expected_version=None)
        upstream = []
        backend_requests = []
        token_scopes = []

        class Credential:
            def get_token(self, scope):
                token_scopes.append(scope)
                return SimpleNamespace(token="fixture-managed-token")

        def upstream_handler(request):
            upstream.append(request)
            if respond is not None:
                return respond(request)
            return _sdk_response(request)

        budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
        upstream_client = httpx.Client(transport=httpx.MockTransport(upstream_handler))
        gateway = ProviderGateway(store, settings=settings, budget=budget, managed_credential=Credential(),
                                  installation=_installation() if installation is None else installation,
                                  http_client=upstream_client, clock=lambda: FIXTURE_NOW)
        run_id = str(uuid4())
        capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=FIXTURE_NOW + 3600)
        public_config, config = _config_from_public_settings(public, capability.serialize())

        def backend_handler(request):
            backend_requests.append(request)
            assert str(request.url) == "https://workbench.example/api/internal/worker/provider-dispatch"
            assert request.headers["x-tawb-run-capability"] == capability.serialize()
            envelope = _json_request_body(request)
            try:
                result = gateway.dispatch(request.headers["x-tawb-run-capability"], **envelope)
            except BudgetError as error:
                status = 429 if error.status_code == 402 else error.status_code
                return httpx.Response(status, json={"error": {"code": "budget_exhausted" if status == 429 else "storage_unavailable"}})
            except GatewayError as error:
                return httpx.Response(error.status_code, json={"error": "Provider request failed."})
            return httpx.Response(200, json=dict(result))

        context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))
        contexts.append((context, upstream_client))
        return SimpleNamespace(context=context, gateway=gateway, budget=budget, upstream=upstream,
                               backend_requests=backend_requests, token_scopes=token_scopes,
                               public_config=public_config, owner=owner.principal, settings=settings)

    yield build
    for context, upstream_client in contexts:
        context.close()
        upstream_client.close()


@pytest.mark.parametrize("failed_path", ["/fred/series", "/fred/series/observations"])
@pytest.mark.parametrize("echo_secrets", [False, True])
def test_hosted_fred_bad_request_preserves_native_error(hosted_gateway, failed_path, echo_secrets):
    from tradingagents.dataflows.fred import get_macro_data

    message = "Bad Request. The series does not exist."

    def respond(request):
        assert request.url.host == "api.stlouisfed.org"
        assert request.url.params["series_id"] == "NOSUCHSERIES"
        if request.url.path != failed_path:
            return httpx.Response(200, json={"seriess": [{"title": "Fixture series"}]})
        error_message = message
        if echo_secrets:
            error_message += " " + request.url.params["api_key"] + " " + case.context.config.run_capability
        return httpx.Response(400, json={
            "error_code": 400, "error_message": error_message,
            "debug": {"api_key": "fixture-fred-key", "capability": case.context.config.run_capability},
        })

    case = hosted_gateway(data_keys={"fred": "fixture-fred-key"}, respond=respond)
    with case.context.activate(), pytest.raises(ValueError) as caught:
        get_macro_data("NOSUCHSERIES", "2026-09-01")
    assert type(caught.value) is ValueError
    expected = "FRED request failed: Bad Request. The series does not exist."
    if echo_secrets:
        expected += " [redacted] [redacted]"
    assert str(caught.value) == expected
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not hasattr(caught.value, "request") and not hasattr(caught.value, "response")
    visible = repr(caught.value) + repr(vars(caught.value)) + "".join(traceback.format_exception(caught.value))
    for secret in ("fixture-fred-key", case.context.config.run_capability, case.context.config.backend_base_url):
        assert secret not in visible
    assert len(case.upstream) == (1 if failed_path == "/fred/series" else 2)


def test_hosted_fred_missing_key_keeps_configured_flags_and_no_fallback(hosted_gateway, monkeypatch):
    from tradingagents.dataflows import fred
    from tradingagents.workbench.worker import run_connectivity

    case = hosted_gateway()
    assert case.context.config.data_key_configured["fred"] is False
    monkeypatch.setenv("FRED_API_KEY", "fixture-local-key-must-not-be-used")
    offline_get = fred.requests.get

    def no_local_fred(url, *args, **kwargs):
        assert not url.startswith("https://api.stlouisfed.org/"), "Hosted FRED used local credentials"
        return offline_get(url, *args, **kwargs)

    monkeypatch.setattr(fred.requests, "get", no_local_fred)
    with case.context.activate():
        with pytest.raises(RuntimeError) as caught:
            fred.get_macro_data("DGS10", "2026-09-01")
        _assert_hosted_error(caught.value, "credentials_unavailable", 409)
        result = run_connectivity(default_request(), lambda *args: None)
    assert next(row for row in result["connectivity"] if row["provider"] == "fred")["status"] == "unconfigured"
    assert sum(_json_request_body(request)["path"].startswith("/data/fred/") for request in case.backend_requests) == 1
    assert all(request.url.host != "api.stlouisfed.org" for request in case.upstream)
    monkeypatch.delenv("FRED_API_KEY")
    with pytest.raises(fred.FredNotConfiguredError):
        fred.get_macro_data("DGS10", "2026-09-01")


def _sdk_response(request):
    body = _json_request_body(request)
    if request.url.path.endswith("/responses"):
        return httpx.Response(200, json={
            "id": "resp_fixture", "object": "response", "model": "gpt-5.6-sol" if "foundry" in request.url.host else body["model"],
            "model_version": "2026-09-10", "created_at": FIXTURE_NOW, "status": "completed",
            "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })
    if request.url.host == "generativelanguage.googleapis.com":
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
        })
    if request.url.host == "api.anthropic.com":
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 2},
        })
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "created": FIXTURE_NOW, "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    })


@pytest.mark.parametrize("provider", (*HOSTED_KEY_PROVIDERS, "managed_identity"))
def test_hosted_connectivity_quick_and_deep_real_sdk(hosted_gateway, provider):
    from tradingagents.workbench.worker import run_connectivity

    case = hosted_gateway("azure" if provider == "managed_identity" else provider,
                          mode="managed_identity" if provider == "managed_identity" else "api_key")
    config = case.context.config
    with case.context.activate():
        result = run_connectivity(default_request(), lambda *args: None)
        assert result["connectivity"][-1]["provider"] == "foundry"
        assert result["connectivity"][-1]["status"] == "ok", [_json_request_body(request) for request in case.backend_requests]
        assert result["stats"]["llm_calls"] == 1
        assert len(case.upstream) == 1
        probe_body = _json_request_body(case.upstream[0])
        if provider == "google":
            assert probe_body["generationConfig"]["maxOutputTokens"] == 128
        elif case.upstream[0].url.path.endswith("/responses"):
            assert probe_body["max_output_tokens"] == 128
        else:
            assert probe_body.get("max_completion_tokens", probe_body.get("max_tokens")) == 128
        options = {"max_output_tokens": 8} if provider == "google" else {"max_tokens": 8}
        deep = case.context.create_llm_client(config.provider, config.deep_model, **options).get_llm().invoke("Reply OK")
    assert deep.content == "OK"
    assert len(case.upstream) == len(case.backend_requests) == 2
    for request, model in zip(case.upstream, (config.quick_model, config.deep_model), strict=True):
        if provider == "google":
            assert request.url.path == f"/v1beta/models/{model}:generateContent"
            assert request.headers["x-goog-api-key"] == "fixture-google-key"
        elif provider == "managed_identity":
            assert _json_request_body(request)["model"] == "deployment-prod-1"
            assert request.headers["authorization"] == "Bearer fixture-managed-token"
        else:
            assert _json_request_body(request)["model"] == model
    assert case.budget.snapshot()["reserved_microusd"] == 0
    assert (case.budget.snapshot()["spent_microusd"] > 0) == (provider == "managed_identity")


@pytest.mark.parametrize("upstream_status", [200, 400, 429, 500])
def test_hosted_mi_probe_reserves_128_output_tokens_for_one_v1_attempt(hosted_gateway, upstream_status):
    from tradingagents.workbench.worker import run_connectivity

    installation = _installation()
    installation["foundry"].update({
        "api_version": "2025-04-01-preview",
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
    })
    installation["foundry"]["pricing"].update({
        "input_microusd_per_million_tokens": 8_000_000,
        "cached_input_microusd_per_million_tokens": 8_000_000,
        "output_microusd_per_million_tokens": 30_000_000,
    })
    reservations = []

    def respond(request):
        reservations.append(case.budget.snapshot()["reserved_microusd"])
        if request.url.params or _json_request_body(request).get("max_output_tokens") != 128:
            return httpx.Response(400, json={"error": {"message": "Invalid v1 probe request"}})
        if upstream_status != 200:
            return httpx.Response(upstream_status, json={"error": {"message": "Fixture failure"}})
        return _sdk_response(request)

    case = hosted_gateway("azure", mode="managed_identity", installation=installation, respond=respond)
    with case.context.activate():
        result = run_connectivity(default_request(), lambda *args: None)

    assert len(case.upstream) == len(case.backend_requests) == 1
    request = case.upstream[0]
    assert request.method == "POST"
    assert request.url == "https://example-foundry.services.ai.azure.com/openai/v1/responses"
    assert _json_request_body(request)["max_output_tokens"] == 128
    assert case.token_scopes == ["https://cognitiveservices.azure.com/.default"]
    assert reservations == [8_403_840]
    assert result["stats"]["llm_calls"] == 1
    assert result["connectivity"][-1]["status"] == ("ok" if upstream_status == 200 else "error")
    assert case.budget.snapshot()["spent_microusd"] == (100 if upstream_status == 200 else 0)
    assert case.budget.snapshot()["reserved_microusd"] == (0 if upstream_status == 200 else 8_403_840)


@pytest.mark.parametrize("text,status", [
    ("OK", "ok"), (" \nOK\t", "ok"), ("", "no_data"), (" \t", "no_data"),
    ("not OK", "no_data"), ("OK extra", "no_data"),
])
def test_hosted_mi_probe_requires_nonempty_ok_response(hosted_gateway, text, status):
    from tradingagents.workbench.worker import run_connectivity

    def respond(request):
        body = _sdk_response(request).json()
        body["output"][0]["content"][0]["text"] = text
        return httpx.Response(200, json=body)

    case = hosted_gateway("azure", mode="managed_identity", respond=respond)
    with case.context.activate():
        result = run_connectivity(default_request(), lambda *args: None)

    assert result["connectivity"][-1]["status"] == status
    assert len(case.upstream) == len(case.backend_requests) == 1
    assert result["stats"]["llm_calls"] == 1
    assert case.budget.snapshot()["reserved_microusd"] == 0
    assert case.budget.snapshot()["spent_microusd"] > 0


def test_hosted_connectivity_data_flags_and_global_quote_category(hosted_gateway):
    from tradingagents.workbench.worker import run_connectivity

    vendors = default_request()["data_vendors"] | {"fundamental_data": "alpha_vantage"}
    case = hosted_gateway(data_keys={"alpha_vantage": "fixture-alpha-key"}, data_vendors=vendors)
    with case.context.activate():
        result = run_connectivity(default_request(), lambda *args: None)
    assert result["connectivity"][1]["status"] == "unconfigured"
    assert result["connectivity"][2]["status"] == "unconfigured"
    assert len(case.backend_requests) == 1
    assert _json_request_body(case.backend_requests[0])["path"] == "/responses"


@pytest.mark.parametrize("provider,signed", [("google", False), ("anthropic", False), ("google", True)])
def test_hosted_langchain_two_turn_tool_history(hosted_gateway, provider, signed):
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool

    turns = []

    def respond(request):
        turns.append(_json_request_body(request))
        if len(turns) > 1:
            return _sdk_response(request)
        if provider == "google":
            return httpx.Response(200, json={
                "candidates": [{"content": {"role": "model", "parts": [{"functionCall": {
                    "name": "lookup", "args": {"ticker": "MSFT"},
                }, **({"thoughtSignature": "Zml4dHVyZS1zaWduYXR1cmU="} if signed else {})}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
            })
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "fixture-quick",
            "content": [{"type": "tool_use", "id": "call_quote", "name": "lookup", "input": {"ticker": "MSFT"}}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 2},
        })

    @tool(description="Look up the quote for a ticker")
    def lookup(ticker: str) -> str:
        assert ticker == "MSFT"
        return "405.00"

    case = hosted_gateway(provider, respond=respond)
    options = {"max_output_tokens": 32} if provider == "google" else {"max_tokens": 32}
    llm = case.context.create_llm_client(provider, case.context.config.quick_model, **options).get_llm().bind_tools([lookup])
    history = [HumanMessage(content="Look up MSFT")]
    try:
        first = llm.invoke(history)
    except Exception:
        pytest.fail(json.dumps([_json_request_body(request) for request in case.backend_requests]))
    assert len(first.tool_calls) == 1
    call = first.tool_calls[0]
    assert call["name"] == "lookup" and call["args"] == {"ticker": "MSFT"}
    history.extend([first, ToolMessage(content=lookup.invoke(call["args"]), name="lookup", tool_call_id=call["id"])])
    second = llm.invoke(history)
    assert second.content == "OK" and not second.tool_calls
    assert len(turns) == len(case.backend_requests) == 2
    if provider == "google":
        assert turns[1]["contents"][1]["parts"][0]["functionCall"] == {"name": "lookup", "args": {"ticker": "MSFT"}}
        if signed:
            assert turns[1]["contents"][1]["parts"][0]["thoughtSignature"] == "Zml4dHVyZS1zaWduYXR1cmU="
        response = turns[1]["contents"][2]["parts"][0]["functionResponse"]
        assert response == {"name": "lookup", "response": {"output": 405.0}}
        assert turns[0]["tools"][0]["functionDeclarations"][0]["parameters_json_schema"] == {
            "type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"],
        }
        assert all(request.headers["x-goog-api-key"] == "fixture-google-key" for request in case.upstream)
        assert all("safetySettings" not in turn and "candidateCount" not in turn["generationConfig"] for turn in turns)
    else:
        assert turns[1]["messages"][1]["content"][0] == {"type": "tool_use", "id": "call_quote", "name": "lookup", "input": {"ticker": "MSFT"}}
        assert turns[1]["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_quote", "content": "405.00", "is_error": False}
        assert all(request.headers["x-api-key"] == "fixture-anthropic-key" for request in case.upstream)
    assert all(request.headers.get("x-tawb-run-capability") is None for request in case.upstream)
    assert case.budget.snapshot()["spent_microusd"] == 0


def test_hosted_toolnode_parallel_contexts_and_local_restoration(hosted_gateway, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from langchain_core.messages import AIMessage
    from langchain_core.runnables.config import set_config_context
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from tradingagents.dataflows import alpha_vantage_common, fred
    from tradingagents.dataflows.config import get_config, set_config
    from tradingagents.workbench.public_worker import current_hosted_worker_context

    baseline = get_config()
    parent_barrier = Barrier(2)
    tool_barrier = Barrier(4)
    observations = []

    def observe(expected):
        tool_barrier.wait(timeout=10)
        context = current_hosted_worker_context()
        observations.append((expected, context.config.run_capability, get_config()["output_language"]))
        assert get_config()["output_language"] == expected

    @tool(description="Get a hosted Alpha quote")
    def alpha_quote(expected: str) -> str:
        observe(expected)
        return alpha_vantage_common._make_api_request("GLOBAL_QUOTE", {"symbol": "MSFT"})["Global Quote"]["owner"]

    @tool(description="Get hosted FRED series metadata and observations")
    def macro_series(expected: str) -> str:
        observe(expected)
        return fred.get_macro_data("DGS10", "2026-09-10", look_back_days=30)

    workflow = StateGraph(MessagesState)
    workflow.add_node("tools", ToolNode([alpha_quote, macro_series], handle_tool_errors=False))
    workflow.add_edge(START, "tools")
    workflow.add_edge("tools", END)
    graph = workflow.compile()
    cases = []
    for language in ("English", "Chinese"):
        def respond(request, language=language):
            if request.url.host == "www.alphavantage.co":
                assert request.url.params["apikey"] == f"fixture-alpha-{language}"
                return httpx.Response(200, json={"Global Quote": {"owner": language}})
            assert request.url.params["api_key"] == f"fixture-fred-{language}"
            if request.url.path == "/fred/series":
                return httpx.Response(200, json={"seriess": [{"title": language, "units": "Percent", "frequency": "Daily"}]})
            assert request.url.path == "/fred/series/observations"
            return httpx.Response(200, json={"observations": [{"date": "2026-09-10", "value": "4.3"}]})

        cases.append(hosted_gateway(data_keys={"alpha_vantage": f"fixture-alpha-{language}", "fred": f"fixture-fred-{language}"},
                                    data_vendors=default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"}, respond=respond))

    def run(case, language):
        assert current_hosted_worker_context() is None
        with case.context.activate():
            set_config({"output_language": language, "data_vendors": dict(case.context.config.data_vendors)})
            parent_barrier.wait(timeout=10)
            with set_config_context({"max_concurrency": 2}) as copied:
                result = copied.run(graph.invoke, {"messages": [AIMessage(content="", tool_calls=[
                    {"name": "alpha_quote", "args": {"expected": language}, "id": "alpha", "type": "tool_call"},
                    {"name": "macro_series", "args": {"expected": language}, "id": "fred", "type": "tool_call"},
                ])]})
        assert current_hosted_worker_context() is None
        assert result["messages"][-2].content == language
        assert language in result["messages"][-1].content

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, case, language) for case, language in zip(cases, ("English", "Chinese"), strict=True)]
        for future in futures:
            future.result(timeout=30)
    assert cases[0].owner.object_id != cases[1].owner.object_id
    assert len(observations) == 4
    for case, language in zip(cases, ("English", "Chinese"), strict=True):
        assert {(capability, output_language) for expected, capability, output_language in observations if expected == language} == {
            (case.context.config.run_capability, language),
        }
        assert len(case.backend_requests) == len(case.upstream) == 3
        assert all(request.headers["x-tawb-run-capability"] == case.context.config.run_capability for request in case.backend_requests)
    assert get_config() == baseline
    local_requests = []

    def local_get(url, *, params, timeout):
        local_requests.append((url, params))
        return httpx.Response(200, json={"local": True}, request=httpx.Request("GET", url))

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fixture-local-alpha")
    monkeypatch.setenv("FRED_API_KEY", "fixture-local-fred")
    monkeypatch.setattr(alpha_vantage_common.requests, "get", local_get)
    assert json.loads(alpha_vantage_common._make_api_request("GLOBAL_QUOTE", {"symbol": "MSFT"})) == {"local": True}
    assert fred._request("series", {"series_id": "DGS10"}) == {"local": True}
    assert local_requests[0][1]["apikey"] == "fixture-local-alpha"
    assert local_requests[1][1]["api_key"] == "fixture-local-fred"
    assert all(len(case.backend_requests) == 3 for case in cases)


def _worker_main_events(case, monkeypatch, capsys, tmp_path, *, mode="analysis"):
    import base64

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.workbench import public_worker, worker
    from tradingagents.workbench.contracts import decode_events

    run_id = case.context.config.run_capability.split(".")[0]
    monkeypatch.setenv("TRADINGAGENTS_RUN_ID", run_id)
    monkeypatch.setenv("TRADINGAGENTS_RUN_REQUEST", base64.b64encode(json.dumps(
        {"mode": mode, "analysts": ["market"], "analysis_date": "2026-09-01"},
    ).encode()).decode())
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", case.context.config.run_capability)
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps(case.public_config))
    monkeypatch.setattr(public_worker, "create_pinned_http_client", lambda: case.context._backend)
    for name in ("results_dir", "data_cache_dir"):
        monkeypatch.setitem(DEFAULT_CONFIG, name, str(tmp_path / name))
    exit_code = worker.main()
    output = capsys.readouterr()
    assert output.err == ""
    assert case.context.config.run_capability not in output.out
    return exit_code, decode_events(output.out.splitlines(), run_id)


@pytest.mark.parametrize("mode", ["analysis", "connectivity"])
def test_hosted_budget_preflight_is_terminal(hosted_gateway, monkeypatch, capsys, tmp_path, mode):
    case = hosted_gateway("azure", mode="managed_identity")
    case.budget.reserve(str(uuid4()), run_id=str(uuid4()), amount_microusd=300_000_000, price_version="fixture-v2")
    exit_code, events = _worker_main_events(case, monkeypatch, capsys, tmp_path, mode=mode)
    assert exit_code == 1
    assert not any(event["kind"] == "result" for event in events)
    assert events[-1]["kind"] == "error"
    assert events[-1]["payload"] == {"code": "budget_exhausted", "message": "Hosted worker provider budget is exhausted."}
    assert sum(request.url.path.endswith("/provider-dispatch") for request in case.backend_requests) == 1
    assert case.upstream == [] and case.token_scopes == []
    assert not any(event["kind"] == "phase" and event["payload"]["stage"] == "initializing" for event in events)
    rows = [event["payload"]["connectivity"] for event in events if event["kind"] == "connectivity"]
    assert rows[-1][-1]["status"] == "rate_limited"


@pytest.mark.parametrize("provider", ["openai", "google", "anthropic"])
@pytest.mark.parametrize("reason", ["expired", "revoked"])
def test_hosted_revocation_is_terminal_not_unconfigured(hosted_gateway, monkeypatch, capsys, tmp_path, provider, reason):
    case = hosted_gateway(provider)
    if reason == "revoked":
        case.gateway.revoke(case.context.config.run_capability.split(".")[0])
    else:
        case.gateway._clock = lambda: FIXTURE_NOW + 3601
    exit_code, events = _worker_main_events(case, monkeypatch, capsys, tmp_path)
    assert exit_code == 1 and not any(event["kind"] == "result" for event in events)
    assert events[-1]["payload"]["code"] == "revoked"
    assert sum(request.url.path.endswith("/provider-dispatch") for request in case.backend_requests) == 1
    assert case.upstream == []
    rows = [event["payload"]["connectivity"] for event in events if event["kind"] == "connectivity"]
    assert rows[-1][-1]["status"] == "auth_error"


@pytest.mark.parametrize("failure", ["budget", "expired"])
def test_hosted_real_graph_preserves_partial_report_on_terminal_failure(hosted_gateway, monkeypatch, capsys, tmp_path, failure):
    installation = _installation()
    installation["foundry"].update(context_window_tokens=4096, max_output_tokens=1024)
    upstream_count = 0

    def respond(request):
        nonlocal upstream_count
        upstream_count += 1
        response = _sdk_response(request)
        if upstream_count == 2:
            body = response.json()
            body["output"][0]["content"][0]["text"] = "Market partial fixture"
            if failure == "budget":
                case.budget.reserve(str(uuid4()), run_id=str(uuid4()),
                                    amount_microusd=case.budget.snapshot()["available_microusd"], price_version="fixture-v2")
            else:
                pending = case.budget.reserve(str(uuid4()), run_id=case.context.config.run_capability.split(".")[0],
                                              amount_microusd=123, price_version="fixture-v2")
                case.budget.mark_dispatched(pending)
                case.gateway._clock = lambda: FIXTURE_NOW + 3601
            return httpx.Response(200, json=body)
        return response

    case = hosted_gateway("azure", mode="managed_identity", respond=respond, installation=installation)
    exit_code, events = _worker_main_events(case, monkeypatch, capsys, tmp_path)
    assert exit_code == 1 and not any(event["kind"] == "result" for event in events)
    reports = [event["payload"] for event in events if event["kind"] == "report"]
    assert {"section": "market_report", "content": "Market partial fixture"} in reports
    assert events[-1]["kind"] == "error"
    assert events[-1]["payload"]["code"] == ("budget_exhausted" if failure == "budget" else "revoked")
    assert upstream_count == 2
    assert sum(request.url.path.endswith("/provider-dispatch") for request in case.backend_requests) == 3
    assert any(event["kind"] == "usage" and event["payload"]["llm_calls"] == 3 for event in events)
    assert case.budget.snapshot()["spent_microusd"] == 180
    if failure == "expired":
        assert case.budget.snapshot()["reserved_microusd"] == 123


def test_hosted_toolnode_optional_data_does_not_swallow_revocation(hosted_gateway):
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from tradingagents.dataflows.interface import route_to_vendor
    from tradingagents.workbench.public_worker import HostedProviderError

    @tool(description="Fetch macro data through the real vendor router")
    def macro(indicator: str) -> str:
        return route_to_vendor("get_macro_indicators", indicator, "2026-09-10")

    case = hosted_gateway(data_keys={"fred": "fixture-fred-key"})
    case.gateway.revoke(case.context.config.run_capability.split(".")[0])
    workflow = StateGraph(MessagesState)
    workflow.add_node("tools", ToolNode([macro]))
    workflow.add_edge(START, "tools")
    workflow.add_edge("tools", END)
    node = workflow.compile()
    with case.context.activate(), pytest.raises(HostedProviderError) as caught:
        node.invoke({"messages": [AIMessage(content="", tool_calls=[
            {"name": "macro", "args": {"indicator": "DGS10"}, "id": "macro", "type": "tool_call"},
        ])]})
    assert caught.value.code == "revoked"
    assert len(case.backend_requests) == 1 and case.upstream == []


@pytest.mark.parametrize("changes", [
    {"mode": "local"}, {"provider": "ollama"}, {"provider": "bedrock"}, {"quick_model": "../model"},
    {"mode": "managed_identity", "provider": "openai"}, {"deep_model": None},
    {"provider_config": {"api_key": "fixture-secret"}}, {"provider_config": {"base_url": "https://evil.example"}},
])
def test_hosted_generator_rejects_invalid_settings(changes):
    from tradingagents.workbench.public_worker import hosted_public_config_for_settings

    values = {"mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
              "asset_type": "stock", "data_vendors": default_request()["data_vendors"]}
    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        hosted_public_config_for_settings(**(values | changes))


@pytest.mark.parametrize("provider,model,base_url", [
    ("anthropic", "gpt-5", None), ("openai", "unselected-model", None),
    ("openai", "gpt-5", "https://evil.example/v1"),
])
def test_hosted_client_factory_cannot_override_bound_selection(hosted_gateway, provider, model, base_url):
    case = hosted_gateway()
    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        case.context.create_llm_client(provider, model, base_url=base_url)
    assert case.backend_requests == case.upstream == []


@pytest.mark.parametrize("raw", [
    '{"mode":"managed_identity","mode":"api_key","provider":"openai","quick_model":"gpt-5",'
    '"deep_model":"gpt-5","asset_type":"stock","data_vendors":{}}',
    '{"mode":"api_key","provider":"openai","quick_model":"gpt-5","deep_model":"gpt-5",'
    '"asset_type":"stock","data_vendors":{"core_stock_apis":"alpha_vantage","core_stock_apis":"yfinance"}}',
    '{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}',
], ids=["duplicate-mode", "duplicate-vendor", "deep-json"])
def test_hosted_config_strict_json(raw):
    from tradingagents.workbench.public_worker import HostedWorkerConfig

    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        HostedWorkerConfig.from_environment({
            "TAWB_WORKER_MODE": "hosted", "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY, "TAWB_PUBLIC_CONFIG": raw,
            "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
        })


def test_hosted_environment_requires_run_uuid():
    from tradingagents.workbench.public_worker import HostedWorkerConfig

    with pytest.raises(RuntimeError, match="Hosted worker configuration is invalid"):
        HostedWorkerConfig.from_environment({
            "TAWB_WORKER_MODE": "hosted", "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
            "TAWB_PUBLIC_CONFIG": json.dumps({
                "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
                "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
            }),
        })


def _installation():
    return {
        "foundry": {
            "endpoint": "https://example-foundry.services.ai.azure.com/openai/v1/",
            "deployment": "deployment-prod-1",
            "api_version": "2025-03-01-preview",
            "model_family": "gpt-5.6-sol",
            "model_version": "2026-09-10",
            "context_window_tokens": 128,
            "max_output_tokens": 128,
            "pricing": {
                "version": "fixture-v2",
                "input_microusd_per_million_tokens": 10_000_000,
                "cached_input_microusd_per_million_tokens": 3_000_000,
                "output_microusd_per_million_tokens": 20_000_000,
            },
            "verification": {
                "status": "verified",
                "evidence": "fixture-contract-v1",
                "recorded_at": "2026-09-10",
            },
        }
    }


def _json_request_body(request: httpx.Request):
    content = request.content
    if isinstance(content, bytes):
        return json.loads(content.decode("utf-8"))
    return json.loads(content)


def _provider_payload(provider: str) -> dict:
    payload = {
        "mode": "api_key",
        "provider": provider,
        "quick_model": "gpt-5" if provider == "openai" else "fixture-quick",
        "deep_model": "gpt-5" if provider == "openai" else "fixture-deep",
        "api_key": f"fixture-{provider}-key",
        "asset_type": "stock",
        "data_vendors": default_request()["data_vendors"],
    }
    if provider == "azure":
        payload.update(base_url="https://fixture.openai.azure.com/", api_version="2025-03-01-preview")
    if provider == "openai_compatible":
        payload.update(base_url="https://relay.example.com/v1")
    return payload


def _hosted_openai_case():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"w" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    settings.update(owner.principal, _provider_payload("openai"), expected_version=None)
    upstream = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "model": "gpt-5",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    return gateway, capability, upstream


def _hosted_provider_case(provider: str, *, data_keys: dict | None = None, data_vendors: dict | None = None,
                          mode: str = "api_key"):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"w" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    if mode == "managed_identity":
        payload = {"mode": "managed_identity"}
    else:
        payload = _provider_payload(provider)
    if data_keys is not None:
        payload["data_keys"] = data_keys
    if data_vendors is not None:
        payload["data_vendors"] = data_vendors
    settings.update(owner.principal, payload, expected_version=None)
    upstream = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "fixture-quick",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            )
        if provider == "google":
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
                    "modelVersion": "gemini-3.5-flash",
                },
            )
        if provider == "azure":
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-123",
                    "object": "chat.completion",
                    "model": "quick-deploy",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
        if provider not in {"openai"}:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-123",
                    "object": "chat.completion",
                    "model": "fixture-quick",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
        if request.url.path == "/query" and request.url.host == "www.alphavantage.co":
            return httpx.Response(
                200,
                json={"Global Quote": {"01. symbol": "MSFT", "05. price": "405.00", "07. latest trading day": "2026-09-10"}},
            )
        if request.url.path == "/fred/series" and request.url.host == "api.stlouisfed.org":
            return httpx.Response(200, json={"seriess": [{"title": "Treasury yield", "units": "Percent", "frequency": "Daily"}]})
        if request.url.path == "/fred/series/observations" and request.url.host == "api.stlouisfed.org":
            return httpx.Response(200, json={"observations": [{"date": "2026-09-10", "value": "4.3"}]})
        raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    return gateway, capability, upstream


def _hosted_managed_identity_case():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"w" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    upstream = []
    token_scopes = []

    class ManagedCredential:
        def get_token(self, scope):
            token_scopes.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    return gateway, capability, upstream, token_scopes


def _hosted_managed_identity_with_data_case(*, data_date="2026-09-10"):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"w" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    settings.update(
        owner.principal,
        {
            "mode": "managed_identity",
            "data_keys": {"alpha_vantage": "fixture-alpha-key", "fred": "fixture-fred-key"},
            "data_vendors": default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"},
        },
        expected_version=None,
    )
    upstream = []
    token_scopes = []

    class ManagedCredential:
        def get_token(self, scope):
            token_scopes.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        if request.url.host == "www.alphavantage.co" and request.url.path == "/query":
            return httpx.Response(
                200,
                json={"Global Quote": {"01. symbol": "MSFT", "05. price": "405.00", "07. latest trading day": data_date}},
            )
        if request.url.host == "api.stlouisfed.org" and request.url.path == "/fred/series":
            return httpx.Response(200, json={"seriess": [{"title": "Treasury yield", "units": "Percent", "frequency": "Daily"}]})
        if request.url.host == "api.stlouisfed.org" and request.url.path == "/fred/series/observations":
            return httpx.Response(200, json={"observations": [{"date": data_date, "value": "4.3"}]})
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "output": [{"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "total_tokens": 6,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    return gateway, capability, upstream, token_scopes


def test_hosted_worker_config_requires_explicit_bounded_environment():
    from tradingagents.workbench.public_worker import (
        HostedWorkerConfig,
        hosted_worker_context_from_environment,
    )

    env = {
        "TAWB_WORKER_MODE": "hosted",
        "TAWB_BACKEND_BASE_URL": "https://workbench.example",
        "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
        "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
        "TAWB_PUBLIC_CONFIG": json.dumps(
            {
                "mode": "api_key",
                "provider": "openai",
                "quick_model": "gpt-5",
                "deep_model": "gpt-5",
                "asset_type": "stock",
                "data_vendors": default_request()["data_vendors"],
            }
        ),
    }

    config = HostedWorkerConfig.from_environment(env)
    assert config.backend_base_url == "https://workbench.example"
    assert config.run_capability == FIXTURE_RUN_CAPABILITY
    assert config.provider == "openai"
    assert config.quick_model == "gpt-5"
    assert config.deep_model == "gpt-5"

    for broken in (
        {**env, "TAWB_PUBLIC_CONFIG": "{"},
        {**env, "TAWB_PUBLIC_CONFIG": json.dumps({"provider": "openai"})},
        {key: value for key, value in env.items() if key != "TAWB_RUN_CAPABILITY"},
    ):
        with pytest.raises(RuntimeError):
            HostedWorkerConfig.from_environment(broken)

    with pytest.raises(RuntimeError):
        hosted_worker_context_from_environment({key: env[key] for key in env if key != "TAWB_WORKER_MODE"})


def test_hosted_openai_bridge_uses_backend_capability_not_process_env(monkeypatch):
    from openai import OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    gateway, capability, upstream = _hosted_openai_case()
    backend_requests = []
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        backend_requests.append(request)
        assert request.url == httpx.URL("https://workbench.example/api/internal/worker/provider-dispatch")
        assert request.headers["x-tawb-run-capability"] == serialized
        body = _json_request_body(request)
        assert body.keys() == {"method", "path", "headers", "body"}
        result = gateway.dispatch(
            serialized,
            method=body["method"],
            path=body["path"],
            headers=body["headers"],
            body=body["body"],
        )
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.setenv("OPENAI_API_KEY", "hostile-owner-key")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "openai",
                    "quick_model": "gpt-5",
                    "deep_model": "gpt-5",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    llm_client = context.create_llm_client("openai", "gpt-5", max_retries=9, max_tokens=7)
    llm = llm_client.get_llm()
    sdk = OpenAI(api_key="dummy-openai", max_retries=0, http_client=llm.http_client)
    response = sdk.responses.create(
        model="gpt-5",
        input=[
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        ],
        max_output_tokens=7,
        tools=[
            {
                "type": "function",
                "name": "lookup",
                "description": "Find quote",
                "parameters": {
                    "type": "object",
                    "properties": {"ticker": {"type": "string"}},
                    "required": ["ticker"],
                },
                "strict": True,
            }
        ],
    )

    assert response.output[0].content[0].text == "ok"
    assert len(backend_requests) == 1
    assert len(upstream) == 1
    assert upstream[0].url == httpx.URL("https://api.openai.com/v1/responses")
    assert upstream[0].headers["authorization"] == "Bearer fixture-openai-key"
    assert upstream[0].headers.get("x-tawb-run-capability") is None


def test_hosted_google_bridge_uses_backend_dispatch_and_supported_client_args(monkeypatch):
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    monkeypatch.setenv("GOOGLE_API_KEY", "hostile-owner-key")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
            "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "google",
                    "quick_model": "gemini-3.5-flash",
                    "deep_model": "gemini-3.5-pro",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )
    context = HostedWorkerContext(config)
    client = context.create_llm_client("google", "gemini-3.5-flash", max_output_tokens=7)

    assert type(client).__name__ == "GoogleClient"
    assert client.kwargs["google_api_key"] == "tawb-backend-dispatch"
    assert type(client.kwargs["client_args"]["transport"]).__name__ == "GatewayDispatchTransport"
    assert client.kwargs["max_retries"] == 0


def test_hosted_azure_bridge_uses_explicit_api_key_mode_not_env_managed_identity(monkeypatch):
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "managed_identity")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://hostile.openai.azure.com/")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
            "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "azure",
                    "quick_model": "quick-deploy",
                    "deep_model": "deep-deploy",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                    "provider_config": {
                        "base_url": "https://fixture.openai.azure.com/",
                        "api_version": "2025-03-01-preview",
                    },
                }
            ),
        }
    )
    context = HostedWorkerContext(config)
    client = context.create_llm_client("azure", "quick-deploy", max_tokens=4)

    assert type(client).__name__ == "AzureOpenAIClient"
    assert client.kwargs["auth_mode"] == "api_key"
    assert client.kwargs["azure_endpoint"] == "https://fixture.openai.azure.com/"
    assert client.kwargs["azure_deployment"] == "quick-deploy"
    assert client.kwargs["api_version"] == "2025-03-01-preview"
    assert client.kwargs["api_key"] == "tawb-backend-dispatch"
    assert client.kwargs["max_retries"] == 0


def test_hosted_alpha_vantage_and_fred_bridges_use_context_without_requests_env(monkeypatch):
    import requests

    from tradingagents.dataflows import alpha_vantage_common, fred
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    gateway, capability, upstream = _hosted_provider_case(
        "openai",
        data_keys={"alpha_vantage": "fixture-alpha-key", "fred": "fixture-fred-key"},
        data_vendors=default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"},
    )
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        body = _json_request_body(request)
        result = gateway.dispatch(serialized, method=body["method"], path=body["path"], headers=body["headers"], body=body["body"])
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "hostile-alpha-key")
    monkeypatch.setenv("FRED_API_KEY", "hostile-fred-key")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: pytest.fail("Hosted data bridge must not call requests.get directly"))
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "openai",
                    "quick_model": "gpt-5",
                    "deep_model": "gpt-5",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"},
                }
            ),
        }
    )
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    with context.activate():
        alpha = alpha_vantage_common._make_api_request("GLOBAL_QUOTE", {"symbol": "MSFT"})
        meta = fred._request("series", {"series_id": "DGS10", "realtime_start": "2026-09-10", "realtime_end": "2026-09-10"})
        observations = fred._request(
            "series/observations",
            {"series_id": "DGS10", "observation_start": "2026-09-01", "observation_end": "2026-09-10", "realtime_start": "2026-09-10", "realtime_end": "2026-09-10"},
        )

    assert alpha["Global Quote"]["01. symbol"] == "MSFT"
    assert meta["seriess"][0]["title"] == "Treasury yield"
    assert observations["observations"][0]["value"] == "4.3"
    assert [(request.url.host, request.url.path) for request in upstream] == [
        ("www.alphavantage.co", "/query"),
        ("api.stlouisfed.org", "/fred/series"),
        ("api.stlouisfed.org", "/fred/series/observations"),
    ]


def test_hosted_anthropic_bridge_invokes_without_global_client_monkeypatch(monkeypatch):
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    gateway, capability, upstream = _hosted_provider_case("anthropic")
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        body = _json_request_body(request)
        result = gateway.dispatch(
            serialized,
            method=body["method"],
            path=body["path"],
            headers=body["headers"],
            body=body["body"],
        )
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "anthropic",
                    "quick_model": "fixture-quick",
                    "deep_model": "fixture-deep",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    response = context.create_llm_client("anthropic", "fixture-quick", max_tokens=32).get_llm().invoke("hi")

    assert response.content == "ok"
    assert len(upstream) == 1
    assert upstream[0].url == httpx.URL("https://api.anthropic.com/v1/messages")
    assert upstream[0].headers["x-api-key"] == "fixture-anthropic-key"


def test_hosted_managed_identity_bridge_uses_responses_dispatch_without_env_credentials(monkeypatch):
    from openai import OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    gateway, capability, upstream, token_scopes = _hosted_managed_identity_case()
    backend_requests = []
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        backend_requests.append(request)
        body = _json_request_body(request)
        assert body["path"] == "/responses"
        result = gateway.dispatch(
            serialized,
            method=body["method"],
            path=body["path"],
            headers=body["headers"],
            body=body["body"],
        )
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "managed_identity")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://hostile.openai.azure.com/")
    monkeypatch.setenv("OPENAI_API_KEY", "hostile-openai-key")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "managed_identity",
                    "quick_model": "gpt-5.6-sol",
                    "deep_model": "gpt-5.6-sol",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )
    assert "fixture-run-capability" not in repr(config)
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    llm_client = context.create_llm_client("azure", "gpt-5.6-sol", max_tokens=7)
    llm = llm_client.get_llm()
    sdk = OpenAI(api_key="dummy-openai", max_retries=0, http_client=llm.http_client)
    response = sdk.responses.create(
        model="gpt-5.6-sol",
        input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        max_output_tokens=7,
    )

    assert type(llm).__name__ == "NormalizedChatOpenAI"
    assert response.output[0].content[0].text == "ok"
    assert len(backend_requests) == 1
    assert len(upstream) == 1
    assert upstream[0].url == httpx.URL("https://example-foundry.services.ai.azure.com/openai/v1/responses")
    assert upstream[0].headers["authorization"] == "Bearer managed-secret-token"
    assert token_scopes == ["https://cognitiveservices.azure.com/.default"]


@pytest.mark.parametrize(
    "provider,base_url,path_prefix",
    [
        ("xai", "https://api.x.ai/v1", "/v1/chat/completions"),
        ("deepseek", "https://api.deepseek.com", "/chat/completions"),
        ("openrouter", "https://openrouter.ai/api/v1", "/api/v1/chat/completions"),
        ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", "/compatible-mode/v1/chat/completions"),
        ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "/compatible-mode/v1/chat/completions"),
        ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/", "/api/paas/v4/chat/completions"),
        ("glm", "https://api.z.ai/api/paas/v4/", "/api/paas/v4/chat/completions"),
        ("minimax", "https://api.minimax.io/v1", "/v1/chat/completions"),
        ("minimax-cn", "https://api.minimaxi.com/v1", "/v1/chat/completions"),
        ("mistral", "https://api.mistral.ai/v1", "/v1/chat/completions"),
        ("kimi", "https://api.moonshot.ai/v1", "/v1/chat/completions"),
        ("groq", "https://api.groq.com/openai/v1", "/openai/v1/chat/completions"),
        ("nvidia", "https://integrate.api.nvidia.com/v1", "/v1/chat/completions"),
        ("openai_compatible", "https://relay.example.com/v1", "/v1/chat/completions"),
    ],
)
def test_hosted_openai_compatible_chat_routes_map_exact_provider_prefix(monkeypatch, provider, base_url, path_prefix):
    from openai import OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    gateway, capability, upstream = _hosted_provider_case(provider)
    backend_requests = []
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        backend_requests.append(request)
        body = _json_request_body(request)
        assert body["path"] == "/chat/completions"
        result = gateway.dispatch(
            serialized,
            method=body["method"],
            path=body["path"],
            headers=body["headers"],
            body=body["body"],
        )
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.setenv("OPENAI_API_KEY", "hostile-owner-key")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": provider,
                    "quick_model": "fixture-quick",
                    "deep_model": "fixture-deep",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                } | ({"provider_config": {"base_url": base_url}} if provider == "openai_compatible" else {})
            ),
        }
    )
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    llm_client = context.create_llm_client(provider, "fixture-quick", max_tokens=4)
    llm = llm_client.get_llm()
    sdk = OpenAI(api_key="dummy-openai", base_url=base_url, max_retries=0, http_client=llm.http_client)
    response = sdk.chat.completions.create(
        model="fixture-quick",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=4,
    )

    assert response.choices[0].message.content == "ok"
    assert len(backend_requests) == 1
    assert len(upstream) == 1
    assert upstream[0].url == httpx.URL(base_url.rstrip("/") + "/chat/completions")
    assert upstream[0].url.path == path_prefix
    assert upstream[0].headers["authorization"] == f"Bearer fixture-{provider}-key"


def test_hosted_connectivity_uses_context_configuration_without_local_env_keys(monkeypatch):
    from datetime import date

    import pandas as pd

    from tradingagents.dataflows import alpha_vantage_common, fred, polymarket, y_finance
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext
    from tradingagents.workbench.worker import run_connectivity

    gateway, capability, upstream, token_scopes = _hosted_managed_identity_with_data_case(data_date=date.today().isoformat())
    serialized = capability.serialize()

    def backend_handler(request: httpx.Request) -> httpx.Response:
        body = _json_request_body(request)
        result = gateway.dispatch(
            serialized,
            method=body["method"],
            path=body["path"],
            headers=body["headers"],
            body=body["body"],
        )
        return httpx.Response(200, json={"status_code": result["status_code"], "body": result["body"]})

    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "managed_identity")
    monkeypatch.setattr(y_finance.yf, "Ticker", lambda *args: SimpleNamespace(history=lambda **kwargs: pd.DataFrame(
        {"Open": [400], "High": [410], "Low": [399], "Close": [405], "Volume": [1000]},
        index=pd.DatetimeIndex([date.today().isoformat()], name="Date"),
    )))
    monkeypatch.setattr(alpha_vantage_common.requests, "get", lambda *args, **kwargs: pytest.fail("Hosted connectivity must not call Alpha Vantage directly"))
    monkeypatch.setattr(fred.requests, "get", lambda *args, **kwargs: pytest.fail("Hosted connectivity must not call FRED directly"))
    monkeypatch.setattr(polymarket, "get_prediction_markets", lambda *args, **kwargs: "## Polymarket prediction markets\n- **Fed rate cut?** - Yes 54% ($1,000 volume, resolves 2099-01-01)")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": serialized,
            "TRADINGAGENTS_RUN_ID": serialized.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "managed_identity",
                    "quick_model": "gpt-5.6-sol",
                    "deep_model": "gpt-5.6-sol",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"},
                    "dataKeyConfigured": {"alpha_vantage": True, "fred": True},
                }
            ),
        }
    )
    context = HostedWorkerContext(config, http_client=httpx.Client(transport=httpx.MockTransport(backend_handler)))

    with context.activate():
        result = run_connectivity(validate_request({"mode": "connectivity", "data_vendors": default_request()["data_vendors"] | {"core_stock_apis": "alpha_vantage"}}), lambda *args: None)

    assert [row["status"] for row in result["connectivity"]] == ["ok"] * 5
    assert [(request.url.host, request.url.path) for request in upstream] == [
        ("www.alphavantage.co", "/query"),
        ("api.stlouisfed.org", "/fred/series"),
        ("api.stlouisfed.org", "/fred/series/observations"),
        ("example-foundry.services.ai.azure.com", "/openai/v1/responses"),
    ]
    assert token_scopes == ["https://cognitiveservices.azure.com/.default"]
