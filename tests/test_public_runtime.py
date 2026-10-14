# Modified for public distribution: installation identifiers replaced with examples.
import base64
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.test_public_auth import IdentityProvider
from tests.test_public_server import ORIGIN, login
from tests.test_public_store import MemoryBackend

SETTINGS = {
    "TAWB_TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "TAWB_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "TAWB_MANAGED_IDENTITY_CLIENT_ID": "33333333-3333-4333-8333-333333333333",
    "TAWB_PUBLIC_ORIGIN": ORIGIN,
    "TAWB_VAULT_URL": "https://tawb-test.vault.azure.net",
    "TAWB_STORAGE_ACCOUNT_URL": "https://tawbtest.blob.core.windows.net",
}

EMAIL_SETTINGS = {
    "TAWB_PUBLIC_RUNTIME_MODE": "email",
    "TAWB_MANAGED_IDENTITY_CLIENT_ID": SETTINGS["TAWB_MANAGED_IDENTITY_CLIENT_ID"],
    "TAWB_PUBLIC_ORIGIN": ORIGIN,
    "TAWB_VAULT_URL": SETTINGS["TAWB_VAULT_URL"],
    "TAWB_STORAGE_ACCOUNT_URL": SETTINGS["TAWB_STORAGE_ACCOUNT_URL"],
    "TAWB_HOSTED_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
    "TAWB_HOSTED_RESOURCE_GROUP": "hosted-fixture",
    "TAWB_HOSTED_JOB_NAME": "job-hosted-research",
    "TAWB_HOSTED_JOB_RESOURCE_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.App/jobs/job-hosted-research",
    "TAWB_HOSTED_ENVIRONMENT_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.App/managedEnvironments/hosted-env",
    "TAWB_HOSTED_WORKER_IDENTITY_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.ManagedIdentity/userAssignedIdentities/worker-pull",
    "TAWB_HOSTED_IMAGE": "fixture.azurecr.io/research@sha256:" + "a" * 64,
    "TAWB_HOSTED_WORKSPACE_ID": "22222222-2222-4222-8222-222222222222",
    "TAWB_HOSTED_LOCATION": "eastus2",
}


def test_runtime_uses_only_explicit_identity_and_preserves_encrypted_sessions(monkeypatch, tmp_path):
    import tradingagents.workbench.public_runtime as runtime

    source = tmp_path / "worker-source.tar.gz"
    source.write_bytes(b"reviewed-source-context")
    environment = {**SETTINGS, "TAWB_SOURCE_ARCHIVE": str(source)}
    backend = MemoryBackend()
    calls = []
    credential = SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token="fixture-host-token"),
        close=lambda: calls.append("credential-close"),
    )
    secrets = {"state-encryption-key": base64.b64encode(b"a" * 32).decode()}
    vault = SimpleNamespace(get_secret=lambda name: SimpleNamespace(value=secrets[name]), close=lambda: calls.append("vault-close"))
    container = SimpleNamespace(close=lambda: calls.append("container-close"))

    def identity(**kwargs):
        assert kwargs == {"client_id": SETTINGS["TAWB_MANAGED_IDENTITY_CLIENT_ID"]}
        return credential

    def blob(**kwargs):
        assert kwargs["account_url"] == SETTINGS["TAWB_STORAGE_ACCOUNT_URL"]
        assert kwargs["container_name"] == "state"
        assert kwargs["credential"] is credential
        return container

    def key_vault(**kwargs):
        assert kwargs["vault_url"] == SETTINGS["TAWB_VAULT_URL"]
        assert kwargs["credential"] is credential
        return vault

    def msal_factory(**kwargs):
        assert kwargs["tenant_id"] == SETTINGS["TAWB_TENANT_ID"]
        assert kwargs["client_id"] == SETTINGS["TAWB_CLIENT_ID"]
        assert kwargs["assertion_credential"] is credential
        return lambda cache: IdentityProvider()

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(runtime, "SecretClient", key_vault)
    monkeypatch.setattr(runtime, "ContainerClient", blob)
    monkeypatch.setattr(runtime, "BlobBackend", lambda value: backend if value is container else None)
    monkeypatch.setattr(runtime, "MsalClientFactory", msal_factory)
    with TestClient(runtime.create_runtime_app(environment), base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        cookies = dict(client.cookies)
        assert client.get("/api/me").status_code == 200
    assert all(b"private-" not in content for content, _ in backend.records.values())
    with TestClient(runtime.create_runtime_app(environment), base_url=ORIGIN, follow_redirects=False) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get("/api/me").json()["user"]["name"] == "Alice"
    assert calls.count("credential-close") == 2
    assert calls.count("vault-close") == 2
    assert calls.count("container-close") == 2


@pytest.mark.parametrize("change", [
    {"TAWB_PUBLIC_ORIGIN": "http://workbench.example.com"},
    {"TAWB_PUBLIC_COMPATIBLE_ORIGINS": "http://research.example.com"},
    {"TAWB_PUBLIC_COMPATIBLE_ORIGINS": "https://research.example.com/path"},
    {"TAWB_PUBLIC_COMPATIBLE_ORIGINS": "https://research.example.com,https://research.example.com"},
    {"TAWB_PUBLIC_COMPATIBLE_ORIGINS": ORIGIN},
    {"TAWB_VAULT_URL": "https://attacker.example"},
    {"TAWB_STORAGE_ACCOUNT_URL": "https://tawbtest.blob.core.windows.net/?sig=private-secret"},
    {"TAWB_MANAGED_IDENTITY_CLIENT_ID": ""},
])
def test_runtime_rejects_invalid_configuration_before_requesting_credentials(monkeypatch, change):
    import tradingagents.workbench.public_runtime as runtime

    def unexpected_identity(**kwargs):
        pytest.fail("Invalid configuration must not access managed identity")

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", unexpected_identity)
    with pytest.raises(RuntimeError, match="Public workbench configuration is invalid") as error:
        runtime.create_runtime_app({**SETTINGS, **change})
    assert "private-secret" not in str(error.value)


def test_runtime_key_vault_failure_is_sanitized_and_closes_identity(monkeypatch, tmp_path):
    import tradingagents.workbench.public_runtime as runtime

    source = tmp_path / "source.gz"
    source.write_bytes(b"source")
    closed = []
    monkeypatch.setattr(runtime, "ManagedIdentityCredential", lambda **kwargs: SimpleNamespace(close=lambda: closed.append(True)))

    def denied(**kwargs):
        raise ValueError("private-certificate-debug")

    monkeypatch.setattr(runtime, "SecretClient", denied)
    with pytest.raises(RuntimeError, match="Public workbench secure startup failed") as error:
        runtime.create_runtime_app({**SETTINGS, "TAWB_SOURCE_ARCHIVE": str(source)})
    assert "private-certificate-debug" not in str(error.value)
    assert closed == [True]


@pytest.mark.parametrize("mail_enabled", [False, True])
@pytest.mark.parametrize("previous_image", [None, "fixture.azurecr.io/research@sha256:" + "b" * 64])
def test_runtime_email_mode_uses_email_stack_without_entra_or_source_archive(monkeypatch, mail_enabled, previous_image):
    import tradingagents.workbench.public_runtime as runtime

    backend = MemoryBackend()
    identity_calls = []
    closed = []
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="fixture-host-token"), close=lambda: closed.append("credential"))
    vault = SimpleNamespace(
        get_secret=lambda name: SimpleNamespace(value=base64.b64encode(b"k" * 32).decode()),
        close=lambda: None,
    )
    container = SimpleNamespace(close=lambda: None)

    def identity(**kwargs):
        identity_calls.append(kwargs)
        return credential

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(runtime, "SecretClient", lambda **kwargs: vault)
    monkeypatch.setattr(runtime, "ContainerClient", lambda **kwargs: container)
    monkeypatch.setattr(runtime, "BlobBackend", lambda value: backend if value is container else None)

    environment = dict(EMAIL_SETTINGS)
    environment["TAWB_PUBLIC_COMPATIBLE_ORIGINS"] = "https://research.example.com"
    if previous_image is not None:
        environment["TAWB_HOSTED_PREVIOUS_IMAGES_JSON"] = json.dumps([previous_image])
    email_calls = []

    def email_client(endpoint, identity, **kwargs):
        assert identity is credential
        email_calls.append((endpoint, kwargs))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(runtime, "EmailClient", email_client)
    if mail_enabled:
        environment.update(TAWB_EMAIL_ENDPOINT="https://fixture.communication.azure.com", TAWB_EMAIL_SENDER="donotreply@fixture.azurecomm.net")
    app = runtime.create_runtime_app(environment)
    assert app.state.email_registration.available is mail_enabled
    assert email_calls == ([("https://fixture.communication.azure.com", {"retry_total": 0, "connection_timeout": 5, "read_timeout": 15})] if mail_enabled else [])
    assert app.state.runtime_mode == "email"
    assert hasattr(app.state, "email_auth")
    assert hasattr(app.state, "user_settings")
    assert hasattr(app.state, "budget_ledger")
    assert hasattr(app.state, "provider_gateway")
    assert hasattr(app.state, "hosted_execution")
    assert hasattr(app.state, "hosted_runs")
    assert set(app.state.hosted_execution._images.values()) == {
        EMAIL_SETTINGS["TAWB_HOSTED_IMAGE"], *([previous_image] if previous_image else []),
    }
    assert app.state.provider_gateway._installation["foundry"] is None
    assert identity_calls == [{"client_id": EMAIL_SETTINGS["TAWB_MANAGED_IDENTITY_CLIENT_ID"]}]
    advanced = threading.Event()
    monkeypatch.setattr(app.state.hosted_runs, "advance_pending", lambda: advanced.set())
    with TestClient(app, base_url=ORIGIN):
        assert advanced.wait(timeout=5)
        custom_domain = TestClient(app, base_url=ORIGIN)
        custom_response = custom_domain.get("/auth/login", headers={"Host": "research.example.com"})
        custom_domain.close()
        assert custom_response.status_code == 200
        assert not closed
    assert closed == ["credential"]


def test_runtime_email_mode_rejects_incomplete_fixed_execution_config_before_identity(monkeypatch):
    import tradingagents.workbench.public_runtime as runtime

    def unexpected_identity(**kwargs):
        pytest.fail("Invalid email runtime config must not access managed identity")

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", unexpected_identity)
    with pytest.raises(RuntimeError, match="Public workbench configuration is invalid"):
        runtime.create_runtime_app({key: value for key, value in EMAIL_SETTINGS.items() if key != "TAWB_HOSTED_JOB_NAME"})


@pytest.mark.parametrize("raw", ["{", "null", '"image"', "[1]", "[]" + " " * 4096])
def test_runtime_rejects_invalid_previous_image_json_before_identity(monkeypatch, raw):
    import tradingagents.workbench.public_runtime as runtime

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", lambda **kwargs: pytest.fail("Invalid list accessed identity"))
    with pytest.raises(RuntimeError, match="Public workbench configuration is invalid"):
        runtime.create_runtime_app({**EMAIL_SETTINGS, "TAWB_HOSTED_PREVIOUS_IMAGES_JSON": raw})


def test_runtime_foundry_installation_accepts_default_and_extra_models():
    import tradingagents.workbench.public_runtime as runtime

    environment = {
        "TAWB_FOUNDRY_ENDPOINT": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
        "TAWB_FOUNDRY_DEPLOYMENT": "gpt-5.6-sol",
        "TAWB_FOUNDRY_API_VERSION": "2026-09-10-preview",
        "TAWB_FOUNDRY_MODEL_FAMILY": "gpt-5.6-sol",
        "TAWB_FOUNDRY_MODEL_VERSION": "2026-09-10",
        "TAWB_FOUNDRY_CONTEXT_WINDOW_TOKENS": "32000",
        "TAWB_FOUNDRY_MAX_OUTPUT_TOKENS": "8000",
        "TAWB_FOUNDRY_PRICING_VERSION": "fixture-v2",
        "TAWB_FOUNDRY_INPUT_MICROUSD_PER_MILLION_TOKENS": "100",
        "TAWB_FOUNDRY_CACHED_INPUT_MICROUSD_PER_MILLION_TOKENS": "50",
        "TAWB_FOUNDRY_OUTPUT_MICROUSD_PER_MILLION_TOKENS": "200",
        "TAWB_FOUNDRY_VERIFICATION_STATUS": "verified",
        "TAWB_FOUNDRY_VERIFICATION_EVIDENCE": "fixture-evidence",
        "TAWB_FOUNDRY_VERIFICATION_RECORDED_AT": "2026-09-10",
        "TAWB_FOUNDRY_MODELS_JSON": json.dumps([
            {
                "endpoint": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
                "deployment": "gpt-5.6-luna",
                "api_version": "2026-09-10-preview",
                "model_family": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "context_window_tokens": 32000,
                "max_output_tokens": 8000,
                "pricing": {
                    "version": "fixture-v2",
                    "input_microusd_per_million_tokens": 150,
                    "cached_input_microusd_per_million_tokens": 75,
                    "output_microusd_per_million_tokens": 250,
                },
                "verification": {
                    "status": "verified",
                    "evidence": "fixture-evidence-luna",
                    "recorded_at": "2026-09-10",
                },
            },
            {
                "endpoint": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
                "deployment": "gpt-5.6-terra",
                "api_version": "2026-09-10-preview",
                "model_family": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "context_window_tokens": 32000,
                "max_output_tokens": 8000,
                "pricing": {
                    "version": "fixture-v2",
                    "input_microusd_per_million_tokens": 160,
                    "cached_input_microusd_per_million_tokens": 80,
                    "output_microusd_per_million_tokens": 260,
                },
                "verification": {
                    "status": "verified",
                    "evidence": "fixture-evidence-terra",
                    "recorded_at": "2026-09-10",
                },
            },
        ]),
    }

    installation = runtime._foundry_installation(environment)
    assert installation["foundry"]["deployment"] == "gpt-5.6-sol"
    assert [model["deployment"] for model in installation["foundry_models"]] == ["gpt-5.6-luna", "gpt-5.6-terra"]


@pytest.mark.parametrize(
    "payload",
    [
        {"TAWB_FOUNDRY_MODELS_JSON": "not-json"},
        {"TAWB_FOUNDRY_MODELS_JSON": '{"deployment": "dup", "deployment": "dup2"}'},
        {"TAWB_FOUNDRY_MODELS_JSON": "[]"},
        {"TAWB_FOUNDRY_MODELS_JSON": json.dumps([
            {
                "endpoint": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
                "deployment": "gpt-5.6-sol",
                "api_version": "2026-09-10-preview",
                "model_family": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "context_window_tokens": 32000,
                "max_output_tokens": 8000,
                "pricing": {
                    "version": "fixture-v2",
                    "input_microusd_per_million_tokens": 100,
                    "cached_input_microusd_per_million_tokens": 50,
                    "output_microusd_per_million_tokens": 200,
                },
                "verification": {
                    "status": "verified",
                    "evidence": "fixture-evidence",
                    "recorded_at": "2026-09-10",
                },
            }
        ])},
    ],
)
def test_runtime_foundry_installation_rejects_malformed_or_orphaned_model_lists(payload):
    import tradingagents.workbench.public_runtime as runtime

    environment = {
        "TAWB_FOUNDRY_ENDPOINT": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
        "TAWB_FOUNDRY_DEPLOYMENT": "gpt-5.6-sol",
        "TAWB_FOUNDRY_API_VERSION": "2026-09-10-preview",
        "TAWB_FOUNDRY_MODEL_FAMILY": "gpt-5.6-sol",
        "TAWB_FOUNDRY_MODEL_VERSION": "2026-09-10",
        "TAWB_FOUNDRY_CONTEXT_WINDOW_TOKENS": "32000",
        "TAWB_FOUNDRY_MAX_OUTPUT_TOKENS": "8000",
        "TAWB_FOUNDRY_PRICING_VERSION": "fixture-v2",
        "TAWB_FOUNDRY_INPUT_MICROUSD_PER_MILLION_TOKENS": "100",
        "TAWB_FOUNDRY_CACHED_INPUT_MICROUSD_PER_MILLION_TOKENS": "50",
        "TAWB_FOUNDRY_OUTPUT_MICROUSD_PER_MILLION_TOKENS": "200",
        "TAWB_FOUNDRY_VERIFICATION_STATUS": "verified",
        "TAWB_FOUNDRY_VERIFICATION_EVIDENCE": "fixture-evidence",
        "TAWB_FOUNDRY_VERIFICATION_RECORDED_AT": "2026-09-10",
        **payload,
    }
    with pytest.raises(ValueError):
        runtime._foundry_installation(environment)


def test_runtime_foundry_installation_keeps_legacy_shape_without_optional_models():
    import tradingagents.workbench.public_runtime as runtime

    installation = runtime._foundry_installation({
        "TAWB_FOUNDRY_ENDPOINT": "https://fixture-foundry.services.ai.azure.com/openai/v1/",
        "TAWB_FOUNDRY_DEPLOYMENT": "gpt-5.6-sol",
        "TAWB_FOUNDRY_API_VERSION": "2026-09-10-preview",
        "TAWB_FOUNDRY_MODEL_FAMILY": "gpt-5.6-sol",
        "TAWB_FOUNDRY_MODEL_VERSION": "2026-09-10",
        "TAWB_FOUNDRY_CONTEXT_WINDOW_TOKENS": "32000",
        "TAWB_FOUNDRY_MAX_OUTPUT_TOKENS": "8000",
        "TAWB_FOUNDRY_PRICING_VERSION": "fixture-v2",
        "TAWB_FOUNDRY_INPUT_MICROUSD_PER_MILLION_TOKENS": "100",
        "TAWB_FOUNDRY_CACHED_INPUT_MICROUSD_PER_MILLION_TOKENS": "50",
        "TAWB_FOUNDRY_OUTPUT_MICROUSD_PER_MILLION_TOKENS": "200",
        "TAWB_FOUNDRY_VERIFICATION_STATUS": "verified",
        "TAWB_FOUNDRY_VERIFICATION_EVIDENCE": "fixture-evidence",
        "TAWB_FOUNDRY_VERIFICATION_RECORDED_AT": "2026-09-10",
    })
    assert installation == {"foundry": installation["foundry"]}
    assert "foundry_models" not in installation
