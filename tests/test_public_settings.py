# Modified for public distribution: installation identifiers replaced with examples.
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier, Lock
from uuid import uuid4

import pytest

from tests.test_public_store import MemoryBackend
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.workbench.contracts import VENDOR_OPTIONS
from tradingagents.workbench.public_accounts import EmailPrincipal
from tradingagents.workbench.public_store import EncryptedStore


def principal(*, admin=False):
    return EmailPrincipal(str(uuid4()), "fixture@example.com", "Fixture", admin)


def test_managed_model_choices_preserve_legacy_pins_and_survive_restart():
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    store = EncryptedStore(MemoryBackend(), b"s" * 32)
    settings = UserSettings(store)
    owner = principal(admin=True)
    first = settings.update(owner, {"mode": "managed_identity"}, expected_version=None)
    pinned = settings.pin(owner, str(uuid4()))
    record, version = store.read(f"{owner.owner_key}/settings")
    record.pop("managed_models", None)
    store.write(f"{owner.owner_key}/settings", record, version)
    assert settings.resolve(owner, pinned).quick_model is None
    choices = {"quick_model": "gpt-5.6-luna", "deep_model": "gpt-5.6-terra"}
    saved = settings.update(owner, {"managed_models": choices}, expected_version=settings.get(owner)["version"])
    assert saved["quick_model"] == choices["quick_model"]
    assert saved["deep_model"] == choices["deep_model"]
    restarted = UserSettings(store)
    resolved = restarted.resolve(owner, restarted.pin(owner, str(uuid4())))
    assert (resolved.quick_model, resolved.deep_model) == tuple(choices.values())
    assert resolved.api_key is None
    with pytest.raises(SettingsError):
        restarted.resolve(owner, pinned)
    assert saved["revision"] != first["revision"]
    with pytest.raises(SettingsError):
        settings.update(principal(), {"managed_models": choices}, expected_version=None)


def model_payload(**changes):
    return {
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-4.1-mini",
        "deep_model": "gpt-4.1", "api_key": "fixture-model-key",
        "data_keys": {"fred": "fixture-fred-key"}, **changes,
    }


def test_encrypted_settings_never_read_back_keys_and_isolate_accounts():
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"s" * 32)
    settings = UserSettings(store)
    guest, other = principal(), principal()
    saved = settings.update(guest, model_payload(), expected_version=None)
    assert saved["model_key_configured"] is True
    assert saved["data_key_configured"] == {"alpha_vantage": False, "fred": True}
    assert saved == UserSettings(store).get(guest)
    assert settings.get(other)["version"] is None
    run_id = str(uuid4())
    snapshot = settings.pin(guest, run_id)
    assert snapshot.owner_key == guest.owner_key
    assert snapshot.run_id == run_id
    resolved = settings.resolve(guest, snapshot)
    assert resolved.api_key == "fixture-model-key"
    assert resolved.data_keys == {"fred": "fixture-fred-key"}
    with pytest.raises(SettingsError):
        settings.resolve(other, snapshot)
    for secret in ("fixture-model-key", "fixture-fred-key"):
        assert secret not in json.dumps(saved)
        assert secret not in repr((snapshot, resolved, settings, backend.records))
        assert all(secret.encode() not in content for content, _ in backend.records.values())


@pytest.fixture
def case():
    from tradingagents.workbench.public_settings import UserSettings

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"s" * 32)
    return UserSettings(store, approved_origins=("https://relay.example.com",)), principal(), backend


KEY_PROVIDERS = [name for name, env in PROVIDER_API_KEY_ENV.items() if env is not None]


def provider_payload(provider):
    config = model_payload(provider=provider, quick_model="fixture-quick", deep_model="fixture-deep")
    if provider == "azure":
        config.update(base_url="https://fixture.openai.azure.com/", api_version="2025-03-01-preview")
    elif provider == "openai_compatible":
        config["base_url"] = "https://relay.example.com/v1"
    return config


@pytest.mark.parametrize("provider", KEY_PROVIDERS)
def test_every_key_provider_has_independent_config_and_native_adapter(case, provider):
    settings, guest, _ = case
    catalog = settings.catalog()
    assert set(catalog["providers"]) == set(KEY_PROVIDERS)
    saved = settings.update(guest, provider_payload(provider), expected_version=None)
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.provider == provider
    assert resolved.quick_model == "fixture-quick"
    assert resolved.deep_model == "fixture-deep"
    assert resolved.api_key == "fixture-model-key"
    assert saved["providers"][provider]["key_configured"] is True
    for model in (resolved.quick_model, resolved.deep_model):
        client = create_llm_client(provider, model, base_url=resolved.provider_config["base_url"])
        expected = {"azure": "AzureOpenAIClient", "anthropic": "AnthropicClient", "google": "GoogleClient"}
        assert type(client).__name__ == expected.get(provider, "OpenAIClient")
        assert client.model == model
    assert catalog["providers"][provider]["custom_models"] is True


@pytest.mark.parametrize("provider,endpoint", [
    ("openai", "https://api.openai.com/v1"),
    ("anthropic", "https://api.anthropic.com"),
    ("google", "https://generativelanguage.googleapis.com"),
    ("xai", "https://api.x.ai/v1"), ("deepseek", "https://api.deepseek.com"),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("glm", "https://api.z.ai/api/paas/v4/"),
    ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
    ("minimax", "https://api.minimax.io/v1"), ("minimax-cn", "https://api.minimaxi.com/v1"),
    ("openrouter", "https://openrouter.ai/api/v1"), ("mistral", "https://api.mistral.ai/v1"),
    ("kimi", "https://api.moonshot.ai/v1"), ("groq", "https://api.groq.com/openai/v1"),
    ("nvidia", "https://integrate.api.nvidia.com/v1"),
])
def test_fixed_provider_destinations_are_literal_and_cannot_be_overridden(case, provider, endpoint):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    saved = settings.update(guest, provider_payload(provider), expected_version=None)
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.provider_config["base_url"] == endpoint
    assert settings.catalog()["providers"][provider]["base_url"] == endpoint
    for value in ("https://relay.example.com/v1", endpoint, None):
        with pytest.raises(SettingsError):
            settings.update(guest, {"base_url": value}, expected_version=saved["version"])


@pytest.mark.parametrize("provider,regional", [("qwen", "qwen-cn"), ("glm", "glm-cn"), ("minimax", "minimax-cn")])
def test_switching_regions_preserves_separate_keys_models_and_destinations(case, provider, regional):
    settings, guest, _ = case
    first = settings.update(guest, provider_payload(provider), expected_version=None)
    second = settings.update(guest, provider_payload(regional) | {"api_key": "fixture-cn-key"},
                             expected_version=first["version"])
    third = settings.update(guest, {"provider": provider}, expected_version=second["version"])
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.api_key == "fixture-model-key"
    assert set(third["providers"]) == {provider, regional}
    assert third["providers"][provider]["base_url"] != third["providers"][regional]["base_url"]


def test_bulk_profiles_preserve_selection_and_remove_without_fallback(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    saved = settings.update(guest, model_payload(), expected_version=None)
    updated = settings.update(guest, {"providers": {
        "anthropic": {"quick_model": "claude-sonnet-5", "deep_model": "claude-fable-5",
                      "api_key": "fixture-anthropic-key"},
    }}, expected_version=saved["version"])
    assert updated["provider"] == "openai"
    selected = settings.update(guest, {"provider": "anthropic"}, expected_version=updated["version"])
    assert selected["quick_model"] == "claude-sonnet-5"
    removed = settings.update(guest, {"providers": {"anthropic": None}}, expected_version=selected["version"])
    assert "anthropic" not in removed["providers"]
    assert removed["provider"] is None
    assert removed["model_key_configured"] is False
    with pytest.raises(SettingsError):
        settings.pin(guest, str(uuid4()))
    assert len(backend.records) == 1


@pytest.mark.parametrize("patch", [
    {"api_key": "fixture-replacement-key"}, {"api_key": None},
    {"quick_model": "new-quick"}, {"data_keys": {"fred": None}},
    {"data_vendors": {"news_data": "alpha_vantage,yfinance"}},
    {"providers": {"openai": None}},
])
def test_every_update_revokes_old_pins_and_old_secrets_are_not_retained(case, patch):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    saved = settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    changed = settings.update(guest, patch, expected_version=saved["version"])
    assert changed["revision"] != saved["revision"]
    assert changed["version"] != saved["version"]
    with pytest.raises(SettingsError) as failure:
        settings.resolve(guest, snapshot)
    assert failure.value.status_code == 409
    assert len(backend.records) == 1
    stored, _ = settings._store.read(f"{guest.owner_key}/settings")
    if "api_key" in patch or "providers" in patch:
        assert "fixture-model-key" not in json.dumps(stored)


@pytest.mark.parametrize("provider", KEY_PROVIDERS)
def test_removed_keys_are_unconfigured_and_never_fall_back_to_environment(case, monkeypatch, provider):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    for name in [env for env in PROVIDER_API_KEY_ENV.values() if env] + ["FRED_API_KEY", "ALPHA_VANTAGE_API_KEY"]:
        monkeypatch.setenv(name, "fixture-forbidden-admin-key")
    assert settings.catalog()["providers"][provider]["key_required"] is True
    saved = settings.update(guest, provider_payload(provider), expected_version=None)
    changed = settings.update(guest, {"api_key": None, "data_keys": {"fred": None}},
                              expected_version=saved["version"])
    assert changed["model_key_configured"] is False
    assert changed["data_key_configured"] == {"alpha_vantage": False, "fred": False}
    with pytest.raises(SettingsError):
        settings.pin(guest, str(uuid4()))


def test_snapshots_are_frozen_and_cannot_change_run_owner_or_generation(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    with pytest.raises(FrozenInstanceError):
        snapshot.run_id = str(uuid4())
    for modified in (replace(snapshot, run_id=str(uuid4())), replace(snapshot, owner_key=principal().owner_key),
                     replace(snapshot, revision=str(uuid4())), replace(snapshot, signature="bad")):
        with pytest.raises(SettingsError):
            settings.resolve(guest, modified)
    resolved = settings.resolve(guest, snapshot)
    with pytest.raises(TypeError):
        resolved.provider_config["base_url"] = "https://evil.example.com"
    with pytest.raises(TypeError):
        resolved.data_keys["fred"] = "replacement"


def test_managed_identity_uses_only_trusted_admin_role_and_no_user_destination(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    spoofed_email = replace(guest, email="owner@example.com")
    with pytest.raises(SettingsError) as failure:
        settings.update(spoofed_email, {"mode": "managed_identity"}, expected_version=None)
    assert failure.value.status_code == 403
    admin = principal(admin=True)
    saved = settings.update(admin, {"mode": "managed_identity"}, expected_version=None)
    resolved = settings.resolve(admin, settings.pin(admin, str(uuid4())))
    assert saved["provider"] is None
    assert resolved.provider == "azure"
    assert resolved.quick_model is None and resolved.deep_model is None
    assert resolved.api_key is None and resolved.provider_config == {}
    with pytest.raises(SettingsError):
        settings.update(admin, {"quick_model": "caller-deployment"}, expected_version=saved["version"])


def test_approved_origin_policy_persists_and_removed_origin_revokes_ready_state():
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"p" * 32)
    admin = principal(admin=True)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    saved = settings.update(admin, provider_payload("openai_compatible"), expected_version=None)
    snapshot = settings.pin(admin, str(uuid4()))
    assert settings.policy(admin) == {"version": None, "approved_origins": ["https://relay.example.com"]}

    updated = settings.update_policy(
        admin,
        {"approved_origins": ["https://next.example.com"]},
        expected_version=None,
    )
    restarted = UserSettings(store)
    assert restarted.policy(admin) == updated
    assert restarted.catalog()["approved_origins"] == ["https://next.example.com"]
    with pytest.raises(SettingsError) as failure:
        restarted.resolve(admin, snapshot)
    assert failure.value.status_code == 409
    current = restarted.get(admin)
    with pytest.raises(SettingsError) as failure:
        restarted.pin(admin, str(uuid4()))
    assert failure.value.status_code == 409
    assert current == saved


@pytest.mark.parametrize("patch", [
    {"mode": "entra_id"}, {"mode": None}, {"mode": []}, {"provider": "ollama"},
    {"provider": "bedrock"}, {"provider": "unknown"}, {"provider": []},
    {"quick_model": ""}, {"deep_model": None}, {"quick_model": "custom"},
    {"deep_model": "a" * 129}, {"deep_model": "../model"}, {"quick_model": "a//b"},
    {"quick_model": "https://evil.example.com"}, {"deep_model": "model\n"},
    {"quick_model": {"name": "gpt"}}, {"deep_model": " model"},
    {"api_key": ""}, {"api_key": " leading"}, {"api_key": "line\nbreak"},
    {"api_key": "x" * 4097}, {"api_key": True}, {"api_key": {}}, {"api_key": "\u00e9"},
    {"data_keys": {"yfinance": "fake"}}, {"data_keys": {"polymarket": "fake"}},
    {"data_keys": {"fred": "bad\rkey"}}, {"data_keys": []},
    {"providers": []}, {"providers": {"unknown": {}}},
    {"providers": {"openai": {"api_key": []}}},
    {"providers": {"azure": {"quick_model": "quick", "deep_model": "deep"}}},
    {"providers": {"openai": {"api_key": "another"}}},
    {"owner_key": "email-v1/users/other"}, {"object_id": str(uuid4())},
    {"is_admin": True}, {"approved_origins": ["https://evil.example.com"]},
    {"subscription_id": str(uuid4())}, {"env": {}}, {"auth_mode": "managed_identity"},
    {"api_version": "2025-03-01-preview"}, {"enable_local_auth": True},
])
def test_invalid_fields_models_keys_and_injection_are_rejected_without_writes(case, patch):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    with pytest.raises(SettingsError) as failure:
        settings.update(guest, model_payload(**patch), expected_version=None)
    assert failure.value.status_code in (400, 403)
    assert backend.records == {}
    assert "fixture-model-key" not in str(failure.value)


@pytest.mark.parametrize("endpoint", [
    "http://relay.example.com/v1", "https://other.example.com/v1",
    "https://relay.example.com.evil.example/v1", "https://relay.example.com@evil.example/v1",
    "https://user:pass@relay.example.com/v1", "https://relay.example.com:443/v1",
    "https://relay.example.com:0443/v1", "https://relay.example.com:8443/v1",
    "https://relay.example.com/v1?key=x", "https://relay.example.com/v1?",
    "https://relay.example.com/v1#", "https://relay.example.com/v1#fragment",
    "https://relay.example.com/v1/../admin", "https://relay.example.com/v1/%2e%2e",
    "https://relay.example.com/v1/chat/completions", "https://relay.example.com//v1",
    "https://relay.example.com/other/v1", "https://relay.example.com/",
    "https://relay.example.com\\@127.0.0.1/v1", "https://relay.example.com./v1",
    "https://relay.example.com\n/v1", " https://relay.example.com/v1",
    "https://127.0.0.1/v1", "https://10.0.0.1/v1", "https://169.254.169.254/v1",
    "https://[::1]/v1", "https://localhost/v1", "https://2130706433/v1",
    "https://0x7f000001/v1", "https://0177.0.0.1/v1",
])
def test_compatible_url_policy_rejects_tricks_and_nonexact_paths(case, endpoint):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    with pytest.raises(SettingsError):
        settings.update(guest, provider_payload("openai_compatible") | {"base_url": endpoint},
                        expected_version=None)
    assert backend.records == {}


@pytest.mark.parametrize("origin", [
    "http://relay.example.com", "https://127.0.0.1", "https://8.8.8.8", "https://[::1]",
    "https://localhost", "https://app.localhost", "https://app.local", "https://app.internal",
    "https://relay.example.com:443", "https://user@relay.example.com",
    "https://relay.example.com/v1", "https://relay.example.com?", "https://relay.example.com#",
])
def test_installation_cannot_approve_unsafe_origins(case, origin):
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    settings, _, _ = case
    with pytest.raises(SettingsError):
        UserSettings(settings._store, approved_origins=(origin,))


@pytest.mark.parametrize("host", ["fixture.openai.azure.com", "fixture.services.ai.azure.com", "fixture.cognitiveservices.azure.com"])
def test_azure_accepts_only_trusted_resource_roots_with_two_deployments(case, host):
    settings, guest, _ = case
    saved = settings.update(guest, provider_payload("azure") | {"base_url": f"https://{host}"},
                            expected_version=None)
    profile = saved["providers"]["azure"]
    assert profile["base_url"] == f"https://{host}/"
    assert profile["api_version"] == "2025-03-01-preview"
    assert profile["quick_model"] == "fixture-quick" and profile["deep_model"] == "fixture-deep"


@pytest.mark.parametrize("patch", [
    {"base_url": "https://evil.example.com/"}, {"base_url": "https://openai.azure.com/"},
    {"base_url": "https://fixture.openai.azure.com.evil.example.com/"},
    {"base_url": "https://fixture.privatelink.openai.azure.com/"},
    {"base_url": "https://fixture.openai.azure.com/openai/v1"},
    {"base_url": "https://fixture.openai.azure.com:443/"},
    {"base_url": "https://user@fixture.openai.azure.com/"},
    {"base_url": "https://fixture.openai.azure.com/?x=1"},
    {"base_url": "http://fixture.openai.azure.com/"},
    {"quick_model": "org/deployment"}, {"deep_model": "deployment:free"},
    {"api_version": "2025-02-30"}, {"api_version": "2025-03-01&x=y"},
    {"api_version": "latest"}, {"api_version": None}, {"enable_local_auth": True},
])
def test_azure_rejects_bad_hosts_paths_deployments_and_versions(case, patch):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    with pytest.raises(SettingsError):
        settings.update(guest, provider_payload("azure") | patch, expected_version=None)


def test_data_catalog_status_chains_and_etf_rule(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    catalog = settings.catalog()
    assert catalog["data_vendors"] == {name: list(options) for name, options in VENDOR_OPTIONS.items()}
    assert catalog["data_keys"] == ["alpha_vantage", "fred"]
    saved = settings.update(guest, model_payload(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"news_data": " alpha_vantage, yfinance "}, asset_type="etf",
    ), expected_version=None)
    assert saved["data_key_configured"] == {"alpha_vantage": True, "fred": False}
    assert saved["data_vendors"]["news_data"] == "alpha_vantage,yfinance"
    assert saved["data_vendors"]["fundamental_data"] == "yfinance"
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.data_keys == {"alpha_vantage": "fixture-alpha-key"}
    for patch in ({"data_vendors": {"fundamental_data": "alpha_vantage"}},
                  {"data_vendors": {"news_data": "yfinance,yfinance"}},
                  {"data_vendors": {"macro_data": "yfinance"}}, {"asset_type": "crypto"}):
        with pytest.raises(SettingsError):
            settings.update(guest, patch, expected_version=saved["version"])


@pytest.mark.parametrize("initial", [True, False])
def test_real_encrypted_store_cas_allows_only_one_writer(initial):
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    class RacingBackend(MemoryBackend):
        def __init__(self):
            super().__init__()
            self.lock = Lock()
            self.barrier = Barrier(2)
            self.racing = False

        def read(self, key):
            with self.lock:
                result = super().read(key)
            if self.racing:
                self.barrier.wait(timeout=5)
            return result

        def write(self, key, content, expected_version=None):
            with self.lock:
                return super().write(key, content, expected_version)

    backend = RacingBackend()
    store = EncryptedStore(backend, b"s" * 32)
    guest = principal()
    version = None
    if not initial:
        version = UserSettings(store).update(guest, model_payload(), expected_version=None)["version"]
    backend.racing = True

    def write(index):
        try:
            return UserSettings(store).update(guest, model_payload(api_key=f"fixture-key-{index}"),
                                              expected_version=version)
        except SettingsError as failure:
            return failure.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, (1, 2)))
    backend.racing = False
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert outcomes.count(409) == 1
    assert backend.revision == (1 if initial else 2)
    winner = next(outcome for outcome in outcomes if isinstance(outcome, dict))
    assert UserSettings(store).get(guest) == winner


def test_accounts_cannot_set_or_remove_another_accounts_credentials(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    saved = settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    other = principal()
    with pytest.raises(SettingsError):
        settings.update(other, {"api_key": None}, expected_version=saved["version"])
    other_saved = settings.update(other, model_payload(api_key="fixture-other-key"), expected_version=None)
    settings.update(other, {"providers": {"openai": None}, "data_keys": {"fred": None}},
                    expected_version=other_saved["version"])
    assert settings.get(guest) == saved
    assert settings.resolve(guest, snapshot).api_key == "fixture-model-key"
    with pytest.raises(SettingsError):
        settings.resolve(other, snapshot)


@pytest.mark.parametrize("bad_id", ["../registry", "../users/other", "sessions/a", "", "not-uuid", None, 1])
def test_malicious_principal_paths_fail_before_storage(case, monkeypatch, bad_id):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    malformed = replace(guest, object_id=bad_id)
    accesses = []
    monkeypatch.setattr(backend, "read", lambda key: accesses.append(key))
    for action in (lambda: settings.get(malformed),
                   lambda: settings.update(malformed, model_payload(), expected_version=None),
                   lambda: settings.pin(malformed, str(uuid4())),
                   lambda: settings.resolve(malformed, None)):
        with pytest.raises(SettingsError):
            action()
    assert accesses == [] and backend.records == {}


def test_mismatched_owner_namespace_and_nonauth_principals_are_rejected(case):
    from types import SimpleNamespace

    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case

    class WrongOwner(EmailPrincipal):
        @property
        def owner_key(self):
            return guest.owner_key

    for invalid in (WrongOwner(str(uuid4()), "guest@example.com", "Guest", False),
                    SimpleNamespace(object_id=guest.object_id, owner_key=guest.owner_key, is_admin=True),
                    replace(guest, is_admin=1)):
        with pytest.raises(SettingsError):
            settings.update(invalid, model_payload(), expected_version=None)
    assert backend.records == {}


@pytest.mark.parametrize("value", [None, "", [], 1, "x" * 257])
def test_expected_version_is_required_and_bounded(case, value):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    settings.update(guest, model_payload(), expected_version=None)
    with pytest.raises(SettingsError):
        settings.update(guest, {"api_key": "fixture-new-key"}, expected_version=value)
    assert backend.revision == 1


@pytest.mark.parametrize("value", [None, "", "../run", "not-a-uuid", [], 42])
def test_pin_rejects_invalid_run_ids(case, value):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    settings.update(guest, model_payload(), expected_version=None)
    with pytest.raises(SettingsError):
        settings.pin(guest, value)


@pytest.mark.parametrize("signature", [None, [], b"digest", "\u00e9" * 64, "x" * 10000])
def test_malformed_snapshot_signatures_are_sanitized(case, signature):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    with pytest.raises(SettingsError):
        settings.resolve(guest, replace(snapshot, signature=signature))


@pytest.mark.parametrize("mutation", [
    {"mode": "other"}, {"provider": "unknown"}, {"revision": None}, {"pin_key": "bad"},
    {"data_keys": {"yahoo": "fixture-hidden-key"}}, {"extra": "fixture-hidden-key"},
    {"providers": {"openai": {"quick_model": "quick", "deep_model": "deep", "extra": "fixture-hidden-key"}}},
    {"data_vendors": {"macro_data": "yfinance"}}, {"asset_type": "bad"},
])
def test_malformed_encrypted_records_fail_closed_without_readback(case, mutation):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    key = f"{guest.owner_key}/settings"
    stored, version = settings._store.read(key)
    settings._store.write(key, stored | mutation, version)
    for action in (lambda: settings.get(guest), lambda: settings.pin(guest, str(uuid4())),
                   lambda: settings.resolve(guest, snapshot)):
        with pytest.raises(SettingsError) as failure:
            action()
        assert failure.value.status_code == 503
        assert "fixture-hidden-key" not in repr(failure.value)


@pytest.mark.parametrize("operation", ["get", "pin", "resolve", "update"])
def test_unknown_read_failure_is_sanitized_and_never_authorizes(case, monkeypatch, operation):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    saved = settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))

    def broken_read(key):
        raise OSError("fixture-provider-secret")

    monkeypatch.setattr(backend, "read", broken_read)
    actions = {
        "get": lambda: settings.get(guest), "pin": lambda: settings.pin(guest, str(uuid4())),
        "resolve": lambda: settings.resolve(guest, snapshot),
        "update": lambda: settings.update(guest, {"api_key": None}, expected_version=saved["version"]),
    }
    with pytest.raises(SettingsError) as failure:
        actions[operation]()
    assert failure.value.status_code == 503
    assert "fixture-provider-secret" not in str(failure.value)
    assert backend.revision == 1


@pytest.mark.parametrize("accepted", [True, False])
def test_uncertain_write_is_not_retried_and_accepted_write_revokes_pin(case, monkeypatch, accepted):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    saved = settings.update(guest, model_payload(), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    write = backend.write
    attempts = []

    def lost_response(key, content, expected_version=None):
        attempts.append(key)
        if accepted:
            write(key, content, expected_version)
        raise OSError("fixture-provider-secret")

    monkeypatch.setattr(backend, "write", lost_response)
    with pytest.raises(SettingsError) as failure:
        settings.update(guest, {"api_key": None}, expected_version=saved["version"])
    assert failure.value.status_code == 503
    assert "fixture-provider-secret" not in repr(failure.value)
    assert len(attempts) == 1
    if accepted:
        with pytest.raises(SettingsError):
            settings.resolve(guest, snapshot)
    else:
        assert settings.resolve(guest, snapshot).api_key == "fixture-model-key"


def test_removing_installation_approval_revokes_use_but_allows_profile_removal(case):
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    settings, guest, _ = case
    saved = settings.update(guest, provider_payload("openai_compatible"), expected_version=None)
    snapshot = settings.pin(guest, str(uuid4()))
    locked = UserSettings(settings._store)
    with pytest.raises(SettingsError):
        locked.resolve(guest, snapshot)
    with pytest.raises(SettingsError):
        locked.pin(guest, str(uuid4()))
    assert locked.get(guest)["version"] == saved["version"]
    removed = locked.update(guest, {"providers": {"openai_compatible": None}}, expected_version=saved["version"])
    assert removed["providers"] == {}


def test_settings_read_only_the_account_record_and_never_auth_credentials(case, monkeypatch):
    settings, guest, backend = case
    read = backend.read
    accesses = []

    def checked_read(key):
        accesses.append(key)
        assert key == f"{guest.owner_key}/settings"
        return read(key)

    monkeypatch.setattr(backend, "read", checked_read)
    settings.update(guest, model_payload(), expected_version=None)
    settings.get(guest)
    settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert len(accesses) == 4


def test_all_providers_fit_one_bounded_record_and_remain_selectable(case):
    settings, guest, backend = case
    profiles = {}
    for provider in KEY_PROVIDERS:
        payload = provider_payload(provider)
        profiles[provider] = {name: value for name, value in payload.items()
                              if name not in ("provider", "mode", "data_keys")}
        profiles[provider]["api_key"] = "x" * 4096
    saved = settings.update(guest, {"providers": profiles}, expected_version=None)
    for provider in KEY_PROVIDERS:
        saved = settings.update(guest, {"provider": provider}, expected_version=saved["version"])
        resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
        assert resolved.api_key == "x" * 4096
        assert resolved.provider == provider
    assert len(saved["providers"]) == len(KEY_PROVIDERS)
    assert len(backend.records) == 1
    assert len(next(iter(backend.records.values()))[0]) < 128 * 1024


def test_unconfigured_model_keys_are_saveable_but_not_pinnable(case):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, _ = case
    payload = model_payload()
    del payload["api_key"]
    saved = settings.update(guest, payload, expected_version=None)
    assert saved["model_key_configured"] is False
    with pytest.raises(SettingsError):
        settings.pin(guest, str(uuid4()))


def test_catalog_and_settings_outputs_are_detached_and_no_dns_is_attempted(case, monkeypatch):
    import socket

    settings, guest, _ = case

    def no_dns(*args, **kwargs):
        pytest.fail("Settings must not perform network or DNS calls")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    catalog = settings.catalog()
    catalog["providers"].clear()
    assert settings.catalog()["providers"]
    saved = settings.update(guest, provider_payload("openai_compatible"), expected_version=None)
    saved["providers"]["openai_compatible"]["base_url"] = "https://evil.example.com/v1"
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.provider_config["base_url"] == "https://relay.example.com/v1"


@pytest.mark.parametrize("acknowledgement", [None, "", 1])
def test_invalid_write_acknowledgement_is_not_reported_as_success(case, monkeypatch, acknowledgement):
    from tradingagents.workbench.public_settings import SettingsError

    settings, guest, backend = case
    write = backend.write

    def bad_acknowledgement(key, content, expected_version=None):
        write(key, content, expected_version)
        return acknowledgement

    monkeypatch.setattr(backend, "write", bad_acknowledgement)
    with pytest.raises(SettingsError) as failure:
        settings.update(guest, model_payload(), expected_version=None)
    assert failure.value.status_code == 503
    assert backend.revision == 1
    assert settings.get(guest)["model_key_configured"] is True


def test_absent_optional_data_keys_stay_absent_despite_host_environment(case, monkeypatch):
    settings, guest, _ = case
    monkeypatch.setenv("FRED_API_KEY", "fixture-forbidden-owner-key")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fixture-forbidden-owner-key")
    saved = settings.update(guest, model_payload(data_keys={}), expected_version=None)
    resolved = settings.resolve(guest, settings.pin(guest, str(uuid4())))
    assert resolved.data_keys == {}
    assert saved["data_key_configured"] == {"alpha_vantage": False, "fred": False}
    assert resolved.data_vendors["macro_data"] == "fred"
