import copy
import json

import httpx
import pytest

from tests.test_public_auth import ALICE, TENANT, auth_harness as auth_harness, sign_in
from tests.test_public_profiles import SUB, WORKSPACE, resources as resources
from tests.test_public_provisioning import INITIALIZE


@pytest.fixture
def initializer(resources, harness):
    from tradingagents.workbench.public_profiles import AzureResources
    from tradingagents.workbench.public_provisioning import Provisioning
    from tradingagents.workbench.public_source import WorkerSource

    _, _, original, _ = resources
    auth, provider, clock, _ = harness
    provider.token_result["access_token"] = "delegated-only"
    _, session = sign_in(auth)
    values = copy.deepcopy(original)
    values[f"/subscriptions/{SUB}/locations"] = {"value": [{"name": "eastus2"}]}
    state = {"requests": [], "builds": 0, "uploads": [], "deny_role": False, "unsafe_upload": False}

    def respond(request):
        state["requests"].append(request)
        if state.get("before_request"):
            state["before_request"](request)
        if request.url.host == "buildcontext.blob.core.windows.net":
            assert "authorization" not in request.headers
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            state["uploads"].append(request.content)
            return httpx.Response(201)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer delegated-only"
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200 if path in values else 404, json=copy.deepcopy(values.get(path, {})))
        if request.method == "POST" and path.endswith("/sharedKeys"):
            return httpx.Response(200, json={"primarySharedKey": "private-log-key"})
        if request.method == "POST" and path.endswith("/listBuildSourceUploadUrl"):
            url = "http://169.254.169.254/metadata" if state["unsafe_upload"] else "https://buildcontext.blob.core.windows.net/source/context.tar.gz?sig=private-upload-sas"
            return httpx.Response(200, json={"uploadUrl": url, "relativePath": "source/context.tar.gz"})
        assert request.method == "PUT"
        if "/roleAssignments/" in path and state["deny_role"]:
            return httpx.Response(403, text="private-rbac-diagnostic")
        assert path not in values
        body = json.loads(request.content)
        properties = body.setdefault("properties", {})
        properties["provisioningState"] = "Succeeded"
        if path.split("/")[-2] == "registries":
            assert body["properties"]["adminUserEnabled"] is False
            properties["loginServer"] = path.split("/")[-1] + ".azurecr.io"
        if "/workspaces/" in path:
            properties["customerId"] = WORKSPACE
        if "/userAssignedIdentities/" in path:
            properties.update(principalId=ALICE, clientId=ALICE, tenantId=TENANT)
        if "/taskRuns/" in path:
            state["builds"] += 1
            assert "forceUpdateTag" not in properties
            assert properties["runRequest"]["sourceLocation"] == "source/context.tar.gz"
            assert "private-" not in request.content.decode()
            registry = path.split("/registries/")[1].split("/")[0] + ".azurecr.io"
            image = properties["runRequest"]["imageNames"][0].split("/")[-1]
            repository, tag = image.split(":")
            properties["runResult"] = {"properties": {"status": "Succeeded", "runId": "build1", "outputImages": [
                {"registry": registry, "repository": repository, "tag": tag, "digest": "sha256:" + "a" * 64}]}}
        if "/jobs/" in path:
            assert properties["configuration"]["triggerType"] == "Manual"
            assert properties["configuration"]["replicaRetryLimit"] == 0
            worker = properties["template"]["containers"][0]
            assert worker["image"].endswith("@sha256:" + "a" * 64)
            assert worker["resources"] == {"cpu": 1, "memory": "2Gi"}
        values[path] = {**body, "id": path, "name": path.split("/")[-1]}
        return httpx.Response(201, json=values[path])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def factory(principal, credential):
            return AzureResources(principal, credential, client=client)

        source = WorkerSource(b"reviewed-worker-source")
        service = Provisioning(auth=auth, resource_factory=factory, source=source, clock=lambda: clock[0])
        yield service, session, state, values, clock


def test_no_confirmation_means_no_azure_write(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, _, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    with pytest.raises(ResourceError):
        service.apply(session.session_id, preview["id"])
    assert {request.method for request in state["requests"]} == {"GET"}


def test_apply_resumes_same_resources_and_build_across_server_restarts(initializer):
    from tradingagents.workbench.public_provisioning import Provisioning
    from tradingagents.workbench.public_source import WorkerSource

    service, session, state, _, clock = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    assert preview["plan"]["source_sha256"] == service.source.sha256
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    for _step in range(40):
        restarted = Provisioning(auth=service.auth, resource_factory=service.resource_factory,
                                 source=WorkerSource(b"different-source-after-app-upgrade"), clock=lambda: clock[0])
        progress = restarted.apply(session.session_id, preview["id"])
        assert progress["plan"] == preview["plan"]
        assert "private-" not in json.dumps(progress)
        if progress["status"] == "succeeded":
            break
    assert progress["status"] == "succeeded"
    assert progress["profile_id"]
    assert state["builds"] == 1
    assert state["uploads"] == [b"reviewed-worker-source"]
    writes = len([request for request in state["requests"] if request.method != "GET"])
    service.apply(session.session_id, preview["id"])
    assert len([request for request in state["requests"] if request.method != "GET"]) == writes


def test_denied_role_assignment_stops_without_privilege_fallback(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, _, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    state["deny_role"] = True
    with pytest.raises(ResourceError) as failure:
        for _step in range(40):
            service.apply(session.session_id, preview["id"])
    assert failure.value.status_code == 403
    progress = service.get(session.session_id, preview["id"])
    assert progress["status"] == "blocked"
    assert "private-" not in json.dumps(progress)
    assert state["builds"] == 0
    assert not any(request.method == "PUT" and "/jobs/" in request.url.path for request in state["requests"])


def test_untrusted_source_upload_url_never_receives_source_or_token(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, _, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    state["unsafe_upload"] = True
    with pytest.raises(ResourceError):
        for _step in range(40):
            service.apply(session.session_id, preview["id"])
    assert state["uploads"] == []
    assert all(request.url.host == "management.azure.com" for request in state["requests"])


def test_stale_request_cannot_clear_a_new_operation_claim(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, _, clock = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])

    def expire_and_replace(request):
        state.pop("before_request")
        clock[0] += 901
        record, version = service._read(session.principal, preview["id"])
        record["internal"].update(claim="new-worker", busy_until=clock[0] + 900)
        service.store.write(service._key(session.principal, preview["id"]), record, version)
        raise ResourceError("A delayed request failed.", 502)

    state["before_request"] = expire_and_replace
    with pytest.raises(ResourceError):
        service.apply(session.session_id, preview["id"])
    record = service._read(session.principal, preview["id"])[0]
    assert record["internal"]["claim"] == "new-worker"
    assert record["internal"]["busy_until"] > clock[0]
    assert record["status"] == "running"


def test_expired_step_cannot_submit_a_new_resource_write(initializer):
    from tradingagents.workbench.public_profiles import ResourceError
    from tradingagents.workbench.public_store import StoreConflict

    service, session, state, _, clock = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])

    def expire(request):
        state.pop("before_request")
        clock[0] += 901

    state["before_request"] = expire
    with pytest.raises((ResourceError, StoreConflict)):
        service.apply(session.session_id, preview["id"])
    assert all(request.method == "GET" for request in state["requests"])


def advance_to(service, session, preview, step):
    for _step in range(40):
        record = service.apply(session.session_id, preview["id"])
        if record.get("current_step") == step:
            return record
    pytest.fail("Initialization did not reach " + step)


@pytest.mark.parametrize("change", ["sku", "state", "ownership"])
def test_changed_or_unconfirmed_registry_is_not_adopted(initializer, change):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, values, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    advance_to(service, session, preview, "registry")
    registry_id = next(item["id"] for item in preview["plan"]["resources"] if item["kind"] == "registry")
    if change == "sku":
        values[registry_id]["sku"]["name"] = "Premium"
    elif change == "state":
        values[registry_id]["properties"].pop("provisioningState")
    else:
        values[registry_id]["tags"]["tawbOperation"] = "another-operation"
    writes = sum(request.method == "PUT" for request in state["requests"])
    with pytest.raises(ResourceError):
        service.apply(session.session_id, preview["id"])
    assert sum(request.method == "PUT" for request in state["requests"]) == writes


def test_changed_build_trigger_is_not_accepted(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, state, values, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    advance_to(service, session, preview, "build")
    task = next(value for key, value in values.items() if "/taskRuns/" in key)
    task["properties"]["forceUpdateTag"] = "changed-trigger"
    with pytest.raises(ResourceError):
        service.apply(session.session_id, preview["id"])
    assert state["builds"] == 1


def test_build_accepted_before_storage_failure_is_never_repeated(initializer, monkeypatch):
    from tradingagents.workbench.public_store import StoreUnavailable

    service, session, state, _, clock = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    advance_to(service, session, preview, "role-1")
    original_write = service.store.write

    def fail_after_build(key, value, expected_version=None):
        if state["builds"]:
            raise StoreUnavailable("Simulated storage outage")
        return original_write(key, value, expected_version)

    monkeypatch.setattr(service.store, "write", fail_after_build)
    with pytest.raises(StoreUnavailable):
        for _step in range(4):
            service.apply(session.session_id, preview["id"])
    monkeypatch.setattr(service.store, "write", original_write)
    clock[0] += 901
    result = service.apply(session.session_id, preview["id"])
    assert "build" in result["completed_steps"]
    assert state["builds"] == 1


def test_initialization_http_requires_owner_csrf_and_exact_payload(initializer, harness):
    from fastapi.testclient import TestClient

    from tests.test_public_auth import BOB
    from tests.test_public_server import ORIGIN, login
    from tradingagents.workbench.public_server import create_public_app

    service, session, state, _, _ = initializer
    app = create_public_app(auth=service.auth, resource_factory=service.resource_factory, source=service.source)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        assert client.get("/api/initializations").status_code == 401
        client.cookies.set("__Host-tawb-session", session.session_id)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": session.csrf_token}
        assert client.post("/api/initializations", json=INITIALIZE).status_code == 403
        response = client.post("/api/initializations", json=INITIALIZE, headers=headers)
        assert response.status_code == 201
        preview = response.json()
        path = "/api/initializations/" + preview["id"]
        assert client.get(path).json() == preview
        assert "internal" not in preview
        assert client.post(path + "/apply", json={}, headers=headers).status_code == 409
        assert client.post(path + "/confirm", json={"confirmation": preview["confirmation"], "plan": {}}, headers=headers).status_code == 400
        assert client.post(path + "/confirm", json={"confirmation": preview["confirmation"]}, headers=headers).status_code == 200
        assert client.post(path + "/apply", json={"plan": {}}, headers=headers).status_code == 400
        assert client.post(path + "/apply", json={}, headers=headers).status_code == 202
        assert len(client.get("/api/initializations").json()["initializations"]) == 1
        _, provider, _, _ = harness
        provider.claims["oid"] = BOB
        client.cookies.clear()
        login(client)
        before = len(state["requests"])
        bob_headers = {"Origin": ORIGIN, "X-Workbench-Token": client.get("/api/me").json()["csrf_token"]}
        assert client.get(path).status_code == 404
        assert client.post(path + "/apply", json={}, headers=bob_headers).status_code == 404
        assert client.get("/api/initializations").json() == {"initializations": []}
        assert len(state["requests"]) == before


def test_failed_task_resource_without_run_result_stops(initializer):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, _, values, _ = initializer
    preview = service.preview(session.session_id, INITIALIZE)
    service.confirm(session.session_id, preview["id"], preview["confirmation"])
    advance_to(service, session, preview, "build")
    task = next(value for key, value in values.items() if "/taskRuns/" in key)
    task["properties"].update(provisioningState="Failed")
    task["properties"].pop("runResult")
    with pytest.raises(ResourceError):
        service.apply(session.session_id, preview["id"])


def test_oversized_source_is_rejected_before_preview_azure_reads(initializer):
    from tradingagents.workbench.public_profiles import ResourceError
    from tradingagents.workbench.public_source import WorkerSource

    service, session, state, _, _ = initializer
    service.source = WorkerSource(b"x" * service.store.max_bytes)
    with pytest.raises(ResourceError):
        service.preview(session.session_id, INITIALIZE)
    assert state["requests"] == []
