"""Managed-identity bootstrap for the separately hosted public workbench."""

import base64
import json
import logging
import os
import re
from contextlib import ExitStack, asynccontextmanager, suppress
from pathlib import Path

import anyio
from azure.communication.email import EmailClient
from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import ContainerClient

from tradingagents.workbench.email_server import create_email_app
from tradingagents.workbench.public_accounts import EmailAuth
from tradingagents.workbench.public_auth import (
    AuthManager,
    MsalClientFactory,
    canonical_uuid,
    public_origin,
)
from tradingagents.workbench.public_budget import BudgetLedger
from tradingagents.workbench.public_email import VerificationEmailSender, email_configuration
from tradingagents.workbench.public_execution import HostedExecution
from tradingagents.workbench.public_gateway import ProviderGateway
from tradingagents.workbench.public_hosted_runs import HostedRuns
from tradingagents.workbench.public_registration import EmailRegistration
from tradingagents.workbench.public_server import create_public_app
from tradingagents.workbench.public_settings import UserSettings
from tradingagents.workbench.public_source import MAX_SOURCE_BYTES, WorkerSource
from tradingagents.workbench.public_store import BlobBackend, EncryptedStore

_ENTRA_MODE = "entra"
_EMAIL_MODE = "email"
_RUNTIME_MODE_ENV = "TAWB_PUBLIC_RUNTIME_MODE"


async def _advance_runs(runs):
    while True:
        try:
            await anyio.to_thread.run_sync(runs.advance_pending)
        except Exception:
            logging.getLogger(__name__).warning("Background research synchronization deferred.")
        await anyio.sleep(5)


def _runtime_mode(environment):
    mode = environment.get(_RUNTIME_MODE_ENV, _ENTRA_MODE)
    if mode not in {_ENTRA_MODE, _EMAIL_MODE}:
        raise RuntimeError("Public workbench configuration is invalid.")
    return mode


def _vault_url(value):
    if not re.fullmatch(r"https://[a-z][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net", value):
        raise ValueError
    return value


def _storage_url(value):
    if not re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", value):
        raise ValueError
    return value


def _split_origins(value):
    if value is None or value == "":
        return ()
    if type(value) is not str:
        raise ValueError
    origins = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(origins) > 128:
        raise ValueError
    return origins


def _public_compatible_origins(value, primary_origin):
    if value is None or value == "":
        return ()
    if type(value) is not str or len(value) > 2048:
        raise ValueError
    entries = value.split(",")
    if len(entries) > 8 or any(not entry.strip() for entry in entries):
        raise ValueError
    origins = tuple(public_origin(entry.strip()) for entry in entries)
    if (any(origin != entry.strip() for origin, entry in zip(origins, entries, strict=True))
            or len(set(origins)) != len(origins)
            or primary_origin in origins):
        raise ValueError
    return origins


def _email_execution_installation(environment, origin):
    return {
        "subscription_id": canonical_uuid(environment["TAWB_HOSTED_SUBSCRIPTION_ID"]),
        "resource_group": environment["TAWB_HOSTED_RESOURCE_GROUP"],
        "job_name": environment["TAWB_HOSTED_JOB_NAME"],
        "job_resource_id": environment["TAWB_HOSTED_JOB_RESOURCE_ID"],
        "environment_id": environment["TAWB_HOSTED_ENVIRONMENT_ID"],
        "worker_identity_id": environment["TAWB_HOSTED_WORKER_IDENTITY_ID"],
        "image": environment["TAWB_HOSTED_IMAGE"],
        "backend_origin": origin,
        "workspace_id": canonical_uuid(environment["TAWB_HOSTED_WORKSPACE_ID"]),
        "location": environment["TAWB_HOSTED_LOCATION"],
    }


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _parse_foundry_models_json(raw):
    if type(raw) is not str:
        raise ValueError
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (TypeError, ValueError, RecursionError):
        raise ValueError from None
    if type(parsed) is not list or not 1 <= len(parsed) <= 16:
        raise ValueError
    return parsed


def _foundry_installation(environment):
    fields = {
        "endpoint": "TAWB_FOUNDRY_ENDPOINT",
        "deployment": "TAWB_FOUNDRY_DEPLOYMENT",
        "api_version": "TAWB_FOUNDRY_API_VERSION",
        "model_family": "TAWB_FOUNDRY_MODEL_FAMILY",
        "model_version": "TAWB_FOUNDRY_MODEL_VERSION",
        "context_window_tokens": "TAWB_FOUNDRY_CONTEXT_WINDOW_TOKENS",
        "max_output_tokens": "TAWB_FOUNDRY_MAX_OUTPUT_TOKENS",
        "pricing_version": "TAWB_FOUNDRY_PRICING_VERSION",
        "input_rate": "TAWB_FOUNDRY_INPUT_MICROUSD_PER_MILLION_TOKENS",
        "cached_input_rate": "TAWB_FOUNDRY_CACHED_INPUT_MICROUSD_PER_MILLION_TOKENS",
        "output_rate": "TAWB_FOUNDRY_OUTPUT_MICROUSD_PER_MILLION_TOKENS",
        "verification_status": "TAWB_FOUNDRY_VERIFICATION_STATUS",
        "verification_evidence": "TAWB_FOUNDRY_VERIFICATION_EVIDENCE",
        "verification_recorded_at": "TAWB_FOUNDRY_VERIFICATION_RECORDED_AT",
    }
    present = {name: environment.get(key) for name, key in fields.items() if key in environment}
    if not present:
        if "TAWB_FOUNDRY_MODELS_JSON" in environment:
            raise ValueError
        return {"foundry": None}
    if len(present) != len(fields):
        raise ValueError
    default = {
        "endpoint": environment[fields["endpoint"]],
        "deployment": environment[fields["deployment"]],
        "api_version": environment[fields["api_version"]],
        "model_family": environment[fields["model_family"]],
        "model_version": environment[fields["model_version"]],
        "context_window_tokens": int(environment[fields["context_window_tokens"]]),
        "max_output_tokens": int(environment[fields["max_output_tokens"]]),
        "pricing": {
            "version": environment[fields["pricing_version"]],
            "input_microusd_per_million_tokens": int(environment[fields["input_rate"]]),
            "cached_input_microusd_per_million_tokens": int(environment[fields["cached_input_rate"]]),
            "output_microusd_per_million_tokens": int(environment[fields["output_rate"]]),
        },
        "verification": {
            "status": environment[fields["verification_status"]],
            "evidence": environment[fields["verification_evidence"]],
            "recorded_at": environment[fields["verification_recorded_at"]],
        },
    }
    models_json = environment.get("TAWB_FOUNDRY_MODELS_JSON")
    if models_json is None:
        return {"foundry": default}
    models = _parse_foundry_models_json(models_json)
    seen = {default["deployment"]}
    normalized = []
    for model in models:
        if type(model) is not dict:
            raise ValueError
        required = {
            "endpoint",
            "deployment",
            "api_version",
            "model_family",
            "model_version",
            "context_window_tokens",
            "max_output_tokens",
            "pricing",
            "verification",
        }
        if set(model) != required or model.get("endpoint") != default["endpoint"]:
            raise ValueError
        deployment = model.get("deployment")
        if type(deployment) is not str or deployment in seen:
            raise ValueError
        if type(model.get("pricing")) is not dict or type(model.get("verification")) is not dict:
            raise ValueError
        seen.add(deployment)
        normalized.append(model)
    return {"foundry": default, "foundry_models": normalized}


def _entra_runtime_app(environment):
    tenant = canonical_uuid(environment["TAWB_TENANT_ID"])
    client = canonical_uuid(environment["TAWB_CLIENT_ID"])
    identity = canonical_uuid(environment["TAWB_MANAGED_IDENTITY_CLIENT_ID"])
    origin = public_origin(environment["TAWB_PUBLIC_ORIGIN"])
    vault_url = _vault_url(environment["TAWB_VAULT_URL"])
    storage_url = _storage_url(environment["TAWB_STORAGE_ACCOUNT_URL"])
    source_path = Path(environment.get("TAWB_SOURCE_ARCHIVE", "/app/worker-source.tar.gz"))
    with source_path.open("rb") as source_file:
        source = WorkerSource(source_file.read(MAX_SOURCE_BYTES + 1))

    clients = ExitStack()
    try:
        credential = ManagedIdentityCredential(client_id=identity)
        clients.callback(credential.close)
        vault = SecretClient(vault_url=vault_url, credential=credential)
        clients.callback(vault.close)
        encryption_key = base64.b64decode(vault.get_secret("state-encryption-key").value, validate=True)
        container = ContainerClient(account_url=storage_url, container_name="state", credential=credential)
        clients.callback(container.close)
        store = EncryptedStore(BlobBackend(container), encryption_key)
        auth = AuthManager(tenant_id=tenant, origin=origin, store=store,
                           client_factory=MsalClientFactory(tenant_id=tenant, client_id=client,
                               assertion_credential=credential))
        app = create_public_app(auth=auth, source=source)
        app.state.runtime_mode = _ENTRA_MODE
    except Exception:
        with suppress(Exception):
            clients.close()
        raise RuntimeError("Public workbench secure startup failed.") from None

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            clients.close()

    app.router.lifespan_context = lifespan
    return app


def _email_runtime_app(environment):
    identity = canonical_uuid(environment["TAWB_MANAGED_IDENTITY_CLIENT_ID"])
    origin = public_origin(environment["TAWB_PUBLIC_ORIGIN"])
    compatible_origins = _public_compatible_origins(
        environment.get("TAWB_PUBLIC_COMPATIBLE_ORIGINS"), origin,
    )
    vault_url = _vault_url(environment["TAWB_VAULT_URL"])
    storage_url = _storage_url(environment["TAWB_STORAGE_ACCOUNT_URL"])
    execution_installation = _email_execution_installation(environment, origin)
    previous_images_raw = environment.get("TAWB_HOSTED_PREVIOUS_IMAGES_JSON", "[]")
    if type(previous_images_raw) is not str or len(previous_images_raw) > 4096:
        raise ValueError
    previous_images = json.loads(previous_images_raw)
    if (type(previous_images) is not list or len(previous_images) > 8
            or any(type(image) is not str for image in previous_images)):
        raise ValueError
    gateway_installation = _foundry_installation(environment)
    approved_origins = _split_origins(environment.get("TAWB_APPROVED_COMPATIBLE_ORIGINS"))
    mail_configuration = email_configuration(environment)

    clients = ExitStack()
    try:
        credential = ManagedIdentityCredential(client_id=identity)
        clients.callback(credential.close)
        vault = SecretClient(vault_url=vault_url, credential=credential)
        clients.callback(vault.close)
        encryption_key = base64.b64decode(vault.get_secret("state-encryption-key").value, validate=True)
        container = ContainerClient(account_url=storage_url, container_name="state", credential=credential)
        clients.callback(container.close)
        store = EncryptedStore(BlobBackend(container), encryption_key)
        auth = EmailAuth(store, origin=origin)
        sender = None
        if mail_configuration is not None:
            endpoint, sender_address = mail_configuration
            email_client = EmailClient(endpoint, credential, retry_total=0, connection_timeout=5, read_timeout=15)
            clients.callback(email_client.close)
            sender = VerificationEmailSender(email_client, sender_address)
        registration = EmailRegistration(auth, sender=sender)
        settings = UserSettings(store, approved_origins=approved_origins)
        budget = BudgetLedger(store)
        gateway = ProviderGateway(
            store,
            settings=settings,
            budget=budget,
            managed_credential=credential,
            installation=gateway_installation,
        )
        clients.callback(gateway._http.close)
        execution = HostedExecution(
            store,
            gateway=gateway,
            installation=execution_installation,
            credential=credential,
            previous_images=previous_images,
        )
        clients.callback(execution.close)
        runs = HostedRuns(auth=auth, settings=settings, execution=execution)
        app = create_email_app(
            auth=auth, settings=settings, budget=budget, runs=runs, gateway=gateway,
            registration=registration, compatible_origins=compatible_origins,
        )
        app.state.runtime_mode = _EMAIL_MODE
        app.state.email_registration = registration
        app.state.email_auth = auth
        app.state.user_settings = settings
        app.state.budget_ledger = budget
        app.state.provider_gateway = gateway
        app.state.hosted_execution = execution
        app.state.hosted_runs = runs
    except Exception:
        with suppress(Exception):
            clients.close()
        raise RuntimeError("Public workbench secure startup failed.") from None

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(_advance_runs, runs)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            clients.close()

    app.router.lifespan_context = lifespan
    return app


def create_runtime_app(environment=None):
    environment = os.environ if environment is None else environment
    try:
        mode = _runtime_mode(environment)
        return _entra_runtime_app(environment) if mode == _ENTRA_MODE else _email_runtime_app(environment)
    except (KeyError, TypeError, ValueError, OSError):
        raise RuntimeError("Public workbench configuration is invalid.") from None

