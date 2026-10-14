import os
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from langchain_openai import AzureChatOpenAI

from .base_client import BaseLLMClient, normalize_content
from .openai_client import NormalizedChatOpenAI

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "reasoning_effort", "temperature", "max_tokens",
    "callbacks", "http_client", "http_async_client",
)

_AZURE_COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com"
_TRUSTED_AZURE_AI_HOST_SUFFIXES = (
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
    ".openai.azure.com",
)


class AzureCliTokenProvider:
    """Return a short-lived Cognitive Services token from the Azure CLI cache."""

    def __init__(self, cache_seconds: float = 300):
        self._cache_seconds = cache_seconds
        self._token = None
        self._refresh_at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._refresh_at:
                return self._token

            azure_cli = shutil.which("az")
            if not azure_cli:
                raise RuntimeError(
                    "Azure CLI is required for Entra ID authentication. Install it and run 'az login'."
                )

            try:
                result = subprocess.run(
                    [
                        azure_cli,
                        "account",
                        "get-access-token",
                        "--resource",
                        _AZURE_COGNITIVE_SERVICES_RESOURCE,
                        "--query",
                        "accessToken",
                        "--output",
                        "tsv",
                        "--only-show-errors",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(
                    "Azure CLI token acquisition failed. Confirm that 'az login' is valid."
                ) from exc

            token = result.stdout.strip()
            if result.returncode != 0 or not token:
                raise RuntimeError(
                    "Azure CLI token acquisition failed. Confirm Cognitive Services access and run 'az login'."
                )

            self._token = token
            self._refresh_at = now + self._cache_seconds
            return token


def _foundry_responses_base_url(endpoint: str) -> str:
    parsed = urlparse(endpoint.strip())
    hostname = parsed.hostname or ""
    trusted_host = any(
        hostname.endswith(suffix) for suffix in _TRUSTED_AZURE_AI_HOST_SUFFIXES
    )
    if (
        parsed.scheme != "https"
        or not trusted_host
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Entra ID Responses endpoint must use HTTPS on a trusted Azure AI host."
        )

    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        path = path.removesuffix("/responses")
    if not path:
        path = "/openai/v1"
    if path != "/openai/v1":
        raise ValueError(
            "Entra ID Responses endpoint path must be /openai/v1 or /openai/v1/responses."
        )

    return urlunparse((parsed.scheme, parsed.netloc, path + "/", "", "", ""))


class NormalizedAzureChatOpenAI(AzureChatOpenAI):
    """AzureChatOpenAI with normalized content output."""

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))


class AzureOpenAIClient(BaseLLMClient):
    """Client for Azure OpenAI deployments.

    API-key mode requires:
        AZURE_OPENAI_API_KEY: API key.

    Entra ID Responses mode requires:
        AZURE_OPENAI_AUTH_MODE: entra_id
        Azure CLI with an active ``az login`` session.

    Managed identity Responses mode requires:
        AZURE_OPENAI_AUTH_MODE: managed_identity
        azure-identity; optional AZURE_CLIENT_ID selects a user-assigned identity.

    All modes require:
        AZURE_OPENAI_ENDPOINT: Endpoint URL (e.g. https://<resource>.openai.azure.com/)
        AZURE_OPENAI_DEPLOYMENT_NAME: Deployment name
        OPENAI_API_VERSION: API version for legacy API-key mode (e.g. 2025-03-01-preview)
    """

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return the configured Azure client for the selected auth mode."""
        self.warn_if_unknown_model()

        auth_mode = str(self.kwargs.get("auth_mode") or os.environ.get("AZURE_OPENAI_AUTH_MODE", "api_key")).strip().lower()
        if auth_mode in {"entra", "entra_id", "aad", "managed_identity"}:
            endpoint = self.kwargs.get("azure_endpoint") or self.base_url or os.environ.get("AZURE_OPENAI_ENDPOINT")
            if not endpoint:
                raise ValueError(
                    "AZURE_OPENAI_ENDPOINT is required for Entra ID Responses authentication."
                )
            base_url = _foundry_responses_base_url(endpoint)
            if auth_mode == "managed_identity":
                from azure.identity import ManagedIdentityCredential, get_bearer_token_provider

                token_provider = get_bearer_token_provider(
                    ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID")),
                    _AZURE_COGNITIVE_SERVICES_RESOURCE + "/.default",
                )
            else:
                token_provider = AzureCliTokenProvider()
            llm_kwargs = {
                "model": self.kwargs.get("azure_deployment") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", self.model),
                "base_url": base_url,
                "api_key": token_provider,
                "use_responses_api": True,
            }
            for key in _PASSTHROUGH_KWARGS:
                if key in self.kwargs and key != "api_key":
                    llm_kwargs[key] = self.kwargs[key]
            return NormalizedChatOpenAI(**llm_kwargs)

        if auth_mode not in {"api_key", "key"}:
            raise ValueError(
                "AZURE_OPENAI_AUTH_MODE must be 'api_key', 'entra_id' or 'managed_identity'."
            )

        llm_kwargs = {
            "model": self.model,
            "azure_deployment": self.kwargs.get("azure_deployment") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", self.model),
        }

        endpoint = self.kwargs.get("azure_endpoint") or self.base_url or os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_version = self.kwargs.get("api_version") or os.environ.get("OPENAI_API_VERSION")
        if endpoint:
            llm_kwargs["azure_endpoint"] = endpoint.rstrip("/")
        if api_version:
            llm_kwargs["api_version"] = api_version

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return NormalizedAzureChatOpenAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Azure accepts any deployed model name."""
        return True
