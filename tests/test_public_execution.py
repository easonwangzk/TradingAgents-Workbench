import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from tests.test_public_store import MemoryBackend
from tradingagents.workbench.contracts import default_request
from tradingagents.workbench.public_store import EncryptedStore

SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
ROOT = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/hosted-fixture"
JOB_ID = ROOT + "/providers/Microsoft.App/jobs/job-hosted-research"
IDENTITY_ID = ROOT + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/worker-pull"
ENVIRONMENT_ID = ROOT + "/providers/Microsoft.App/managedEnvironments/hosted-env"
IMAGE = "fixture.azurecr.io/research@sha256:" + "a" * 64
ORIGIN = "https://workbench.example.com"
NOW = 1800000000
SECRET_NAME = "tawb-run-capability"


def installation():
    return {
        "subscription_id": SUBSCRIPTION,
        "resource_group": "hosted-fixture",
        "job_name": "job-hosted-research",
        "job_resource_id": JOB_ID,
        "environment_id": ENVIRONMENT_ID,
        "worker_identity_id": IDENTITY_ID,
        "image": IMAGE,
        "backend_origin": ORIGIN,
        "workspace_id": WORKSPACE,
        "location": "eastus2",
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_credential_and_new_job_required_before_cloud(legacy):
    from tradingagents.workbench.public_execution import ExecutionError, HostedExecution

    config = copy.deepcopy(installation())
    if legacy:
        config["job_name"] = "job-research-example"
        config["job_resource_id"] = ROOT + "/providers/Microsoft.App/jobs/job-research-example"
    with pytest.raises(ExecutionError) as caught:
        HostedExecution(object(), gateway=object(), installation=config,
                        credential=object() if legacy else None)
    assert caught.value.status_code == 503


class HostCredential:
    def get_token(self, scope):
        assert scope in {"https://management.azure.com/.default", "https://api.loganalytics.io/.default"}
        return SimpleNamespace(token="fixture-host-token")


def baseline_job():
    return actual_arm_defaults({
        "id": JOB_ID, "name": "job-hosted-research", "type": "Microsoft.App/jobs",
        "location": "eastus2", "tags": {},
        "systemData": {"createdByType": "Application", "createdAt": "2026-09-10T00:00:00Z"},
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {
            IDENTITY_ID: {"principalId": SUBSCRIPTION, "clientId": WORKSPACE},
        }},
        "properties": {
            "provisioningState": "Succeeded", "environmentId": ENVIRONMENT_ID,
            "workloadProfileName": "Consumption",
            "configuration": {
                "triggerType": "Manual", "replicaTimeout": 3600, "replicaRetryLimit": 0,
                "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                "registries": [{"server": "fixture.azurecr.io", "identity": IDENTITY_ID}],
                "secrets": [],
            },
            "template": {"containers": [{
                "name": "worker", "image": IMAGE, "resources": {"cpu": 1, "memory": "2Gi"},
                "env": [{"name": "TAWB_WORKER_MODE", "value": "hosted"},
                        {"name": "TAWB_BACKEND_BASE_URL", "value": ORIGIN}],
            }], "initContainers": []},
        },
    })


def actual_arm_defaults(job):
    value = copy.deepcopy(job)
    value["location"] = "East US 2"
    configuration = value["properties"]["configuration"]
    configuration["dapr"] = None
    configuration["eventTriggerConfig"] = None
    configuration["scheduleTriggerConfig"] = None
    configuration["registries"][0]["username"] = ""
    configuration["registries"][0]["passwordSecretRef"] = ""
    if configuration["secrets"] == []:
        configuration["secrets"] = None
    value["properties"]["eventStreamEndpoint"] = "https://example.invalid/eventstream"
    value["properties"]["outboundIpAddresses"] = ["20.1.250.250"]
    template = value["properties"]["template"]
    template["initContainers"] = None
    template["volumes"] = None
    template["containers"][0]["resources"]["ephemeralStorage"] = ""
    return value


@dataclass(repr=False)
class ArmWire:
    job: dict = field(default_factory=baseline_job)
    calls: list = field(default_factory=list)
    secrets: list = field(default_factory=list)
    executions: list = field(default_factory=list)
    fault: str | None = None
    provisioning: str = "Succeeded"
    before: object = None
    log_lines: list = field(default_factory=list)

    def __call__(self, request):
        path = request.url.path.lower()
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.before:
            self.before(request, body)
        if request.url.host == "api.loganalytics.azure.com":
            return httpx.Response(200, json={"tables": [{"name": "PrimaryResult",
                "columns": [{"name": "Log_s", "type": "string"}],
                "rows": [[line] for line in self.log_lines]}]})
        if request.method == "GET" and path == JOB_ID.lower():
            return httpx.Response(200, json=self.job)
        if request.method in {"PUT", "PATCH"} and path == JOB_ID.lower():
            if self.fault == "linked_scope" and (
                request.method == "PUT" or set(body) != {"properties"}
                or set(body["properties"]) != {"configuration", "template"}
                or set(body["properties"]["configuration"]) != {"secrets"}
            ):
                return httpx.Response(403, json={"error": {"code": "LinkedAuthorizationFailed"}})
            self.secrets = copy.deepcopy(body["properties"]["configuration"]["secrets"])
            if request.method == "PATCH":
                self.job["properties"]["configuration"].update(copy.deepcopy(body["properties"]["configuration"]))
                self.job["properties"]["template"].update(copy.deepcopy(body["properties"]["template"]))
                self.job = actual_arm_defaults(self.job)
            else:
                self.job = actual_arm_defaults({
                    "id": JOB_ID, "name": "job-hosted-research", "type": "Microsoft.App/jobs", **copy.deepcopy(body)
                })
            self.job["properties"]["configuration"]["secrets"] = [{"name": item["name"]} for item in self.secrets]
            self.job["properties"]["provisioningState"] = self.provisioning
            if self.fault == "secret":
                raise httpx.ReadTimeout("fixture-private-cloud-error")
            if self.fault == "accepted_empty":
                return httpx.Response(202)
            return httpx.Response(202 if self.provisioning != "Succeeded" else 200, json=self.job)
        if request.method == "POST" and path == JOB_ID.lower() + "/listsecrets":
            assert request.headers["content-type"] == "application/json"
            assert body == {}
            return httpx.Response(200, json={"value": self.secrets})
        if request.method == "GET" and path == JOB_ID.lower() + "/executions":
            return httpx.Response(200, json={"value": self.executions})
        if request.method == "POST" and path == JOB_ID.lower() + "/start":
            name = "job-hosted-research-abc123"
            execution = {"id": JOB_ID + "/executions/" + name, "name": name,
                         "type": "Microsoft.App/jobs/executions", "properties": {
                             "status": "Running", "startTime": "2026-09-10T00:00:00Z",
                             "template": copy.deepcopy(body),
                         }}
            self.executions.append(execution)
            if self.fault == "start":
                raise httpx.ReadTimeout("fixture-private-cloud-error")
            return httpx.Response(200, json=execution)
        for execution in self.executions:
            if path == execution["id"].lower() and request.method == "GET":
                return httpx.Response(200, json=execution)
            if path == execution["id"].lower() + "/stop" and request.method == "POST":
                if self.fault == "stop":
                    raise httpx.ReadTimeout("fixture-private-cloud-error")
                execution["properties"]["status"] = "Stopped"
                return httpx.Response(200, json={})
        raise AssertionError("Unexpected ARM operation")


@pytest.fixture(scope="module")
def account_seed():
    from tradingagents.workbench.public_accounts import EmailAuth

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"e" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: NOW)
    password = "fixture-account-password-2026"
    hashed = auth._hasher.hash(password)
    with patch.object(type(auth._hasher), "hash", return_value=hashed):
        owner = auth.bootstrap(password)
        invitation = auth.invite(owner.session_id, "guest@example.com")
        guest = auth.redeem(invitation.token, "guest@example.com", password)
    return backend, owner.principal, guest.principal


@pytest.fixture
def rig(account_seed):
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    seed, owner, guest = account_seed
    backend = copy.deepcopy(seed)
    store = EncryptedStore(backend, b"e" * 32)
    settings = UserSettings(store)
    for principal in (owner, guest):
        settings.update(principal, {
            "mode": "api_key", "provider": "openai", "quick_model": "gpt-5",
            "deep_model": "gpt-5", "api_key": "fixture-real-api-key",
            "data_keys": {"fred": "fixture-real-fred-key"},
        }, expected_version=None)
    gateway = ProviderGateway(store, settings=settings, budget=BudgetLedger(store, clock=lambda: NOW),
                              managed_credential=object(), installation={"foundry": None},
                              http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
                              clock=lambda: NOW)
    wire = ArmWire()
    result = SimpleNamespace(store=store, backend=backend, owner=owner, guest=guest,
                             settings=settings, gateway=gateway, wire=wire)

    def controller(config=None, **kwargs):
        from tradingagents.workbench.public_execution import HostedExecution

        return HostedExecution(store, gateway=gateway, installation=config or installation(),
                               credential=HostCredential(), client=httpx.Client(transport=httpx.MockTransport(wire)),
                               clock=lambda: NOW, **kwargs)

    result.controller = controller
    result.run_id = str(uuid4())
    result.snapshot = settings.pin(owner, result.run_id)
    return result


@pytest.mark.parametrize("old_state", ["prepared", "running", "terminal"])
def test_explicit_image_upgrade_preserves_old_runs_and_prepares_new_image(rig, old_state):
    old = rig.controller()
    old.prepare(rig.run_id, rig.owner, rig.snapshot)
    if old_state != "prepared":
        old.start(rig.run_id, rig.owner, default_request())
    if old_state == "terminal":
        rig.wire.executions[0]["properties"]["status"] = "Succeeded"
        old.reconcile(rig.run_id, rig.owner)
    upgraded_image = "fixture.azurecr.io/research@sha256:" + "b" * 64
    upgraded = rig.controller({**installation(), "image": upgraded_image}, previous_images=[IMAGE])
    before_starts = sum(path.endswith("/start") for method, path, body in rig.wire.calls)
    assert upgraded.get(rig.run_id, rig.owner)["status"] == old_state
    if old_state == "running":
        assert upgraded.reconcile(rig.run_id, rig.owner)["status"] == "running"
        rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    if old_state == "prepared":
        assert upgraded.stop(rig.run_id, rig.owner)["status"] == "terminal"
    else:
        assert upgraded.reconcile(rig.run_id, rig.owner)["status"] == "terminal"
    historical = copy.deepcopy(rig.store.read("email-v1/executions/" + rig.run_id)[0])

    next_id = str(uuid4())
    upgraded.prepare(next_id, rig.owner, rig.settings.pin(rig.owner, next_id))

    assert rig.wire.job["properties"]["template"]["containers"][0]["image"] == upgraded_image
    assert upgraded.get(next_id, rig.owner)["status"] == "prepared"
    assert upgraded.get(rig.run_id, rig.owner)["status"] == "terminal"
    assert rig.store.read("email-v1/executions/" + rig.run_id)[0] == historical
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == before_starts
    assert upgraded._reservation()[0]["run_id"] == next_id
    assert upgraded._reservation()[0]["released"] is False


@pytest.mark.parametrize("change", ["unlisted", "other_origin"])
def test_image_upgrade_does_not_accept_unlisted_installations(rig, change):
    from tradingagents.workbench.public_execution import ExecutionError

    old = rig.controller()
    old.prepare(rig.run_id, rig.owner, rig.snapshot)
    changed = {**installation(), "image": "fixture.azurecr.io/research@sha256:" + "b" * 64}
    if change == "other_origin":
        changed["backend_origin"] = "https://other.example.com"
    upgraded = rig.controller(changed, previous_images=[] if change == "unlisted" else [IMAGE])
    before = len(rig.wire.calls)
    with pytest.raises(ExecutionError):
        upgraded.get(rig.run_id, rig.owner)
    next_id = str(uuid4())
    snapshot = rig.settings.pin(rig.owner, next_id)
    with pytest.raises(ExecutionError):
        upgraded.prepare(next_id, rig.owner, snapshot)
    assert len(rig.wire.calls) == before


@pytest.mark.parametrize("images", [["latest"], [IMAGE, IMAGE], "not-a-list", ["other.azurecr.io/research@sha256:" + "c" * 64]])
def test_previous_worker_images_require_unique_same_repository_digests(rig, images):
    from tradingagents.workbench.public_execution import ExecutionError

    with pytest.raises(ExecutionError):
        rig.controller(previous_images=images)
    assert not rig.wire.calls


def test_prepare_updates_run_configuration_without_rebinding_linked_resources(rig):
    rig.wire.fault = "linked_scope"
    before = copy.deepcopy(rig.wire.job)
    execution = rig.controller()

    execution.prepare(rig.run_id, rig.owner, rig.snapshot)

    assert execution.get(rig.run_id, rig.owner)["status"] == "prepared"
    updates = [body for method, path, body in rig.wire.calls if method == "PATCH"]
    assert len(updates) == 1
    assert set(updates[0]) == {"properties"}
    assert set(updates[0]["properties"]) == {"configuration", "template"}
    assert set(updates[0]["properties"]["configuration"]) == {"secrets"}
    assert set(updates[0]["properties"]["template"]) == {"containers"}
    assert rig.wire.job["identity"] == before["identity"]
    assert rig.wire.job["properties"]["environmentId"] == before["properties"]["environmentId"]
    assert rig.wire.job["properties"]["configuration"]["registries"] == before["properties"]["configuration"]["registries"]
    assert not any(method == "PUT" or path.endswith("/start") for method, path, body in rig.wire.calls)


def test_prepare_installs_one_private_secret_and_returns_nonsecret_config(rig):
    execution = rig.controller()
    config = execution.prepare(rig.run_id, rig.owner, rig.snapshot)
    assert config["provider"] == "openai"
    assert config["dataKeyConfigured"] == {"fred": True, "alpha_vantage": False}
    assert execution.get(rig.run_id, rig.owner)["status"] == "prepared"
    puts = [body for method, path, body in rig.wire.calls if method == "PATCH"]
    assert len(puts) == 1
    secrets = puts[0]["properties"]["configuration"].pop("secrets")
    assert len(secrets) == 1 and secrets[0]["name"] == SECRET_NAME
    capability = secrets[0]["value"]
    assert capability.startswith(rig.run_id + ".")
    assert len([path for method, path, body in rig.wire.calls if path.endswith("/listsecrets")]) == 1
    public = json.dumps([config, execution.get(rig.run_id, rig.owner), puts]) + repr(execution)
    assert not any(secret in public for secret in (capability, "fixture-real-api-key", "fixture-real-fred-key"))
    assert all(capability.encode() not in value[0] for value in rig.backend.records.values())
    assert execution.prepare(rig.run_id, rig.owner, rig.snapshot) == config
    assert len([body for method, path, body in rig.wire.calls if method == "PATCH"]) == 1


def test_validate_job_accepts_actual_arm_defaults_without_mutating_input(rig):
    execution = rig.controller()
    job = actual_arm_defaults(rig.wire.job)
    original = copy.deepcopy(job)

    execution._validate_job(job)

    assert job == original


def prepared(rig):
    execution = rig.controller()
    execution.prepare(rig.run_id, rig.owner, rig.snapshot)
    return execution


def test_start_exact_secretref_environment_and_single_execution(rig):
    execution = prepared(rig)

    def assert_journal(request, body):
        if request.url.path.endswith("/start"):
            record = rig.store.read("email-v1/executions/" + rig.run_id)[0]
            assert record["start_attempted"] is True
            assert record["request"] == default_request()

    rig.wire.before = assert_journal
    view = execution.start(rig.run_id, rig.owner, default_request())
    assert view["status"] == "running"
    assert view["execution_name"] == "job-hosted-research-abc123"
    body = next(body for method, path, body in rig.wire.calls if path.endswith("/start"))
    assert set(body) == {"containers"}
    assert len(body["containers"]) == 1
    worker = body["containers"][0]
    assert worker["image"] == IMAGE
    assert worker["resources"] == {"cpu": 1, "memory": "2Gi"}
    env = worker["env"]
    assert env[0] == {"name": "TAWB_RUN_CAPABILITY", "secretRef": SECRET_NAME}
    assert {entry["name"] for entry in env} == {
        "TAWB_RUN_CAPABILITY", "TAWB_WORKER_MODE", "TAWB_BACKEND_BASE_URL", "TAWB_PUBLIC_CONFIG",
        "TRADINGAGENTS_RUN_REQUEST", "TRADINGAGENTS_RUN_ID",
    }
    values = {entry["name"]: entry.get("value") for entry in env}
    assert values["TRADINGAGENTS_RUN_ID"] == rig.run_id
    assert values["TAWB_BACKEND_BASE_URL"] == ORIGIN
    assert values["TAWB_WORKER_MODE"] == "hosted"
    assert json.loads(base64.b64decode(values["TRADINGAGENTS_RUN_REQUEST"])) == default_request()
    public = json.dumps([body, view])
    assert not any(secret in public for secret in (
        rig.wire.secrets[0]["value"], "fixture-real-api-key", "fixture-real-fred-key",
    ))
    assert execution.start(rig.run_id, rig.owner, default_request())["execution_name"] == view["execution_name"]
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


@pytest.mark.parametrize("field_name", ["ephemeralStorage", "imageType", "secretRef", "all"])
def test_start_accepts_verified_azure_execution_defaults(rig, field_name):
    execution = prepared(rig)

    def add_execution_defaults(request, body):
        if request.method == "GET" and request.url.path.endswith("/executions"):
            for item in rig.wire.executions:
                container = item["properties"]["template"]["containers"][0]
                if field_name in {"ephemeralStorage", "all"}:
                    container["resources"]["ephemeralStorage"] = ""
                if field_name in {"imageType", "all"}:
                    container["imageType"] = "ContainerImage"
                if field_name in {"secretRef", "all"}:
                    container["env"][0]["secretRef"] = "cappjob-job-hosted-research"

    rig.wire.before = add_execution_defaults
    view = execution.start(rig.run_id, rig.owner, default_request())

    assert view["status"] == "running"
    assert view["execution_name"] == "job-hosted-research-abc123"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    assert execution.reconcile(rig.run_id, rig.owner)["status"] == "terminal"
    assert execution.get(rig.run_id, rig.owner)["revoked"] is True


@pytest.mark.parametrize("problem", ["storage", "image_type", "secret_ref", "secret_value"])
def test_execution_defaults_do_not_hide_unverified_drift(rig, problem):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = lost_start(rig)
    container = rig.wire.executions[0]["properties"]["template"]["containers"][0]
    if problem == "storage":
        container["resources"]["ephemeralStorage"] = "8Gi"
    elif problem == "image_type":
        container["imageType"] = "UntrustedImage"
    elif problem == "secret_ref":
        container["env"][0]["secretRef"] = "cappjob-another-job"
    else:
        container["env"][0]["secretRef"] = "cappjob-job-hosted-research"
        rig.wire.secrets[0]["value"] = "different-capability"

    with pytest.raises(ExecutionError):
        execution.reconcile(rig.run_id, rig.owner)

    assert execution.get(rig.run_id, rig.owner)["execution_name"] is None
    assert execution._reservation()[0]["released"] is False


def test_accepted_lost_start_reconciles_after_restart_without_second_post(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    rig.wire.fault = "start"
    with pytest.raises(ExecutionError) as caught:
        execution.start(rig.run_id, rig.owner, default_request())
    assert caught.value.status_code == 503
    assert "fixture-private-cloud-error" not in repr(caught.value)
    view = rig.controller().reconcile(rig.run_id, rig.owner)
    assert view["execution_name"] == "job-hosted-research-abc123"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


@pytest.mark.parametrize("problem", ["wrong_run", "duplicate", "wrong_env"])
def test_uncertain_start_requires_single_exact_execution_template(rig, problem):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    rig.wire.fault = "start"
    with pytest.raises(ExecutionError):
        execution.start(rig.run_id, rig.owner, default_request())
    found = rig.wire.executions[0]
    if problem == "duplicate":
        other = copy.deepcopy(found)
        other["name"] = "job-hosted-research-other"
        other["id"] = JOB_ID + "/executions/" + other["name"]
        rig.wire.executions.append(other)
    else:
        env = found["properties"]["template"]["containers"][0]["env"]
        if problem == "wrong_run":
            next(entry for entry in env if entry["name"] == "TRADINGAGENTS_RUN_ID")["value"] = str(uuid4())
        else:
            env.append({"name": "AZURE_CLIENT_ID", "value": WORKSPACE})
    with pytest.raises(ExecutionError):
        rig.controller().reconcile(rig.run_id, rig.owner)
    assert execution.get(rig.run_id, rig.owner)["execution_name"] is None
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_stop_revokes_before_cloud_timeout_and_keeps_global_hold(rig):
    from tradingagents.workbench.public_execution import ExecutionError
    from tradingagents.workbench.public_gateway import GatewayError

    execution = prepared(rig)
    execution.start(rig.run_id, rig.owner, default_request())
    capability = rig.wire.secrets[0]["value"]

    def assert_revoked(request, body):
        if request.url.path.endswith("/stop"):
            record = rig.store.read("email-v1/executions/" + rig.run_id)[0]
            assert record["stop_attempted"] is True
            with pytest.raises(GatewayError):
                rig.gateway.worker_config(capability)

    rig.wire.before = assert_revoked
    rig.wire.fault = "stop"
    with pytest.raises(ExecutionError):
        execution.stop(rig.run_id, rig.owner)
    assert execution.get(rig.run_id, rig.owner)["revoked"] is True
    other = str(uuid4())
    with pytest.raises(ExecutionError):
        rig.controller().prepare(other, rig.guest, rig.settings.pin(rig.guest, other))
    rig.controller().stop(rig.run_id, rig.owner)
    assert sum(path.endswith("/stop") for method, path, body in rig.wire.calls) == 1


@pytest.mark.parametrize("started", [False, True])
def test_release_only_after_no_start_or_verified_terminal_and_revocation(rig, started):
    execution = prepared(rig)
    if started:
        execution.start(rig.run_id, rig.owner, default_request())
        rig.wire.executions[0]["properties"]["status"] = "Succeeded"
        view = execution.reconcile(rig.run_id, rig.owner)
    else:
        view = execution.stop(rig.run_id, rig.owner)
    assert view["status"] == "terminal" and view["revoked"] is True
    other = str(uuid4())
    rig.controller().prepare(other, rig.guest, rig.settings.pin(rig.guest, other))
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 2


def test_stop_retains_unknown_budget_spend(rig):
    execution = prepared(rig)
    ledger = rig.gateway._budget
    held = ledger.reserve(str(uuid4()), run_id=rig.run_id, amount_microusd=123, price_version="fixture-v1")
    ledger.mark_dispatched(held)
    execution.stop(rig.run_id, rig.owner)
    assert ledger.snapshot()["reserved_microusd"] == 123


def change_path(value, path, replacement):
    parts = path.split("/")
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    last = int(parts[-1]) if isinstance(value, list) else parts[-1]
    value[last] = replacement


@pytest.mark.parametrize("path,replacement", [
    ("name", "job-research-example"),
    ("id", ROOT + "/providers/Microsoft.App/jobs/job-research-example"),
    ("properties/environmentId", ENVIRONMENT_ID + "-other"),
    ("identity/type", "SystemAssigned, UserAssigned"),
    ("properties/template/containers/0/image", "fixture.azurecr.io/research:latest"),
    ("properties/template/containers/0/resources/cpu", 2),
    ("properties/template/containers/0/resources/cpu", True),
    ("properties/template/containers/0/resources/memory", "4Gi"),
    ("properties/template/containers/0/resources/ephemeralStorage", "1Gi"),
    ("properties/template/containers/0/resources/ephemeralStorage", None),
    ("properties/template/containers/0/command", ["sh", "-c", "other"]),
    ("properties/template/containers/0/env", [{"name": "OPENAI_API_KEY", "value": "fixture-real-api-key"}]),
    ("properties/template/initContainers", [{"name": "sidecar", "image": IMAGE}]),
    ("properties/configuration/replicaTimeout", 3601),
    ("properties/configuration/replicaRetryLimit", 1),
    ("properties/configuration/replicaRetryLimit", False),
    ("properties/configuration/manualTriggerConfig/parallelism", 2),
    ("properties/configuration/manualTriggerConfig/parallelism", True),
    ("properties/configuration/manualTriggerConfig/replicaCompletionCount", 2),
    ("properties/configuration/triggerType", "Schedule"),
    ("properties/configuration/dapr", {"enabled": True}),
    ("properties/configuration/eventTriggerConfig", {"parallelism": 2}),
    ("properties/configuration/scheduleTriggerConfig", {"cronExpression": "0 * * * *"}),
    ("properties/configuration/secrets", [{"name": "fred-key"}]),
    ("properties/configuration/registries/0/identity", IDENTITY_ID + "-other"),
    ("properties/configuration/registries/0/username", "fixture-user"),
    ("properties/configuration/registries/0/passwordSecretRef", "registry-password"),
    ("properties/template/volumes", [{"name": "scratch"}]),
    ("properties/unexpectedMetadata", "unexpected"),
    ("properties/workloadProfileName", "dedicated"),
], ids=lambda value: value if isinstance(value, str) and "/" in value and len(value) < 90 else None)
def test_malformed_job_denies_secret_write(rig, path, replacement):
    from tradingagents.workbench.public_execution import ExecutionError

    change_path(rig.wire.job, path, replacement)
    with pytest.raises(ExecutionError):
        rig.controller().prepare(rig.run_id, rig.owner, rig.snapshot)
    assert not any(method in {"PUT", "PATCH"} or path.endswith("/start") for method, path, body in rig.wire.calls)


@pytest.mark.parametrize("path,replacement", [
    ("properties/template/containers/0/image", "fixture.azurecr.io/research:latest"),
    ("properties/template/containers/0/env", [{"name": "TAWB_RUN_CAPABILITY", "value": "fixture-plaintext"}]),
    ("properties/configuration/manualTriggerConfig/parallelism", 2),
    ("properties/configuration/secrets", [{"name": "unrelated-secret"}]),
    ("properties/configuration/secrets", None),
    ("properties/template/containers/0/resources/ephemeralStorage", "1Gi"),
])
def test_job_drift_between_prepare_and_start_denies_post(rig, path, replacement):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    change_path(rig.wire.job, path, replacement)
    with pytest.raises(ExecutionError):
        execution.start(rig.run_id, rig.owner, default_request())
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


@pytest.mark.parametrize("fault", ["secret", "pending", "accepted_empty"])
def test_uncertain_secret_readback_never_repeats_patch(rig, fault):
    from tradingagents.workbench.public_execution import ExecutionError

    if fault in {"secret", "accepted_empty"}:
        rig.wire.fault = fault
    else:
        rig.wire.provisioning = "InProgress"
    with pytest.raises(ExecutionError):
        rig.controller().prepare(rig.run_id, rig.owner, rig.snapshot)
    assert rig.wire.calls[-1][0] == "PATCH"
    if fault == "pending":
        with pytest.raises(ExecutionError):
            rig.controller().prepare(rig.run_id, rig.owner, rig.snapshot)
        assert rig.wire.calls[-1][0] == "GET"
        rig.wire.job["properties"]["provisioningState"] = "Succeeded"
    rig.controller().prepare(rig.run_id, rig.owner, rig.snapshot)
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


def test_uncertain_secret_not_overwritten_or_released_by_stop(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    rig.wire.provisioning = "InProgress"
    execution = rig.controller()
    with pytest.raises(ExecutionError):
        execution.prepare(rig.run_id, rig.owner, rig.snapshot)
    with pytest.raises(ExecutionError):
        execution.stop(rig.run_id, rig.owner)
    other = str(uuid4())
    with pytest.raises(ExecutionError):
        rig.controller().prepare(other, rig.guest, rig.settings.pin(rig.guest, other))
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    rig.wire.job["properties"]["provisioningState"] = "Succeeded"
    assert rig.controller().reconcile(rig.run_id, rig.owner)["status"] == "terminal"


@pytest.mark.parametrize("target", ["authorization_attempted", "grant", "secret_attempted", "start_attempted"])
def test_accepted_lost_store_journal_never_reissues_cloud_operation(rig, target):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig) if target == "start_attempted" else rig.controller()
    original = rig.backend.write
    fired = []

    def lost(key, content, expected_version=None):
        version = original(key, content, expected_version)
        value = rig.store.read(key)[0]
        matched = (key.startswith("email-v1/gateway/runs/") if target == "grant"
                   else key == "email-v1/executions/" + rig.run_id and value["status"] == target)
        if matched and not fired:
            fired.append(True)
            raise RuntimeError("fixture-private-store-error")
        return version

    rig.backend.write = lost
    operation = (lambda service: service.start(rig.run_id, rig.owner, default_request())) if target == "start_attempted" else (
        lambda service: service.prepare(rig.run_id, rig.owner, rig.snapshot))
    with pytest.raises(ExecutionError) as caught:
        operation(execution)
    assert caught.value.status_code == 503
    writes = sum(method in {"PUT", "PATCH"} or path.endswith("/start") for method, path, body in rig.wire.calls)
    with pytest.raises(ExecutionError):
        operation(rig.controller())
    assert sum(method in {"PUT", "PATCH"} or path.endswith("/start") for method, path, body in rig.wire.calls) == writes
    assert "fixture-private-store-error" not in str(caught.value)
    assert caught.value.__context__ is None


def test_competing_users_casefolded_global_reservation_survives_restart(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    original = rig.backend.write
    barrier, lock = Barrier(2), Lock()

    def competing(key, content, expected_version=None):
        if key.startswith("email-v1/execution-jobs/") and expected_version is None:
            barrier.wait(timeout=5)
        with lock:
            return original(key, content, expected_version)

    rig.backend.write = competing
    other = str(uuid4())
    guest_snapshot = rig.settings.pin(rig.guest, other)
    upper = installation()
    for name in ("resource_group", "job_name", "job_resource_id", "environment_id", "worker_identity_id"):
        upper[name] = upper[name].upper()

    def attempt(run_id, principal, snapshot, config):
        try:
            rig.controller(config).prepare(run_id, principal, snapshot)
            return run_id
        except ExecutionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner_future = pool.submit(attempt, rig.run_id, rig.owner, rig.snapshot, installation())
        guest_future = pool.submit(attempt, other, rig.guest, guest_snapshot, upper)
        results = [owner_future.result(), guest_future.result()]
    assert sum(result is not None for result in results) == 1
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    loser = (rig.run_id, rig.owner, rig.snapshot) if results[0] is None else (other, rig.guest, guest_snapshot)
    with pytest.raises(ExecutionError):
        rig.controller().prepare(*loser)


@pytest.mark.parametrize("field_name,replacement", [
    ("execution_name", "fixture-private-store-error"),
    ("cloud_status", "fixture-private-store-error"),
    ("status", "fixture-private-store-error"),
    ("revoked", "fixture-private-store-error"),
    ("secret_attempted", "fixture-private-store-error"),
    ("unknown", "fixture-private-store-error"),
])
def test_corrupted_private_record_never_leaks_into_public_view(rig, field_name, replacement):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    key = "email-v1/executions/" + rig.run_id
    value, version = rig.store.read(key)
    value[field_name] = replacement
    rig.store.write(key, value, version)
    with pytest.raises(ExecutionError) as caught:
        execution.get(rig.run_id, rig.owner)
    assert caught.value.status_code == 503
    assert replacement not in str(caught.value)


def test_terminal_run_cannot_reacquire_reservation(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    execution.stop(rig.run_id, rig.owner)
    with pytest.raises(ExecutionError):
        execution.prepare(rig.run_id, rig.owner, rig.snapshot)
    other = str(uuid4())
    rig.controller().prepare(other, rig.guest, rig.settings.pin(rig.guest, other))


@pytest.mark.parametrize("action", ["get", "start", "stop", "reconcile", "prepare"])
def test_wrong_owner_and_wrong_run_make_zero_cloud_calls(rig, action):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    count = len(rig.wire.calls)
    arguments = [rig.run_id, rig.guest]
    if action == "start":
        arguments.append(default_request())
    if action == "prepare":
        arguments.append(rig.snapshot)
    with pytest.raises(ExecutionError):
        getattr(execution, action)(*arguments)
    arguments[0] = str(uuid4())
    arguments[1] = rig.owner
    with pytest.raises(ExecutionError):
        getattr(execution, action)(*arguments)
    assert len(rig.wire.calls) == count


def lost_start(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    rig.wire.fault = "start"
    with pytest.raises(ExecutionError):
        execution.start(rig.run_id, rig.owner, default_request())
    return execution


def test_paginated_execution_reconciliation_reads_all_pages(rig):
    execution = lost_start(rig)
    pages = []

    def transport(request):
        if request.url.path.endswith("/executions"):
            pages.append(request.url)
            if "$skipToken" not in request.url.params:
                return httpx.Response(200, json={"value": [], "nextLink":
                    "https://management.azure.com" + JOB_ID + "/executions?api-version=2024-03-01&$skipToken=page-2"})
        return rig.wire(request)

    execution._cloud.client = httpx.Client(transport=httpx.MockTransport(transport))
    assert execution.reconcile(rig.run_id, rig.owner)["status"] == "running"
    assert len(pages) == 2
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


@pytest.mark.parametrize("next_link", [
    "https://attacker.example/executions", "http://management.azure.com/other",
    "https://management.azure.com" + JOB_ID + "-other/executions?api-version=2024-03-01",
    "https://user@management.azure.com" + JOB_ID + "/executions?api-version=2024-03-01",
    "https://management.azure.com" + JOB_ID + "/executions?api-version=wrong",
    "https://management.azure.com" + JOB_ID + "/executions?api-version=2024-03-01&api-version=2024-03-01",
], ids=["other-host", "http", "other-job", "userinfo", "wrong-version", "duplicate-query"])
def test_pagination_rejects_untrusted_next_links(rig, next_link):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = lost_start(rig)
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"value": [], "nextLink": next_link})

    execution._cloud.client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(ExecutionError):
        execution.reconcile(rig.run_id, rig.owner)
    assert len(calls) == 1


def test_pagination_has_fixed_page_bound(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = lost_start(rig)
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"value": [], "nextLink":
            "https://management.azure.com" + JOB_ID + "/executions?api-version=2024-03-01&$skipToken=" + str(len(calls))})

    execution._cloud.client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(ExecutionError):
        execution.reconcile(rig.run_id, rig.owner)
    assert len(calls) == 10


def test_cloud_active_execution_blocks_start_even_after_prepare(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    rig.wire.executions.append({"name": "job-hosted-research-external", "properties": {"status": "Running"}})
    with pytest.raises(ExecutionError):
        execution.start(rig.run_id, rig.owner, default_request())
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


def test_quarantine_survives_stop_and_capability_expiry(rig):
    from tradingagents.workbench.public_execution import ExecutionError
    from tradingagents.workbench.public_gateway import GatewayError

    execution = lost_start(rig)
    other = copy.deepcopy(rig.wire.executions[0])
    other["name"] = "job-hosted-research-duplicate"
    other["id"] = JOB_ID + "/executions/" + other["name"]
    rig.wire.executions.append(other)
    with pytest.raises(ExecutionError):
        execution.reconcile(rig.run_id, rig.owner)
    rig.wire.executions.pop()
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    with pytest.raises(ExecutionError):
        execution.stop(rig.run_id, rig.owner)
    with pytest.raises(GatewayError):
        rig.gateway.worker_config(rig.wire.secrets[0]["value"])
    execution._clock = lambda: NOW + 4000
    new_run = str(uuid4())
    with pytest.raises(ExecutionError):
        execution.prepare(new_run, rig.guest, rig.settings.pin(rig.guest, new_run))


def test_verified_log_helper_redacts_capability_and_checks_owner(rig):
    from tradingagents.workbench.contracts import encode_event
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    capability = rig.wire.secrets[0]["value"]
    rig.wire.log_lines = encode_event(rig.run_id, "report", {"section": "partial", "content": capability})
    events = execution.get_events(rig.run_id, rig.owner)
    assert len(events) == 1
    public = json.dumps(events)
    redacted = capability not in public
    assert redacted
    calls = len(rig.wire.calls)
    with pytest.raises(ExecutionError):
        execution.get_events(rig.run_id, rig.guest)
    assert len(rig.wire.calls) == calls


def test_hosted_logs_query_original_authorization_window_after_three_days(rig):
    execution = prepared(rig)
    execution._clock = lambda: NOW + 3 * 86400

    execution.get_events(rig.run_id, rig.owner)

    query = rig.wire.calls[-1][2]["query"]
    assert "ago(2d)" not in query
    assert "2027-01-15T08:00:00" in query
    assert "2027-01-15T09:10:00" in query
    assert rig.run_id in query


def test_worker_config_grant_is_nonsecret_and_reauthorized(rig):
    from tradingagents.workbench.public_gateway import GatewayError

    capability = rig.gateway.authorize(rig.run_id, rig.owner, rig.snapshot, expires_at=NOW + 3600)
    config = rig.gateway.worker_config(capability)
    assert config["provider"] == "openai"
    assert "fixture-real-api-key" not in json.dumps(config)
    saved = rig.settings.get(rig.owner)
    rig.settings.update(rig.owner, {"data_keys": {"fred": None}}, expected_version=saved["version"])
    with pytest.raises(GatewayError):
        rig.gateway.worker_config(capability)


def test_managed_identity_worker_config_uses_verified_fixed_deployment(rig):
    from tests.test_public_gateway import foundry_installation

    saved = rig.settings.get(rig.owner)
    rig.settings.update(rig.owner, {"mode": "managed_identity"}, expected_version=saved["version"])
    rig.gateway._installation = foundry_installation()
    snapshot = rig.settings.pin(rig.owner, rig.run_id)
    config = rig.controller().prepare(rig.run_id, rig.owner, snapshot)
    assert config["mode"] == "managed_identity"
    assert config["quick_model"] == config["deep_model"] == "deployment-prod-1"
    assert "provider_config" not in config


def test_prepare_rejects_unavailable_managed_identity_before_reservation_or_arm(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    saved = rig.settings.get(rig.owner)
    rig.settings.update(rig.owner, {"mode": "managed_identity"}, expected_version=saved["version"])

    with pytest.raises(ExecutionError) as caught:
        rig.controller().prepare(rig.run_id, rig.owner, rig.settings.pin(rig.owner, rig.run_id))

    assert caught.value.status_code == 409
    assert rig.store.read("email-v1/executions/" + rig.run_id) is None
    assert rig.wire.calls == []


@pytest.mark.parametrize("fault", ["read", "write"])
def test_state_outages_are_sanitized_503_without_cloud_operations(rig, fault):
    from tradingagents.workbench.public_execution import ExecutionError

    def fail(*args, **kwargs):
        raise RuntimeError("fixture-private-store-error")

    setattr(rig.backend, fault, fail)
    with pytest.raises(ExecutionError) as caught:
        rig.controller().prepare(rig.run_id, rig.owner, rig.snapshot)
    assert caught.value.status_code == 503
    assert "fixture-private-store-error" not in repr(caught.value)
    assert caught.value.__context__ is None
    assert not rig.wire.calls


def test_accepted_lost_stop_journal_still_revokes_immediately(rig):
    from tradingagents.workbench.public_execution import ExecutionError
    from tradingagents.workbench.public_gateway import GatewayError

    execution = prepared(rig)
    execution.start(rig.run_id, rig.owner, default_request())
    original = rig.backend.write
    fired = []

    def lost(key, content, expected_version=None):
        version = original(key, content, expected_version)
        if key == "email-v1/executions/" + rig.run_id:
            value = rig.store.read(key)[0]
            if value["stop_attempted"] and not fired:
                fired.append(True)
                raise RuntimeError("fixture-private-store-error")
        return version

    rig.backend.write = lost
    with pytest.raises(ExecutionError):
        execution.stop(rig.run_id, rig.owner)
    with pytest.raises(GatewayError):
        rig.gateway.worker_config(rig.wire.secrets[0]["value"])
    assert not any(path.endswith("/stop") for method, path, body in rig.wire.calls)


@pytest.mark.parametrize("mismatch", ["value", "extra", "reference"])
def test_secret_readback_mismatch_blocks_start_without_rewrite(rig, mismatch):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    if mismatch == "value":
        rig.wire.secrets[0]["value"] = str(uuid4()) + "." + "b" * 43
    elif mismatch == "extra":
        rig.wire.secrets.append({"name": "fred-key", "value": "fixture-real-fred-key"})
    else:
        rig.wire.secrets[0] = {"name": SECRET_NAME, "keyVaultUrl": "https://fixture.vault.azure.net/secrets/run"}
    with pytest.raises(ExecutionError):
        execution.start(rig.run_id, rig.owner, default_request())
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


@pytest.mark.parametrize("field_name,replacement", [
    ("image", "fixture.azurecr.io/research:latest"),
    ("backend_origin", "http://workbench.example.com"),
    ("workspace_id", "not-a-uuid"),
    ("job_resource_id", ROOT + "/providers/Microsoft.App/jobs/other"),
    ("environment_id", f"/subscriptions/{SUBSCRIPTION}/resourceGroups/../providers/Microsoft.App/managedEnvironments/env"),
    ("worker_identity_id", ROOT + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/*"),
    ("resource_group", ".."),
])
def test_installation_rejects_unsafe_resource_selections(rig, field_name, replacement):
    from tradingagents.workbench.public_execution import ExecutionError

    config = installation()
    config[field_name] = replacement
    if field_name == "resource_group":
        config["job_resource_id"] = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/../providers/Microsoft.App/jobs/job-hosted-research"
    with pytest.raises(ExecutionError):
        rig.controller(config)
    assert not rig.wire.calls


def test_secret_and_authorization_writes_follow_both_durable_records(rig):
    original = rig.backend.write

    def journaled():
        reservation_keys = [key for key in rig.backend.records if key.startswith("email-v1/execution-jobs/")]
        assert len(reservation_keys) == 1
        reservation = rig.store.read(reservation_keys[0])[0]
        assert reservation["run_id"] == rig.run_id and reservation["released"] is False
        return rig.store.read("email-v1/executions/" + rig.run_id)[0]

    def authorize(key, content, expected_version=None):
        if key.startswith("email-v1/gateway/runs/"):
            assert journaled()["authorization_attempted"] is True
        return original(key, content, expected_version)

    def secret(request, body):
        if request.method == "PATCH":
            assert journaled()["secret_attempted"] is True

    rig.backend.write = authorize
    rig.wire.before = secret
    prepared(rig)


def test_next_run_never_overwrites_an_unrecognized_previous_capability(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    execution.stop(rig.run_id, rig.owner)
    rig.wire.secrets[0]["value"] = str(uuid4()) + "." + "c" * 43
    other = str(uuid4())
    with pytest.raises(ExecutionError):
        rig.controller().prepare(other, rig.guest, rig.settings.pin(rig.guest, other))
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1


def test_cancel_before_secret_patch_preserves_last_installed_baseline(rig):
    from tradingagents.workbench.public_execution import ExecutionError

    execution = prepared(rig)
    execution.stop(rig.run_id, rig.owner)
    cancelled = str(uuid4())

    def failed_read(request, body):
        if request.method == "GET":
            raise httpx.ReadTimeout("fixture-private-cloud-error")

    rig.wire.before = failed_read
    with pytest.raises(ExecutionError):
        execution.prepare(cancelled, rig.guest, rig.settings.pin(rig.guest, cancelled))
    rig.wire.before = None
    execution.stop(cancelled, rig.guest)
    next_run = str(uuid4())
    config = rig.controller().prepare(next_run, rig.owner, rig.settings.pin(rig.owner, next_run))
    assert config["provider"] == "openai"
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 2
