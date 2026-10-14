# Modified for public distribution: installation identifiers replaced with examples.
import base64
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

APP_ID = "00000000-0000-4000-8000-000000000130"
OBJECT_ID = "00000000-0000-4000-8000-000000000111"
ORIGIN = "https://workbench.example.com"


class Vault:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get_secret(self, name):
        if name not in self.values:
            raise ResourceNotFoundError()
        return SimpleNamespace(value=self.values[name])

    def set_secret(self, name, value, **kwargs):
        self.writes.append(name)
        self.values[name] = value


def test_bootstrap_preserves_encryption_and_existing_app_settings_on_retry():
    from scripts.public_bootstrap import configure_application

    vault = Vault()
    old_key = {"keyId": "old-key", "customKeyIdentifier": "old-thumbprint", "key": "old-public-key", "type": "AsymmetricX509Cert", "usage": "Verify"}
    application = {"id": OBJECT_ID, "appId": APP_ID, "signInAudience": "AzureADMyOrg", "keyCredentials": [old_key], "web": {"redirectUris": ["https://prior.example.com/auth/callback"], "implicitGrantSettings": {"enableIdTokenIssuance": False, "enableAccessTokenIssuance": False}}}
    requests = []

    def graph(request):
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["authorization"] == "Bearer private-graph-token"
        if request.method == "GET":
            return httpx.Response(200, json=copy.deepcopy(application))
        assert request.method == "PATCH"
        body = json.loads(request.content)
        assert set(body) == {"keyCredentials", "web"}
        application.update(body)
        requests.append(body)
        return httpx.Response(204)

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="private-graph-token"))
    with httpx.Client(transport=httpx.MockTransport(graph)) as client:
        first = configure_application(vault, credential, client, OBJECT_ID, APP_ID, ORIGIN)
        saved = dict(vault.values)
        second = configure_application(vault, credential, client, OBJECT_ID, APP_ID, ORIGIN)
    assert vault.values == saved
    assert len(vault.writes) == 2
    assert len(base64.b64decode(vault.values["state-encryption-key"])) == 32
    material = json.loads(vault.values["entra-certificate"])
    cert = x509.load_pem_x509_certificate(material["public_certificate"].encode())
    key = serialization.load_pem_private_key(material["private_key"].encode(), password=None)
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert cert.fingerprint(hashes.SHA1()).hex() == material["thumbprint"]
    assert application["keyCredentials"][0] == old_key
    assert len(application["keyCredentials"]) == 2
    assert ORIGIN + "/auth/callback" in application["web"]["redirectUris"]
    assert "https://prior.example.com/auth/callback" in application["web"]["redirectUris"]
    assert first == second
    assert "PRIVATE KEY" not in json.dumps(first)
    assert len(requests) == 1


def test_bootstrap_wrong_application_fails_before_writing_secrets():
    from scripts.public_bootstrap import configure_application

    vault = Vault()
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="private-graph-token"))
    with (httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"appId": "wrong"}))) as client,
          pytest.raises(ValueError)):
        configure_application(vault, credential, client, OBJECT_ID, APP_ID, ORIGIN)
    assert vault.writes == []


@pytest.mark.parametrize("failure", [None, "graph", "accepted", "missing"])
def test_private_bootstrap_retries_without_replacing_or_disclosing_keys(tmp_path, failure):
    from scripts.public_bootstrap import configure_private_application

    vault_id = "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/test/providers/Microsoft.KeyVault/vaults/test-vault"
    receipt = tmp_path / "bootstrap.json"
    application = {"id": OBJECT_ID, "appId": APP_ID, "signInAudience": "AzureADMyOrg", "keyCredentials": [], "web": {"redirectUris": []}}
    values, metadata, writes = {}, {}, []
    pending = failure

    def service(request):
        nonlocal pending
        assert request.url.scheme == "https"
        if request.url.host == "graph.microsoft.com":
            assert request.headers["authorization"] == "Bearer graph-token"
            if request.method == "GET":
                assert "keyCredentials" in request.url.params["$select"].split(",")
                return httpx.Response(200, json=copy.deepcopy(application))
            assert request.method == "PATCH"
            if pending == "graph":
                pending = None
                return httpx.Response(503)
            application.update(json.loads(request.content))
            return httpx.Response(204)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer arm-token"
        assert request.url.params["api-version"] == "2023-07-01"
        name = request.url.path.rsplit("/", 1)[1]
        assert name in {"state-encryption-key", "entra-certificate"}
        if request.method == "GET":
            return httpx.Response(200, json=metadata[name]) if name in metadata else httpx.Response(404)
        assert request.method == "PUT"
        writes.append(name)
        body = json.loads(request.content)
        if pending == "missing":
            pending = None
            raise httpx.ReadTimeout("uncertain", request=request)
        values[name] = body["properties"]["value"]
        metadata[name] = {"id": vault_id + "/secrets/" + name, "name": name, "type": "Microsoft.KeyVault/vaults/secrets", "tags": body["tags"], "properties": {"attributes": {"enabled": True}}}
        metadata[name]["tags"]["environment"] = "policy-managed"
        if pending == "accepted":
            pending = None
            raise httpx.ReadTimeout("accepted", request=request)
        return httpx.Response(200, json=metadata[name])

    def token(scope):
        assert scope in {"https://graph.microsoft.com/.default", "https://management.azure.com/.default"}
        return SimpleNamespace(token="graph-token" if "graph" in scope else "arm-token")

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        args = (SimpleNamespace(get_token=token), client, OBJECT_ID, APP_ID, ORIGIN, vault_id, receipt)
        if failure:
            with pytest.raises(httpx.HTTPError):
                configure_private_application(*args)
        if failure == "missing":
            with pytest.raises(ValueError, match="uncertain"):
                configure_private_application(*args)
            assert writes == ["state-encryption-key"]
            return
        result = configure_private_application(*args)
        saved = dict(values)
        assert configure_private_application(*args) == result
    assert values == saved
    assert writes == ["state-encryption-key", "entra-certificate"]
    material = json.loads(values["entra-certificate"])
    cert = x509.load_pem_x509_certificate(material["public_certificate"].encode())
    private = serialization.load_pem_private_key(material["private_key"].encode(), password=None)
    assert cert.public_key().public_numbers() == private.public_key().public_numbers()
    assert len(base64.b64decode(values["state-encryption-key"])) == 32
    assert len(application["keyCredentials"]) == 1
    assert base64.b64decode(application["keyCredentials"][0]["key"]) == cert.public_bytes(serialization.Encoding.DER)
    assert application["web"]["redirectUris"] == [ORIGIN + "/auth/callback"]
    public_text = receipt.read_text() + json.dumps(result) + json.dumps(application)
    assert "PRIVATE KEY" not in public_text
    assert values["state-encryption-key"] not in public_text


def test_private_bootstrap_refuses_existing_secret_without_matching_receipt(tmp_path):
    from scripts.public_bootstrap import configure_private_application

    def service(request):
        assert request.method == "GET"
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"id": OBJECT_ID, "appId": APP_ID, "signInAudience": "AzureADMyOrg", "keyCredentials": [], "web": {}})
        return httpx.Response(200, json={"tags": {}, "properties": {"attributes": {"enabled": True}}})

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="token"))
    vault_id = "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/test/providers/Microsoft.KeyVault/vaults/test-vault"
    with (httpx.Client(transport=httpx.MockTransport(service)) as client,
          pytest.raises(ValueError, match="receipt")):
        configure_private_application(credential, client, OBJECT_ID, APP_ID, ORIGIN, vault_id, tmp_path / "bootstrap.json")


def test_email_state_bootstrap_creates_state_key_then_retries_without_extra_put(tmp_path):
    from scripts import bootstrap_email_state

    subscription_id = "00000000-0000-4000-8000-000000000116"
    tenant_id = "00000000-0000-4000-8000-000000000105"
    resource_group = "rg-research-workbench"
    identity_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    vault_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.KeyVault/vaults/previous-vault-example"
    )
    receipt_path = tmp_path / "state-bootstrap.json"

    metadata = {}
    values = {}
    puts = []

    def service(request):
        assert request.headers["authorization"] == "Bearer arm-token"
        if request.url.path == identity_id:
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "id": identity_id,
                    "properties": {
                        "tenantId": tenant_id,
                        "clientId": APP_ID,
                        "principalId": "00000000-0000-4000-8000-000000000107",
                    },
                },
            )
        assert request.url.path == vault_id + "/secrets/state-encryption-key"
        assert request.url.params["api-version"] == "2023-07-01"
        if request.method == "GET":
            if "state-encryption-key" not in metadata:
                return httpx.Response(404)
            return httpx.Response(200, json=metadata["state-encryption-key"])
        assert request.method == "PUT"
        puts.append(request)
        body = json.loads(request.content)
        values["state-encryption-key"] = body["properties"]["value"]
        metadata["state-encryption-key"] = {
            "id": vault_id + "/secrets/state-encryption-key",
            "name": "state-encryption-key",
            "type": "Microsoft.KeyVault/vaults/secrets",
            "tags": dict(body["tags"]),
            "properties": {"attributes": {"enabled": True}},
        }
        metadata["state-encryption-key"]["tags"]["environment"] = "policy-managed"
        return httpx.Response(200, json=metadata["state-encryption-key"])

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        first = bootstrap_email_state.bootstrap_email_state_key(
            credential,
            client,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            identity_id=identity_id,
            vault_id=vault_id,
            receipt_path=receipt_path,
        )
        second = bootstrap_email_state.bootstrap_email_state_key(
            credential,
            client,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            identity_id=identity_id,
            vault_id=vault_id,
            receipt_path=receipt_path,
        )

    assert first == second == {
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "identity_id": identity_id,
        "identity_client_id": APP_ID,
        "vault_id": vault_id,
        "receipt_path": str(receipt_path),
        "credential": "managed-identity-state-key",
        "records": json.loads(receipt_path.read_text())["records"],
    }
    assert puts == [puts[0]]
    assert len(puts) == 1
    assert len(base64.b64decode(values["state-encryption-key"], validate=True)) == 32
    public_text = receipt_path.read_text() + json.dumps(first)
    assert values["state-encryption-key"] not in public_text
    assert "arm-token" not in json.dumps(first)


@pytest.mark.parametrize("failure", ["accepted", "missing"])
def test_email_state_bootstrap_uncertain_write_paths(tmp_path, failure):
    from scripts import bootstrap_email_state

    subscription_id = "00000000-0000-4000-8000-000000000116"
    tenant_id = "00000000-0000-4000-8000-000000000105"
    resource_group = "rg-research-workbench"
    identity_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    vault_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.KeyVault/vaults/previous-vault-example"
    )
    receipt_path = tmp_path / "state-bootstrap.json"
    metadata = {}
    values = {}
    puts = []
    pending = failure

    def service(request):
        nonlocal pending
        assert request.headers["authorization"] == "Bearer arm-token"
        if request.url.path == identity_id:
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "id": identity_id,
                    "properties": {
                        "tenantId": tenant_id,
                        "clientId": APP_ID,
                        "principalId": "00000000-0000-4000-8000-000000000107",
                    },
                },
            )
        assert request.url.path == vault_id + "/secrets/state-encryption-key"
        assert request.url.params["api-version"] == "2023-07-01"
        if request.method == "GET":
            if "state-encryption-key" not in metadata:
                return httpx.Response(404)
            return httpx.Response(200, json=metadata["state-encryption-key"])
        assert request.method == "PUT"
        puts.append(request)
        body = json.loads(request.content)
        values["state-encryption-key"] = body["properties"]["value"]
        if pending == "missing":
            pending = None
            raise httpx.ReadTimeout("uncertain", request=request)
        metadata["state-encryption-key"] = {
            "id": vault_id + "/secrets/state-encryption-key",
            "name": "state-encryption-key",
            "type": "Microsoft.KeyVault/vaults/secrets",
            "tags": dict(body["tags"]),
            "properties": {"attributes": {"enabled": True}},
        }
        metadata["state-encryption-key"]["tags"]["environment"] = "policy-managed"
        if pending == "accepted":
            pending = None
            raise httpx.ReadTimeout("accepted", request=request)
        return httpx.Response(200, json=metadata["state-encryption-key"])

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(httpx.ReadTimeout):
            bootstrap_email_state.bootstrap_email_state_key(
                credential,
                client,
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
                vault_id=vault_id,
                receipt_path=receipt_path,
            )
        if failure == "accepted":
            retried = bootstrap_email_state.bootstrap_email_state_key(
                credential,
                client,
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
                vault_id=vault_id,
                receipt_path=receipt_path,
            )
            assert retried["records"] == json.loads(receipt_path.read_text())["records"]
            assert len(puts) == 1
            return
        with pytest.raises(ValueError, match="uncertain"):
            bootstrap_email_state.bootstrap_email_state_key(
                credential,
                client,
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
                vault_id=vault_id,
                receipt_path=receipt_path,
            )
    assert len(puts) == 1


@pytest.mark.parametrize(
    ("identity_id", "vault_id", "match"),
    [
        (
            "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-a/providers/Microsoft.ManagedIdentity/userAssignedIdentities/host",
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-a/providers/Microsoft.KeyVault/vaults/test",
            "subscription",
        ),
        (
            "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-a/providers/Microsoft.ManagedIdentity/userAssignedIdentities/host",
            "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-b/providers/Microsoft.KeyVault/vaults/test",
            "resource group",
        ),
    ],
)
def test_email_state_bootstrap_rejects_target_mismatch_before_token_or_secret_write(
    tmp_path, identity_id, vault_id, match
):
    from scripts import bootstrap_email_state

    class Credential:
        def get_token(self, scope):
            raise AssertionError("token request should not happen")

    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client,
        pytest.raises(ValueError, match=match),
    ):
        bootstrap_email_state.bootstrap_email_state_key(
            Credential(),
            client,
            tenant_id="00000000-0000-4000-8000-000000000105",
            subscription_id="00000000-0000-4000-8000-000000000116",
            identity_id=identity_id,
            vault_id=vault_id,
            receipt_path=tmp_path / "receipt.json",
        )


def test_email_state_cli_credential_uses_subscription_without_conflicting_tenant():
    from scripts.bootstrap_email_state import _build_cli_credential

    credential = _build_cli_credential(
        "00000000-0000-4000-8000-000000000128", "00000000-0000-4000-8000-000000000103"
    )
    assert credential.subscription == "00000000-0000-4000-8000-000000000128"
    assert not credential.tenant_id


def test_email_state_bootstrap_rejects_mismatched_identity_tenant_before_secret_write(tmp_path):
    from scripts import bootstrap_email_state

    subscription_id = "00000000-0000-4000-8000-000000000116"
    resource_group = "rg-research-workbench"
    identity_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    vault_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.KeyVault/vaults/previous-vault-example"
    )

    def service(request):
        return httpx.Response(
            200,
            json={
                "id": identity_id,
                "properties": {
                    "tenantId": "00000000-0000-4000-8000-000000000103",
                    "clientId": APP_ID,
                    "principalId": "00000000-0000-4000-8000-000000000107",
                },
            },
        )

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    with (
        httpx.Client(transport=httpx.MockTransport(service)) as client,
        pytest.raises(ValueError, match="tenant"),
    ):
        bootstrap_email_state.bootstrap_email_state_key(
            credential,
            client,
            tenant_id="00000000-0000-4000-8000-000000000105",
            subscription_id=subscription_id,
            identity_id=identity_id,
            vault_id=vault_id,
            receipt_path=tmp_path / "receipt.json",
        )


def test_email_state_bootstrap_missing_receipt_with_existing_metadata_is_rejected(tmp_path):
    from scripts import bootstrap_email_state

    subscription_id = "00000000-0000-4000-8000-000000000116"
    tenant_id = "00000000-0000-4000-8000-000000000105"
    resource_group = "rg-research-workbench"
    identity_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    vault_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.KeyVault/vaults/previous-vault-example"
    )
    receipt_path = tmp_path / "state-bootstrap.json"

    def service(request):
        assert request.headers["authorization"] == "Bearer arm-token"
        if request.url.path == identity_id:
            return httpx.Response(
                200,
                json={
                    "id": identity_id,
                    "properties": {
                        "tenantId": tenant_id,
                        "clientId": APP_ID,
                        "principalId": "00000000-0000-4000-8000-000000000107",
                    },
                },
            )
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "id": vault_id + "/secrets/state-encryption-key",
                "name": "state-encryption-key",
                "type": "Microsoft.KeyVault/vaults/secrets",
                "tags": {"tawbApp": APP_ID, "tawbSha256": "abc123"},
                "properties": {"attributes": {"enabled": True}},
            },
        )

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    with (
        httpx.Client(transport=httpx.MockTransport(service)) as client,
        pytest.raises(ValueError, match="receipt"),
    ):
        bootstrap_email_state.bootstrap_email_state_key(
            credential,
            client,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            identity_id=identity_id,
            vault_id=vault_id,
            receipt_path=receipt_path,
        )


def test_build_cli_credential_binds_supported_subscription_argument(monkeypatch):
    from scripts import bootstrap_email_state

    seen = {}

    class Credential:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(bootstrap_email_state, "AzureCliCredential", Credential)

    credential = bootstrap_email_state._build_cli_credential(
        "00000000-0000-4000-8000-000000000116",
        "00000000-0000-4000-8000-000000000105",
    )

    assert isinstance(credential, Credential)
    assert seen == {
        "subscription": "00000000-0000-4000-8000-000000000116",
    }


def test_email_state_bootstrap_uncertain_put_never_replaced(tmp_path, monkeypatch):
    from scripts import bootstrap_email_state

    subscription_id = "00000000-0000-4000-8000-000000000116"
    tenant_id = "00000000-0000-4000-8000-000000000105"
    resource_group = "rg-research-workbench"
    identity_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    vault_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.KeyVault/vaults/previous-vault-example"
    )
    receipt_path = tmp_path / "state-bootstrap.json"
    attempts = []

    def fake_helper(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("accepted")
        raise ValueError("Previous secret write is uncertain; inspect it before any further writes")

    monkeypatch.setattr(bootstrap_email_state, "_ensure_private_secrets", fake_helper)

    def service(request):
        return httpx.Response(
            200,
            json={
                "id": identity_id,
                "properties": {
                    "tenantId": tenant_id,
                    "clientId": APP_ID,
                    "principalId": "00000000-0000-4000-8000-000000000107",
                },
            },
        )

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(httpx.ReadTimeout):
            bootstrap_email_state.bootstrap_email_state_key(
                credential,
                client,
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
                vault_id=vault_id,
                receipt_path=receipt_path,
            )
        with pytest.raises(ValueError, match="uncertain"):
            bootstrap_email_state.bootstrap_email_state_key(
                credential,
                client,
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
                vault_id=vault_id,
                receipt_path=receipt_path,
            )

    assert len(attempts) == 2
