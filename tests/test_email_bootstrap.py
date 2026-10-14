import io
import json
import warnings

import pytest

from tests.test_public_accounts import ORIGIN
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_accounts import OWNER_EMAIL, EmailAuth
from tradingagents.workbench.public_store import EncryptedStore


class FakeStdin(io.StringIO):
    def __init__(self, *, interactive):
        super().__init__("")
        self._interactive = interactive

    def isatty(self):
        return self._interactive


def test_bootstrap_cli_rejects_non_interactive_stdin_before_runtime(monkeypatch):
    import tradingagents.workbench.email_bootstrap as bootstrap

    called = []

    def unexpected_runtime():
        called.append(True)
        raise AssertionError("runtime must not initialize")

    stderr = io.StringIO()
    with pytest.raises(SystemExit, match="interactive terminal"):
        bootstrap.main(
            argv=[],
            stdin=FakeStdin(interactive=False),
            stderr=stderr,
            runtime_factory=unexpected_runtime,
        )
    assert called == []
    assert stderr.getvalue() == ""


def test_bootstrap_cli_rejects_secret_arguments_without_echoing_values():
    import tradingagents.workbench.email_bootstrap as bootstrap

    secret = "super-secret-password-value"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.raises(SystemExit, match="Command line secrets") as error:
        bootstrap.main(
            argv=["--password", secret],
            stdin=FakeStdin(interactive=True),
            stdout=stdout,
            stderr=stderr,
        )
    rendered = str(error.value) + stdout.getvalue() + stderr.getvalue()
    assert secret not in rendered


def test_bootstrap_cli_rejects_password_confirmation_mismatch_before_runtime():
    import tradingagents.workbench.email_bootstrap as bootstrap

    called = []

    def unexpected_runtime(_environment):
        called.append(True)
        raise AssertionError("runtime must not initialize")

    responses = iter(["owner-password-2026", "different-password-2026"])
    with pytest.raises(SystemExit, match="did not match") as error:
        bootstrap.main(
            argv=[],
            stdin=FakeStdin(interactive=True),
            runtime_factory=unexpected_runtime,
            getpass_fn=lambda prompt: next(responses),
        )
    assert called == []
    assert "owner-password-2026" not in str(error.value)


def test_bootstrap_cli_bootstraps_owner_and_revokes_minted_session_with_real_auth():
    import tradingagents.workbench.email_bootstrap as bootstrap

    backend = MemoryBackend()
    auth = EmailAuth(EncryptedStore(backend, b"b" * 32), origin=ORIGIN)
    runtime = bootstrap.BootstrapRuntime(auth=auth, close=lambda: None)
    stdout = io.StringIO()

    bootstrap.main(
        argv=[],
        stdin=FakeStdin(interactive=True),
        stdout=stdout,
        runtime_factory=lambda _environment: runtime,
        getpass_fn=lambda prompt: "owner-password-2026",
        environment={"TAWB_PUBLIC_RUNTIME_MODE": "email"},
    )

    result = json.loads(stdout.getvalue())
    assert result["owner_email"] == OWNER_EMAIL
    assert isinstance(result["owner_id"], str) and result["owner_id"]
    assert set(backend.records) == {"email-v1/registry"}


def test_bootstrap_cli_refuses_existing_owner_without_password_echo():
    import tradingagents.workbench.email_bootstrap as bootstrap

    password = "owner-password-2026"
    backend = MemoryBackend()
    auth = EmailAuth(EncryptedStore(backend, b"c" * 32), origin=ORIGIN)
    auth.bootstrap(password)
    runtime = bootstrap.BootstrapRuntime(auth=auth, close=lambda: None)
    stdout = io.StringIO()
    stderr = io.StringIO()

    with pytest.raises(SystemExit, match="already initialized") as error:
        bootstrap.main(
            argv=[],
            stdin=FakeStdin(interactive=True),
            stdout=stdout,
            stderr=stderr,
            runtime_factory=lambda _environment: runtime,
            getpass_fn=lambda prompt: password,
            environment={"TAWB_PUBLIC_RUNTIME_MODE": "email"},
        )

    rendered = str(error.value) + stdout.getvalue() + stderr.getvalue()
    assert password not in rendered


def test_bootstrap_cli_rejects_getpass_warning_without_fallback_echo(monkeypatch):
    import tradingagents.workbench.email_bootstrap as bootstrap

    events = []

    def warning_getpass(prompt):
        warnings.warn("warning", bootstrap.getpass.GetPassWarning, stacklevel=2)
        events.append(prompt)
        return "owner-password-2026"

    monkeypatch.setattr(bootstrap.getpass, "getpass", warning_getpass)
    with pytest.raises(SystemExit, match="password input"):
        bootstrap.main(argv=[], stdin=FakeStdin(interactive=True), environment={"TAWB_PUBLIC_RUNTIME_MODE": "email"})
    assert events == []


def test_bootstrap_cli_sanitizes_runtime_factory_failure_and_secret_text():
    import tradingagents.workbench.email_bootstrap as bootstrap

    secret = "runtime-secret-value"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.raises(SystemExit, match="email-mode public runtime") as error:
        bootstrap.main(
            argv=[],
            stdin=FakeStdin(interactive=True),
            stdout=stdout,
            stderr=stderr,
            runtime_factory=lambda environment: (_ for _ in ()).throw(RuntimeError(secret)),
            getpass_fn=lambda prompt: "owner-password-2026",
            environment={"TAWB_PUBLIC_RUNTIME_MODE": "email"},
        )
    rendered = str(error.value) + stdout.getvalue() + stderr.getvalue()
    assert secret not in rendered


def test_bootstrap_cli_ignores_runtime_close_failure_after_success():
    import tradingagents.workbench.email_bootstrap as bootstrap

    backend = MemoryBackend()
    auth = EmailAuth(EncryptedStore(backend, b"d" * 32), origin=ORIGIN)
    stdout = io.StringIO()
    runtime = bootstrap.BootstrapRuntime(
        auth=auth,
        close=lambda: (_ for _ in ()).throw(RuntimeError("close-secret-value")),
    )

    bootstrap.main(
        argv=[],
        stdin=FakeStdin(interactive=True),
        stdout=stdout,
        runtime_factory=lambda _environment: runtime,
        getpass_fn=lambda prompt: "owner-password-2026",
        environment={"TAWB_PUBLIC_RUNTIME_MODE": "email"},
    )

    result = json.loads(stdout.getvalue())
    assert result["owner_email"] == OWNER_EMAIL


def test_create_runtime_uses_only_explicit_mi_and_state_key_without_secret_writes(monkeypatch):
    import tradingagents.workbench.email_bootstrap as bootstrap

    backend = MemoryBackend()
    calls = []

    class Credential:
        def close(self):
            calls.append("credential-close")

    class Vault:
        def get_secret(self, name):
            calls.append(("get_secret", name))
            return type("Secret", (), {"value": "ZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWU="})()

        def close(self):
            calls.append("vault-close")

        def set_secret(self, *args, **kwargs):
            pytest.fail("bootstrap runtime must not write secrets")

    class Container:
        def close(self):
            calls.append("container-close")

        def upload_blob(self, *args, **kwargs):
            pytest.fail("bootstrap runtime must not write blob state during setup")

    monkeypatch.setattr(bootstrap, "ManagedIdentityCredential", lambda *, client_id: calls.append(("mi", client_id)) or Credential())
    monkeypatch.setattr(bootstrap, "SecretClient", lambda *, vault_url, credential: calls.append(("vault", vault_url, credential.__class__.__name__)) or Vault())
    monkeypatch.setattr(bootstrap, "ContainerClient", lambda *, account_url, container_name, credential: calls.append(("blob", account_url, container_name, credential.__class__.__name__)) or Container())
    monkeypatch.setattr(bootstrap, "BlobBackend", lambda value: backend)

    runtime = bootstrap._create_runtime(
        {
            "TAWB_PUBLIC_RUNTIME_MODE": "email",
            "TAWB_MANAGED_IDENTITY_CLIENT_ID": "33333333-3333-4333-8333-333333333333",
            "TAWB_VAULT_URL": "https://tawb-test.vault.azure.net",
            "TAWB_STORAGE_ACCOUNT_URL": "https://tawbtest.blob.core.windows.net",
            "TAWB_PUBLIC_ORIGIN": ORIGIN,
        }
    )

    session = runtime.auth.bootstrap("owner-password-2026")
    runtime.auth.logout(session.session_id)
    runtime.close()

    assert ("mi", "33333333-3333-4333-8333-333333333333") in calls
    assert ("get_secret", "state-encryption-key") in calls
    assert set(backend.records) == {"email-v1/registry"}
