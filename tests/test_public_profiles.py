import copy

import httpx
import pytest

from tests.test_public_auth import ALICE, BOB, TENANT, auth_harness as auth_harness
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_auth import AuthenticationRequired, Principal
from tradingagents.workbench.public_store import EncryptedStore, StoreConflict

SUB = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
BASE = f"/subscriptions/{SUB}/resourceGroups/research"
JOB = BASE + "/providers/Microsoft.App/jobs/research-job"
ENVIRONMENT = BASE + "/providers/Microsoft.App/managedEnvironments/research-env"
LOGS = BASE + "/providers/Microsoft.OperationalInsights/workspaces/research-logs"
FOUNDRY = BASE + "/providers/Microsoft.CognitiveServices/accounts/research-ai"
DEPLOYMENT = FOUNDRY + "/deployments/research-model"
ALICE_PRINCIPAL = Principal(TENANT, ALICE, "Alice")
BOB_PRINCIPAL = Principal(TENANT, BOB, "Bob")
PAYLOAD = {
    "name": "My research", "job_id": JOB, "workspace_resource_id": LOGS,
    "foundry_account_id": FOUNDRY, "deployment_name": "research-model",
}


class Credential:
    def get_token(self, scope):
        from azure.core.credentials import AccessToken

        assert scope == "https://management.azure.com/.default"
        return AccessToken("delegated-only", 2000000000)


@pytest.fixture
def resources():
    from tradingagents.workbench.public_profiles import AzureResources, Profiles

    values = {
        f"/subscriptions/{SUB}": {"subscriptionId": SUB, "tenantId": TENANT, "state": "Enabled"},
        JOB: {"id": JOB, "identity": {"type": "SystemAssigned", "principalId": ALICE},
              "properties": {"environmentId": ENVIRONMENT,
                             "configuration": {"triggerType": "Manual"},
                             "template": {"containers": [{"name": "worker", "image": "registry.azurecr.io/research:v1",
                                                          "env": [{"name": "AZURE_OPENAI_AUTH_MODE", "value": "managed_identity"}]}]}}},
        ENVIRONMENT: {"id": ENVIRONMENT, "properties": {"appLogsConfiguration": {
            "destination": "log-analytics", "logAnalyticsConfiguration": {"customerId": WORKSPACE}}}},
        LOGS: {"id": LOGS, "properties": {"customerId": WORKSPACE}},
        FOUNDRY: {"id": FOUNDRY, "kind": "AIServices", "properties": {"customSubDomainName": "research-ai"}},
        DEPLOYMENT: {"id": DEPLOYMENT, "name": "research-model", "properties": {
            "provisioningState": "Succeeded", "model": {"format": "OpenAI", "name": "gpt-5.6-sol"}}},
    }
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer delegated-only"
        value = values.get(request.url.path)
        return httpx.Response(404 if value is None else 200, json=copy.deepcopy(value or {}))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cloud = AzureResources(ALICE_PRINCIPAL, Credential(), client=client)
    profiles = Profiles(EncryptedStore(MemoryBackend(), b"b" * 32))
    yield cloud, profiles, values, requests
    client.close()


def test_profile_is_validated_and_owned_independently(resources):
    cloud, profiles, _, _ = resources
    saved = profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    assert saved["config"]["workspace_id"] == WORKSPACE
    assert saved["config"]["model"] == "research-model"
    assert saved["config"]["endpoint"] == "https://research-ai.services.ai.azure.com/"
    assert profiles.get(ALICE_PRINCIPAL, saved["id"]) == saved
    assert profiles.list(BOB_PRINCIPAL) == []
    from tradingagents.workbench.public_profiles import ResourceError

    for operation in (lambda: profiles.get(BOB_PRINCIPAL, saved["id"]),
                      lambda: profiles.delete(BOB_PRINCIPAL, saved["id"], saved["revision"]),
                      lambda: profiles.save(BOB_PRINCIPAL, PAYLOAD, cloud, saved["id"], saved["revision"])):
        with pytest.raises(ResourceError) as failure:
            operation()
        assert failure.value.status_code == 404
    assert profiles.get(ALICE_PRINCIPAL, saved["id"]) == saved


def test_edit_requires_current_revision_and_preserves_previous_snapshot(resources):
    cloud, profiles, _, _ = resources
    first = profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    edited = profiles.save(ALICE_PRINCIPAL, {**PAYLOAD, "name": "Changed"}, cloud, first["id"], first["revision"])
    assert first["name"] == "My research"
    assert edited["revision"] != first["revision"]
    with pytest.raises(StoreConflict):
        profiles.delete(ALICE_PRINCIPAL, first["id"], first["revision"])
    profiles.delete(ALICE_PRINCIPAL, edited["id"], edited["revision"])
    assert profiles.list(ALICE_PRINCIPAL) == []


@pytest.mark.parametrize("field,value", [
    ("endpoint", "http://169.254.169.254/metadata"),
    ("owner", BOB), ("job_id", "https://attacker.example/job"),
    ("job_id", JOB + "/../secrets"), ("deployment_name", "../../other"),
])
def test_untrusted_profile_fields_never_reach_azure(resources, field, value):
    from tradingagents.workbench.public_profiles import ResourceError

    cloud, profiles, _, requests = resources
    with pytest.raises(ResourceError):
        profiles.save(ALICE_PRINCIPAL, {**PAYLOAD, field: value}, cloud)
    assert requests == []


@pytest.mark.parametrize("change", ["tenant", "workspace", "trigger", "identity", "model", "endpoint"])
def test_incompatible_resources_cannot_be_saved(resources, change):
    from tradingagents.workbench.public_profiles import ResourceError

    cloud, profiles, values, _ = resources
    if change == "tenant":
        values[f"/subscriptions/{SUB}"]["tenantId"] = BOB
    if change == "workspace":
        values[LOGS]["properties"]["customerId"] = BOB
    if change == "trigger":
        values[JOB]["properties"]["configuration"]["triggerType"] = "Schedule"
    if change == "identity":
        values[JOB]["identity"] = {"type": "None"}
    if change == "model":
        values[DEPLOYMENT]["properties"]["model"]["format"] = "Other"
    if change == "endpoint":
        values[FOUNDRY]["properties"]["customSubDomainName"] = "attacker.example/path"
    with pytest.raises(ResourceError):
        profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    assert profiles.list(ALICE_PRINCIPAL) == []


def test_discovery_projects_only_safe_fields_and_rejects_foreign_next_link(resources):
    from tradingagents.workbench.public_profiles import ResourceError

    cloud, _, values, requests = resources
    values["/subscriptions"] = {"value": [
        {"subscriptionId": SUB, "tenantId": TENANT, "displayName": "Mine", "state": "Enabled", "private": "hidden"},
        {"subscriptionId": BOB, "tenantId": BOB, "displayName": "Other", "state": "Enabled"},
    ]}
    assert cloud.subscriptions() == [{"id": SUB, "name": "Mine"}]
    values["/subscriptions"]["nextLink"] = "https://attacker.example/steal"
    with pytest.raises(ResourceError):
        cloud.subscriptions()
    assert all(request.url.host == "management.azure.com" for request in requests)


def test_missing_consent_is_not_hidden_or_replaced(resources, monkeypatch):
    cloud, profiles, _, requests = resources

    def denied(scope):
        raise AuthenticationRequired(scope=scope)

    monkeypatch.setattr(cloud.credential, "get_token", denied)
    with pytest.raises(AuthenticationRequired):
        profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    assert requests == []


def test_azure_continuation_preserves_skip_token(resources):
    from tradingagents.workbench.public_profiles import AzureResources

    cloud, _, _, _ = resources
    observed = []

    def respond(request):
        observed.append(str(request.url))
        if len(observed) == 1:
            return httpx.Response(200, json={"value": [], "nextLink":
                "https://management.azure.com/subscriptions?api-version=2022-12-01&$skiptoken=next-page"})
        assert request.url.params.get("$skiptoken") == "next-page"
        return httpx.Response(200, json={"value": [{"subscriptionId": SUB, "tenantId": TENANT,
                                                   "state": "Enabled", "displayName": "Page two"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = AzureResources(ALICE_PRINCIPAL, cloud.credential, client=client)
        assert api.subscriptions() == [{"id": SUB, "name": "Page two"}]
    assert len(observed) == 2


def test_profile_http_crud_is_scoped_to_session_not_submitted_identity(resources, harness):
    from fastapi.testclient import TestClient

    from tests.test_public_server import ORIGIN, login
    from tradingagents.workbench.public_profiles import AzureResources
    from tradingagents.workbench.public_server import create_public_app

    cloud, _, _, _ = resources
    auth, provider, _, _ = harness
    provider.token_result["access_token"] = "delegated-only"
    app = create_public_app(auth=auth, resource_factory=lambda principal, credential:
                            AzureResources(principal, credential, client=cloud.client))
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": client.get("/api/me").json()["csrf_token"]}
        saved = client.post("/api/profiles", json=PAYLOAD, headers=headers)
        assert saved.status_code == 201
        profile = saved.json()
        assert client.get("/api/profiles").json() == {"profiles": [profile]}
        assert client.get("/api/profiles/" + profile["id"]).json() == profile
        assert client.put("/api/profiles/" + profile["id"], json=PAYLOAD, headers=headers).status_code == 409
        updated = client.put("/api/profiles/" + profile["id"], json={**PAYLOAD, "name": "Renamed"},
                             headers={**headers, "If-Match": profile["revision"]})
        assert updated.status_code == 200
        client.post("/auth/logout", headers=headers)
        provider.claims["oid"] = BOB
        login(client)
        headers["X-Workbench-Token"] = client.get("/api/me").json()["csrf_token"]
        assert client.get("/api/profiles").json() == {"profiles": []}
        assert client.get("/api/profiles/" + profile["id"]).status_code == 404
        assert client.put("/api/profiles/" + profile["id"], json=PAYLOAD, headers=headers).status_code == 404
        assert client.delete("/api/profiles/" + profile["id"], headers=headers).status_code == 404
