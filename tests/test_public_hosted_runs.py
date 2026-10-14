import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier, RLock
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from tests.test_public_execution import NOW, ORIGIN, ArmWire, HostCredential, installation
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.contracts import encode_event
from tradingagents.workbench.public_accounts import EmailAuth
from tradingagents.workbench.public_budget import BudgetLedger
from tradingagents.workbench.public_execution import HostedExecution
from tradingagents.workbench.public_gateway import ProviderGateway
from tradingagents.workbench.public_settings import UserSettings
from tradingagents.workbench.public_store import EncryptedStore, StoreConflict, StoreUnavailable

PASSWORD = "fixture-hosted-account-password"
PAYLOAD = {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]}


class AtomicBackend(MemoryBackend):
    def __init__(self, seed):
        super().__init__()
        self.records = copy.deepcopy(seed.records)
        self.revision = seed.revision
        self.lock = RLock()
        self.after_read = None
        self.before_write = None
        self.after_write = None

    def read(self, key):
        with self.lock:
            value = super().read(key)
        if self.after_read:
            self.after_read(key)
        return value

    def write(self, key, content, expected_version=None):
        if self.before_write:
            self.before_write(key, expected_version)
        with self.lock:
            version = super().write(key, content, expected_version)
        if self.after_write:
            self.after_write(key, version)
        return version


@pytest.fixture(scope="module")
def accounts():
    backend = MemoryBackend()
    auth = EmailAuth(EncryptedStore(backend, b"h" * 32), origin=ORIGIN, clock=lambda: NOW)
    owner = auth.bootstrap(PASSWORD)
    invite = auth.invite(owner.session_id, "hosted-guest@example.com")
    guest = auth.redeem(invite.token, invite.email, PASSWORD)
    return backend, owner, guest


@pytest.fixture
def rig(accounts):
    seed, owner, guest = accounts
    backend = AtomicBackend(seed)
    store = EncryptedStore(backend, b"h" * 32)
    now = [NOW]

    def clock():
        return now[0]

    auth = EmailAuth(store, origin=ORIGIN, clock=clock)
    settings = UserSettings(store)
    for session in (owner, guest):
        settings.update(session.principal, {
            "mode": "api_key", "provider": "openai", "quick_model": "gpt-5",
            "deep_model": "gpt-5", "api_key": "fixture-real-api-key",
            "data_keys": {"fred": "fixture-real-fred-key"},
        }, expected_version=None)
    gateway = ProviderGateway(
        store, settings=settings, budget=BudgetLedger(store, clock=clock),
        managed_credential=object(), installation={"foundry": None},
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        clock=clock,
    )
    wire = ArmWire()

    def execution():
        return HostedExecution(
            store, gateway=gateway, installation=installation(), credential=HostCredential(),
            client=httpx.Client(transport=httpx.MockTransport(wire)), clock=clock,
        )

    return SimpleNamespace(backend=backend, store=store, now=now, clock=clock, auth=auth,
                           settings=settings, gateway=gateway, wire=wire, execution=execution,
                           owner=owner, guest=guest)


def test_create_deduplicates_and_persists_intent_before_cloud(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRuns

    service = HostedRuns(auth=rig.auth, settings=rig.settings, execution=rig.execution(), clock=rig.clock)
    submission = str(uuid4())
    root = rig.owner.principal.owner_key + "/hosted-runs/"

    def check_intent(request, body):
        if request.method in {"PUT", "PATCH"} or request.url.path.endswith("/start"):
            index = rig.store.read(root + "index")[0]
            assert len(index["runs"]) == 1
            run_id = index["runs"][0]["intent"]["id"]
            record = rig.store.read(root + run_id)[0]
            assert record["id"] == run_id
            assert record["submission_id"] == submission

    rig.wire.before = check_intent
    created = service.create(rig.owner.session_id, PAYLOAD, submission)
    assert str(UUID(created["id"])) == created["id"]
    assert created["status"] == "running"
    assert created["request"]["ticker"] == "MSFT"
    assert created["request"]["analysts"] == ["market"]
    assert service.create(rig.owner.session_id, PAYLOAD, submission)["id"] == created["id"]
    assert service.get(rig.owner.session_id, created["id"])["request"] == created["request"]
    assert len(service.list(rig.owner.session_id)) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    public = json.dumps([created, service.list(rig.owner.session_id)]) + repr(service)
    for secret in ("signature", "settings_snapshot", "profile_id", "fixture-real-api-key",
                   "fixture-real-fred-key", rig.wire.secrets[0]["value"]):
        assert secret not in public


def service_for(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRuns

    return HostedRuns(auth=rig.auth, settings=rig.settings, execution=rig.execution(), clock=rig.clock)


def test_background_submission_is_durable_and_idempotent_without_cloud_calls(rig):
    service = service_for(rig)
    submission = str(uuid4())
    first = service.submit(rig.owner.session_id, PAYLOAD, submission)
    second = service_for(rig).submit(rig.owner.session_id, PAYLOAD, submission)
    assert first["id"] == second["id"]
    assert first["status"] == second["status"] == "starting"
    assert len(service.list(rig.owner.session_id)) == 1
    assert rig.store.read("email-v1/executions/" + first["id"]) is None
    assert rig.wire.calls == []


def test_background_recovers_preparation_after_restart_and_logout_without_manual_resume(rig):
    service = service_for(rig)
    rig.wire.fault = "accepted_empty"
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    rig.auth.logout(rig.owner.session_id)
    assert service_for(rig).advance_pending() == 1
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)
    rig.now[0] += 10
    assert service_for(rig).advance_pending() == 1
    record = rig.store.read(rig.owner.principal.owner_key + "/hosted-runs/" + created["id"])[0]
    assert record["status"] == "running"
    rig.now[0] += 10
    service_for(rig).advance_pending()
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1


def test_background_rejects_conflicting_submission_and_excludes_legacy(rig, monkeypatch):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    submission = str(uuid4())
    with monkeypatch.context() as interrupted:
        interrupted.setattr(service, "_begin", lambda *args: None)
        service.create(rig.owner.session_id, PAYLOAD, submission)
    existing = service.submit(rig.owner.session_id, PAYLOAD, submission)
    with pytest.raises(HostedRunsError) as caught:
        service.submit(rig.owner.session_id, {**PAYLOAD, "ticker": "AAPL"}, submission)
    assert caught.value.status_code == 409
    assert service.advance_pending() == 0
    assert service.get(rig.owner.session_id, existing["id"])["status"] == "starting"
    assert rig.wire.calls == []


@pytest.mark.parametrize("fault", ["start", "secret"])
def test_background_lost_cloud_response_never_repeats_start_or_prepare(rig, fault):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    rig.wire.fault = fault
    service.advance_pending()
    for _attempt in range(3):
        rig.now[0] += 30
        service_for(rig).advance_pending()
    assert service.get(rig.owner.session_id, created["id"])["status"] == "running"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1


def test_background_claim_excludes_second_coordinator(rig):
    service = service_for(rig)
    service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    entered, resume = Barrier(2), Barrier(2)

    def hold(request, body):
        if request.method == "PATCH":
            entered.wait(timeout=10)
            resume.wait(timeout=10)

    rig.wire.before = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.advance_pending)
        try:
            entered.wait(timeout=10)
            assert service_for(rig).advance_pending() == 0
        finally:
            resume.wait(timeout=10)
        assert running.result(timeout=10) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_background_recovers_crash_before_lower_start_journal(rig):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    principal = rig.owner.principal
    record, version = service._read(principal, created["id"])
    from tradingagents.workbench.public_settings import SettingsSnapshot

    rig.execution().prepare(created["id"], principal, SettingsSnapshot(**record["settings_snapshot"]))
    record.update(prepare_attempted=True, start_attempted=True, status="prepared")
    service._save(principal, record, version)
    assert service_for(rig).advance_pending() == 1
    assert service.get(rig.owner.session_id, created["id"])["status"] == "running"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


@pytest.mark.parametrize("cancel", ["stop", "disable", "generation", "deadline"])
def test_background_cancellation_never_starts(rig, cancel):
    service = service_for(rig)
    created = service.submit(rig.guest.session_id, PAYLOAD, str(uuid4()))
    if cancel == "stop":
        service.stop(rig.guest.session_id, created["id"])
    elif cancel == "disable":
        rig.auth.disable(rig.owner.session_id, rig.guest.principal.object_id,
                         expected_version=rig.auth.accounts(rig.owner.session_id)['version'])
    elif cancel == "generation":
        from tradingagents.workbench.public_accounts import REGISTRY_KEY

        registry, version = rig.auth._registry()
        registry["users"][rig.guest.principal.object_id]["generation"] += 1
        rig.store.write(REGISTRY_KEY, registry, version)
    else:
        rig.now[0] += 4201
    service_for(rig).advance_pending()
    record = rig.store.read(rig.guest.principal.owner_key + "/hosted-runs/" + created["id"])[0]
    assert record["status"] == ("stopped" if cancel == "stop" else "failed")
    assert rig.wire.calls == []


def test_background_ambiguous_start_expires_without_resubmission(rig):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    rig.wire.fault = "start"
    service.advance_pending()
    rig.wire.executions = []
    rig.now[0] += 30
    service.advance_pending()
    rig.now[0] += 4201
    service.advance_pending()
    record = rig.store.read(rig.owner.principal.owner_key + "/hosted-runs/" + created["id"])[0]
    assert record["status"] == "reconciliation_required" and record["error_code"] == "automatic_expired"
    assert record["stop_pending"]
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_background_retries_unconfirmed_stop_after_authorization_changes(rig):
    service = service_for(rig)
    created = service.submit(rig.guest.session_id, PAYLOAD, str(uuid4()))
    service.advance_pending()
    rig.auth.disable(rig.owner.session_id, rig.guest.principal.object_id,
                     expected_version=rig.auth.accounts(rig.owner.session_id)['version'])
    rig.wire.fault = "stop"
    rig.now[0] += 30
    service.advance_pending()
    record, _ = service._read(rig.guest.principal, created["id"])
    assert record["status"] == "reconciliation_required" and record["stop_pending"]
    rig.wire.executions[0]["properties"]["status"] = "Stopped"
    rig.now[0] += 30
    service.advance_pending()
    record, _ = service._read(rig.guest.principal, created["id"])
    assert record["status"] == "failed" and not record["stop_pending"]
    assert rig.store.read("email-v1/executions/" + created["id"])[0]["revoked"]


def test_background_corrupt_claim_does_not_block_another_task(rig):
    service = service_for(rig)
    first = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    second = service.submit(rig.guest.session_id, PAYLOAD, str(uuid4()))
    rig.store.write(rig.owner.principal.owner_key + "/hosted-runs/work/" + first["id"], {"attempts": "invalid", "next_at": None})
    assert service.advance_pending() == 1
    assert service.get(rig.guest.session_id, second["id"])["status"] == "running"


def test_background_expired_claim_recovers_without_repeating_start(rig):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    key = rig.owner.principal.owner_key + "/hosted-runs/work/" + created["id"]
    rig.store.write(key, {"attempts": 1, "next_at": rig.now[0] + 120})
    assert service.advance_pending() == 0
    rig.now[0] += 121
    assert service_for(rig).advance_pending() == 1
    rig.now[0] += 30
    service_for(rig).advance_pending()
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_background_stop_during_prepare_prevents_start(rig):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))

    def stop_before_start(request, body):
        if request.method == "PATCH":
            rig.wire.before = None
            service.stop(rig.owner.session_id, created["id"])

    rig.wire.before = stop_before_start
    service.advance_pending()
    rig.now[0] += 30
    service.advance_pending()
    assert service.get(rig.owner.session_id, created["id"])["status"] == "stopped"
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


def test_background_cancellation_preserves_concurrent_terminal_record(rig, monkeypatch):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    service.advance_pending()
    expected = []

    def concurrently_finished(run_id, principal):
        record, version = service._read(principal, run_id)
        record.update(status="succeeded", cloud_status="Succeeded", stop_pending=False)
        record, _ = service._save(principal, record, version)
        expected.append(record)
        return {"status": "terminal"}

    monkeypatch.setattr(service._execution, "stop", concurrently_finished)
    service._end_automatic(rig.owner.principal, created["id"], "automatic_expired")
    assert service._read(rig.owner.principal, created["id"])[0] == expected[0]


def test_background_cancellation_is_not_overwritten_by_stale_poll(rig, monkeypatch):
    service = service_for(rig)
    created = service.submit(rig.owner.session_id, PAYLOAD, str(uuid4()))
    service.advance_pending()
    rig.now[0] += 30
    cancelled = []

    def cancel_before_poll_save(run_id, principal):
        service._end_automatic(principal, run_id, "automatic_expired")
        cancelled.append(service._read(principal, run_id)[0])
        return []

    monkeypatch.setattr(service._execution, "get_events", cancel_before_poll_save)
    service.refresh(rig.owner.session_id, created["id"])
    record, _ = service._read(rig.owner.principal, created["id"])
    assert record["status"] == cancelled[0]["status"]
    assert record["error_code"] == "automatic_expired" and record["stop_pending"]
    service._end_automatic(rig.owner.principal, created["id"], "automatic_expired")
    assert service._read(rig.owner.principal, created["id"])[0]["status"] == "failed"


@pytest.mark.parametrize("lost_start", [False, True])
def test_explicit_resume_starts_reconciled_preparation_once_after_restart(rig, lost_start):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    rig.wire.fault = "accepted_empty"
    created = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    assert created["status"] == "reconciliation_required"
    assert service_for(rig).resume(rig.owner.session_id, run_id)["status"] == "reconciliation_required"
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "prepared"
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)
    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).resume(rig.guest.session_id, run_id)
    assert caught.value.status_code == 404
    rig.wire.fault = "start" if lost_start else None
    resumed = service_for(rig).resume(rig.owner.session_id, run_id)
    assert resumed["status"] == ("reconciliation_required" if lost_start else "running")
    assert service_for(rig).resume(rig.owner.session_id, run_id)["status"] == resumed["status"]
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1


def test_explicit_resume_never_restarts_stopped_preparation(rig):
    rig.wire.fault = "accepted_empty"
    run_id = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "prepared"
    assert service_for(rig).stop(rig.owner.session_id, run_id)["status"] == "stopped"
    assert service_for(rig).resume(rig.owner.session_id, run_id)["status"] == "stopped"
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)


@pytest.mark.parametrize("mode", ["analysis", "connectivity"])
def test_unavailable_managed_identity_rejects_new_run_before_intent_or_execution(rig, mode):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    principal = rig.owner.principal
    current = rig.settings.get(principal)
    rig.settings.update(principal, {"mode": "managed_identity"}, expected_version=current["version"])
    payload = PAYLOAD if mode == "analysis" else {
        **PAYLOAD,
        "mode": "connectivity",
        "language": "English",
        "ticker": "qqq",
    }

    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).create(rig.owner.session_id, payload, str(uuid4()))

    assert caught.value.status_code == 409
    assert rig.store.read(principal.owner_key + "/hosted-runs/index") is None
    assert rig.wire.calls == []


def test_selected_managed_identity_models_create_run_without_orphaned_index(rig):
    from tests.test_public_gateway import foundry_installation

    installation_config = foundry_installation(deployment="gpt-5.6-sol")
    installation_config["foundry_models"] = [
        foundry_installation(deployment=name, model_family=name)["foundry"]
        for name in ("gpt-5.6-luna", "gpt-5.6-terra")
    ]
    rig.gateway._installation = installation_config
    principal = rig.owner.principal
    current = rig.settings.get(principal)
    rig.settings.update(principal, {
        "mode": "managed_identity",
        "managed_models": {"quick_model": "gpt-5.6-terra", "deep_model": "gpt-5.6-sol"},
    }, expected_version=current["version"])

    created = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))

    assert created["status"] == "running"
    assert rig.store.read(principal.owner_key + "/hosted-runs/" + created["id"]) is not None
    execution = rig.store.read("email-v1/executions/" + created["id"])[0]
    assert execution["worker_config"]["quick_model"] == "gpt-5.6-terra"
    assert execution["worker_config"]["deep_model"] == "gpt-5.6-sol"


def test_revoke_absent_grant_permanently_fences_authorization(rig):
    from tradingagents.workbench.public_gateway import GatewayError

    run_id = str(uuid4())
    snapshot = rig.settings.pin(rig.owner.principal, run_id)
    rig.gateway.revoke(run_id)
    rig.now[0] += 7200
    with pytest.raises(GatewayError):
        rig.gateway.authorize(run_id, rig.owner.principal, snapshot,
                              expires_at=rig.now[0] + 3600)
    rig.gateway.revoke(run_id)
    with pytest.raises(GatewayError) as caught:
        rig.gateway.worker_config(run_id + "." + "a" * 43)
    assert caught.value.status_code == 403
    assert rig.wire.calls == []


@pytest.mark.parametrize("operation", ["start", "create", "reconcile", "stop"])
def test_expired_prepared_run_cancels_without_start_and_new_user_can_submit(rig, operation):
    from tradingagents.workbench.public_gateway import GatewayError

    service = service_for(rig)
    rig.wire.fault = "secret"
    submission = str(uuid4())
    created = service.create(rig.owner.session_id, PAYLOAD, submission)
    run_id = created["id"]
    assert service.reconcile(rig.owner.session_id, run_id)["status"] == "prepared"
    capability = rig.wire.secrets[0]["value"]
    rig.wire.fault = None
    rig.now[0] += 3601
    before = len(rig.wire.calls)
    if operation == "start":
        view = rig.execution().start(run_id, rig.owner.principal, created["request"])
        assert view["status"] == "terminal"
        view = service_for(rig).reconcile(rig.owner.session_id, run_id)
    elif operation == "create":
        view = service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    else:
        view = getattr(service_for(rig), operation)(rig.owner.session_id, run_id)
    assert view["status"] == "stopped"
    assert view["error_code"] == "authorization_expired"
    assert "expired" in view["error"].lower()
    assert not view["start_pending"] and not view["start_uncertain"]
    assert not view["stop_pending"]
    assert view["execution_name"] is None and view["cloud_status"] is None
    assert not any(method in {"PUT", "PATCH", "POST"} for method, path, body in rig.wire.calls[before:])
    private = rig.store.read("email-v1/executions/" + run_id)[0]
    assert private["start_attempted"] is False
    assert private["stop_attempted"] is True and private["revoked"] is True
    assert rig.execution().get(run_id, rig.owner.principal)["authorization_expires_at"] == NOW + 3600
    assert rig.gateway._budget.snapshot()["reserved_microusd"] == 0
    assert rig.gateway._budget.snapshot()["spent_microusd"] == 0
    assert service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)["status"] == "stopped"
    assert service_for(rig).stop(rig.owner.session_id, run_id)["status"] == "stopped"
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["error_code"] == "authorization_expired"
    with pytest.raises(GatewayError) as caught:
        rig.gateway.worker_config(capability)
    assert caught.value.status_code == 403
    following = service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))
    assert following["status"] == "running" and following["id"] != run_id
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert rig.wire.secrets[0]["value"] != capability


@pytest.mark.parametrize("target", ["authorization_attempted", "grant"])
@pytest.mark.parametrize("operation", ["reconcile", "stop"])
def test_lost_authorization_cancels_only_after_durable_revoke_barrier(rig, target, operation):
    from tradingagents.workbench.public_gateway import GatewayError

    def lose_ack(key, version):
        matched = key.startswith("email-v1/gateway/runs/") if target == "grant" else (
            key.startswith("email-v1/executions/")
            and rig.store.read(key)[0]["status"] == target)
        if matched:
            rig.backend.after_write = None
            raise StoreUnavailable("fixture-authorization-accepted-lost")

    rig.backend.after_write = lose_ack
    submission = str(uuid4())
    created = service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    run_id = created["id"]
    assert created["status"] == "reconciliation_required"
    key = "email-v1/executions/" + run_id
    assert rig.store.read(key)[0]["capability"] is None
    view = getattr(service_for(rig), operation)(rig.owner.session_id, run_id)
    assert view["status"] == "stopped"
    assert view["error_code"] == "cancelled_before_start"
    record = rig.store.read(key)[0]
    assert record["revoked"] and record["stop_attempted"]
    assert not record["secret_attempted"] and not record["start_attempted"]
    assert rig.wire.calls == []
    with pytest.raises(GatewayError):
        rig.gateway.authorize(run_id, rig.owner.principal,
                              rig.settings.pin(rig.owner.principal, run_id), expires_at=NOW + 3600)
    assert service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)["status"] == "stopped"
    following = service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))
    assert following["status"] == "running" and following["id"] != run_id


@pytest.mark.parametrize("gate", ["before_grant", "after_grant", "after_authorized"])
def test_stop_barrier_fences_inflight_prepare_before_any_secret_write(rig, gate):
    from tradingagents.workbench.public_gateway import GatewayError

    entered, resume = Barrier(2), Barrier(2)
    blocked = []

    def hold(key, version):
        matched = (key.startswith("email-v1/executions/")
                   and rig.store.read(key)[0]["status"] == "authorized") if gate == "after_authorized" else (
                       key.startswith("email-v1/gateway/runs/"))
        if matched and not blocked:
            blocked.append(key)
            entered.wait(timeout=10)
            resume.wait(timeout=10)

    if gate == "before_grant":
        rig.backend.before_write = hold
    else:
        rig.backend.after_write = hold
    submission = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        preparing = pool.submit(service_for(rig).create, rig.owner.session_id, PAYLOAD, submission)
        try:
            entered.wait(timeout=10)
            run_id = service_for(rig).list(rig.owner.session_id)[0]["id"]
            stopped = service_for(rig).stop(rig.owner.session_id, run_id)
            assert stopped["status"] == "stopped"
            assert rig.store.read("email-v1/gateway/runs/" + run_id)[0]["revoked"] is True
            following = service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))
            assert following["status"] == "running"
        finally:
            resume.wait(timeout=10)
        late = preparing.result(timeout=10)
    assert late["status"] == "stopped"
    record = rig.store.read("email-v1/executions/" + run_id)[0]
    assert record["status"] == "terminal" and record["revoked"]
    assert not record["secret_attempted"] and not record["start_attempted"]
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    with pytest.raises(GatewayError):
        rig.gateway.authorize(run_id, rig.owner.principal, rig.settings.pin(rig.owner.principal, run_id),
                              expires_at=NOW + 3600)


def test_gateway_rechecks_revocation_before_returning_delayed_authorization(rig):
    from tradingagents.workbench.public_gateway import GatewayError

    run_id = str(uuid4())
    snapshot = rig.settings.pin(rig.owner.principal, run_id)
    entered, resume = Barrier(2), Barrier(2)
    blocked = []

    def hold(key, version):
        if key == "email-v1/gateway/runs/" + run_id and not blocked:
            blocked.append(True)
            entered.wait(timeout=10)
            resume.wait(timeout=10)

    rig.backend.after_write = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        authorizing = pool.submit(rig.gateway.authorize, run_id, rig.owner.principal,
                                  snapshot, expires_at=NOW + 3600)
        try:
            entered.wait(timeout=10)
            grant = rig.store.read("email-v1/gateway/runs/" + run_id)[0]
            rig.gateway.revoke(run_id)
            revoked = rig.store.read("email-v1/gateway/runs/" + run_id)[0]
            assert revoked == {**grant, "revoked": True}
        finally:
            resume.wait(timeout=10)
        with pytest.raises(GatewayError) as caught:
            authorizing.result(timeout=10)
        assert caught.value.status_code == 403


def test_stop_between_worker_config_and_secret_journal_rejects_stale_cas(rig, monkeypatch):
    entered, resume = Barrier(2), Barrier(2)
    original = rig.gateway.worker_config
    conflicts = []

    def delayed(capability):
        config = original(capability)
        entered.wait(timeout=10)
        resume.wait(timeout=10)
        return config

    monkeypatch.setattr(rig.gateway, "worker_config", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        preparing = pool.submit(service_for(rig).create, rig.owner.session_id, PAYLOAD, str(uuid4()))
        try:
            entered.wait(timeout=10)
            run_id = service_for(rig).list(rig.owner.session_id)[0]["id"]
            assert service_for(rig).stop(rig.owner.session_id, run_id)["status"] == "stopped"

            def stale_write(key, version):
                if key == "email-v1/executions/" + run_id:
                    record, current = rig.store.read(key)
                    assert record["status"] == "terminal" and version != current
                    conflicts.append(key)

            rig.backend.before_write = stale_write
        finally:
            resume.wait(timeout=10)
        assert preparing.result(timeout=10)["status"] == "stopped"
    assert conflicts == ["email-v1/executions/" + run_id]
    assert rig.wire.calls == []


def test_authorize_winning_tombstone_cas_race_does_not_release_until_retry(rig):
    from tradingagents.workbench.public_gateway import GatewayError

    def lose_journal(key, version):
        if key.startswith("email-v1/executions/") and rig.store.read(key)[0]["authorization_attempted"]:
            rig.backend.after_write = None
            raise StoreUnavailable("fixture-lost-journal")

    rig.backend.after_write = lose_journal
    run_id = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    grant_key = "email-v1/gateway/runs/" + run_id
    entered, resume = Barrier(2), Barrier(2)
    blocked = []

    def hold(key, version):
        if key == grant_key and not blocked:
            assert version is None
            blocked.append(True)
            entered.wait(timeout=10)
            resume.wait(timeout=10)

    rig.backend.before_write = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(service_for(rig).stop, rig.owner.session_id, run_id)
        try:
            entered.wait(timeout=10)
            capability = rig.gateway.authorize(run_id, rig.owner.principal,
                rig.settings.pin(rig.owner.principal, run_id), expires_at=NOW + 3600)
        finally:
            resume.wait(timeout=10)
        assert stopping.result(timeout=10)["status"] == "reconciliation_required"
    reservation_key = next(key for key in rig.backend.records if key.startswith("email-v1/execution-jobs/"))
    assert rig.store.read(reservation_key)[0]["released"] is False
    original_grant = rig.store.read(grant_key)[0]
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "stopped"
    assert rig.store.read(grant_key)[0] == {**original_grant, "revoked": True}
    with pytest.raises(GatewayError) as caught:
        rig.gateway.worker_config(capability)
    assert caught.value.status_code == 403
    assert rig.wire.calls == []
    assert service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))["status"] == "running"


@pytest.mark.parametrize("replacement", [
    {"revoked": False}, {"revoked": 1}, {"expires_at": NOW}, {"principal": {}}, {"run_id": str(uuid4())},
])
def test_malformed_tombstone_never_confirms_revocation(rig, replacement):
    from tradingagents.workbench.public_gateway import GatewayError

    run_id = str(uuid4())
    rig.store.write("email-v1/gateway/runs/" + run_id, {"run_id": run_id, "revoked": True, **replacement})
    for operation in (lambda: rig.gateway.revoke(run_id),
                      lambda: rig.gateway.worker_config(run_id + "." + "a" * 43)):
        with pytest.raises(GatewayError) as caught:
            operation()
        assert caught.value.status_code == 503


@pytest.mark.parametrize("fault", ["read", "before_write", "after_write"])
@pytest.mark.parametrize("grant_exists", [False, True])
def test_uncertain_revocation_keeps_reservation_until_barrier_confirmed(rig, fault, grant_exists):
    from tradingagents.workbench.public_gateway import GatewayError

    def lose_authorization(key, version):
        matched = key.startswith("email-v1/gateway/runs/") if grant_exists else (
            key.startswith("email-v1/executions/")
            and rig.store.read(key)[0]["status"] == "authorization_attempted")
        if matched:
            rig.backend.after_write = None
            raise StoreUnavailable("fixture-lost-authorization")

    rig.backend.after_write = lose_authorization
    created = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    grant_key = "email-v1/gateway/runs/" + run_id

    def fail(key, *args):
        if key == grant_key:
            raise StoreUnavailable("fixture-uncertain-revocation")

    setattr(rig.backend, "after_read" if fault == "read" else fault, fail)
    view = service_for(rig).stop(rig.owner.session_id, run_id)
    assert view["status"] == "reconciliation_required"
    record = rig.store.read("email-v1/executions/" + run_id)[0]
    assert record["stop_attempted"] and not record["revoked"]
    reservation_key = next(key for key in rig.backend.records if key.startswith("email-v1/execution-jobs/"))
    assert rig.store.read(reservation_key)[0]["released"] is False
    assert rig.wire.calls == []
    rig.backend.after_read = rig.backend.before_write = rig.backend.after_write = None
    terminal = service_for(rig).reconcile(rig.owner.session_id, run_id)
    assert terminal["status"] == "stopped"
    assert rig.store.read(reservation_key)[0]["released"] is True
    with pytest.raises(GatewayError):
        rig.gateway.authorize(run_id, rig.owner.principal, rig.settings.pin(rig.owner.principal, run_id),
                              expires_at=NOW + 3600)
    assert service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))["status"] == "running"


@pytest.mark.parametrize("stage", ["secret", "start"])
def test_expiry_during_last_cloud_read_cancels_before_mutation(rig, stage):
    seen = []

    def age(request, body):
        if request.method == "GET" and request.url.path.endswith("/executions"):
            seen.append(True)
            if len(seen) == (1 if stage == "secret" else 2):
                rig.now[0] += 3601

    rig.wire.before = age
    view = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    assert view["status"] == "stopped"
    assert view["error_code"] == "authorization_expired"
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == (0 if stage == "secret" else 1)
    assert rig.store.read("email-v1/executions/" + view["id"])[0]["start_attempted"] is False


@pytest.mark.parametrize("unknown", ["secret", "start"])
def test_expired_unknown_cloud_attempt_stays_held_without_retry(rig, unknown):
    rig.wire.fault = unknown
    if unknown == "secret":
        rig.wire.provisioning = "InProgress"
    view = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = view["id"]
    if unknown == "start":
        rig.wire.executions = []
    ledger = rig.gateway._budget
    held = ledger.reserve(str(uuid4()), run_id=run_id, amount_microusd=123, price_version="fixture-v1")
    ledger.mark_dispatched(held)
    rig.now[0] += 3601
    for action in ("reconcile", "stop", "reconcile"):
        view = getattr(service_for(rig), action)(rig.owner.session_id, run_id)
        assert view["status"] == "reconciliation_required"
        assert view["error_code"] == "authorization_expired"
    following = service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))
    assert following["status"] == "reconciliation_required"
    assert ledger.snapshot()["reserved_microusd"] == 123
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == (1 if unknown == "start" else 0)


@pytest.mark.parametrize("accepted", [False, True])
def test_cancellation_stop_intent_must_be_durable_before_revoke(rig, accepted):
    from tradingagents.workbench.public_execution import ExecutionError

    run_id = str(uuid4())
    principal = rig.owner.principal
    rig.execution().prepare(run_id, principal, rig.settings.pin(principal, run_id))
    key = "email-v1/executions/" + run_id
    grant_key = "email-v1/gateway/runs/" + run_id

    def fail(target, version):
        if target == key:
            rig.backend.before_write = rig.backend.after_write = None
            raise StoreUnavailable("fixture-stop-journal-failure")

    setattr(rig.backend, "after_write" if accepted else "before_write", fail)
    with pytest.raises(ExecutionError):
        rig.execution().stop(run_id, principal)
    assert rig.store.read(key)[0]["stop_attempted"] is accepted
    assert rig.store.read(grant_key)[0]["revoked"] is accepted
    reservation_key = next(key for key in rig.backend.records if key.startswith("email-v1/execution-jobs/"))
    assert rig.store.read(reservation_key)[0]["released"] is False
    assert rig.execution().stop(run_id, principal)["status"] == "terminal"


@pytest.mark.parametrize("stage", ["terminal", "release"])
@pytest.mark.parametrize("accepted", [False, True])
def test_cancel_release_requires_confirmed_terminal_cas(rig, stage, accepted):
    rig.wire.fault = "secret"
    created = service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "prepared"
    rig.wire.fault = None
    execution_key = "email-v1/executions/" + run_id
    reservation_key = next(key for key in rig.backend.records if key.startswith("email-v1/execution-jobs/"))
    fired = []

    def fail(key, version):
        record = rig.store.read(execution_key)[0]
        matched = key == reservation_key if stage == "release" else (
            key == execution_key and record["revoked"]
            and record["status"] == ("terminal" if accepted else "stop_attempted"))
        if matched:
            fired.append(True)
            rig.backend.before_write = rig.backend.after_write = None
            raise StoreUnavailable("fixture-cancellation-ack-failure")

    setattr(rig.backend, "after_write" if accepted else "before_write", fail)
    service_for(rig).stop(rig.owner.session_id, run_id)
    assert fired == [True]
    record = rig.store.read(execution_key)[0]
    released = rig.store.read(reservation_key)[0]["released"]
    assert released is (stage == "release" and accepted)
    assert not released or record["status"] == "terminal" and record["revoked"]
    assert not record["start_attempted"]
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "stopped"
    assert rig.store.read(reservation_key)[0]["released"] is True
    assert not any(path.endswith("/start") for method, path, body in rig.wire.calls)
    assert service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))["status"] == "running"


@pytest.mark.parametrize("stage", ["prepared", "authorization_attempted", "grant"])
def test_legacy_journal_cancels_without_reissuing_authorization(rig, stage):
    from tradingagents.workbench.public_execution import ExecutionError

    run_id = str(uuid4())
    principal = rig.owner.principal
    snapshot = rig.settings.pin(principal, run_id)
    key = "email-v1/executions/" + run_id

    def lose_ack(target, version):
        matched = target.startswith("email-v1/gateway/runs/") if stage == "grant" else (
            target == key and rig.store.read(key)[0]["status"] == "authorization_attempted")
        if matched:
            rig.backend.after_write = None
            raise StoreUnavailable("fixture-legacy-ack-loss")

    if stage == "prepared":
        rig.execution().prepare(run_id, principal, snapshot)
    else:
        rig.backend.after_write = lose_ack
        with pytest.raises(ExecutionError):
            rig.execution().prepare(run_id, principal, snapshot)
    record, version = rig.store.read(key)
    record.pop("authorization_expires_at")
    record.pop("error_code")
    rig.store.write(key, record, version)
    rig.now[0] += 3601
    before = len(rig.wire.calls)
    view = rig.execution().reconcile(run_id, principal)
    assert view["status"] == "terminal" and view["revoked"]
    assert view["authorization_expires_at"] == (None if stage == "authorization_attempted" else NOW + 3600)
    assert len(rig.wire.calls) == before
    following = service_for(rig).create(rig.guest.session_id, PAYLOAD, str(uuid4()))
    assert following["status"] == "running"


def result_payload():
    return {"decision": "Hold", "reports": {"market_report": "Verified market report."},
            "stats": {"llm_calls": 2, "tool_calls": 1, "input_tokens": 40, "output_tokens": 20},
            "connectivity": []}


def emit(rig, run_id, kind, payload):
    rig.wire.log_lines.extend(encode_event(run_id, kind, payload))


def test_direct_worker_events_reject_cross_run_injection(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    other_run_id = str(uuid4())
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]

    with pytest.raises(HostedRunsError) as caught:
        service.ingest_worker_events(
            capability,
            encode_event(other_run_id, "phase", {"stage": "analyst:market"}),
        )

    assert caught.value.status_code == 400
    assert service.get(rig.owner.session_id, run_id)["events"] == []


def test_direct_worker_events_reject_extra_foreign_line(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    lines = encode_event(run_id, "phase", {"stage": "analyst:market"})
    lines += encode_event(str(uuid4()), "phase", {"stage": "analyst:news"})
    with pytest.raises(HostedRunsError) as caught:
        service.ingest_worker_events(capability, lines)
    assert caught.value.status_code == 400
    assert service.get(rig.owner.session_id, run_id)["events"] == []


@pytest.mark.parametrize("corrupt", ["extra", "duplicate", "incomplete", "malformed"])
def test_direct_worker_events_require_one_complete_message(rig, corrupt):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    lines = encode_event(run_id, "phase", {"stage": "analyst:market"})
    if corrupt == "extra":
        lines += encode_event(run_id, "phase", {"stage": "analyst:news"})
    elif corrupt == "duplicate":
        lines += lines
    elif corrupt == "incomplete":
        lines = encode_event(run_id, "report", {"section": "market_report", "content": "".join(
            f"{index:08x}" for index in range(160000))})[:-1]
    else:
        lines += ["not a protocol line"]
    with pytest.raises(HostedRunsError) as caught:
        service.ingest_worker_events(capability, lines)
    assert caught.value.status_code == 400
    assert service.get(rig.owner.session_id, run_id)["events"] == []


def test_direct_worker_events_redact_full_and_signature_capability(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    lines = encode_event(run_id, "report", {"section": "market_report", "content": {
        "nested": [capability, capability.split(".")[1]],
    }})
    assert service.ingest_worker_events(capability, lines) == {"accepted": True}
    persisted = json.dumps(service.get(rig.owner.session_id, run_id))
    assert capability not in persisted
    assert capability.split(".")[1] not in persisted
    assert "[REDACTED]" in persisted


def test_direct_worker_events_source_write_failure_is_not_labeled_logs(rig, monkeypatch):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    original = service._save_source
    monkeypatch.setattr(service, "_save_source", lambda *args: (_ for _ in ()).throw(RuntimeError("sidecar")))
    with pytest.raises(RuntimeError):
        service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "analyst:market"}))
    monkeypatch.setattr(service, "_save_source", original)
    assert service.event_snapshot(rig.owner.session_id, run_id)["events"] == []
    rig.wire.log_lines.extend(encode_event(run_id, "phase", {"stage": "analyst:news"}))
    service.refresh(rig.owner.session_id, run_id)
    assert service.event_snapshot(rig.owner.session_id, run_id)["source"] == "logs"


def test_direct_worker_events_reject_revoked_capability(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    rig.gateway.revoke(run_id)

    with pytest.raises(HostedRunsError) as caught:
        service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "analyst:market"}))

    assert caught.value.status_code == 403


def test_direct_worker_events_revalidate_capability_on_cas_retry(rig, monkeypatch):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    original = service._save
    attempted = [False]

    def lose_first_write(principal, record, version):
        if not attempted[0]:
            attempted[0] = True
            rig.gateway.revoke(run_id)
            raise StoreConflict("concurrent revoke")
        return original(principal, record, version)

    monkeypatch.setattr(service, "_save", lose_first_write)
    with pytest.raises(HostedRunsError) as caught:
        service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "analyst:market"}))
    assert caught.value.status_code == 403
    assert service.get(rig.owner.session_id, run_id)["events"] == []


def test_direct_worker_events_deduplicate_with_polled_logs(rig):
    service = service_for(rig)
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    lines = encode_event(run_id, "phase", {"stage": "analyst:market"})

    assert service.ingest_worker_events(capability, lines) == {"accepted": True}
    rig.wire.log_lines.extend(lines)
    refreshed = service.refresh(rig.owner.session_id, run_id)

    assert [event["kind"] for event in refreshed["events"]] == ["phase"]
    assert refreshed["events"][0]["payload"] == {"stage": "analyst:market"}
    assert service.event_snapshot(rig.owner.session_id, run_id)["source"] == "direct"


def test_distinct_direct_and_log_events_have_mixed_provenance(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "analyst:market"}))
    rig.wire.log_lines.extend(encode_event(run_id, "phase", {"stage": "analyst:news"}))
    service.refresh(rig.owner.session_id, run_id)
    snapshot = service.event_snapshot(rig.owner.session_id, run_id)
    assert snapshot["source"] == "mixed"
    assert len(snapshot["events"]) == 2


def test_direct_worker_events_preserve_unknown_legacy_provenance(rig):
    from tradingagents.workbench.contracts import decode_events

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    record, version = service._read(rig.owner.principal, run_id)
    record["events"] = decode_events(encode_event(run_id, "phase", {"stage": "market"}), run_id)
    service._save(rig.owner.principal, record, version)
    assert service.event_snapshot(rig.owner.session_id, run_id)["source"] == "mixed"

    service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "news"}))

    snapshot = service.event_snapshot(rig.owner.session_id, run_id)
    assert snapshot["source"] == "mixed"
    assert len(snapshot["events"]) == 2


def test_log_source_write_failure_cannot_publish_log_events_as_direct(rig, monkeypatch):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    service.ingest_worker_events(capability, encode_event(run_id, "phase", {"stage": "market"}))
    rig.wire.log_lines.extend(encode_event(run_id, "phase", {"stage": "news"}))
    monkeypatch.setattr(service, "_save_source", lambda *args: (_ for _ in ()).throw(RuntimeError("sidecar")))
    with pytest.raises(HostedRunsError):
        service.refresh(rig.owner.session_id, run_id)
    snapshot = service.event_snapshot(rig.owner.session_id, run_id)
    assert len(snapshot["events"]) == 1 or snapshot["source"] != "direct"


def test_direct_result_does_not_mark_cloud_success_or_release_budget(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.store.read("email-v1/executions/" + run_id)[0]["capability"]
    before = rig.gateway._budget.snapshot()
    service.ingest_worker_events(capability, encode_event(run_id, "result", result_payload()))
    record = service.get(rig.owner.session_id, run_id)
    assert record["result"] == result_payload()
    assert record["cloud_status"] != "Succeeded" and record["status"] != "succeeded"
    assert rig.gateway._budget.snapshot() == before


def test_full_lifecycle_collects_decoded_events_and_exports_bytes(rig):
    service = service_for(rig)
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    emit(rig, run_id, "report", {"section": "market_report", "content": "Intermediate report."})
    emit(rig, run_id, "result", result_payload())
    rig.wire.log_lines *= 2
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    completed = service.refresh(rig.owner.session_id, run_id)
    assert completed["status"] == "succeeded"
    assert completed["cloud_status"] == "Succeeded"
    assert completed["result"] == result_payload()
    assert len(completed["events"]) == 2
    assert rig.execution().get(run_id, rig.owner.principal)["revoked"] is True
    assert service.list(rig.owner.session_id)[0]["has_result"] is True
    assert service.list(rig.owner.session_id)[0]["events"] == []
    assert service.get(rig.owner.session_id, run_id) == completed
    exported = service.export(rig.owner.session_id, run_id)
    assert exported["media_type"] == "text/markdown"
    assert exported["filename"] == f"MSFT-{run_id}.md"
    assert isinstance(exported["content"], bytes)
    assert b"Verified market report." in exported["content"]
    assert b"Hold" in exported["content"]
    exported_json = service.export(rig.owner.session_id, run_id, output_format="json")
    assert json.loads(exported_json["content"]) == completed
    assert "signature" not in exported_json["content"].decode()


@pytest.mark.parametrize("cloud_status", ["Succeeded", "Failed"])
@pytest.mark.parametrize("partial_result", [False, True])
def test_budget_error_never_succeeds_and_retains_partial_reports(rig, cloud_status, partial_result):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    emit(rig, run_id, "report", {"section": "market_report", "content": "Partial research."})
    if partial_result:
        emit(rig, run_id, "result", {**result_payload(), "decision": None})
    emit(rig, run_id, "error", {"code": "budget_exhausted", "message": "fixture-private-provider-error"})
    rig.wire.executions[0]["properties"]["status"] = cloud_status
    view = service.refresh(rig.owner.session_id, run_id)
    assert view["status"] == "failed"
    assert view["error_code"] == "budget_exhausted"
    assert "budget" in view["error"].lower()
    assert "fixture-private-provider-error" not in json.dumps(view)
    assert any(event["kind"] == "report" for event in view["events"])
    assert b"budget" in service.export(rig.owner.session_id, run_id)["content"].lower()
    assert (view["result"] is not None) == partial_result


@pytest.mark.parametrize("core_vendor,alpha_status,model_status,expected", [
    ("yfinance", "unconfigured", "ok", "succeeded"),
    ("alpha_vantage", "unconfigured", "ok", "failed"),
    ("yfinance,alpha_vantage", "unconfigured", "ok", "failed"),
    ("yfinance", "error", "ok", "failed"),
    ("yfinance", "unconfigured", "error", "failed"),
])
def test_connectivity_only_ignores_unselected_alpha_probe(rig, core_vendor, alpha_status, model_status, expected):
    principal = rig.owner.principal
    rig.settings.update(principal, {"data_vendors": {"core_stock_apis": core_vendor}},
                        expected_version=rig.settings.get(principal)["version"])
    service = service_for(rig)
    created = service.create(rig.owner.session_id, {**PAYLOAD, "mode": "connectivity"}, str(uuid4()))
    run_id = created["id"]
    result = {**result_payload(), "decision": None, "reports": {}, "connectivity": [
        {"provider": provider, "status": status, "detail": "Fixture result.", "elapsed_seconds": 0.1}
        for provider, status in [("yfinance", "ok"), ("alpha_vantage", alpha_status), ("fred", "ok"),
                                 ("polymarket", "ok"), ("foundry", model_status)]
    ]}
    emit(rig, run_id, "result", result)
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"

    completed = service.refresh(rig.owner.session_id, run_id)

    assert completed["status"] == expected
    assert completed["error_code"] == (None if expected == "succeeded" else "partial_result")
    assert completed["result"] == result
    assert service.list(rig.owner.session_id)[0]["status"] == expected


def test_late_logs_poll_interval_and_terminal_ingestion_window(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    assert service.refresh(rig.owner.session_id, run_id)["status"] == "collecting"
    before = len(rig.wire.calls)
    rig.now[0] += 4
    assert service.refresh(rig.owner.session_id, run_id)["status"] == "collecting"
    assert len(rig.wire.calls) == before
    rig.now[0] += 1
    emit(rig, run_id, "result", result_payload())
    assert service.refresh(rig.owner.session_id, run_id)["status"] == "succeeded"
    emit(rig, run_id, "error", {"code": "budget_exhausted"})
    rig.now[0] += 5
    assert service.refresh(rig.owner.session_id, run_id)["error_code"] == "budget_exhausted"
    rig.now[0] += 600
    service.refresh(rig.owner.session_id, run_id)
    before = len(rig.wire.calls)
    service.refresh(rig.owner.session_id, run_id)
    assert len(rig.wire.calls) == before


def test_restart_after_accepted_lost_start_never_reposts(rig):
    service = service_for(rig)
    rig.wire.fault = "start"
    submission = str(uuid4())
    created = service.create(rig.owner.session_id, PAYLOAD, submission)
    assert created["status"] == "reconciliation_required"
    assert created["start_uncertain"] is True
    restarted = service_for(rig)
    reconciled = restarted.reconcile(rig.owner.session_id, created["id"])
    assert reconciled["status"] == "running"
    assert reconciled["start_uncertain"] is False
    assert restarted.create(rig.owner.session_id, PAYLOAD, submission)["id"] == created["id"]
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_stop_during_log_poll_wins_over_success_and_revokes(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    emit(rig, run_id, "result", result_payload())

    def stop_on_logs(request, body):
        if request.url.host == "api.loganalytics.azure.com":
            rig.wire.before = None
            service_for(rig).stop(rig.owner.session_id, run_id)

    rig.wire.before = stop_on_logs
    view = service.refresh(rig.owner.session_id, run_id)
    assert view["stop_requested"] is True
    assert view["status"] != "succeeded"
    rig.now[0] += 5
    assert service.refresh(rig.owner.session_id, run_id)["status"] == "stopped"
    assert rig.execution().get(run_id, rig.owner.principal)["revoked"] is True
    assert service.export(rig.owner.session_id, run_id)["content"]


def test_explicit_reconcile_bypasses_poll_throttle_and_cloud_failure_is_retryable(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]

    def fail_logs(request, body):
        if request.url.host == "api.loganalytics.azure.com":
            raise httpx.ReadTimeout("fixture-private-provider-error")

    rig.wire.before = fail_logs
    assert service.refresh(rig.owner.session_id, run_id)["poll_error"]
    rig.wire.before = None
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    emit(rig, run_id, "result", result_payload())
    view = service.reconcile(rig.owner.session_id, run_id)
    assert view["status"] == "succeeded"
    assert view["poll_error"] is None


def overwrite(rig, key, mutate):
    value, version = rig.store.read(key)
    mutate(value)
    rig.store.write(key, value, version)


def test_settings_choices_are_pinned_without_request_default_overrides(rig):
    principal = rig.owner.principal
    rig.settings.update(principal, {"asset_type": "etf", "data_vendors": {
        "core_stock_apis": "alpha_vantage", "news_data": "alpha_vantage",
    }}, expected_version=rig.settings.get(principal)["version"])
    service = service_for(rig)
    submission = str(uuid4())
    payload = {**PAYLOAD, "mode": "connectivity", "language": "English", "ticker": "qqq",
               "data_vendors": {"core_stock_apis": "alpha_vantage"}}
    created = service.create(rig.owner.session_id, payload, submission)
    assert created["request"]["asset_type"] == "etf"
    assert created["request"]["data_vendors"]["news_data"] == "alpha_vantage"
    assert created["request"]["mode"] == "connectivity"
    assert created["request"]["language"] == "English"
    assert created["request"]["ticker"] == "QQQ"
    rig.settings.update(principal, {"asset_type": "stock", "api_key": "fixture-rotated-key"},
                        expected_version=rig.settings.get(principal)["version"])
    duplicate = service.create(rig.owner.session_id, payload, submission)
    assert duplicate["request"] == created["request"]
    assert duplicate["settings"] == created["settings"]
    assert service.stop(rig.owner.session_id, created["id"])["stop_requested"] is True
    assert rig.execution().get(created["id"], principal)["revoked"] is True


@pytest.mark.parametrize("payload", [
    {**PAYLOAD, "asset_type": "etf"},
    {**PAYLOAD, "data_vendors": {"news_data": "alpha_vantage"}},
    {**PAYLOAD, "profile_id": "old-profile"},
    {**PAYLOAD, "subscription_id": "cloud-choice"},
    {**PAYLOAD, "api_key": "fixture-private-key"},
    {**PAYLOAD, "ticker": "../../secret"},
    {**PAYLOAD, "ticker": "MSFT\\secret"},
    {**PAYLOAD, "provider": "openai"},
])
def test_request_conflicts_and_cloud_key_overrides_rejected_before_cloud(rig, payload):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).create(rig.owner.session_id, payload, str(uuid4()))
    assert caught.value.status_code == 400
    assert rig.wire.calls == []
    assert service_for(rig).list(rig.owner.session_id) == []


def test_same_submission_different_request_conflicts(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    submission = str(uuid4())
    service.create(rig.owner.session_id, PAYLOAD, submission)
    with pytest.raises(HostedRunsError) as caught:
        service.create(rig.owner.session_id, {**PAYLOAD, "ticker": "AAPL"}, submission)
    assert caught.value.status_code == 409
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_concurrent_same_submission_has_one_intent_and_one_paid_start(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    barrier = Barrier(2)
    root = rig.owner.principal.owner_key + "/hosted-runs/"

    def race_index(key, version):
        if key == root + "index" and version is None:
            barrier.wait(timeout=10)

    rig.backend.before_write = race_index
    submission = str(uuid4())

    def create():
        try:
            return service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
        except HostedRunsError as error:
            assert error.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda attempt: create(), range(2)))
    rig.backend.before_write = None
    found = service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    assert all(view is None or view["id"] == found["id"] for view in views)
    assert len(service_for(rig).list(rig.owner.session_id)) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_concurrent_distinct_creates_merge_index_and_global_execution_reservation(rig):
    barrier = Barrier(2)
    index_key = rig.owner.principal.owner_key + "/hosted-runs/index"

    def race_index(key, version):
        if key == index_key and version is None:
            barrier.wait(timeout=10)

    rig.backend.before_write = race_index
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda submission: service_for(rig).create(
            rig.owner.session_id, PAYLOAD, submission), [str(uuid4()), str(uuid4())]))
    rig.backend.before_write = None
    history = service_for(rig).list(rig.owner.session_id)
    assert {item["id"] for item in history} == {item["id"] for item in views}
    assert [item["id"] for item in history] == sorted((item["id"] for item in history), reverse=True)
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert sorted(item["status"] for item in history) == ["reconciliation_required", "running"]


@pytest.mark.parametrize("operation", ["get", "refresh", "stop", "reconcile", "export"])
def test_cross_account_run_access_is_not_found_without_cloud(rig, operation):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    before = len(rig.wire.calls)
    with pytest.raises(HostedRunsError) as caught:
        getattr(service, operation)(rig.guest.session_id, run_id)
    assert caught.value.status_code == 404
    assert len(rig.wire.calls) == before
    assert service.list(rig.guest.session_id) == []


def test_cross_account_submission_id_is_independent_but_job_is_global(rig):
    service = service_for(rig)
    submission = str(uuid4())
    owner = service.create(rig.owner.session_id, PAYLOAD, submission)
    guest = service.create(rig.guest.session_id, PAYLOAD, submission)
    assert owner["id"] != guest["id"]
    assert guest["status"] == "reconciliation_required"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1
    assert rig.execution().get(owner["id"], rig.owner.principal)["revoked"] is False


@pytest.mark.parametrize("operation", ["list", "get", "refresh", "stop", "reconcile", "export", "create"])
def test_disabled_sessions_cannot_read_or_mutate_runs(rig, operation):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.guest.session_id, PAYLOAD, str(uuid4()))["id"]
    rig.auth.disable(rig.owner.session_id, rig.guest.principal.object_id,
                     expected_version=rig.auth.accounts(rig.owner.session_id)['version'])
    before = len(rig.wire.calls)
    args = () if operation == "list" else (PAYLOAD, str(uuid4())) if operation == "create" else (run_id,)
    with pytest.raises(HostedRunsError) as caught:
        getattr(service, operation)(rig.guest.session_id, *args)
    assert caught.value.status_code == 401
    assert len(rig.wire.calls) == before


@pytest.mark.parametrize("stage", ["index", "record"])
@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_early_storage_writes_never_start_until_retry_finds_intent(rig, stage, accepted):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    root = rig.owner.principal.owner_key + "/hosted-runs/"

    def fail(key, version):
        if key.startswith(root) and (key.endswith("/index") == (stage == "index")):
            rig.backend.before_write = rig.backend.after_write = None
            raise StoreUnavailable("fixture-private-store-error")

    if accepted:
        rig.backend.after_write = fail
    else:
        rig.backend.before_write = fail
    submission = str(uuid4())
    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    assert caught.value.status_code == 503
    assert caught.value.__context__ is None
    assert rig.wire.calls == []
    history = service_for(rig).list(rig.owner.session_id)
    retried = service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    if history:
        assert retried["id"] == history[0]["id"]
    assert len(service_for(rig).list(rig.owner.session_id)) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) <= 1


def test_secret_start_unknown_retained_and_refresh_does_not_repeat_mutation(rig):
    service = service_for(rig)
    rig.wire.fault = "secret"
    submission = str(uuid4())
    created = service.create(rig.owner.session_id, PAYLOAD, submission)
    assert created["status"] == "reconciliation_required"
    view = service_for(rig).refresh(rig.owner.session_id, created["id"])
    assert view["status"] == "prepared"
    assert sum(method == "PATCH" for method, path, body in rig.wire.calls) == 1
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 0
    assert rig.execution().get(created["id"], rig.owner.principal)["revoked"] is False
    retried = service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    assert retried["status"] == "running"
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_stop_during_start_response_never_overwrites_stopped_terminal(rig):
    service = service_for(rig)

    def stop_before_start_readback(request, body):
        if request.method == "GET" and request.url.path.endswith("/executions") and rig.wire.executions:
            rig.wire.before = None
            run_id = service.list(rig.owner.session_id)[0]["id"]
            service_for(rig).stop(rig.owner.session_id, run_id)

    rig.wire.before = stop_before_start_readback
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    assert created["stop_requested"] is True
    view = service.reconcile(rig.owner.session_id, created["id"])
    assert view["status"] == "stopped"
    assert rig.execution().get(created["id"], rig.owner.principal)["revoked"] is True


@pytest.mark.parametrize("target", ["index_shape", "index_summary", "index_metadata", "events", "missing_record"])
def test_malformed_stored_state_fails_closed_before_cloud(rig, target):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    root = rig.owner.principal.owner_key + "/hosted-runs/"
    if target == "index_shape":
        overwrite(rig, root + "index", lambda value: value.update(runs={}))
    elif target == "index_summary":
        overwrite(rig, root + "index", lambda value: value["runs"][0]["summary"].update(signature="private"))
    elif target == "index_metadata":
        def contaminate(value):
            for location in ("intent", "summary"):
                value["runs"][0][location]["settings"]["provider"] = "fixture-private-provider-error"
        overwrite(rig, root + "index", contaminate)
    elif target == "events":
        overwrite(rig, root + run_id, lambda value: value.update(events=[{"kind": "result", "payload": {}}]))
    else:
        rig.backend.records.pop(root + run_id)
    before = len(rig.wire.calls)
    with pytest.raises(HostedRunsError) as caught:
        service.refresh(rig.owner.session_id, run_id)
    assert caught.value.status_code == 503
    assert len(rig.wire.calls) == before
    assert "fixture-private" not in repr(caught.value)


def test_cas_failures_before_start_do_not_orphan_or_start_cloud(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    index_key = rig.owner.principal.owner_key + "/hosted-runs/index"

    def fail_projection(key, version):
        if key == index_key and version is not None:
            raise StoreConflict("fixture-private-store-error")

    rig.backend.before_write = fail_projection
    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    assert caught.value.status_code == 409
    assert rig.wire.calls == []
    rig.backend.before_write = None
    assert len(service_for(rig).list(rig.owner.session_id)) == 1


@pytest.mark.parametrize("field,value", [
    ("provider", "https://fixture-private-key.example"),
    ("quick_model", {"api_key": "fixture-private-key"}),
    ("deep_model", "fixture-private-key\nextra"),
])
def test_list_rejects_malformed_metadata_without_reading_private_execution(rig, field, value):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))

    def contaminate(index):
        for location in ("intent", "summary"):
            index["runs"][0][location]["settings"][field] = value

    overwrite(rig, rig.owner.principal.owner_key + "/hosted-runs/index", contaminate)
    with pytest.raises(HostedRunsError) as caught:
        service.list(rig.owner.session_id)
    assert caught.value.status_code == 503
    assert "fixture-private" not in repr(caught.value)


def test_finished_run_stop_preserves_completed_status(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    emit(rig, run_id, "result", result_payload())
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    completed = service.refresh(rig.owner.session_id, run_id)
    assert completed["status"] == "succeeded"
    assert service.stop(rig.owner.session_id, run_id) == completed


@pytest.mark.parametrize("accepted", [False, True])
def test_stop_storage_failure_still_revokes_using_execution_journal(rig, accepted):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    key = rig.owner.principal.owner_key + "/hosted-runs/" + run_id

    def fail(target, version):
        if target == key:
            rig.backend.before_write = rig.backend.after_write = None
            raise StoreUnavailable("fixture-private-stop-error")

    if accepted:
        rig.backend.after_write = fail
    else:
        rig.backend.before_write = fail
    with pytest.raises(HostedRunsError) as caught:
        service.stop(rig.owner.session_id, run_id)
    assert caught.value.status_code == 503
    assert caught.value.__context__ is None
    assert rig.execution().get(run_id, rig.owner.principal)["revoked"] is True
    assert service_for(rig).reconcile(rig.owner.session_id, run_id)["status"] == "stopped"


@pytest.mark.parametrize("accepted", [False, True])
def test_local_record_failure_after_paid_start_recovers_same_execution(rig, accepted):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    root = rig.owner.principal.owner_key + "/hosted-runs/"

    def fail(target, version):
        if target.startswith(root) and not target.endswith("/index") and rig.wire.executions:
            rig.backend.before_write = rig.backend.after_write = None
            raise StoreUnavailable("fixture-private-start-error")

    if accepted:
        rig.backend.after_write = fail
    else:
        rig.backend.before_write = fail
    submission = str(uuid4())
    with pytest.raises(HostedRunsError) as caught:
        service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)
    assert caught.value.status_code == 503
    history = service_for(rig).list(rig.owner.session_id)
    assert len(history) == 1
    recovered = service_for(rig).reconcile(rig.owner.session_id, history[0]["id"])
    assert recovered["status"] == "running"
    assert service_for(rig).create(rig.owner.session_id, PAYLOAD, submission)["id"] == recovered["id"]
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 1


def test_password_rotation_invalidates_old_session_but_new_session_owns_history(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    replacement = rig.auth.change_password(rig.owner.session_id, PASSWORD, "fixture-new-password-2026")
    with pytest.raises(HostedRunsError) as caught:
        service.get(rig.owner.session_id, run_id)
    assert caught.value.status_code == 401
    assert service.get(replacement.session_id, run_id)["id"] == run_id
    assert service.stop(replacement.session_id, run_id)["stop_requested"] is True
    assert rig.execution().get(run_id, replacement.principal)["revoked"] is True


@pytest.mark.parametrize("result", [None, {**result_payload(), "decision": None},
                                    {**result_payload(), "decision": " ", "reports": {}}])
def test_cloud_success_without_usable_complete_result_is_never_success(rig, result):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    if result is not None:
        emit(rig, run_id, "result", result)
    view = service.refresh(rig.owner.session_id, run_id)
    assert view["status"] in {"collecting", "failed"}
    rig.now[0] += 600
    final = service.refresh(rig.owner.session_id, run_id)
    assert final["status"] == "failed"
    assert final["error_code"] == ("no_result" if result is None else "partial_result")


@pytest.mark.parametrize("code", ["upstream-echo-private-key", {"secret": "private"}, "budget_exhausted"])
def test_error_event_codes_and_capability_echo_are_safe(rig, code):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    capability = rig.wire.secrets[0]["value"]
    emit(rig, run_id, "report", {"section": "market_report", "content": capability})
    emit(rig, run_id, "error", {"code": code, "message": "fixture-real-api-key", "request": "fixture-real-fred-key"})
    rig.wire.executions[0]["properties"]["status"] = "Failed"
    view = service.refresh(rig.owner.session_id, run_id)
    expected_code = "budget_exhausted" if code == "budget_exhausted" else "worker_error"
    assert view["error_code"] == expected_code
    public = json.dumps(view) + service.export(rig.owner.session_id, run_id)["content"].decode()
    for secret in (capability, capability.split(".")[1], "fixture-real-api-key", "fixture-real-fred-key", "upstream-echo-private-key"):
        assert secret not in public
    assert "[REDACTED]" in public


def test_event_bytes_bounded_and_previous_reports_preserved(rig):
    rig.store.max_bytes = 12000
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    emit(rig, run_id, "report", {"section": "market_report", "content": "Preserved."})
    assert service.refresh(rig.owner.session_id, run_id)["events"]
    emit(rig, run_id, "report", {"section": "extra", "content": "large" * 1200})
    rig.wire.executions[0]["properties"]["status"] = "Succeeded"
    rig.now[0] += 5
    view = service.refresh(rig.owner.session_id, run_id)
    assert view["log_warning"]
    assert view["status"] == "failed"
    assert b"Preserved." in service.export(rig.owner.session_id, run_id)["content"]
    assert all(len(content) <= rig.store.max_bytes + 29 for content, version in rig.backend.records.values())


def test_event_count_bound_applies_after_cross_poll_deduplication(rig):
    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    for number in range(501):
        emit(rig, run_id, "phase", {"stage": "research", "step": number})
    assert len(service.refresh(rig.owner.session_id, run_id)["events"]) == 501
    rig.now[0] += 5
    view = service.refresh(rig.owner.session_id, run_id)
    assert len(view["events"]) == 501
    assert view["log_warning"] is None


def test_index_count_limit_rejects_next_submission_without_cloud(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    root = rig.owner.principal.owner_key + "/hosted-runs/"
    index, version = rig.store.read(root + "index")
    seed = index["runs"][0]
    for _number in range(999):
        entry = copy.deepcopy(seed)
        run_id, submission = str(uuid4()), str(uuid4())
        entry["intent"].update(id=run_id, submission_id=submission,
                               settings_snapshot=asdict(rig.settings.pin(rig.owner.principal, run_id)))
        entry["summary"].update(id=run_id, submission_id=submission)
        index["runs"].append(entry)
    rig.store.write(root + "index", index, version)
    before = len(rig.wire.calls)
    assert len(service.list(rig.owner.session_id)) == 1000
    with pytest.raises(HostedRunsError) as caught:
        service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    assert caught.value.status_code == 429
    assert len(rig.wire.calls) == before


@pytest.mark.parametrize("output_format", ["html", [], None])
def test_export_invalid_format_safe_error(rig, output_format):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    with pytest.raises(HostedRunsError) as caught:
        service.export(rig.owner.session_id, run_id, output_format=output_format)
    assert caught.value.status_code == 400
    assert caught.value.__context__ is None


def test_stale_prepared_poll_cannot_resurrect_stop_before_start(rig):
    service = service_for(rig)
    rig.wire.fault = "secret"
    created = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))
    run_id = created["id"]
    assert service.reconcile(rig.owner.session_id, run_id)["status"] == "prepared"
    reads = [0]

    def stop_before_readback(key):
        if key == "email-v1/executions/" + run_id:
            reads[0] += 1
            if reads[0] == 2:
                rig.backend.after_read = None
                service_for(rig).stop(rig.owner.session_id, run_id)

    rig.backend.after_read = stop_before_readback
    view = service.reconcile(rig.owner.session_id, run_id)
    assert view["status"] == "stopped"
    assert view["stop_requested"] is True
    assert view["start_pending"] is False
    assert sum(path.endswith("/start") for method, path, body in rig.wire.calls) == 0


def test_record_older_than_index_fails_closed(rig):
    from tradingagents.workbench.public_hosted_runs import HostedRunsError

    service = service_for(rig)
    run_id = service.create(rig.owner.session_id, PAYLOAD, str(uuid4()))["id"]
    key = rig.owner.principal.owner_key + "/hosted-runs/" + run_id
    overwrite(rig, key, lambda record: record.update(generation=0))
    with pytest.raises(HostedRunsError) as caught:
        service.get(rig.owner.session_id, run_id)
    assert caught.value.status_code == 503
