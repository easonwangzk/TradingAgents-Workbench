import copy
import json

import httpx
import pytest

from tests.test_public_auth import BOB, auth_harness as auth_harness, sign_in
from tests.test_public_profiles import JOB, PAYLOAD, resources as resources


@pytest.fixture
def key_service(resources, harness):
    from tradingagents.workbench.public_keys import ProviderKeys
    from tradingagents.workbench.public_profiles import AzureResources, Profiles
    from tradingagents.workbench.public_runs import PublicRuns

    _, _, original, _ = resources
    auth, provider, _, _ = harness
    provider.token_result["access_token"] = "delegated-only"
    _, session = sign_in(auth)
    values = copy.deepcopy(original)
    job = values[JOB]
    job["properties"]["provisioningState"] = "Succeeded"
    job["properties"]["configuration"]["secrets"] = [{"name": "fred-existing"}, {"name": "unrelated"}]
    job["properties"]["template"]["containers"][0]["env"].extend([
        {"name": "FRED_API_KEY", "secretRef": "fred-existing"},
        {"name": "OTHER_KEY", "secretRef": "unrelated"}])
    state = {"requests": [], "patches": 0, "uncertain": False,
             "secrets": [{"name": "fred-existing", "value": "private-fred"},
                         {"name": "unrelated", "value": "private-unrelated"}]}

    def respond(request):
        state["requests"].append(request)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer delegated-only"
        if request.method == "POST" and request.url.path == JOB + "/listSecrets":
            response = httpx.Response(200, json={"value": copy.deepcopy(state["secrets"])})
            if state.pop("external_change", False):
                job["properties"]["template"]["containers"][0]["env"].append({"name": "PORTAL_EDIT", "value": "keep"})
                state["secrets"].append({"name": "portal-secret", "value": "private-portal"})
            return response
        if request.method == "PATCH":
            assert request.url.path == JOB
            state["patches"] += 1
            body = json.loads(request.content)
            assert set(body) == {"properties"}
            assert set(body["properties"]) == {"configuration", "template"}
            state["secrets"] = copy.deepcopy(body["properties"]["configuration"]["secrets"])
            body["properties"]["configuration"]["secrets"] = [{"name": item["name"]} for item in state["secrets"]]
            job["properties"].update(body["properties"])
            if state["uncertain"]:
                raise httpx.ReadError("private-upstream-diagnostic")
            return httpx.Response(200, json=job)
        assert request.method == "GET"
        return httpx.Response(200 if request.url.path in values else 404, json=copy.deepcopy(values.get(request.url.path, {})))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def factory(principal, credential):
            return AzureResources(principal, credential, client=client)

        profiles = Profiles(auth.store)
        cloud = factory(session.principal, auth.credential(session.session_id))
        profile = profiles.save(session.principal, PAYLOAD, cloud)
        runs = PublicRuns(auth=auth, profiles=profiles, resource_factory=factory)
        service = ProviderKeys(auth=auth, profiles=profiles, runs=runs, resource_factory=factory)
        state["requests"].clear()
        yield service, session, profile, job, state, provider


def test_provider_update_preserves_other_secrets_and_never_returns_values(key_service):
    service, session, profile, job, state, _ = key_service
    before = copy.deepcopy(job)
    status = service.get(session.session_id, profile["id"])
    assert status == {"configured": {"alpha_vantage": False, "fred": True}, "pending": False}
    assert all(request.method == "GET" for request in state["requests"])
    result = service.update(session.session_id, profile["id"], {"alpha_vantage": "private-new-alpha"})
    assert "private-" not in json.dumps(result)
    assert {item["name"]: item.get("value") for item in state["secrets"]} == {
        "fred-existing": "private-fred", "unrelated": "private-unrelated", "tawb-alpha-vantage": "private-new-alpha"}
    assert job["identity"] == before["identity"]
    assert job["properties"]["template"]["containers"][0]["image"] == profile["image"]
    assert {"name": "FRED_API_KEY", "secretRef": "fred-existing"} in job["properties"]["template"]["containers"][0]["env"]
    assert service.get(session.session_id, profile["id"]) == {"configured": {"alpha_vantage": True, "fred": True}, "pending": False}
    assert state["patches"] == 1


def test_key_update_isolated_before_any_azure_call(key_service):
    from tradingagents.workbench.public_profiles import ResourceError

    service, _, profile, _, state, provider = key_service
    provider.claims["oid"] = BOB
    _, bob = sign_in(service.auth)
    with pytest.raises(ResourceError) as failure:
        service.update(bob.session_id, profile["id"], {"fred": "private-bob"})
    assert failure.value.status_code == 404
    assert state["requests"] == []


@pytest.mark.parametrize("payload", [{}, {"unknown": "private-value"}, {"fred": []}, {"fred": ""}])
def test_invalid_keys_rejected_before_azure(key_service, payload):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, _, state, _ = key_service
    with pytest.raises(ResourceError):
        service.update(session.session_id, profile["id"], payload)
    assert state["requests"] == []


def test_active_research_reservation_blocks_key_changes(key_service):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, _, state, _ = key_service
    key = service.runs._reservation_key(profile)
    service.auth.store.write(key, {"owner": session.principal.owner_key, "run_id": "11111111-1111-4111-8111-111111111111"})
    with pytest.raises(ResourceError):
        service.update(session.session_id, profile["id"], {"fred": "private-value"})
    assert state["patches"] == 0


def test_ambiguous_update_is_reconciled_without_another_patch(key_service):
    from tradingagents.workbench.public_keys import ProviderKeys
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, _, state, _ = key_service
    state["uncertain"] = True
    with pytest.raises(ResourceError):
        service.update(session.session_id, profile["id"], {"fred": "private-new-fred"})
    key = service.runs._reservation_key(profile)
    assert service.auth.store.read(key) is not None
    assert "private-" not in json.dumps(service.auth.store.read(key)[0])
    restarted = ProviderKeys(auth=service.auth, profiles=service.profiles, runs=service.runs, resource_factory=service.resource_factory)
    assert restarted.get(session.session_id, profile["id"])["pending"] is False
    assert service.auth.store.read(key) is None
    assert state["patches"] == 1


def test_disabling_provider_preserves_other_bindings(key_service):
    service, session, profile, job, state, _ = key_service
    service.update(session.session_id, profile["id"], {"fred": None})
    assert service.get(session.session_id, profile["id"])["configured"]["fred"] is False
    assert {"name": "OTHER_KEY", "secretRef": "unrelated"} in job["properties"]["template"]["containers"][0]["env"]
    assert {"name": "unrelated", "value": "private-unrelated"} in state["secrets"]


def test_provider_secret_used_by_registry_is_not_changed(key_service):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, job, state, _ = key_service
    job["properties"]["configuration"]["registries"] = [{"server": "registry.azurecr.io", "passwordSecretRef": "tawb-fred"}]
    state["secrets"].append({"name": "tawb-fred", "value": "private-registry"})
    with pytest.raises(ResourceError):
        service.update(session.session_id, profile["id"], {"fred": "private-new-fred"})
    assert state["patches"] == 0


def test_missing_secret_values_cannot_be_written_back_as_empty(key_service):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, _, state, _ = key_service
    state["secrets"][0].pop("value")
    with pytest.raises(ResourceError):
        service.update(session.session_id, profile["id"], {"alpha_vantage": "private-new-alpha"})
    assert state["patches"] == 0


def test_provider_key_http_never_returns_submitted_secrets(key_service):
    from fastapi.testclient import TestClient

    from tests.test_public_server import ORIGIN
    from tradingagents.workbench.public_server import create_public_app

    service, session, profile, _, _, _ = key_service
    app = create_public_app(auth=service.auth, resource_factory=service.resource_factory)
    path = "/api/profiles/" + profile["id"] + "/providers"
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        assert client.get(path).status_code == 401
        client.cookies.set("__Host-tawb-session", session.session_id)
        assert client.put(path, json={"fred": "private-key"}).status_code == 403
        headers = {"Origin": ORIGIN, "X-Workbench-Token": session.csrf_token}
        response = client.put(path, json={"fred": "private-key"}, headers=headers)
        assert response.status_code == 200
        assert "private-" not in response.text
        assert client.get(path).json()["configured"]["fred"] is True


def test_detected_external_change_aborts_without_overwriting(key_service):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, job, state, _ = key_service
    state["external_change"] = True
    with pytest.raises(ResourceError) as failure:
        service.update(session.session_id, profile["id"], {"fred": "private-new-fred"})
    assert failure.value.status_code == 409
    assert state["patches"] == 0
    assert {"name": "PORTAL_EDIT", "value": "keep"} in job["properties"]["template"]["containers"][0]["env"]
    assert {"name": "portal-secret", "value": "private-portal"} in state["secrets"]
