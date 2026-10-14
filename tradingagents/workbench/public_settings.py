"""Account-owned settings. Only authenticated server principals may enter this API.

Each successful update replaces one encrypted record and revokes every older pin.
Resolve is private to the future gateway, not a browser API or an auth service.
"""

import hashlib
import hmac
import json
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.azure_client import _TRUSTED_AZURE_AI_HOST_SUFFIXES
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
from tradingagents.workbench.contracts import VENDOR_OPTIONS, default_request, validate_request
from tradingagents.workbench.public_accounts import EmailPrincipal
from tradingagents.workbench.public_store import EncryptedStore, StoreConflict

PROVIDERS = tuple(name for name, env in PROVIDER_API_KEY_ENV.items() if env is not None)
DATA_KEYS = ("alpha_vantage", "fred")
PROFILE_FIELDS = {"quick_model", "deep_model", "api_key", "base_url", "api_version"}
MAX_RECORD_BYTES = 128 * 1024
POLICY_KEY = "email-v1/settings-policy"


class SettingsError(RuntimeError):
    def __init__(self, *, status_code=400):
        super().__init__({
            400: "Invalid settings input.",
            403: "Settings operation is not permitted.",
            409: "Settings changed or credentials are unavailable.",
            503: "Settings storage is unavailable.",
        }[status_code])
        self.status_code = status_code


@dataclass(frozen=True)
class SettingsSnapshot:
    owner_key: str
    run_id: str
    revision: str
    signature: str


@dataclass(frozen=True)
class ResolvedSettings:
    snapshot: SettingsSnapshot
    mode: str
    provider: str
    quick_model: str | None
    deep_model: str | None
    provider_config: MappingProxyType
    asset_type: str
    data_vendors: MappingProxyType
    api_key: str | None = field(repr=False)
    data_keys: MappingProxyType = field(repr=False)


def _uuid(value):
    try:
        valid = type(value) is str and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError()
    return value


def _owner(principal):
    if not isinstance(principal, EmailPrincipal) or type(principal.is_admin) is not bool:
        raise SettingsError(status_code=403)
    user_id = _uuid(principal.object_id)
    owner = f"email-v1/users/{user_id}"
    if principal.owner_key != owner:
        raise SettingsError(status_code=403)
    return owner


def _empty():
    return {
        "revision": None, "pin_key": None, "mode": "api_key", "provider": None,
        "providers": {}, "data_keys": {}, "asset_type": "stock",
        "managed_models": {},
        "data_vendors": default_request()["data_vendors"],
    }


def _provider(provider):
    if type(provider) is not str or provider not in PROVIDERS:
        raise SettingsError()
    native_endpoints = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "google": "https://generativelanguage.googleapis.com",
    }
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    if spec is not None:
        return {
            "adapter": "openai_compatible",
            "endpoint_kind": "approved_origin" if spec.require_base_url else "fixed",
            "base_url": spec.base_url or native_endpoints.get(provider),
        }
    if provider == "azure":
        return {"adapter": "azure", "endpoint_kind": "azure", "base_url": None}
    if provider in native_endpoints:
        return {"adapter": provider, "endpoint_kind": "fixed", "base_url": native_endpoints[provider]}
    raise SettingsError(status_code=503)


def _https(value, paths):
    """Validate URL spelling and host syntax only; dispatch must enforce DNS policy."""
    if type(value) is not str or len(value) > 2048 or not re.fullmatch(r"[\x21-\x7e]+", value):
        raise SettingsError()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        raise SettingsError() from None
    origin = f"https://{host}"
    labels = host.split(".")
    if (value != origin + parsed.path or parsed.path not in paths or len(host) > 253
            or len(labels) < 2 or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
            or labels[-1] in {"localhost", "local", "internal", "lan", "home"}
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in labels)):
        raise SettingsError()
    return origin, host


def _model(value, *, azure=False):
    pattern = r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[/:][A-Za-z0-9][A-Za-z0-9._-]*)*"
    if azure:
        pattern = r"[A-Za-z0-9][A-Za-z0-9._-]*"
    if (type(value) is not str or not 1 <= len(value) <= 128 or value == "custom"
            or ".." in value or not re.fullmatch(pattern, value)):
        raise SettingsError()
    return value


def _keys(current, updates, allowed):
    if type(updates) is not dict or updates.keys() - set(allowed):
        raise SettingsError()
    result = dict(current)
    for name, value in updates.items():
        if value is None:
            result.pop(name, None)
        elif type(value) is str and re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
            result[name] = value
        else:
            raise SettingsError()
    return result


def _bounded(value):
    try:
        size = len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii"))
    except (ValueError, TypeError, RecursionError):
        raise SettingsError() from None
    if size > MAX_RECORD_BYTES:
        raise SettingsError()


def _profile_public(provider, profile):
    metadata = _provider(provider)
    return {
        "quick_model": profile["quick_model"], "deep_model": profile["deep_model"],
        "base_url": metadata["base_url"] or profile["base_url"],
        **({"api_version": profile["api_version"]} if provider == "azure" else {}),
    }


def _public(record, version):
    profiles = {
        provider: {**_profile_public(provider, profile),
                   "key_configured": bool(profile.get("api_key"))}
        for provider, profile in record["providers"].items()
    }
    selected = record["managed_models"] if record["mode"] == "managed_identity" else profiles.get(record["provider"], {})
    return deepcopy({
        "version": version, "revision": record["revision"], "mode": record["mode"],
        "provider": record["provider"], "quick_model": selected.get("quick_model"),
        "deep_model": selected.get("deep_model"), "providers": profiles,
        "managed_models": record["managed_models"],
        "model_key_configured": selected.get("key_configured", False),
        "data_key_configured": {
            name: name in record["data_keys"] for name in DATA_KEYS
        },
        "asset_type": record["asset_type"], "data_vendors": record["data_vendors"],
    })


def _signature(record, owner, run_id):
    message = json.dumps([owner, run_id, record["revision"]], separators=(",", ":"))
    return hmac.new(bytes.fromhex(record["pin_key"]), message.encode(), hashlib.sha256).hexdigest()


class UserSettings:
    """Write-only credentials and nonsecret configuration for an authenticated account.

    Update patches accept mode, provider, providers, data_keys, data_vendors and
    asset_type. quick_model/deep_model/api_key/base_url/api_version are shortcuts
    for the selected provider; do not also patch that provider in providers.
    Each providers entry has those same five fields, with base_url permitted only
    for Azure/generic endpoints and api_version only for Azure. Both model names
    are required on profile creation; Azure names identify quick/deep deployments.
    Unknown safe model IDs are accepted like BaseLLMClient's warning-only catalog.

    Omitted keys/profiles are preserved; a null key removes that key, and a null
    providers entry removes the entire profile. Removing the selection clears it
    without fallback. Every successful write, including a no-op patch, rotates
    revision and invalidates every previous snapshot. Empty patches are rejected.
    expected_version is the opaque version from get/update, or None on creation.
    Conflicts return 409; uncertain storage returns 503 without retry.

    Managed identity is selected solely from the trusted principal's is_admin;
    its endpoint and allowed deployments are installation-owned. Only selected
    deployment names are stored here; the gateway validates catalog membership.
    Resolve returns a frozen private object, never a public response. The gateway
    must resolve again for each dispatch, bind snapshot.run_id to its trusted run,
    disable redirects and validate DNS at dispatch; cached keys are not revocable.
    """

    def __init__(self, store: EncryptedStore, *, approved_origins=()):
        if type(approved_origins) not in (tuple, list, set, frozenset) or len(approved_origins) > 128:
            raise SettingsError()
        self._store = store
        self._default_approved_origins = tuple(sorted({_https(origin, ("", "/"))[0] for origin in approved_origins}))

    def _read_policy(self):
        try:
            stored = self._store.read(POLICY_KEY)
        except Exception:
            raise SettingsError(status_code=503) from None
        if stored is None:
            return {"approved_origins": list(self._default_approved_origins)}, None
        record, version = stored
        try:
            origins = record["approved_origins"]
            if (
                type(record) is not dict
                or set(record) != {"approved_origins"}
                or type(origins) is not list
                or len(origins) > 128
            ):
                raise ValueError
            normalized = sorted({_https(origin, ("", "/"))[0] for origin in origins})
            if normalized != origins:
                raise ValueError
            if version is not None and (type(version) is not str or not 1 <= len(version) <= 256):
                raise ValueError
            return {"approved_origins": normalized}, version
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SettingsError(status_code=503) from None

    def _approved_origins(self):
        return frozenset(self._read_policy()[0]["approved_origins"])

    def catalog(self) -> dict:
        return {
            "providers": {
                provider: {
                    **_provider(provider), "key_required": True, "custom_models": True,
                    "models": {
                        mode: [{"label": label, "value": value} for label, value in
                               MODEL_OPTIONS.get(provider, {}).get(mode, []) if value != "custom"]
                        for mode in ("quick", "deep")
                    },
                } for provider in PROVIDERS
            },
            "modes": {"api_key": {"admin_only": False}, "managed_identity": {"admin_only": True}},
            "data_vendors": {name: list(options) for name, options in VENDOR_OPTIONS.items()},
            "data_keys": list(DATA_KEYS), "approved_origins": sorted(self._approved_origins()),
            "compatible_paths": ["/v1", "/v1/"],
        }

    def policy(self, principal: EmailPrincipal) -> dict:
        if not principal.is_admin:
            raise SettingsError(status_code=403)
        record, version = self._read_policy()
        return {"version": version, "approved_origins": list(record["approved_origins"])}

    def update_policy(self, principal: EmailPrincipal, payload: dict, *, expected_version: str | None) -> dict:
        if not principal.is_admin:
            raise SettingsError(status_code=403)
        if expected_version == "":
            expected_version = None
        if expected_version is not None and (type(expected_version) is not str or not 1 <= len(expected_version) <= 256):
            raise SettingsError()
        if type(payload) is not dict or set(payload) != {"approved_origins"} or type(payload["approved_origins"]) is not list:
            raise SettingsError()
        origins = payload["approved_origins"]
        if len(origins) > 128:
            raise SettingsError()
        record, version = self._read_policy()
        if version != expected_version:
            raise SettingsError(status_code=409)
        updated = {"approved_origins": sorted({_https(origin, ("", "/"))[0] for origin in origins})}
        try:
            next_version = self._store.write(POLICY_KEY, updated, version)
        except StoreConflict:
            raise SettingsError(status_code=409) from None
        except Exception:
            raise SettingsError(status_code=503) from None
        if type(next_version) is not str or not 1 <= len(next_version) <= 256:
            raise SettingsError(status_code=503)
        return {"version": next_version, "approved_origins": list(updated["approved_origins"])}

    def _profile(self, provider, current, patch, *, enforce_approval=True):
        metadata = _provider(provider)
        if type(patch) is not dict or patch.keys() - PROFILE_FIELDS:
            raise SettingsError()
        if metadata["endpoint_kind"] == "fixed" and "base_url" in patch:
            raise SettingsError()
        if provider != "azure" and "api_version" in patch:
            raise SettingsError()
        result = {**current, **patch}
        for name in ("quick_model", "deep_model"):
            result[name] = _model(result.get(name), azure=provider == "azure")
        if metadata["endpoint_kind"] == "approved_origin":
            origin, _ = _https(result.get("base_url"), ("/v1", "/v1/"))
            if enforce_approval and origin not in self._approved_origins():
                raise SettingsError(status_code=409)
            result["base_url"] = origin + "/v1"
        elif provider == "azure":
            origin, host = _https(result.get("base_url"), ("", "/"))
            if not any(
                host.endswith(suffix) and "." not in host[:-len(suffix)]
                and host[:-len(suffix)] not in ("", "privatelink")
                for suffix in _TRUSTED_AZURE_AI_HOST_SUFFIXES
            ):
                raise SettingsError()
            result["base_url"] = origin + "/"
            version = result.get("api_version")
            if type(version) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?", version):
                raise SettingsError()
            try:
                date.fromisoformat(version[:10])
            except ValueError:
                raise SettingsError() from None
        credentials = _keys({}, {"api_key": result.pop("api_key", None)}, ("api_key",))
        return {**result, **credentials}

    def _apply(self, record, payload, *, enforce_approval=True):
        allowed = PROFILE_FIELDS | {"mode", "provider", "providers", "data_keys", "data_vendors", "asset_type", "managed_models"}
        if type(payload) is not dict or not payload or payload.keys() - allowed:
            raise SettingsError()
        _bounded(payload)
        result = deepcopy(record)
        mode = payload.get("mode", result["mode"])
        if type(mode) is not str or mode not in ("api_key", "managed_identity"):
            raise SettingsError()
        result["mode"] = mode
        if "managed_models" in payload:
            models = payload["managed_models"]
            if type(models) is not dict or set(models) not in (set(), {"quick_model", "deep_model"}):
                raise SettingsError()
            result["managed_models"] = {name: _model(value, azure=True) for name, value in models.items()}
        provider = payload.get("provider", result["provider"])
        if provider is not None:
            _provider(provider)
        flat = {name: payload[name] for name in PROFILE_FIELDS if name in payload}
        profiles = payload.get("providers", {})
        if type(profiles) is not dict or profiles.keys() - set(PROVIDERS):
            raise SettingsError()
        if flat and (provider is None or provider in profiles or mode != "api_key"):
            raise SettingsError()
        if mode == "managed_identity":
            if "provider" in payload and provider is not None:
                raise SettingsError()
            provider = None
        for name, patch in profiles.items():
            if patch is None:
                result["providers"].pop(name, None)
                if provider == name:
                    provider = None
            else:
                result["providers"][name] = self._profile(
                    name, result["providers"].get(name, {}), patch, enforce_approval=enforce_approval,
                )
        if flat:
            result["providers"][provider] = self._profile(
                provider, result["providers"].get(provider, {}), flat, enforce_approval=enforce_approval,
            )
        if provider is not None and provider not in result["providers"]:
            raise SettingsError()
        result["provider"] = provider
        result["data_keys"] = _keys(result["data_keys"], payload.get("data_keys", {}), DATA_KEYS)
        vendors = payload.get("data_vendors", {})
        if type(vendors) is not dict:
            raise SettingsError()
        try:
            request = validate_request({
                "asset_type": payload.get("asset_type", result["asset_type"]),
                "data_vendors": {**result["data_vendors"], **vendors},
            })
        except ValueError:
            raise SettingsError() from None
        result["asset_type"] = request["asset_type"]
        result["data_vendors"] = request["data_vendors"]
        return result

    def _read(self, owner):
        try:
            stored = self._store.read(f"{owner}/settings")
            if stored is None:
                return _empty(), None
            record, version = stored
            if type(record) is dict and "managed_models" not in record:
                record = {**record, "managed_models": {}}
            if (type(record) is not dict or record.keys() != _empty().keys()
                    or type(version) is not str or not 1 <= len(version) <= 256):
                raise ValueError
            _bounded(record)
            _uuid(record["revision"])
            if type(record["pin_key"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", record["pin_key"]):
                raise ValueError
            normalized = self._apply(
                _empty(), {key: value for key, value in record.items() if key not in ("revision", "pin_key")},
                enforce_approval=False,
            )
            normalized.update(revision=record["revision"], pin_key=record["pin_key"])
            if normalized != record:
                raise ValueError
            return record, version
        except Exception:
            raise SettingsError(status_code=503) from None

    def get(self, principal: EmailPrincipal) -> dict:
        return _public(*self._read(_owner(principal)))

    def data_key(self, principal: EmailPrincipal, name: str) -> str | None:
        if type(name) is not str or name != "alpha_vantage":
            raise SettingsError()
        record, _ = self._read(_owner(principal))
        return record["data_keys"].get(name)

    def update(self, principal: EmailPrincipal, payload: dict, *, expected_version: str | None) -> dict:
        owner = _owner(principal)
        if expected_version is not None and (type(expected_version) is not str or not 1 <= len(expected_version) <= 256):
            raise SettingsError()
        record, version = self._read(owner)
        if expected_version != version:
            raise SettingsError(status_code=409)
        record = self._apply(record, payload)
        if (record["mode"] == "managed_identity" or "managed_models" in payload) and not principal.is_admin:
            raise SettingsError(status_code=403)
        record["revision"] = str(uuid4())
        record["pin_key"] = secrets.token_hex(32)
        _bounded(record)
        try:
            version = self._store.write(f"{owner}/settings", record, version)
            if type(version) is not str or not 1 <= len(version) <= 256:
                raise ValueError
        except StoreConflict:
            raise SettingsError(status_code=409) from None
        except Exception:
            raise SettingsError(status_code=503) from None
        return _public(record, version)

    def pin(self, principal: EmailPrincipal, run_id: str) -> SettingsSnapshot:
        owner = _owner(principal)
        _uuid(run_id)
        record, _ = self._read(owner)
        self._ready(principal, record)
        return SettingsSnapshot(owner, run_id, record["revision"], _signature(record, owner, run_id))

    def _ready(self, principal, record):
        if record["revision"] is None:
            raise SettingsError(status_code=409)
        if record["mode"] == "managed_identity":
            if not principal.is_admin:
                raise SettingsError(status_code=403)
        elif not record["providers"].get(record["provider"], {}).get("api_key"):
            raise SettingsError(status_code=409)
        else:
            self._profile(record["provider"], {}, record["providers"][record["provider"]])

    def resolve(self, principal: EmailPrincipal, snapshot: SettingsSnapshot) -> ResolvedSettings:
        owner = _owner(principal)
        if type(snapshot) is not SettingsSnapshot or snapshot.owner_key != owner:
            raise SettingsError(status_code=403)
        _uuid(snapshot.run_id)
        record, _ = self._read(owner)
        if record["revision"] is None or snapshot.revision != record["revision"]:
            raise SettingsError(status_code=409)
        if (type(snapshot.signature) is not str or not re.fullmatch(r"[0-9a-f]{64}", snapshot.signature)
            or not hmac.compare_digest(snapshot.signature, _signature(record, owner, snapshot.run_id))):
            raise SettingsError(status_code=403)
        self._ready(principal, record)
        profile = record["providers"].get(record["provider"], {})
        models = record["managed_models"] if record["mode"] == "managed_identity" else profile
        return ResolvedSettings(
            snapshot, record["mode"], record["provider"] or "azure", models.get("quick_model"),
            models.get("deep_model"),
            MappingProxyType(_profile_public(record["provider"], profile) if profile else {}),
            record["asset_type"], MappingProxyType(record["data_vendors"]),
            profile.get("api_key"), MappingProxyType(record["data_keys"]),
        )
