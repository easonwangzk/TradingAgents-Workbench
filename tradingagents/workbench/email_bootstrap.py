"""Interactive owner bootstrap inside the hosted email-mode public workbench."""

import base64
import getpass
import json
import os
import sys
import warnings
from contextlib import ExitStack, suppress
from dataclasses import dataclass

from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import ContainerClient

from tradingagents.workbench.public_accounts import AccountError, EmailAuth
from tradingagents.workbench.public_auth import canonical_uuid, public_origin
from tradingagents.workbench.public_store import BlobBackend, EncryptedStore

_EMAIL_MODE = "email"
_RUNTIME_MODE_ENV = "TAWB_PUBLIC_RUNTIME_MODE"
_FORBIDDEN_ARGS = frozenset({"--password", "--token", "--secret", "--api-key"})


@dataclass(frozen=True)
class BootstrapRuntime:
    auth: EmailAuth
    close: callable


def _vault_url(value):
    from tradingagents.workbench.public_runtime import _vault_url as validate_vault_url

    return validate_vault_url(value)


def _storage_url(value):
    from tradingagents.workbench.public_runtime import _storage_url as validate_storage_url

    return validate_storage_url(value)


def _reject_arguments(argv):
    if not isinstance(argv, list):
        raise SystemExit("This command accepts no arguments.")
    for raw in argv:
        if not isinstance(raw, str):
            raise SystemExit("This command accepts no arguments.")
        option = raw.split("=", 1)[0].lower()
        if option in _FORBIDDEN_ARGS:
            raise SystemExit("Command line secrets are not allowed.")
    if argv:
        raise SystemExit("This command accepts no arguments.")


def _require_interactive(stdin):
    if stdin is None or not callable(getattr(stdin, "isatty", None)) or not stdin.isatty():
        raise SystemExit("This command requires an interactive terminal.")


def _prompt_password(getpass_fn):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass_fn("Owner password: ")
            confirmation = getpass_fn("Confirm owner password: ")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt, OSError):
        raise SystemExit("Owner bootstrap password input failed securely.") from None
    except Exception:
        raise SystemExit("Owner bootstrap password input failed securely.") from None
    if password != confirmation:
        raise SystemExit("Password confirmation did not match.")
    return password


def _create_runtime(environment):
    if not isinstance(environment, dict):
        raise RuntimeError
    if environment.get(_RUNTIME_MODE_ENV) != _EMAIL_MODE:
        raise RuntimeError
    clients = ExitStack()
    try:
        credential = ManagedIdentityCredential(client_id=canonical_uuid(environment["TAWB_MANAGED_IDENTITY_CLIENT_ID"]))
        clients.callback(credential.close)
        vault = SecretClient(
            vault_url=_vault_url(environment["TAWB_VAULT_URL"]),
            credential=credential,
        )
        clients.callback(vault.close)
        encryption_key = base64.b64decode(vault.get_secret("state-encryption-key").value, validate=True)
        if len(encryption_key) != 32:
            raise ValueError
        container = ContainerClient(
            account_url=_storage_url(environment["TAWB_STORAGE_ACCOUNT_URL"]),
            container_name="state",
            credential=credential,
        )
        clients.callback(container.close)
        store = EncryptedStore(BlobBackend(container), encryption_key)
        auth = EmailAuth(store, origin=public_origin(environment["TAWB_PUBLIC_ORIGIN"]))
        return BootstrapRuntime(auth=auth, close=clients.close)
    except Exception:
        with suppress(Exception):
            clients.close()
        raise RuntimeError from None


def _run_bootstrap(*, runtime, password, stdout):
    try:
        session = runtime.auth.bootstrap(password)
        runtime.auth.logout(session.session_id)
    except AccountError as exc:
        if exc.status_code == 409:
            raise SystemExit("Owner bootstrap is unavailable because an owner is already initialized.") from None
        raise SystemExit("Owner bootstrap failed closed.") from None
    except Exception:
        raise SystemExit("Owner bootstrap failed closed.") from None
    result = {
        "owner_id": session.principal.object_id,
        "owner_email": session.principal.email,
    }
    stdout.write(json.dumps(result, ensure_ascii=True) + "\n")


def main(argv=None, *, stdin=None, stdout=None, stderr=None, environment=None, runtime_factory=None, getpass_fn=None):
    del stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environment = dict(os.environ) if environment is None else environment
    runtime_factory = _create_runtime if runtime_factory is None else runtime_factory
    getpass_fn = getpass.getpass if getpass_fn is None else getpass_fn

    _reject_arguments(argv)
    _require_interactive(stdin)
    password = _prompt_password(getpass_fn)
    try:
        runtime = runtime_factory(environment)
    except Exception:
        raise SystemExit("Owner bootstrap requires the deployed email-mode public runtime.") from None
    outcome = None
    try:
        _run_bootstrap(runtime=runtime, password=password, stdout=stdout)
    except SystemExit as exc:
        outcome = exc
    finally:
        with suppress(Exception):
            runtime.close()
    if outcome is not None:
        raise outcome


if __name__ == "__main__":
    main()
