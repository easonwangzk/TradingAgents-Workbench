# Modified for public distribution: installation identifiers replaced with examples.
import base64
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tests.test_public_bootstrap import APP_ID, OBJECT_ID, ORIGIN

TENANT = "00000000-0000-4000-8000-000000000105"
PRINCIPAL = "11111111-1111-4111-8111-111111111111"
IDENTITY_CLIENT = "22222222-2222-4222-8222-222222222222"
GROUP = "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/test"
IDENTITY = GROUP + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/workbench"
VAULT = GROUP + "/providers/Microsoft.KeyVault/vaults/test-vault"


@pytest.mark.parametrize("scenario", ["new", "existing", "wrong_tenant", "wrong_trust", "denied", "accepted_lost"])
def test_federation_binds_only_verified_identity_and_preserves_secrets_on_retry(tmp_path, scenario):
    from scripts.public_bootstrap import configure_federated_application

    application = {"id": OBJECT_ID, "appId": APP_ID, "signInAudience": "AzureADMyOrg", "keyCredentials": [],
                   "web": {"redirectUris": ["https://prior.example.com/auth/callback"]}}
    trust = {"name": "public-workbench-host", "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
             "subject": PRINCIPAL, "audiences": ["api://AzureADTokenExchange"]}
    credentials = [copy.deepcopy(trust)] if scenario in {"existing", "wrong_trust"} else []
    if scenario == "wrong_trust":
        credentials[0]["subject"] = IDENTITY_CLIENT
    requests, stored = [], {}
    pending = scenario

    def service(request):
        nonlocal pending
        requests.append((request.method, request.url.path))
        assert request.url.host in {"management.azure.com", "graph.microsoft.com"}
        assert request.headers["authorization"] == ("Bearer arm-token" if request.url.host == "management.azure.com" else "Bearer graph-token")
        if request.url.path == IDENTITY:
            assert request.method == "GET"
            return httpx.Response(200, json={"id": IDENTITY, "properties": {"clientId": IDENTITY_CLIENT,
                "principalId": PRINCIPAL, "tenantId": IDENTITY_CLIENT if scenario == "wrong_tenant" else TENANT}})
        if request.url.path.endswith("/federatedIdentityCredentials"):
            if request.method == "GET":
                return httpx.Response(200, json={"value": copy.deepcopy(credentials)})
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body == trust
            if scenario == "denied":
                return httpx.Response(403, json={"error": {"code": "policy_denied"}})
            credentials.append(body)
            if pending == "accepted_lost":
                pending = None
                raise httpx.ReadTimeout("accepted", request=request)
            return httpx.Response(201, json=body)
        if request.url.host == "graph.microsoft.com":
            assert request.url.path == "/v1.0/applications/" + OBJECT_ID
            if request.method == "GET":
                return httpx.Response(200, json=copy.deepcopy(application))
            assert request.method == "PATCH"
            body = json.loads(request.content)
            assert set(body) == {"web"}
            application.update(body)
            return httpx.Response(204)
        assert request.url.path == VAULT + "/secrets/state-encryption-key"
        if request.method == "GET":
            if not stored:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": request.url.path, "tags": stored["tags"],
                                          "properties": {"attributes": {"enabled": True}}})
        assert request.method == "PUT"
        stored.update(json.loads(request.content))
        return httpx.Response(200, json={"id": request.url.path})

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="graph-token" if "graph" in scope else "arm-token"))
    receipt = tmp_path / "bootstrap.json"
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        args = (credential, client, OBJECT_ID, APP_ID, ORIGIN, TENANT, IDENTITY, VAULT, receipt)
        if scenario in {"wrong_tenant", "wrong_trust", "denied"}:
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                configure_federated_application(*args)
            assert not stored
            assert not any(method == "PATCH" for method, _ in requests)
            return
        if scenario == "accepted_lost":
            with pytest.raises(httpx.ReadTimeout):
                configure_federated_application(*args)
            assert not stored
        first = configure_federated_application(*args)
        assert configure_federated_application(*args) == first
    assert len(credentials) == 1
    assert sum(method == "POST" for method, _ in requests) == (0 if scenario == "existing" else 1)
    assert sum(method == "PUT" for method, _ in requests) == 1
    assert application["web"]["redirectUris"] == ["https://prior.example.com/auth/callback", ORIGIN + "/auth/callback"]
    assert application["web"]["implicitGrantSettings"] == {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False}
    assert first["identity_client_id"] == IDENTITY_CLIENT
    value = stored["properties"]["value"]
    assert len(base64.b64decode(value)) == 32
    assert value not in receipt.read_text() + json.dumps(first)
    assert "entra-certificate" not in receipt.read_text()


@pytest.mark.parametrize("deny_identity", [False, True])
def test_real_msal_authorization_code_uses_fresh_identity_assertion_not_user_resource_tokens(monkeypatch, deny_identity):
    import msal
    import requests

    from tradingagents.workbench.public_auth import MsalClientFactory

    identity_scopes, token_requests = [], []
    assertions = iter(["private-first-assertion", "private-second-assertion"])

    def identity_token(scope):
        identity_scopes.append(scope)
        if deny_identity:
            raise RuntimeError("Managed identity unavailable")
        return SimpleNamespace(token=next(assertions))

    def response(session, method, url, **kwargs):
        assert url.startswith(f"https://login.microsoftonline.com/{TENANT}/")
        if method.upper() == "GET":
            assert url.endswith("/.well-known/openid-configuration")
            body = {"authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
                    "token_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                    "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0"}
        else:
            assert method.upper() == "POST"
            token_requests.append(dict(kwargs["data"]))
            body = {"error": "invalid_grant", "error_description": "test authorization code"}
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(body).encode()
        return result

    monkeypatch.setattr(requests.Session, "request", response)
    factory = MsalClientFactory(tenant_id=TENANT, client_id=APP_ID,
                                assertion_credential=SimpleNamespace(get_token=identity_token))
    app = factory(msal.SerializableTokenCache())
    assert identity_scopes == []
    for code in ("first-code", "second-code"):
        flow = app.initiate_auth_code_flow(["https://management.azure.com/.default"], redirect_uri=ORIGIN + "/auth/callback")
        if deny_identity:
            with pytest.raises(RuntimeError, match="Managed identity unavailable"):
                app.acquire_token_by_auth_code_flow(flow, {"state": flow["state"], "code": code})
            assert identity_scopes == ["api://AzureADTokenExchange/.default"]
            assert token_requests == []
            return
        result = app.acquire_token_by_auth_code_flow(flow, {"state": flow["state"], "code": code})
        assert result["error"] == "invalid_grant"
    assert identity_scopes == ["api://AzureADTokenExchange/.default"] * 2
    assert [request["client_assertion"] for request in token_requests] == ["private-first-assertion", "private-second-assertion"]
    assert all(request["grant_type"] == "authorization_code" for request in token_requests)
    assert all(request["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer" for request in token_requests)
    assert all(request["code_verifier"] and "client_secret" not in request for request in token_requests)
