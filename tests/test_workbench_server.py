import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_workbench_azure_control import CONFIG, TEMPLATE, Credential
from tradingagents.workbench.contracts import encode_event

ORIGIN = "http://127.0.0.1:8765"
RESULT = {"decision": "HOLD", "reports": {"market_report": "Market report", "risk_judge_decision": "Risk report"},
          "stats": {"llm_calls": 1, "tool_calls": 2, "input_tokens": 3, "output_tokens": 4}, "connectivity": []}


class FakeAzure:
    def __init__(self):
        self.status = "Running"
        self.lines = []
        self.logs_fail = False
        self.logs_partial = False
        self.status_fail = False
        self.stop_fail = False
        self.stop_completes = True
        self.stop_executions = []
        self.start_fail = False
        self.template_fail = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.start_release.set()
        self.starts = 0
        self.stops = 0
        self.log_queries = 0
        self.logs_entered = threading.Event()
        self.logs_release = threading.Event()
        self.logs_release.set()

    def __call__(self, request):
        if request.url.host == "api.loganalytics.azure.com":
            self.log_queries += 1
            self.logs_entered.set()
            assert self.logs_release.wait(5)
            if self.logs_fail:
                return httpx.Response(503, text="secret-log-service-error")
            body = {"tables": [{"columns": [{"name": "Log_s"}],
                                 "rows": [[line] for line in self.lines]}]}
            if self.logs_partial:
                body["error"] = {"code": "PartialError", "message": "raw-partial-query-secret"}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/start"):
            self.starts += 1
            self.start_entered.set()
            assert self.start_release.wait(5)
            if self.start_fail:
                return httpx.Response(500, text="secret-start-service-error")
            return httpx.Response(202, json={"name": f"execution-{self.starts}"})
        if request.url.path.endswith("/stop"):
            self.stops += 1
            self.stop_executions.append(request.url.path.split("/")[-2])
            if self.stop_fail:
                return httpx.Response(503, text="secret-stop-service-error")
            if self.stop_completes:
                self.status = "Stopped"
            return httpx.Response(202)
        if "/executions/" in request.url.path:
            if self.status_fail:
                return httpx.Response(503, text="secret-status-service-error")
            return httpx.Response(200, json={"properties": {"status": self.status}})
        if self.template_fail:
            return httpx.Response(403, text="secret-template-error")
        return httpx.Response(200, json={"properties": {"template": TEMPLATE}})


@pytest.fixture
def harness(tmp_path):
    from tradingagents.workbench.azure_control import AzureController
    from tradingagents.workbench.server import create_app

    fake = FakeAzure()
    clock = [1800000000.0]
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(CONFIG))
    cloud = AzureController(CONFIG, credential=Credential(), client=httpx.Client(transport=httpx.MockTransport(fake)))
    app = create_app(config_path=config_path, state_dir=tmp_path / "runs", controller=cloud, clock=lambda: clock[0])
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.get("/api/config").json()["csrf_token"]
        headers = {"Origin": ORIGIN, "X-Workbench-Token": token}
        yield client, fake, clock, headers, tmp_path, cloud
    fake.start_release.set()
    cloud.client.close()


def eventually(client, run_id, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = client.get(f"/api/runs/{run_id}").json()
        if predicate(record):
            return record
        threading.Event().wait(0.01)
    pytest.fail(f"Run did not reach expected state: {record}")


def submit(harness, payload=None):
    client, _, _, headers, _, _ = harness
    response = client.post("/api/runs", json=payload or {}, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "starting"
    return eventually(client, response.json()["id"], lambda record: record["status"] == "running")


@pytest.mark.parametrize("script", ["app.js", "ui-core.mjs"])
def test_config_static_and_readiness_without_live_azure(harness, script):
    client, fake, _, _, _, _ = harness
    config = client.get("/api/config").json()
    assert set(config) == {"defaults", "analyst_choices", "vendor_choices", "model", "deployment_ready", "csrf_token"}
    assert config["deployment_ready"] is True
    assert config["model"] == "gpt-5.6-sol"
    assert [choice["value"] for choice in config["analyst_choices"]] == ["market", "social", "news", "fundamentals"]
    assert config["vendor_choices"]["macro_data"] == ["fred", "alpha_vantage"]
    assert len(config["csrf_token"]) >= 32
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get(f"/static/{script}").headers["content-type"].split(";")[0] in {
        "application/javascript", "text/javascript",
    }
    assert fake.starts == fake.log_queries == 0


@pytest.mark.parametrize("headers", [{"Host": "attacker.example:8765"}, {"Host": "127.0.0.1:8766"},
                                    {"Host": "localhost"}, {"Origin": "https://attacker.example"},
                                    {"Origin": "null"}, {"Host": "127.0.0.1.evil:8765"}])
def test_hostile_reads_are_denied(harness, headers):
    client = harness[0]
    assert client.get("/api/config", headers=headers).status_code in (400, 403)


def test_write_requires_matching_origin_and_process_token(harness):
    client, fake, _, headers, _, _ = harness
    for rejected in ({}, {"Origin": ORIGIN}, {"X-Workbench-Token": headers["X-Workbench-Token"]},
                     {**headers, "Origin": "http://localhost:8765"},
                     {**headers, "X-Workbench-Token": "wrong"}):
        assert client.post("/api/runs", json={}, headers=rejected).status_code == 403
    assert fake.starts == 0


@pytest.mark.parametrize("payload", [{"ticker": "../../secret"}, {"environment": {"TOKEN": "secret"}},
                                    {"debate_rounds": 4}, {"endpoint": "https://attacker.example"}, []])
def test_invalid_request_is_rejected_without_cloud_submission(harness, payload):
    client, fake, _, headers, _, _ = harness
    assert client.post("/api/runs", json=payload, headers=headers).status_code == 400
    assert fake.starts == 0


def test_body_limit_and_invalid_json(harness):
    client, fake, _, headers, _, _ = harness
    assert client.post("/api/runs", content=b"x" * 16385, headers=headers).status_code == 413
    assert client.post("/api/runs", content=b"{broken", headers=headers).status_code == 400
    assert fake.starts == 0


def test_start_returns_promptly_and_stop_during_start_cannot_leave_execution_running(harness):
    client, fake, _, headers, _, _ = harness
    fake.start_release.clear()
    try:
        started = time.monotonic()
        response = client.post("/api/runs", json={}, headers=headers)
        assert time.monotonic() - started < 1
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert fake.start_entered.wait(1)
        assert client.post("/api/runs", json={}, headers=headers).status_code == 409
        stopped = client.post(f"/api/runs/{run_id}/stop", headers=headers)
        assert stopped.json()["status"] == "stopped"
        assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    finally:
        fake.start_release.set()
    record = eventually(client, run_id, lambda value: value.get("execution_name") and not value.get("stop_pending"))
    assert record["status"] == "stopped"
    assert fake.starts == fake.stops == 1
    assert client.post(f"/api/runs/{run_id}/stop", headers=headers).json()["status"] == "stopped"
    assert fake.stops == 1


def test_duplicate_run_rejected_and_normal_stop_persisted(harness):
    record = submit(harness)
    client, fake, _, headers, directory, _ = harness
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    assert client.post(f"/api/runs/{record['id']}/stop", headers=headers).status_code == 200
    stopped = eventually(client, record["id"], lambda value: not value.get("stop_pending"))
    assert stopped["status"] == "stopped"
    assert json.loads((directory / "runs" / f"{record['id']}.json").read_text())["status"] == "stopped"
    assert fake.stops == 1


def test_success_waits_for_verified_result_and_polling_is_throttled(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    fake.status = "Succeeded"
    clock[0] += 5
    collected = client.get(f"/api/runs/{record['id']}").json()
    assert collected["status"] == "collecting" and collected["result"] is None
    count = fake.log_queries
    for _ in range(4):
        client.get(f"/api/runs/{record['id']}")
    assert fake.log_queries == count
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    completed = client.get(f"/api/runs/{record['id']}").json()
    assert completed["status"] == "succeeded"
    assert completed["result"] == RESULT


@pytest.mark.parametrize("cloud_status", ["Running", "Failed", "Stopped", "Succeeded"])
def test_analysis_connectivity_persists_and_exports_before_or_after_completion(harness, cloud_status):
    record = submit(harness)
    client, fake, clock, _, directory, _ = harness
    run_id = record["id"]
    rows = [{"provider": "fred", "status": "ok", "detail": "Native observations available.",
             "elapsed_seconds": 0.4}]
    fake.lines = encode_event(run_id, "connectivity", {"connectivity": rows})
    fake.lines += encode_event(run_id, "connectivity", {"connectivity": [None]})
    fake.status = cloud_status
    if cloud_status == "Succeeded":
        fake.lines += encode_event(run_id, "result", {**RESULT, "connectivity": rows})
    clock[0] += 5
    updated = client.get(f"/api/runs/{run_id}").json()
    assert updated["status"] == cloud_status.lower()
    if cloud_status != "Succeeded":
        assert updated["result"] is None
    saved = json.loads((directory / "runs" / f"{run_id}.json").read_text())
    assert saved["events"][0]["payload"]["connectivity"] == rows
    download = client.get(f"/api/runs/{run_id}/download?format=json").json()
    assert download["events"][0]["payload"]["connectivity"] == rows
    markdown = client.get(f"/api/runs/{run_id}/download?format=markdown")
    assert markdown.status_code == 200
    assert "## Connectivity" in markdown.text
    assert "fred: ok" in markdown.text and "Native observations available." in markdown.text


def test_partial_log_warning_reaches_api_without_discarding_complete_result(harness):
    record = submit(harness)
    client, fake, clock, _, directory, _ = harness
    fake.status = "Succeeded"
    fake.logs_partial = True
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    completed = client.get(f"/api/runs/{record['id']}").json()
    assert completed["result"] == RESULT and completed["status"] == "succeeded"
    assert "partial" in completed["log_warning"].lower()
    assert completed["error"] is None and completed["poll_error"] is None
    assert "raw-partial-query-secret" not in (directory / "runs" / f"{record['id']}.json").read_text()
    fake.logs_partial = False
    clock[0] += 5
    recovered = client.get(f"/api/runs/{record['id']}").json()
    assert recovered["log_warning"] is None
    assert recovered["result"] == RESULT and recovered["status"] == "succeeded"


def test_stop_status_poll_failure_stays_visible_and_reserved(harness):
    record = submit(harness)
    client, fake, clock, headers, _, _ = harness
    fake.stop_completes = False
    fake.status_fail = True
    service = client.app.state.service
    service.request_stop(record["id"])
    service.stop(record["id"])
    clock[0] += 5
    pending = client.get(f"/api/runs/{record['id']}").json()
    assert pending["stop_pending"] is True and pending["poll_error"]
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    fake.status_fail = False
    fake.status = "Stopped"
    clock[0] += 5
    completed = client.get(f"/api/runs/{record['id']}").json()
    assert completed["stop_pending"] is False and completed["poll_error"] is None


def test_missing_logs_timeout_after_ten_minutes(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    fake.status = "Succeeded"
    clock[0] += 5
    assert client.get(f"/api/runs/{record['id']}").json()["status"] == "collecting"
    clock[0] += 599
    assert client.get(f"/api/runs/{record['id']}").json()["status"] == "collecting"
    clock[0] += 5
    expired = client.get(f"/api/runs/{record['id']}").json()
    assert expired["status"] == "failed"
    assert "10 minutes" in expired["error"]


def test_transient_log_and_status_errors_retry_and_never_persist_raw_errors(harness):
    record = submit(harness)
    client, fake, clock, _, directory, _ = harness
    fake.logs_fail = fake.status_fail = True
    clock[0] += 5
    failed_poll = client.get(f"/api/runs/{record['id']}").json()
    assert failed_poll["status"] == "running"
    assert failed_poll.get("poll_error")
    assert "secret" not in (directory / "runs" / f"{record['id']}.json").read_text()
    fake.logs_fail = fake.status_fail = False
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    recovered = client.get(f"/api/runs/{record['id']}").json()
    assert recovered["status"] == "succeeded"
    assert not recovered.get("poll_error")


def test_failed_stop_remains_reserved_and_retries(harness):
    record = submit(harness)
    client, fake, clock, headers, _, _ = harness
    fake.stop_fail = True
    client.post(f"/api/runs/{record['id']}/stop", headers=headers)
    eventually(client, record["id"], lambda value: bool(value["error"]))
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    fake.stop_fail = False
    clock[0] += 5
    recovered = client.get(f"/api/runs/{record['id']}").json()
    assert recovered["status"] == "stopped" and not recovered["stop_pending"]


@pytest.mark.parametrize("terminal", ["Succeeded", "Failed", "Stopped"])
def test_accepted_stop_reserves_until_cloud_terminal_without_resurrection(harness, terminal):
    record = submit(harness)
    client, fake, clock, headers, _, _ = harness
    fake.stop_completes = False
    service = client.app.state.service
    service.request_stop(record["id"])
    service.stop(record["id"])
    stopped = client.get(f"/api/runs/{record['id']}").json()
    assert fake.status == "Running"
    assert stopped["status"] == "stopped" and stopped["stop_pending"] is True
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    clock[0] += 5
    assert client.get(f"/api/runs/{record['id']}").json()["stop_pending"] is True
    assert fake.stops == 1
    assert fake.stop_executions == [record["execution_name"]]
    fake.status = terminal
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    terminal_record = client.get(f"/api/runs/{record['id']}").json()
    assert terminal_record["status"] == "stopped"
    assert terminal_record["stop_pending"] is False
    assert terminal_record["result"] == RESULT
    assert client.post("/api/runs", json={}, headers=headers).status_code == 202


def test_natural_completion_releases_reservation_after_stop_error(harness):
    record = submit(harness)
    client, fake, clock, headers, _, _ = harness
    fake.stop_fail = True
    service = client.app.state.service
    service.request_stop(record["id"])
    service.stop(record["id"])
    assert service.get(record["id"])["stop_pending"] is True
    assert service.get(record["id"])["error"]
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409
    fake.status = "Succeeded"
    clock[0] += 5
    recovered = client.get(f"/api/runs/{record['id']}").json()
    assert recovered["status"] == "stopped"
    assert recovered["stop_pending"] is False and recovered["error"] is None
    assert client.post("/api/runs", json={}, headers=headers).status_code == 202


def test_reopen_recovers_cloud_result_without_browser_polling(harness):
    from tradingagents.workbench.server import create_app

    record = submit(harness)
    client, fake, clock, _, directory, cloud = harness
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 3600
    app = create_app(config_path=directory / "deployment.json", state_dir=directory / "runs",
                     controller=cloud, clock=lambda: clock[0])
    with TestClient(app, base_url=ORIGIN) as reopened:
        loaded = reopened.get(f"/api/runs/{record['id']}").json()
        assert loaded["status"] == "succeeded"
        assert loaded["result"] == RESULT
        assert reopened.get("/api/config").json()["csrf_token"] != client.get("/api/config").json()["csrf_token"]


def test_downloads_include_all_reports_and_safe_attachment_names(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "report", {"section": "news_report", "content": "Earlier news report"})
    fake.lines += encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    downloaded = client.get(f"/api/runs/{record['id']}/download?format=json")
    assert downloaded.status_code == 200
    assert downloaded.json()["result"] == RESULT
    markdown = client.get(f"/api/runs/{record['id']}/download?format=markdown")
    assert markdown.status_code == 200
    for text in ("Market report", "Risk report", "Earlier news report", "HOLD"):
        assert text in markdown.text
    assert f"workbench-{record['id']}.md" in markdown.headers["content-disposition"]
    assert client.get(f"/api/runs/{record['id']}/download?format=html").status_code == 400


def test_corrupt_result_cannot_complete_and_failed_execution_stays_terminal(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", {"reports": "wrong"})
    clock[0] += 5
    assert client.get(f"/api/runs/{record['id']}").json()["status"] == "collecting"
    fake.status = "Failed"
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    failed = client.get(f"/api/runs/{record['id']}").json()
    assert failed["status"] == "failed"
    assert failed["result"] == RESULT


def test_unknown_or_invalid_id_and_missing_deployment(tmp_path):
    from tradingagents.workbench.server import create_app

    app = create_app(config_path=tmp_path / "missing.json", state_dir=tmp_path / "runs")
    with TestClient(app, base_url=ORIGIN) as client:
        config = client.get("/api/config").json()
        assert config["deployment_ready"] is False
        headers = {"Origin": ORIGIN, "X-Workbench-Token": config["csrf_token"]}
        assert client.post("/api/runs", json={}, headers=headers).status_code == 503
        assert client.get("/api/runs/not-a-uuid").status_code == 400
        assert client.get(f"/api/runs/{uuid4()}").status_code == 404


def test_history_is_lightweight_and_refresh_does_not_block(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", RESULT)
    clock[0] += 5
    assert client.get(f"/api/runs/{record['id']}").json()["result"] == RESULT
    summary = client.get("/api/runs").json()["runs"][0]
    assert summary["status"] == "succeeded"
    assert summary["events"] == [] and summary["result"] is None
    assert summary["has_result"] is True


def test_failed_start_is_not_retried_or_replaced_with_duplicate_execution(harness):
    client, fake, clock, headers, directory, _ = harness
    fake.start_fail = True
    response = client.post("/api/runs", json={}, headers=headers)
    run_id = response.json()["id"]
    failed = eventually(client, run_id, lambda record: record["status"] == "failed")
    assert "could not be confirmed" in failed["error"]
    assert failed["start_uncertain"] is True
    assert "secret" not in (directory / "runs" / f"{run_id}.json").read_text()
    clock[0] += 30
    client.get(f"/api/runs/{run_id}")
    assert fake.starts == 1
    assert client.post("/api/runs", json={}, headers=headers).status_code == 409


def test_pre_submission_failure_allows_retry_after_access_recovers(harness):
    client, fake, _, headers, _, _ = harness
    fake.template_fail = True
    response = client.post("/api/runs", json={}, headers=headers)
    failed = eventually(client, response.json()["id"], lambda record: record["status"] == "failed")
    assert failed["start_uncertain"] is False
    assert fake.starts == 0
    fake.template_fail = False
    assert submit(harness)["status"] == "running"


def test_startup_recovers_ambiguous_start_and_ignores_broken_record(harness):
    from tradingagents.workbench.server import create_app

    record = submit(harness)
    _, _, clock, _, directory, cloud = harness
    record.update(status="starting", execution_name=None, start_pending=True, start_attempted=True)
    (directory / "runs" / f"{record['id']}.json").write_text(json.dumps(record))
    (directory / "runs" / f"{uuid4()}.json").write_text("{broken")
    app = create_app(config_path=directory / "deployment.json", state_dir=directory / "runs",
                     controller=cloud, clock=lambda: clock[0])
    with TestClient(app, base_url=ORIGIN) as reopened:
        history = reopened.get("/api/runs").json()["runs"]
        assert len(history) == 1 and history[0]["status"] == "failed"
        assert history[0]["start_uncertain"] is True


def test_connectivity_result_accepts_null_decision(harness):
    record = submit(harness, {"mode": "connectivity"})
    client, fake, clock, _, _, _ = harness
    result = {**RESULT, "decision": None, "reports": {}, "connectivity": [
        {"provider": "fred", "status": "unconfigured", "detail": "No key configured", "elapsed_seconds": 0},
    ]}
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", result)
    clock[0] += 5
    completed = client.get(f"/api/runs/{record['id']}").json()
    assert completed["status"] == "succeeded" and completed["result"] == result


def test_chunked_oversize_body_is_rejected_without_content_length(harness):
    client, fake, _, headers, _, _ = harness
    response = client.post("/api/runs", content=iter([b"x" * 9000, b"x" * 9000]), headers=headers)
    assert response.status_code == 413
    assert fake.starts == 0


def test_worker_error_event_never_persists_raw_error_text(harness):
    record = submit(harness)
    client, fake, clock, _, directory, _ = harness
    fake.lines = encode_event(record["id"], "error", {"message": "secret-raw-sdk-error"})
    fake.status = "Failed"
    clock[0] += 5
    assert client.get(f"/api/runs/{record['id']}").json()["status"] == "failed"
    assert "secret-raw-sdk" not in (directory / "runs" / f"{record['id']}.json").read_text()


def test_main_binds_loopback_and_enforces_selected_port(tmp_path, monkeypatch):
    import uvicorn

    from tradingagents.workbench import server

    monkeypatch.setattr(server, "STATE_ROOT", tmp_path)
    monkeypatch.delenv("TRADINGAGENTS_WORKBENCH_CONFIG", raising=False)

    def run(app, **options):
        assert options["host"] == "127.0.0.1" and options["port"] == 8877
        assert options["access_log"] is False
        with TestClient(app, base_url="http://localhost:8877") as client:
            assert client.get("/api/config").status_code == 200
            assert client.get("/api/config", headers={"Host": "localhost:8765"}).status_code == 400

    monkeypatch.setattr(uvicorn, "run", run)
    server.main(["--port", "8877"])


def test_stale_success_poll_cannot_overwrite_explicit_stop(harness):
    record = submit(harness)
    client, fake, clock, headers, _, _ = harness
    fake.status = "Succeeded"
    fake.lines = encode_event(record["id"], "result", RESULT)
    fake.logs_entered.clear()
    fake.logs_release.clear()
    clock[0] += 5
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.get, f"/api/runs/{record['id']}")
        try:
            assert fake.logs_entered.wait(1)
            stopped = client.post(f"/api/runs/{record['id']}/stop", headers=headers)
            assert stopped.json()["status"] == "stopped"
            assert client.post("/api/runs", json={}, headers=headers).status_code == 409
        finally:
            fake.logs_release.set()
        assert pending.result(timeout=3).json()["status"] == "stopped"
    final = eventually(client, record["id"], lambda value: not value["stop_pending"])
    assert final["status"] == "stopped" and fake.stops == 1


@pytest.mark.parametrize("attempted", [True, False])
def test_restart_after_stop_during_start_has_explicit_recovery_state(harness, attempted):
    from tradingagents.workbench.server import create_app

    record = submit(harness)
    _, _, clock, _, directory, cloud = harness
    record.update(status="stopped", execution_name=None, start_pending=True, stop_pending=True,
                  stop_requested=True, start_attempted=attempted)
    (directory / "runs" / f"{record['id']}.json").write_text(json.dumps(record))
    app = create_app(config_path=directory / "deployment.json", state_dir=directory / "runs",
                     controller=cloud, clock=lambda: clock[0])
    with TestClient(app, base_url=ORIGIN) as reopened:
        recovered = reopened.get(f"/api/runs/{record['id']}").json()
        assert recovered["status"] == "stopped"
        assert recovered["stop_pending"] is False
        assert recovered["start_uncertain"] is attempted
        assert bool(recovered["error"]) is attempted


def test_history_returns_immediately_while_log_refresh_is_blocked(harness):
    record = submit(harness)
    client, fake, clock, _, _, _ = harness
    clock[0] += 5
    fake.logs_entered.clear()
    fake.logs_release.clear()
    try:
        before = time.monotonic()
        history = client.get("/api/runs")
        assert time.monotonic() - before < 1
        assert history.json()["runs"][0]["id"] == record["id"]
        assert fake.logs_entered.wait(1)
    finally:
        fake.logs_release.set()
