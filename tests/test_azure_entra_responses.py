# Modified for public distribution: installation identifiers replaced with examples.
import sys
from subprocess import CompletedProcess
from types import ModuleType
from unittest.mock import patch

import pytest

from tradingagents.llm_clients.azure_client import AzureOpenAIClient
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


def _configure_entra(monkeypatch, endpoint):
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra_id")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5.6-sol")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)


def test_entra_mode_uses_responses_api_and_caches_cli_token(monkeypatch):
    _configure_entra(
        monkeypatch,
        "https://example-foundry.services.ai.azure.com/openai/v1/responses",
    )
    completed = CompletedProcess([], 0, stdout="test-bearer-token\n", stderr="")

    with (
        patch("shutil.which", return_value="az") as which,
        patch("subprocess.run", return_value=completed) as run,
    ):
        llm = AzureOpenAIClient("gpt-5.6-sol", max_tokens=256).get_llm()
        token_provider = llm.openai_api_key
        first_token = token_provider()
        second_token = token_provider()

    assert isinstance(llm, NormalizedChatOpenAI)
    assert llm.use_responses_api is True
    assert str(llm.openai_api_base) == (
        "https://example-foundry.services.ai.azure.com/openai/v1/"
    )
    assert llm.max_tokens == 256
    assert first_token == second_token == "test-bearer-token"
    which.assert_called_once_with("az")
    assert run.call_count == 1
    assert "https://cognitiveservices.azure.com" in run.call_args.args[0]


def test_entra_mode_rejects_non_azure_endpoint(monkeypatch):
    _configure_entra(monkeypatch, "https://example.com/openai/v1/responses")

    with pytest.raises(ValueError, match="trusted Azure AI host"):
        AzureOpenAIClient("gpt-5.6-sol").get_llm()


@pytest.mark.parametrize("client_id", [None, "11111111-1111-1111-1111-111111111111"])
def test_managed_identity_uses_lazy_token_provider_without_cli(monkeypatch, client_id):
    _configure_entra(monkeypatch, "https://test.services.ai.azure.com/openai/v1/responses")
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "managed_identity")
    if client_id:
        monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    else:
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    identity = ModuleType("azure.identity")
    credentials = []

    class Credential:
        def __init__(self, *, client_id):
            self.client_id = client_id
            credentials.append(self)

    def provider(credential, scope):
        assert credential.client_id == client_id
        assert scope == "https://cognitiveservices.azure.com/.default"
        return lambda: "managed-test-token"

    identity.ManagedIdentityCredential = Credential
    identity.get_bearer_token_provider = provider
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    with patch("subprocess.run", side_effect=AssertionError("CLI must not run")):
        llm = AzureOpenAIClient("gpt-5.6-sol", api_key="ignored-key", max_tokens=16).get_llm()
        assert callable(llm.openai_api_key)
        assert llm.openai_api_key() == "managed-test-token"
    assert len(credentials) == 1
    assert llm.use_responses_api is True
    assert llm.model_name == "gpt-5.6-sol"
    assert llm.max_tokens == 16
    assert str(llm.openai_api_base) == "https://test.services.ai.azure.com/openai/v1/"


@pytest.mark.parametrize("endpoint", [
    "https://example.com/openai/v1", "http://test.openai.azure.com/",
    "https://test.openai.azure.com.evil.test/", "https://user:pass@test.openai.azure.com/",
    "https://test.openai.azure.com/openai/v1?key=secret",
    "https://test.openai.azure.com/arbitrary", "https://test.openai.azure.com:444/",
])
def test_managed_identity_validates_url_before_constructing_credentials(monkeypatch, endpoint):
    _configure_entra(monkeypatch, endpoint)
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "managed_identity")
    identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = lambda **kwargs: pytest.fail("Untrusted credential target")
    identity.get_bearer_token_provider = lambda *args: pytest.fail("Untrusted token target")
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    with pytest.raises(ValueError, match="endpoint"):
        AzureOpenAIClient("gpt-5.6-sol").get_llm()


def test_api_key_mode_remains_legacy_azure_client(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "api_key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "legacy-test-key")
    monkeypatch.setenv("OPENAI_API_VERSION", "2025-03-01-preview")
    from tradingagents.llm_clients.azure_client import NormalizedAzureChatOpenAI

    llm = AzureOpenAIClient("gpt-5.6-sol").get_llm()
    assert isinstance(llm, NormalizedAzureChatOpenAI)
    assert llm.openai_api_key.get_secret_value() == "legacy-test-key"
