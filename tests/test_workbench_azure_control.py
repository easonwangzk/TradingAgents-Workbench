import base64
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tradingagents.workbench.contracts import default_request, encode_event

CONFIG = {
    "subscription_id": "11111111-1111-4111-8111-111111111111",
    "resource_group": "rg-workbench",
    "job_name": "workbench-job",
    "workspace_id": "22222222-2222-4222-8222-222222222222",
    "model": "gpt-5.6-sol",
    "endpoint": "https://test.services.ai.azure.com/",
}
TEMPLATE = {
    "containers": [{
        "name": "worker", "image": "registry.azurecr.io/worker:latest",
        "env": [{"name": "ALPHA_VANTAGE_API_KEY", "secretRef": "alpha"},
                {"name": "AZURE_CLIENT_ID", "value": "managed-identity"},
                {"name": "TRADINGAGENTS_RUN_ID", "value": "old"}],
        "resources": {"cpu": 2, "memory": "4Gi"},
        "command": ["python", "-m", "tradingagents.workbench.worker"],
        "provisioningState": "Succeeded",
    }],
    "initContainers": [{"name": "init", "image": "registry.azurecr.io/init:latest",
                        "env": [{"name": "KEEP", "value": "yes"}]}],
    "provisioningState": "Succeeded",
}


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token="boundary-secret-token")


def controller(handler, config=None):
    from tradingagents.workbench.azure_control import AzureController

    credential = Credential()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AzureController(config or CONFIG, credential=credential, client=client), credential


def test_start_posts_execution_template_not_job_and_preserves_secrets():
    run_id = str(uuid4())
    template = copy.deepcopy(TEMPLATE)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer boundary-secret-token"
        assert request.extensions["timeout"]["read"] == 30
        assert request.url.params["api-version"] == "2024-03-01"
        if request.method == "GET":
            return httpx.Response(200, json={"properties": {"template": template},
                                             "identity": {"type": "UserAssigned"}})
        assert request.method == "POST" and request.url.path.endswith("/jobs/workbench-job/start")
        body = json.loads(request.content)
        assert set(body) == {"containers", "initContainers"}
        worker = body["containers"][0]
        assert "provisioningState" not in worker
        assert worker["resources"] == {"cpu": 2, "memory": "4Gi"}
        assert worker["image"] == "registry.azurecr.io/worker:latest"
        env = {entry["name"]: entry for entry in worker["env"]}
        assert len(worker["env"]) == 4
        assert env["ALPHA_VANTAGE_API_KEY"] == {"name": "ALPHA_VANTAGE_API_KEY", "secretRef": "alpha"}
        assert env["AZURE_CLIENT_ID"]["value"] == "managed-identity"
        assert env["TRADINGAGENTS_RUN_ID"]["value"] == run_id
        assert json.loads(base64.b64decode(env["TRADINGAGENTS_RUN_REQUEST"]["value"])) == default_request()
        assert body["initContainers"] == TEMPLATE["initContainers"]
        return httpx.Response(202, json={"name": "workbench-job-execution"})

    cloud, credential = controller(handle)
    assert cloud.start_run(run_id, {}) == "workbench-job-execution"
    assert template == TEMPLATE
    assert len(calls) == 2
    assert set(credential.scopes) == {"https://management.azure.com/.default"}


def test_execution_status_and_stop_use_job_execution_contract():
    def handle(request):
        if request.method == "GET":
            assert request.url.path.endswith("/jobs/workbench-job/executions/execution-1")
            return httpx.Response(200, json={"properties": {"status": "Running"}})
        assert request.url.path.endswith("/jobs/workbench-job/executions/execution-1/stop")
        assert "executionName" not in request.url.params
        assert request.url.params["api-version"] == "2024-03-01"
        assert not request.content
        return httpx.Response(202)

    cloud, _ = controller(handle)
    assert cloud.get_status("execution-1") == "Running"
    assert cloud.stop_run("execution-1") is None


def test_logs_use_workspace_customer_id_scope_and_bounded_schema_safe_query():
    run_id = str(uuid4())
    lines = encode_event(run_id, "phase", {"stage": "analysis"})

    def handle(request):
        assert str(request.url) == f"https://api.loganalytics.azure.com/v1/workspaces/{CONFIG['workspace_id']}/query"
        query = json.loads(request.content)["query"]
        for required in ("ContainerAppConsoleLogs_CL", "column_ifexists", "JobName_s", "ContainerJobName_s",
                         "ContainerAppName_s", "ago(2d)", "startswith 'TAWB1 '", "project Log_s", "10000", run_id):
            assert required in query
        return httpx.Response(200, json={"tables": [{"columns": [{"name": "Log_s", "type": "string"}],
                                                    "rows": [[line] for line in lines]}]})

    cloud, credential = controller(handle)
    assert cloud.get_events(run_id)[0]["payload"] == {"stage": "analysis"}
    assert credential.scopes == ["https://api.loganalytics.io/.default"]


@pytest.mark.parametrize(("since", "until"), [
    ("2026-09-11T15:47:42.748189Z", "2026-09-11T16:57:42.748189Z"),
    ("2026-09-11T23:47:42.748189+08:00", "2026-09-11T12:57:42.748189-04:00"),
    (datetime(2026, 9, 11, 15, 47, 42, 748189, tzinfo=timezone.utc),
     datetime(2026, 9, 11, 16, 57, 42, 748189, tzinfo=timezone.utc)),
])
def test_logs_use_historical_utc_window_and_retain_run_isolation(since, until):
    run_id = str(uuid4())
    lines = encode_event(run_id, "phase", {"stage": "analysis"})
    lines += encode_event(str(uuid4()), "phase", {"stage": "other-run"})

    def handle(request):
        query = json.loads(request.content)["query"]
        assert "TimeGenerated between (datetime(2026-09-11T15:47:42.748189Z) .. datetime(2026-09-11T16:57:42.748189Z))" in query
        assert "ago(" not in query and "now(" not in query and "ingestion_time(" not in query
        assert "| where WorkbenchJob == 'workbench-job'" in query
        assert f"| where Log_s has '{run_id}' and Log_s startswith 'TAWB1 '" in query
        assert "| order by TimeGenerated asc | project Log_s | limit 10000" in query
        return httpx.Response(200, json={"tables": [{
            "columns": [{"name": "Log_s", "type": "string"}],
            "rows": [[line] for line in lines],
        }]})

    cloud, credential = controller(handle)
    assert [event["payload"] for event in cloud.get_events(run_id, since=since, until=until)] == [
        {"stage": "analysis"},
    ]
    assert credential.scopes == ["https://api.loganalytics.io/.default"]


@pytest.mark.parametrize(("since", "until"), [
    pytest.param("2026-09-11T15:47:42Z", None, id="missing-end"),
    pytest.param(None, "2026-09-11T16:57:42Z", id="missing-start"),
    pytest.param("2026-09-11T15:47:42", "2026-09-11T16:57:42Z", id="naive-start"),
    pytest.param("2026-09-11T15:47:42Z", "2026-09-11T16:57:42", id="naive-end"),
    pytest.param(datetime(2026, 9, 11, 15, 47, 42), "2026-09-11T16:57:42Z", id="naive-datetime"),
    pytest.param("2026-09-11T15:47:42Z) | union * //", "2026-09-11T16:57:42Z", id="injected-start"),
    pytest.param("2026-09-11T15:47:42Z", "2026-09-11T16:57:42Z) | union * //", id="injected-end"),
    pytest.param("2026-09-11T15:47:42Z", "2026-09-11T15:47:42Z", id="empty-window"),
    pytest.param("2026-09-11T15:47:42Z", "2026-09-11T15:47:41Z", id="reversed-window"),
    pytest.param("2026-09-11T15:47:42Z", "2026-09-11T16:57:42.000001Z", id="over-slack-limit"),
    pytest.param("2026-08-12T15:47:42Z", "2026-09-11T15:47:42Z", id="thirty-days"),
    pytest.param(123, "2026-09-11T16:57:42Z", id="non-datetime-start"),
    pytest.param("2026-09-11T15:47:42Z", True, id="non-datetime-end"),
    pytest.param("0001-01-01T00:00:00+01:00", "0001-01-01T00:10:00Z", id="utc-overflow"),
])
def test_invalid_log_windows_fail_before_credentials_or_network(since, until):
    cloud, credential = controller(lambda request: pytest.fail("No network expected"))
    with pytest.raises(ValueError, match="log time window") as caught:
        cloud.get_events(str(uuid4()), since=since, until=until)
    assert "union" not in str(caught.value)
    assert credential.scopes == []


@pytest.mark.parametrize("change", [{"endpoint": "https://attacker.example"}, {"job_name": "bad'; query"},
                                    {"workspace_id": "not-a-guid"}, {"model": "other-model"}, {"secret": "bad"}])
def test_untrusted_deployment_config_is_rejected(change):
    with pytest.raises(ValueError, match="deployment"):
        controller(lambda request: pytest.fail("No network expected"), {**CONFIG, **change})


def test_cloud_errors_never_expose_response_or_credential_details():
    from tradingagents.workbench.azure_control import CloudError

    cloud, _ = controller(lambda request: httpx.Response(403, text="sensitive-service-error"))
    with pytest.raises(CloudError) as caught:
        cloud.get_status("execution-1")
    assert "sensitive" not in str(caught.value)
    assert "boundary-secret" not in str(caught.value)


def test_invalid_identifiers_and_requests_never_reach_network():
    cloud, _ = controller(lambda request: pytest.fail("No network expected"))
    for operation in (lambda: cloud.start_run("not-uuid", {}),
                      lambda: cloud.start_run(str(uuid4()), {"env": {"TOKEN": "bad"}}),
                      lambda: cloud.stop_run("../other-job")):
        with pytest.raises(ValueError):
            operation()


def test_optional_null_template_fields_are_omitted_and_sidecar_is_not_injected():
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"properties": {"template": {
                "containers": [{"name": "worker", "image": "worker", "env": None},
                               {"name": "sidecar", "image": "sidecar", "env": [{"name": "KEEP", "value": "yes"}]}],
                "initContainers": None,
            }}})
        body = json.loads(request.content)
        assert set(body) == {"containers"}
        assert len(body["containers"][0]["env"]) == 2
        assert body["containers"][1]["env"] == [{"name": "KEEP", "value": "yes"}]
        return httpx.Response(202, json={"name": "execution-1"})

    cloud, _ = controller(handle)
    assert cloud.start_run(str(uuid4()), {}) == "execution-1"


@pytest.mark.parametrize("body", [{"properties": None}, {"properties": {"status": {"secret": "raw"}}}])
def test_malformed_status_responses_raise_public_safe_cloud_error(body):
    from tradingagents.workbench.azure_control import CloudError

    cloud, _ = controller(lambda request: httpx.Response(200, json=body))
    with pytest.raises(CloudError):
        cloud.get_status("execution-1")


@pytest.mark.parametrize("partial", [False, True])
def test_logs_are_bounded_across_decoded_events(monkeypatch, partial):
    from tradingagents.workbench import azure_control

    run_id = str(uuid4())
    lines = encode_event(run_id, "report", {"section": "first", "content": "A" * 4000})
    lines += encode_event(run_id, "report", {"section": "second", "content": "B" * 4000})
    cloud, _ = controller(lambda request: httpx.Response(200, json={"tables": [{
        "columns": [{"name": "Log_s"}], "rows": [[line] for line in lines],
    }], **({"error": {"code": "PartialError"}} if partial else {})}))
    monkeypatch.setattr(azure_control, "MAX_LOG_BYTES", 6000)
    with pytest.raises(azure_control.CloudError, match="size limit"):
        cloud.get_events(run_id)


def test_partial_corrupt_and_unrelated_events_do_not_produce_reports():
    run_id = str(uuid4())
    lines = encode_event(str(uuid4()), "result", {"secret": "other-run"})
    lines += ["TAWB1 broken", "not an event"]
    valid = encode_event(run_id, "report", {"section": "market", "content": "valid report"})
    part = json.loads(valid[0][6:])
    part["total"] = 2
    lines.append("TAWB1 " + json.dumps(part))
    cloud, _ = controller(lambda request: httpx.Response(200, json={"tables": [{
        "columns": [{"name": "Log_s"}], "rows": [[line] for line in lines],
    }]}))
    assert cloud.get_events(run_id) == []
    lines[:] = valid
    assert cloud.get_events(run_id)[0]["payload"]["content"] == "valid report"


def test_http_response_limit_and_credential_errors_are_public_safe(monkeypatch):
    from tradingagents.workbench import azure_control

    cloud, _ = controller(lambda request: httpx.Response(200, content=b"x" * 101))
    monkeypatch.setattr(azure_control, "MAX_HTTP_BYTES", 100)
    with pytest.raises(azure_control.CloudError, match="size limit"):
        cloud.get_status("execution-1")

    def fail_token(scope):
        raise RuntimeError("secret-credential-message")

    cloud.credential.get_token = fail_token
    with pytest.raises(azure_control.CloudError) as caught:
        cloud.get_status("execution-1")
    assert "secret-credential" not in str(caught.value)


def test_logs_partial_error_preserves_verified_events_and_safe_warning():
    run_id = str(uuid4())
    lines = encode_event(run_id, "report", {"section": "market_report", "content": "usable evidence"})
    lines += encode_event(str(uuid4()), "report", {"section": "other", "content": "unrelated"})
    lines += ["TAWB1 corrupt"]
    body = {"tables": [{"columns": [{"name": "Log_s"}], "rows": [[line] for line in lines]}],
            "error": {"code": "PartialError", "message": "raw-service-secret"}}
    cloud, _ = controller(lambda request: httpx.Response(200, json=body))
    assert [event["payload"] for event in cloud.get_events(run_id)] == [
        {"section": "market_report", "content": "usable evidence"},
    ]
    warnings = []
    cloud.get_events(run_id, warnings=warnings)
    assert len(warnings) == 1 and "partial" in warnings[0].lower()
    assert "raw-service-secret" not in warnings[0]
    del body["error"]
    warnings = []
    cloud.get_events(run_id, warnings=warnings)
    assert warnings == []


@pytest.mark.parametrize("code", ["PartialError", "BadArgumentError", "Forbidden"])
def test_arm_error_envelopes_remain_fatal_even_with_usable_properties(code):
    from tradingagents.workbench.azure_control import CloudError

    cloud, _ = controller(lambda request: httpx.Response(200, json={
        "properties": {"status": "Succeeded"}, "error": {"code": code, "message": "raw-secret"},
    }))
    with pytest.raises(CloudError):
        cloud.get_status("execution-1")


@pytest.mark.parametrize("tables", [None, {}, [None], [{"columns": [], "rows": []}],
    [{"columns": [{"name": "Log_s"}], "rows": None}],
    [{"columns": [{"name": "Log_s"}], "rows": [{}]}],
    [{"columns": [{"name": "Log_s"}], "rows": [["TAWB1 invalid", "extra"]]}],
    [{"columns": [{"name": "Log_s"}, {"name": "Log_s"}], "rows": []}],
])
def test_partial_error_does_not_bypass_log_table_validation(tables):
    from tradingagents.workbench.azure_control import CloudError

    cloud, _ = controller(lambda request: httpx.Response(200, json={
        "tables": tables, "error": {"code": "PartialError"},
    }))
    with pytest.raises(CloudError):
        cloud.get_events(str(uuid4()))


@pytest.mark.parametrize("limit", ["MAX_LOG_LINES", "MAX_LOG_BYTES"])
def test_partial_error_does_not_bypass_log_limits(monkeypatch, limit):
    from tradingagents.workbench import azure_control

    lines = encode_event(str(uuid4()), "phase", {"stage": "market"})
    cloud, _ = controller(lambda request: httpx.Response(200, json={
        "tables": [{"columns": [{"name": "Log_s"}], "rows": [[lines[0]], [lines[0]]]}],
        "error": {"code": "PartialError"},
    }))
    monkeypatch.setattr(azure_control, limit, 1)
    with pytest.raises(azure_control.CloudError, match="size limit"):
        cloud.get_events(str(uuid4()))


def test_log_errors_other_than_partial_remain_fatal():
    from tradingagents.workbench.azure_control import CloudError

    cloud, _ = controller(lambda request: httpx.Response(200, json={
        "tables": [], "error": {"code": "Forbidden"},
    }))
    with pytest.raises(CloudError):
        cloud.get_events(str(uuid4()))
