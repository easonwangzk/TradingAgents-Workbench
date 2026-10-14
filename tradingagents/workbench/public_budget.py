"""Durable admission ledger for the hosted workbench's model allowance."""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from tradingagents.workbench.public_store import StoreConflict, StoreUnavailable

MONTHLY_LIMIT_MICROUSD = 300_000_000
MAX_ATTEMPTS = 10_000
LEDGER_KEY = "email-v1/budget/ledger"


class BudgetError(RuntimeError):
    def __init__(self, message="Model allowance is unavailable.", *, status_code=409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BudgetReservation:
    attempt_id: str
    month: str
    run_id: str
    amount_microusd: int
    price_version: str


def _identifier(value):
    if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError("Invalid budget identifier")
    return value


def _reset_at(month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("Invalid budget month")
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    return (start.replace(year=start.year + 1, month=1) if start.month == 12
            else start.replace(month=start.month + 1)).isoformat()


class BudgetLedger:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def _read(self):
        try:
            stored = self.store.read(LEDGER_KEY)
        except StoreUnavailable:
            raise BudgetError("Model allowance storage is unavailable.", status_code=503) from None
        if stored is None:
            return {"version": 1, "blocked": False, "calls": {}}, None
        record, version = stored
        try:
            if (set(record) != {"version", "blocked", "calls"}
                    or type(record["version"]) is not int or record["version"] != 1
                    or type(record["blocked"]) is not bool or type(record["calls"]) is not dict
                    or len(record["calls"]) > MAX_ATTEMPTS):
                raise ValueError
            for attempt_id, call in record["calls"].items():
                _identifier(attempt_id)
                _identifier(call["run_id"])
                _reset_at(call["month"])
                fields = {"month", "run_id", "amount_microusd", "price_version", "state"}
                if call["state"] == "settled":
                    fields.add("actual_microusd")
                    actual = call["actual_microusd"]
                    if type(actual) is not int or actual < 0:
                        raise ValueError
                    if actual > call["amount_microusd"] and not record["blocked"]:
                        raise ValueError
                if (set(call) != fields or call["state"] not in {"reserved", "dispatched", "settled", "cancelled"}
                        or type(call["amount_microusd"]) is not int
                        or not 1 <= call["amount_microusd"] <= MONTHLY_LIMIT_MICROUSD
                        or not isinstance(call["price_version"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call["price_version"])):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BudgetError("Model allowance state requires operator review.", status_code=503) from None
        return record, version

    def _mutate(self, operation):
        for _ in range(4):
            record, version = self._read()
            result = operation(record)
            try:
                self.store.write(LEDGER_KEY, record, version)
                return result
            except StoreConflict:
                continue
            except StoreUnavailable:
                raise BudgetError("Model allowance write outcome is unavailable.", status_code=503) from None
        raise BudgetError("Model allowance changed concurrently. Retry later.", status_code=503)

    def _month(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m")

    def _snapshot(self, record, month):
        reset = _reset_at(month)
        calls = [call for call in record["calls"].values() if call["month"] == month]
        reserved = sum(call["amount_microusd"] for call in calls
                       if call["state"] in {"reserved", "dispatched"})
        spent = sum(call["actual_microusd"] for call in calls if call["state"] == "settled")
        return {"month": month, "limit_microusd": MONTHLY_LIMIT_MICROUSD,
                "spent_microusd": spent, "reserved_microusd": reserved,
                "available_microusd": max(0, MONTHLY_LIMIT_MICROUSD - reserved - spent),
                "blocked": record["blocked"], "resets_at": reset}

    def snapshot(self, month=None):
        record, _ = self._read()
        return self._snapshot(record, self._month() if month is None else month)

    def reserve(self, attempt_id, *, run_id, amount_microusd, price_version):
        _identifier(attempt_id)
        _identifier(run_id)
        if type(amount_microusd) is not int or amount_microusd <= 0:
            raise ValueError("Invalid model cost reservation")
        if not isinstance(price_version, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", price_version):
            raise ValueError("Invalid model price version")

        def admit(record):
            month = self._month()
            snapshot = self._snapshot(record, month)
            if record["blocked"] or len(record["calls"]) >= MAX_ATTEMPTS:
                raise BudgetError("Model allowance requires operator review.", status_code=503)
            if attempt_id in record["calls"]:
                raise BudgetError("This model attempt was already admitted.")
            if amount_microusd > snapshot["available_microusd"]:
                raise BudgetError("Monthly model allowance is insufficient.", status_code=402)
            record["calls"][attempt_id] = {
                "month": month, "run_id": run_id, "amount_microusd": amount_microusd,
                "price_version": price_version, "state": "reserved",
            }
            return BudgetReservation(attempt_id, month, run_id, amount_microusd, price_version)

        return self._mutate(admit)

    def _call(self, record, reservation):
        if not isinstance(reservation, BudgetReservation) or type(reservation.amount_microusd) is not int:
            raise BudgetError("Invalid model reservation.")
        try:
            _identifier(reservation.attempt_id)
        except ValueError:
            raise BudgetError("Invalid model reservation.") from None
        call = record["calls"].get(reservation.attempt_id)
        if call is None or any(call[field] != getattr(reservation, field) for field in
                               ("month", "run_id", "amount_microusd", "price_version")):
            raise BudgetError("Model reservation does not match its stored identity.")
        return call

    def mark_dispatched(self, reservation):
        def dispatch(record):
            call = self._call(record, reservation)
            if record["blocked"] or call["state"] != "reserved":
                raise BudgetError("Model attempt cannot be dispatched.")
            call["state"] = "dispatched"

        self._mutate(dispatch)

    def cancel(self, reservation):
        def release(record):
            call = self._call(record, reservation)
            if call["state"] not in {"reserved", "cancelled"}:
                raise BudgetError("A dispatched model attempt cannot be refunded without usage.")
            call["state"] = "cancelled"

        self._mutate(release)

    def settle(self, reservation, actual_microusd):
        if type(actual_microusd) is not int or actual_microusd < 0:
            raise ValueError("Invalid actual model cost")

        def charge(record):
            call = self._call(record, reservation)
            if call["state"] == "settled":
                if call["actual_microusd"] != actual_microusd:
                    raise BudgetError("Model attempt already has a different settlement.")
            elif call["state"] != "dispatched":
                raise BudgetError("Only a dispatched model attempt can be settled.")
            call["state"] = "settled"
            call["actual_microusd"] = actual_microusd
            overrun = actual_microusd > call["amount_microusd"]
            record["blocked"] = record["blocked"] or overrun
            return overrun

        if self._mutate(charge):
            raise BudgetError("Reported model cost exceeded its bound. Paid calls are disabled.", status_code=503)

