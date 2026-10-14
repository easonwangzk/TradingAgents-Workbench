import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_store import EncryptedStore, StoreUnavailable


def test_reservations_reject_overspend_and_admit_exact_remaining_credit():
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger

    ledger = BudgetLedger(EncryptedStore(MemoryBackend(), b"b" * 32), clock=lambda: 1800000000)
    ledger.reserve(str(uuid4()), run_id=str(uuid4()), amount_microusd=299_000_000,
                   price_version="fixture-v1")
    with pytest.raises(BudgetError):
        ledger.reserve(str(uuid4()), run_id=str(uuid4()), amount_microusd=2_000_000,
                       price_version="fixture-v1")
    assert ledger.snapshot()["available_microusd"] == 1_000_000
    ledger.reserve(str(uuid4()), run_id=str(uuid4()), amount_microusd=1_000_000,
                   price_version="fixture-v1")
    assert ledger.snapshot()["reserved_microusd"] == 300_000_000
    assert ledger.snapshot()["available_microusd"] == 0


def ledger_harness(*, backend=None, timestamp=1800000000):
    from tradingagents.workbench.public_budget import BudgetLedger

    clock = [timestamp]
    store = EncryptedStore(MemoryBackend() if backend is None else backend, b"b" * 32)
    return BudgetLedger(store, clock=lambda: clock[0]), store, clock


def reserve(ledger, amount=100_000_000):
    return ledger.reserve(str(uuid4()), run_id=str(uuid4()), amount_microusd=amount,
                          price_version="fixture-v1")


def test_dispatched_attempt_settles_once_and_cannot_be_cancelled():
    from tradingagents.workbench.public_budget import BudgetError

    ledger, _, _ = ledger_harness()
    held = reserve(ledger)
    ledger.mark_dispatched(held)
    with pytest.raises(BudgetError):
        ledger.mark_dispatched(held)
    with pytest.raises(BudgetError):
        ledger.cancel(held)
    ledger.settle(held, 25_000_000)
    ledger.settle(held, 25_000_000)
    assert ledger.snapshot()["spent_microusd"] == 25_000_000
    assert ledger.snapshot()["reserved_microusd"] == 0
    assert ledger.snapshot()["available_microusd"] == 275_000_000
    with pytest.raises(BudgetError):
        ledger.settle(held, 0)
    with pytest.raises(BudgetError):
        ledger.cancel(held)


def test_only_undispatched_reservations_can_release_credit():
    from tradingagents.workbench.public_budget import BudgetError

    ledger, _, _ = ledger_harness()
    held = reserve(ledger)
    with pytest.raises(BudgetError):
        ledger.settle(held, 0)
    ledger.cancel(held)
    ledger.cancel(held)
    assert ledger.snapshot()["available_microusd"] == 300_000_000
    with pytest.raises(BudgetError):
        ledger.mark_dispatched(held)
    with pytest.raises(BudgetError):
        ledger.reserve(held.attempt_id, run_id=held.run_id, amount_microusd=held.amount_microusd,
                       price_version=held.price_version)


def test_restart_and_time_passage_do_not_refund_an_unknown_dispatch():
    from tradingagents.workbench.public_budget import BudgetLedger

    ledger, store, clock = ledger_harness()
    held = reserve(ledger)
    ledger.mark_dispatched(held)
    clock[0] += 172800
    restarted = BudgetLedger(store, clock=lambda: clock[0])
    assert restarted.snapshot()["reserved_microusd"] == 100_000_000
    assert restarted.snapshot()["available_microusd"] == 200_000_000


def test_late_settlement_stays_in_its_original_utc_month():
    timestamp = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    ledger, _, clock = ledger_harness(timestamp=timestamp)
    held = reserve(ledger)
    ledger.mark_dispatched(held)
    assert held.month == "2026-09"
    assert ledger.snapshot()["resets_at"] == "2026-10-01T00:00:00+00:00"
    clock[0] += 2
    reserve(ledger, 50_000_000)
    ledger.settle(held, 25_000_000)
    assert ledger.snapshot("2026-09")["spent_microusd"] == 25_000_000
    assert ledger.snapshot("2026-09")["reserved_microusd"] == 0
    assert ledger.snapshot()["month"] == "2026-10"
    assert ledger.snapshot()["spent_microusd"] == 0
    assert ledger.snapshot()["available_microusd"] == 250_000_000
    assert ledger.snapshot("2026-12")["resets_at"] == "2027-01-01T00:00:00+00:00"


def test_usage_above_reserved_bound_is_recorded_and_blocks_future_months():
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger

    ledger, store, clock = ledger_harness()
    held = reserve(ledger, 300_000_000)
    ledger.mark_dispatched(held)
    with pytest.raises(BudgetError):
        ledger.settle(held, 301_000_000)
    assert ledger.snapshot()["spent_microusd"] == 301_000_000
    assert ledger.snapshot()["reserved_microusd"] == 0
    assert ledger.snapshot()["available_microusd"] == 0
    assert ledger.snapshot()["blocked"] is True
    clock[0] += 40 * 86400
    restarted = BudgetLedger(store, clock=lambda: clock[0])
    with pytest.raises(BudgetError):
        reserve(restarted, 1)


@pytest.mark.parametrize("field,value", [
    ("attempt_id", "11111111-1111-4111-8111-111111111111"),
    ("run_id", "22222222-2222-4222-8222-222222222222"),
    ("month", "2030-01"), ("amount_microusd", 1), ("price_version", "other-price"),
])
def test_forged_reservation_cannot_dispatch_cancel_or_settle(field, value):
    from tradingagents.workbench.public_budget import BudgetError

    ledger, _, _ = ledger_harness()
    held = reserve(ledger)
    forged = replace(held, **{field: value})
    with pytest.raises(BudgetError):
        ledger.mark_dispatched(forged)
    with pytest.raises(BudgetError):
        ledger.cancel(forged)
    ledger.mark_dispatched(held)
    with pytest.raises(BudgetError):
        ledger.settle(forged, 0)
    assert ledger.snapshot()["reserved_microusd"] == 100_000_000


@pytest.mark.parametrize("amount", [0, -1, True, False, 1.5, float("nan"), "1"])
def test_invalid_reservation_cost_does_not_create_credit(amount):
    ledger, _, _ = ledger_harness()
    with pytest.raises(ValueError):
        reserve(ledger, amount)
    assert ledger.snapshot()["reserved_microusd"] == 0


@pytest.mark.parametrize("amount", [-1, True, 1.5, float("inf"), None, "1"])
def test_invalid_actual_cost_preserves_the_reservation(amount):
    ledger, _, _ = ledger_harness()
    held = reserve(ledger)
    ledger.mark_dispatched(held)
    with pytest.raises(ValueError):
        ledger.settle(held, amount)
    assert ledger.snapshot()["reserved_microusd"] == 100_000_000


@pytest.mark.parametrize("field,value", [
    ("attempt_id", "../other"), ("run_id", None), ("price_version", ""),
    ("price_version", "private-price\n"),
])
def test_invalid_request_identity_or_price_label_is_rejected(field, value):
    ledger, _, _ = ledger_harness()
    request = {"attempt_id": str(uuid4()), "run_id": str(uuid4()),
               "amount_microusd": 1, "price_version": "fixture-v1", field: value}
    with pytest.raises(ValueError):
        ledger.reserve(**request)
    assert ledger.snapshot()["reserved_microusd"] == 0


class RacingBackend(MemoryBackend):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.read_count = 0

    def read(self, key):
        with self.lock:
            stored = super().read(key)
            self.read_count += 1
            wait = self.read_count <= 2
        if wait:
            self.barrier.wait(timeout=5)
        return stored

    def write(self, key, content, expected_version=None):
        with self.lock:
            return super().write(key, content, expected_version)


def test_two_instances_cannot_spend_the_same_remaining_credit():
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger

    backend = RacingBackend()
    first, store, _ = ledger_harness(backend=backend)
    second = BudgetLedger(store, clock=lambda: 1800000000)

    def attempt(ledger):
        try:
            reserve(ledger, 200_000_000)
            return "reserved"
        except BudgetError as error:
            assert error.status_code == 402
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first, second)))
    assert sorted(results) == ["denied", "reserved"]
    assert first.snapshot()["reserved_microusd"] == 200_000_000


@pytest.mark.parametrize("failure", ["read", "write", "accepted_lost"])
def test_storage_failures_never_return_an_admission(failure):
    from tradingagents.workbench.public_budget import BudgetError

    class FaultBackend(MemoryBackend):
        failing = True

        def read(self, key):
            if self.failing and failure == "read":
                raise StoreUnavailable("Read unavailable")
            return super().read(key)

        def write(self, key, content, expected_version=None):
            if self.failing and failure == "write":
                raise StoreUnavailable("Write unavailable")
            result = super().write(key, content, expected_version)
            if self.failing and failure == "accepted_lost":
                raise StoreUnavailable("Response unavailable")
            return result

    backend = FaultBackend()
    ledger, _, _ = ledger_harness(backend=backend)
    attempt_id, run_id = str(uuid4()), str(uuid4())
    with pytest.raises(BudgetError) as rejected:
        ledger.reserve(attempt_id, run_id=run_id, amount_microusd=100_000_000,
                       price_version="fixture-v1")
    assert rejected.value.status_code == 503
    backend.failing = False
    if failure == "accepted_lost":
        assert ledger.snapshot()["reserved_microusd"] == 100_000_000
        with pytest.raises(BudgetError):
            ledger.reserve(attempt_id, run_id=run_id, amount_microusd=100_000_000,
                           price_version="fixture-v1")
    else:
        assert ledger.snapshot()["reserved_microusd"] == 0


def test_ledger_capacity_failure_does_not_discard_older_reservations(monkeypatch):
    from tradingagents.workbench import public_budget

    monkeypatch.setattr(public_budget, "MAX_ATTEMPTS", 2)
    ledger, _, _ = ledger_harness()
    reserve(ledger, 1)
    reserve(ledger, 1)
    with pytest.raises(public_budget.BudgetError):
        reserve(ledger, 1)
    assert ledger.snapshot()["reserved_microusd"] == 2


@pytest.mark.parametrize("record", [
    {"version": 2, "blocked": False, "calls": {}},
    {"version": 1, "blocked": "false", "calls": {}},
    {"version": 1, "blocked": False, "calls": []},
])
def test_unrecognized_ledger_state_never_grants_new_credit(record):
    from tradingagents.workbench.public_budget import LEDGER_KEY, BudgetError

    ledger, store, _ = ledger_harness()
    store.write(LEDGER_KEY, record)
    with pytest.raises(BudgetError):
        reserve(ledger, 1)


@pytest.mark.parametrize("actions", [("mark_dispatched", "cancel"),
                                   ("mark_dispatched", "mark_dispatched"),
                                   ("settle", "settle")])
def test_concurrent_state_transitions_preserve_one_authoritative_outcome(actions):
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger

    backend = RacingBackend()
    backend.read_count = 2
    ledger, store, _ = ledger_harness(backend=backend)
    held = reserve(ledger)
    if actions[0] == "settle":
        ledger.mark_dispatched(held)
    backend.read_count = 0

    def transition(action):
        instance = BudgetLedger(store, clock=lambda: 1800000000)
        try:
            getattr(instance, action)(held, *([25_000_000] if action == "settle" else []))
            return action
        except BudgetError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transition, actions))
    state = ledger.snapshot()
    if actions[0] == "settle":
        assert results == ["settle", "settle"]
        assert state["spent_microusd"] == 25_000_000
        assert state["reserved_microusd"] == 0
    else:
        assert results.count("denied") == 1
        assert state["reserved_microusd"] == (0 if "cancel" in results else 100_000_000)
        assert state["spent_microusd"] == 0


@pytest.mark.parametrize("action", ["mark_dispatched", "settle", "cancel"])
@pytest.mark.parametrize("accepted", [False, True])
def test_transition_write_failure_is_conservative_and_restart_recoverable(action, accepted):
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger

    class FaultBackend(MemoryBackend):
        failing = False

        def write(self, key, content, expected_version=None):
            if self.failing:
                if accepted:
                    super().write(key, content, expected_version)
                raise StoreUnavailable("private-storage-detail")
            return super().write(key, content, expected_version)

    backend = FaultBackend()
    ledger, store, _ = ledger_harness(backend=backend)
    held = reserve(ledger)
    if action == "settle":
        ledger.mark_dispatched(held)
    backend.failing = True
    args = [25_000_000] if action == "settle" else []
    with pytest.raises(BudgetError) as failed:
        getattr(ledger, action)(held, *args)
    assert failed.value.status_code == 503
    assert "private-storage-detail" not in str(failed.value)
    backend.failing = False
    restarted = BudgetLedger(store, clock=lambda: 1800000000)
    if accepted and action == "mark_dispatched":
        with pytest.raises(BudgetError):
            restarted.mark_dispatched(held)
        with pytest.raises(BudgetError):
            restarted.cancel(held)
        assert restarted.snapshot()["reserved_microusd"] == 100_000_000
    else:
        getattr(restarted, action)(held, *args)
        assert restarted.snapshot()["reserved_microusd"] == (100_000_000 if action == "mark_dispatched" else 0)
        assert restarted.snapshot()["spent_microusd"] == (25_000_000 if action == "settle" else 0)


@pytest.mark.parametrize("field,value", [("state", "unknown"), ("month", "2026-13"),
                                        ("run_id", None), ("amount_microusd", -1),
                                        ("price_version", None)])
def test_malformed_attempt_cannot_be_reinterpreted_as_available_credit(field, value):
    from tradingagents.workbench.public_budget import LEDGER_KEY, BudgetError

    ledger, store, _ = ledger_harness()
    held = reserve(ledger)
    record, version = store.read(LEDGER_KEY)
    record["calls"][held.attempt_id][field] = value
    store.write(LEDGER_KEY, record, version)
    with pytest.raises(BudgetError):
        reserve(ledger, 1)
    with pytest.raises(BudgetError):
        ledger.snapshot()


