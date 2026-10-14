import copy
import json
from uuid import uuid4

import httpx
import pytest

from tests.test_public_auth import BOB, TENANT, auth_harness as auth_harness, sign_in
from tests.test_public_profiles import (
    ALICE_PRINCIPAL,
    ENVIRONMENT,
    JOB,
    PAYLOAD,
    Credential,
    resources as resources,
)
from tradingagents.workbench.public_auth import AuthenticationRequired


def test_public_execution_uses_verified_model_without_modifying_job(resources):
    from tradingagents.workbench.public_runs import DelegatedAzureController

    cloud, profiles, values, _ = resources
    profile = profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    job = copy.deepcopy(values[JOB])
    job["properties"]["template"]["containers"][0]["env"].append({"name": "FRED_API_KEY", "secretRef": "fred-key"})
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer delegated-only"
        if request.method == "GET":
            return httpx.Response(200, json=job)
        assert request.method == "POST" and request.url.path == JOB + "/start"
        body = json.loads(request.content)
        env = {entry["name"]: entry for entry in body["containers"][0]["env"]}
        assert env["AZURE_OPENAI_DEPLOYMENT_NAME"]["value"] == "research-model"
        assert env["AZURE_OPENAI_ENDPOINT"]["value"] == "https://research-ai.services.ai.azure.com/"
        assert env["AZURE_OPENAI_AUTH_MODE"]["value"] == "managed_identity"
        assert env["FRED_API_KEY"] == {"name": "FRED_API_KEY", "secretRef": "fred-key"}
        assert "delegated-only" not in request.content.decode()
        return httpx.Response(202, json={"name": "research-job-execution"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        controller = DelegatedAzureController(profile, credential=Credential(), client=client)
        assert controller.start_run(str(uuid4()), {}) == "research-job-execution"
    assert [request.method for request in calls] == ["GET", "POST"]


@pytest.mark.parametrize("failure", ["consent", "rbac", "changed_environment", "changed_image"])
def test_delegated_start_fails_before_execution_when_unauthorized_or_snapshot_changed(resources, failure):
    from tradingagents.workbench.public_profiles import ResourceError
    from tradingagents.workbench.public_runs import DelegatedAzureController

    cloud, profiles, values, _ = resources
    profile = profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    job = copy.deepcopy(values[JOB])
    if failure == "changed_environment":
        job["properties"]["environmentId"] = ENVIRONMENT + "-changed"
    if failure == "changed_image":
        job["properties"]["template"]["containers"][0]["image"] = "different.azurecr.io/worker:latest"
    credential = Credential()
    if failure == "consent":
        def denied(scope):
            raise AuthenticationRequired(scope=scope)
        credential.get_token = denied

    def respond(request):
        assert request.method == "GET"
        return httpx.Response(403 if failure == "rbac" else 200, json=job)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        controller = DelegatedAzureController(profile, credential=credential, client=client)
        with pytest.raises(AuthenticationRequired if failure == "consent" else ResourceError):
            controller.start_run(str(uuid4()), {})


@pytest.fixture
def runs(resources, harness):
    from tradingagents.workbench.public_profiles import AzureResources
    from tradingagents.workbench.public_runs import DelegatedAzureController, PublicRuns

    cloud, profiles, values, _ = resources
    auth, provider, clock, _ = harness
    provider.token_result["access_token"] = "delegated-only"
    _, session = sign_in(auth)
    profile = profiles.save(ALICE_PRINCIPAL, PAYLOAD, cloud)
    state = {"status": "Running", "lines": [], "starts": 0, "stops": 0, "uncertain": False, "targets": []}

    def respond(request):
        state["targets"].append(str(request.url))
        assert request.headers["authorization"] == "Bearer delegated-only"
        if request.url.host == "api.loganalytics.azure.com":
            return httpx.Response(200, json={"tables": [{"columns": [{"name": "Log_s"}],
                                                        "rows": [[line] for line in state["lines"]]}]})
        if request.url.path.endswith("/start"):
            state["starts"] += 1
            if state["uncertain"]:
                raise httpx.ReadTimeout("private-network-details", request=request)
            if state.get("deny_start"):
                return httpx.Response(403)
            return httpx.Response(202, json={"name": "research-job-execution"})
        if request.url.path.endswith("/stop"):
            state["stops"] += 1
            return httpx.Response(202)
        if "/executions/" in request.url.path:
            return httpx.Response(200, json={"properties": {"status": state["status"], "template": {
                "containers": [{"name": "worker", "env": [{"name": "TRADINGAGENTS_RUN_ID",
                                                             "value": state.get("execution_run_id", "different-run")}]}]}}})
        return httpx.Response(200, json=copy.deepcopy(values[JOB]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def factory(snapshot, credential):
            return DelegatedAzureController(snapshot, credential=credential, client=client)

        def resource_factory(principal, credential):
            return AzureResources(principal, credential, client=cloud.client)

        service = PublicRuns(auth=auth, profiles=profiles, controller_factory=factory,
                             resource_factory=resource_factory, clock=lambda: clock[0])
        yield service, session, profile, state, auth, provider, clock


def submit(service, session, profile, submission_id=None):
    return service.create(session.session_id, profile["id"], {}, submission_id or str(uuid4()))


def test_submission_is_idempotent_and_reserves_one_job(runs):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, state, _, _, _ = runs
    submission_id = str(uuid4())
    first = submit(service, session, profile, submission_id)
    assert first["status"] == "running"
    assert submit(service, session, profile, submission_id)["id"] == first["id"]
    assert state["starts"] == 1
    with pytest.raises(ResourceError) as failure:
        submit(service, session, profile)
    assert failure.value.status_code == 409
    assert state["starts"] == 1


def test_run_snapshot_and_history_survive_a_service_restart(runs):
    from tradingagents.workbench.contracts import encode_event
    from tradingagents.workbench.public_runs import PublicRuns

    service, session, profile, state, auth, _, clock = runs
    first = submit(service, session, profile)
    profile_record, version = service.profiles.store.read(session.principal.owner_key + "/profiles")
    profile_record["profiles"][0]["config"]["job_name"] = "changed-job"
    service.profiles.store.write(session.principal.owner_key + "/profiles", profile_record, version)
    restarted = PublicRuns(auth=auth, profiles=service.profiles, controller_factory=service.controller_factory,
                           resource_factory=service.resource_factory, clock=lambda: clock[0])
    assert first["id"] in [record["id"] for record in restarted.list(session.session_id)]
    result = {"decision": "Hold", "reports": {"market_report": "Verified report"},
              "stats": {"llm_calls": 1, "tool_calls": 0, "input_tokens": 10, "output_tokens": 5}, "connectivity": []}
    state["status"] = "Succeeded"
    collected = restarted.refresh(session.session_id, first["id"])
    assert collected["status"] == "collecting"
    state["lines"] = encode_event(first["id"], "result", result)
    clock[0] += 6
    completed = restarted.refresh(session.session_id, first["id"])
    assert completed["status"] == "succeeded"
    assert completed["result"] == result
    assert all("changed-job" not in target for target in state["targets"])


def test_uncertain_start_remains_reserved_after_restart(runs):
    from tradingagents.workbench.public_profiles import ResourceError
    from tradingagents.workbench.public_runs import PublicRuns

    service, session, profile, state, auth, _, _ = runs
    state["uncertain"] = True
    first = submit(service, session, profile)
    assert first["start_uncertain"] is True
    assert "private-" not in json.dumps(first)
    restarted = PublicRuns(auth=auth, profiles=service.profiles, controller_factory=service.controller_factory,
                           resource_factory=service.resource_factory)
    with pytest.raises(ResourceError):
        submit(restarted, session, profile)
    with pytest.raises(ResourceError):
        restarted.stop(session.session_id, first["id"])
    assert state["starts"] == 1
    assert state["stops"] == 0


def test_other_user_cannot_read_refresh_stop_or_list_a_run(runs):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, state, auth, provider, _ = runs
    first = submit(service, session, profile)
    provider.claims["oid"] = BOB
    provider.accounts = [{"local_account_id": BOB, "realm": TENANT}]
    _, bob = sign_in(auth)
    assert service.list(bob.session_id) == []
    count = len(state["targets"])
    for operation in (service.get, service.refresh, service.stop):
        with pytest.raises(ResourceError) as failure:
            operation(bob.session_id, first["id"])
        assert failure.value.status_code == 404
    assert len(state["targets"]) == count


def test_stop_keeps_reservation_until_azure_confirms_terminal_state(runs):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, state, _, _, clock = runs
    first = submit(service, session, profile)
    stopped = service.stop(session.session_id, first["id"])
    assert stopped["stop_pending"] is True
    with pytest.raises(ResourceError):
        submit(service, session, profile)
    state["status"] = "Stopped"
    clock[0] += 6
    final = service.refresh(session.session_id, first["id"])
    assert final["status"] == "stopped"
    assert final["stop_pending"] is False
    assert submit(service, session, profile)["id"] != first["id"]
    assert state["stops"] == 1


def test_reconciliation_requires_matching_azure_execution_run_id(runs):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, profile, state, _, _, _ = runs
    state["uncertain"] = True
    first = submit(service, session, profile)
    with pytest.raises(ResourceError):
        service.reconcile(session.session_id, first["id"], "research-job-execution")
    assert service.get(session.session_id, first["id"])["start_uncertain"] is True
    state["execution_run_id"] = first["id"]
    resumed = service.reconcile(session.session_id, first["id"], "research-job-execution")
    assert resumed["start_uncertain"] is False
    assert resumed["execution_name"] == "research-job-execution"
    assert state["starts"] == 1
    assert service.stop(session.session_id, first["id"])["stop_pending"] is True


def test_public_run_http_and_exports_reject_another_user(runs):
    from fastapi.testclient import TestClient

    from tests.test_public_server import ORIGIN
    from tradingagents.workbench.public_server import SESSION_COOKIE, create_public_app

    service, session, profile, state, auth, provider, _ = runs
    auth.store.write(session.principal.owner_key + "/profiles", {"profiles": [profile]})
    app = create_public_app(auth=auth, resource_factory=service.resource_factory,
                            controller_factory=service.controller_factory)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, session.session_id)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": session.csrf_token}
        payload = {"profile_id": profile["id"], "submission_id": str(uuid4()), "request": {}}
        response = client.post("/api/runs", json=payload, headers=headers)
        assert response.status_code == 202
        first = response.json()
        duplicate = client.post("/api/runs", json=payload, headers=headers)
        assert duplicate.json()["id"] == first["id"]
        assert state["starts"] == 1
        exported = client.get(f"/api/runs/{first['id']}/download?format=json")
        assert exported.status_code == 200
        assert exported.json()["id"] == first["id"]
        assert "delegated-only" not in exported.text
        assert client.get(f"/api/runs/{first['id']}/download?format=markdown").status_code == 200
        provider.claims["oid"] = BOB
        provider.accounts = [{"local_account_id": BOB, "realm": TENANT}]
        _, bob = sign_in(auth)
        client.cookies.set(SESSION_COOKIE, bob.session_id)
        headers["X-Workbench-Token"] = bob.csrf_token
        assert client.get("/api/runs").json() == {"runs": []}
        assert client.get(f"/api/runs/{first['id']}").status_code == 404
        assert client.get(f"/api/runs/{first['id']}/download").status_code == 404
        assert client.post(f"/api/runs/{first['id']}/stop", headers=headers).status_code == 404
        assert client.post(f"/api/runs/{first['id']}/reconcile", json={"execution_name": "research-job-execution"}, headers=headers).status_code == 404


def test_crash_after_azure_accepts_start_never_reposts_the_submission(runs, monkeypatch):
    from tradingagents.workbench.public_store import StoreUnavailable

    service, session, profile, state, _, _, clock = runs
    original_write = service.store.backend.write
    submission_id = str(uuid4())

    def fail_execution_persistence(key, content, expected_version=None):
        if "/runs/" in key and state["starts"] == 1:
            raise StoreUnavailable("Simulated storage outage")
        return original_write(key, content, expected_version)

    monkeypatch.setattr(service.store.backend, "write", fail_execution_persistence)
    with pytest.raises(StoreUnavailable):
        submit(service, session, profile, submission_id)
    monkeypatch.setattr(service.store.backend, "write", original_write)
    retry = submit(service, session, profile, submission_id)
    assert retry["execution_name"] is None
    assert state["starts"] == 1
    clock[0] += 181
    recovered = service.refresh(session.session_id, retry["id"])
    assert recovered["start_uncertain"] is True
    assert recovered["start_pending"] is False
    assert recovered["status"] == "failed"
    assert state["starts"] == 1


def test_job_reservation_is_shared_across_users_and_case_variants(runs, resources):
    from tests.test_public_profiles import BOB_PRINCIPAL
    from tradingagents.workbench.public_profiles import AzureResources, ResourceError

    service, session, profile, state, auth, provider, _ = runs
    submit(service, session, profile)
    cloud, _, _, _ = resources
    bob_cloud = AzureResources(BOB_PRINCIPAL, Credential(), client=cloud.client)
    bob_profile = service.profiles.save(BOB_PRINCIPAL, PAYLOAD, bob_cloud)
    record, version = service.profiles.store.read(BOB_PRINCIPAL.owner_key + "/profiles")
    record["profiles"][0]["job_id"] = record["profiles"][0]["job_id"].upper()
    service.profiles.store.write(BOB_PRINCIPAL.owner_key + "/profiles", record, version)
    provider.claims["oid"] = BOB
    provider.accounts = [{"local_account_id": BOB, "realm": TENANT}]
    _, bob = sign_in(auth)
    with pytest.raises(ResourceError) as failure:
        submit(service, bob, bob_profile)
    assert failure.value.status_code == 409
    assert state["starts"] == 1


def test_stop_repairs_reservation_when_terminal_state_was_saved_before_crash(runs, monkeypatch):
    from tradingagents.workbench.public_store import StoreUnavailable

    service, session, profile, state, _, _, _ = runs
    first = submit(service, session, profile)
    state["status"] = "Stopped"
    original_delete = service.store.backend.delete

    def fail_release(key, expected_version):
        raise StoreUnavailable("Simulated release outage")

    monkeypatch.setattr(service.store.backend, "delete", fail_release)
    with pytest.raises(StoreUnavailable):
        service.refresh(session.session_id, first["id"])
    monkeypatch.setattr(service.store.backend, "delete", original_delete)
    assert service.stop(session.session_id, first["id"])["status"] == "stopped"
    assert service.store.read(service._reservation_key(profile)) is None


def test_new_submission_recovers_a_definite_failure_even_if_history_update_failed(runs, monkeypatch):
    from tradingagents.workbench.public_store import StoreUnavailable

    service, session, profile, state, _, _, _ = runs
    original_write = service.store.backend.write
    state["deny_start"] = True

    def fail_history_update(key, content, expected_version=None):
        if key.endswith("/run-index") and state["starts"] == 1:
            raise StoreUnavailable("Simulated index outage")
        return original_write(key, content, expected_version)

    monkeypatch.setattr(service.store.backend, "write", fail_history_update)
    with pytest.raises(StoreUnavailable):
        submit(service, session, profile)
    monkeypatch.setattr(service.store.backend, "write", original_write)
    state["deny_start"] = False
    assert submit(service, session, profile)["status"] == "running"
    assert state["starts"] == 2


@pytest.mark.parametrize("status", ["Failed", "Stopped"])
def test_terminal_execution_keeps_collecting_late_reports_within_ingestion_window(runs, status):
    from tradingagents.workbench.contracts import encode_event

    service, session, profile, state, _, _, clock = runs
    first = submit(service, session, profile)
    state["status"] = status
    early = service.refresh(session.session_id, first["id"])
    assert early["events"] == []
    assert service.store.read(service._reservation_key(profile)) is None
    state["lines"] = encode_event(first["id"], "report", {"section": "market_report", "content": "Late verified report"})
    clock[0] += 6
    late = service.refresh(session.session_id, first["id"])
    assert late["events"][0]["payload"]["content"] == "Late verified report"
    count = len(state["targets"])
    clock[0] += 601
    service.refresh(session.session_id, first["id"])
    assert len(state["targets"]) == count
