"""Email-owned durable run intents and nonsecret workbench projections."""

import hashlib
import json
import math
import re
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import wraps
from uuid import UUID, uuid4

from tradingagents.workbench import contracts
from tradingagents.workbench.azure_control import MAX_LOG_BYTES
from tradingagents.workbench.public_accounts import AccountError
from tradingagents.workbench.public_execution import ExecutionError
from tradingagents.workbench.public_gateway import GatewayError
from tradingagents.workbench.public_settings import (
    PROVIDERS,
    SettingsError,
    SettingsSnapshot,
    _model,
)
from tradingagents.workbench.public_store import StoreConflict
from tradingagents.workbench.public_usage import UsageLedger
from tradingagents.workbench.server import (
    INGESTION_SECONDS,
    POLL_SECONDS,
    WORKER_ERROR,
    _json_bytes,
    _markdown,
    _valid_result,
)

_ATTEMPTS = 8
_LIMIT = 1000
_INTENT = {"id", "submission_id", "signature", "settings_snapshot", "settings", "request", "created_at"}
_PRIVATE = {"signature", "settings_snapshot", "prepare_attempted", "generation"}
_FLAGS = {"prepare_attempted", "start_attempted", "start_pending", "start_uncertain",
          "stop_requested", "stop_pending", "stop_accepted"}
_TIMES = {"last_poll_at", "collection_started_at"}
_STATUS = {"starting", "prepared", "running", "reconciliation_required", "collecting",
           "succeeded", "failed", "stopped"}
_CLOUD = {None, "Succeeded", "Failed", "Stopped", "Running", "Processing", "Pending",
          "Stopping", "Unknown"}
_TERMINAL = {"Succeeded", "Failed", "Stopped"}
_POLL_ERROR = "Hosted status or logs are temporarily unavailable. Reconcile to retry."
_LOG_LIMIT = "Run events exceed the storage size limit. Available reports are incomplete."
_EVENT_SOURCES = {"direct", "logs", "mixed"}
_FIELDS = _INTENT | _FLAGS | _TIMES | {
    "status", "execution_name", "cloud_status", "events", "result", "error", "error_code",
    "poll_error", "log_warning", "generation",
}
_ERRORS = {
    "reconciliation_required": "Hosted execution is unconfirmed. Reconcile before further action.",
    "authorization_expired": "Run authorization expired. Available reports are partial; no automatic renewal is permitted.",
    "cancelled_before_start": "Run was cancelled before execution started.",
    "budget_exhausted": "The workbench model budget is exhausted. Available reports are partial.",
    "revoked": "Run authorization was revoked. Available reports are partial.",
    "credentials_unavailable": "Run credentials changed or are unavailable. Available reports are partial.",
    "worker_error": WORKER_ERROR,
    "cloud_failed": "Hosted Job execution failed. Review available reports.",
    "no_result": "No validated result arrived after 10 minutes of log ingestion.",
    "partial_result": "The worker returned an incomplete result. Available reports are partial.",
    "event_limit": _LOG_LIMIT,
}
_WORKER_CODES = {"budget_exhausted", "revoked", "credentials_unavailable", "worker_error"}
_AUTOMATIC_ERRORS = {"automatic_expired", "authorization_changed"}
_ERRORS.update({
    "automatic_expired": "Automatic execution recovery timed out. Operator review is required; no restart was attempted.",
    "authorization_changed": "Account authorization changed. Automatic execution has been cancelled.",
})


@dataclass(frozen=True)
class _RunAuthorization:
    user_id: str
    run_id: str
    grant: dict


class HostedRunsError(RuntimeError):
    def __init__(self, *, status_code=503):
        super().__init__({
            400: "Invalid hosted run request.",
            401: "Invalid credentials or expired authorization.",
            403: "Hosted run operation is not permitted.",
            404: "Hosted run was not found.",
            409: "Hosted run state changed or requires reconciliation.",
            429: "Hosted run history limit reached.",
            503: "Hosted run storage or execution is unavailable.",
        }[status_code])
        self.status_code = status_code


def _safe(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as error:
            code = error.status_code if isinstance(error, (
                HostedRunsError, AccountError, SettingsError, ExecutionError,
            )) else 409 if isinstance(error, StoreConflict) else 503
        raise HostedRunsError(status_code=code)
    return invoke


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _signature(request):
    return hashlib.sha256(_json_bytes(request)).hexdigest()


def _redact_worker_capability(value, capability):
    secrets = (capability, capability.split(".", 1)[1])
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {_redact_worker_capability(key, capability): _redact_worker_capability(item, capability)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_worker_capability(item, capability) for item in value]
    return value


def _initial(intent):
    return {**deepcopy(intent), **dict.fromkeys(_FLAGS, False), **dict.fromkeys(_TIMES),
            "status": "starting", "execution_name": None, "cloud_status": None,
            "events": [], "result": None, "error": None, "error_code": None,
            "poll_error": None, "log_warning": None, "generation": 0, "start_pending": True}


def _public(record):
    return deepcopy({name: record[name] for name in _FIELDS - _PRIVATE})


def _summary(record):
    return {**_public(record), "events": [], "result": None, "has_result": record["result"] is not None}


class HostedRuns:
    def __init__(self, *, auth, settings, execution, clock=time.time, usage=None):
        self._auth = auth
        self._store = auth.store
        self._settings = settings
        self._execution = execution
        self._clock = clock
        self._usage = usage if usage is not None else UsageLedger(auth.store, clock=clock)

    def _root(self, principal):
        _uuid(principal.object_id)
        return principal.owner_key + "/hosted-runs/"

    def _principal_for(self, access):
        if type(access) is not _RunAuthorization:
            principal = self._auth.session(access).principal
            self._auth.require_feature(principal, "research")
            return principal
        registry, _ = self._auth._registry()
        user = registry["users"].get(access.user_id)
        grant = access.grant
        if (user is None or user["disabled"] or user["generation"] != grant["generation"]
                or user["email"] != grant["email"] or registry["owner_id"] != grant["owner_id"]
                or self._clock() >= grant["deadline"]):
            raise AccountError(status_code=403)
        principal = self._auth._principal(registry, access.user_id)
        self._auth.require_feature(principal, "research")
        index, _ = self._index(principal)
        entry = next(item for item in index["runs"] if item["intent"]["id"] == access.run_id)
        if entry.get("automatic") != grant:
            raise AccountError(status_code=403)
        return principal

    def _bounded(self, value):
        if len(_json_bytes(value)) > self._store.max_bytes:
            raise HostedRunsError()

    def _source_key(self, principal, run_id):
        return self._root(principal) + "sources/" + run_id

    def _read_source(self, principal, run_id):
        stored = self._store.read(self._source_key(principal, run_id))
        if stored is None:
            return {"latest": "logs"}, None
        source, version = stored
        if type(source) is not dict or set(source) != {"latest"} or source["latest"] not in _EVENT_SOURCES:
            raise HostedRunsError()
        return source, version

    def _save_source(self, principal, run_id, latest):
        if latest not in _EVENT_SOURCES:
            raise HostedRunsError(status_code=400)
        for _attempt in range(_ATTEMPTS):
            current, version = self._read_source(principal, run_id)
            source = ("mixed" if version is not None and current["latest"] != latest
                      else latest) if current["latest"] != "mixed" else "mixed"
            if current["latest"] == source and version is not None:
                return
            try:
                self._store.write(self._source_key(principal, run_id), {"latest": source}, version)
                return
            except StoreConflict:
                continue
        raise StoreConflict

    def _validate_intent(self, intent, principal):
        if type(intent) is not dict or set(intent) != _INTENT:
            raise ValueError
        for name in ("id", "submission_id"):
            _uuid(intent[name])
        if intent["signature"] != _signature(intent["request"]):
            raise ValueError
        if contracts.validate_request(intent["request"]) != intent["request"]:
            raise ValueError
        if datetime.fromisoformat(intent["created_at"]).tzinfo is None:
            raise ValueError
        snapshot = intent["settings_snapshot"]
        if (type(snapshot) is not dict or set(snapshot) != {"owner_key", "run_id", "revision", "signature"}
                or snapshot["owner_key"] != principal.owner_key or snapshot["run_id"] != intent["id"]
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot["signature"])):
            raise ValueError
        _uuid(snapshot["revision"])
        metadata = intent["settings"]
        if (set(metadata) != {"revision", "mode", "provider", "quick_model", "deep_model", "asset_type", "data_vendors"}
                or metadata["revision"] != snapshot["revision"]
                or metadata["mode"] not in {"api_key", "managed_identity"}
                or any(metadata[name] != intent["request"][name] for name in ("asset_type", "data_vendors"))):
            raise ValueError
        if metadata["mode"] == "managed_identity":
            if metadata["provider"] is not None:
                raise ValueError
            try:
                for name in ("quick_model", "deep_model"):
                    if metadata[name] is not None:
                        _model(metadata[name], azure=True)
            except SettingsError:
                raise ValueError from None
        else:
            if type(metadata["provider"]) is not str or metadata["provider"] not in PROVIDERS:
                raise ValueError
            try:
                for name in ("quick_model", "deep_model"):
                    _model(metadata[name], azure=metadata["provider"] == "azure")
            except SettingsError:
                raise ValueError from None

    def _index(self, principal):
        stored = self._store.read(self._root(principal) + "index")
        index, version = stored if stored is not None else ({"runs": []}, None)
        if set(index) != {"runs"} or type(index["runs"]) is not list or len(index["runs"]) > _LIMIT:
            raise HostedRunsError()
        identifiers, submissions = set(), set()
        for entry in index["runs"]:
            if set(entry) not in ({"intent", "summary", "generation"}, {"intent", "summary", "generation", "automatic"}):
                raise HostedRunsError()
            intent = entry["intent"]
            self._validate_intent(intent, principal)
            if "automatic" in entry:
                grant = entry["automatic"]
                if (type(grant) is not dict or set(grant) != {"generation", "email", "owner_id", "deadline"}
                        or type(grant["generation"]) is not int or grant["generation"] < 1
                        or grant["email"] != principal.email or type(grant["deadline"]) not in (int, float)
                        or not math.isfinite(grant["deadline"]) or grant["deadline"] <= 0):
                    raise HostedRunsError()
                _uuid(grant["owner_id"])
            if intent["id"] in identifiers or intent["submission_id"] in submissions:
                raise HostedRunsError()
            identifiers.add(intent["id"])
            submissions.add(intent["submission_id"])
            summary = entry["summary"]
            if (set(summary) != (_FIELDS - _PRIVATE) | {"has_result"}
                    or summary["events"] != [] or summary["result"] is not None
                    or type(summary["has_result"]) is not bool
                    or type(entry["generation"]) is not int or entry["generation"] < 0):
                raise HostedRunsError()
            candidate = {**summary, **{name: intent[name] for name in _PRIVATE & _INTENT},
                         "generation": entry["generation"], "prepare_attempted": False}
            candidate.pop("has_result")
            self._validate_record(candidate, principal)
            if any(candidate[name] != intent[name] for name in _INTENT):
                raise HostedRunsError()
        self._bounded(index)
        return index, version

    def _validate_record(self, record, principal):
        if set(record) != _FIELDS:
            raise ValueError
        self._validate_intent({name: record[name] for name in _INTENT}, principal)
        if (record["status"] not in _STATUS or record["cloud_status"] not in _CLOUD
                or any(type(record[name]) is not bool for name in _FLAGS)
                or type(record["generation"]) is not int or record["generation"] < 0
                or any(record[name] is not None and (type(record[name]) not in (int, float)
                    or not math.isfinite(record[name]) or record[name] < 0) for name in _TIMES)
                or record["error_code"] not in {None, *_ERRORS}
                or record["error"] != _ERRORS.get(record["error_code"])
                or record["poll_error"] not in (None, _POLL_ERROR)
                or record["log_warning"] not in (None, _LOG_LIMIT)):
            raise ValueError
        if self._events(record["events"]) != record["events"]:
            raise ValueError
        if record["result"] is not None and (not _valid_result(record["result"])
                or not any(event["kind"] == "result" and event["payload"] == record["result"]
                           for event in record["events"])):
            raise ValueError
        if record["execution_name"] is not None and not re.fullmatch(r"[a-z][a-z0-9-]{1,96}", record["execution_name"]):
            raise ValueError
        self._bounded(record)

    def _read(self, principal, run_id):
        try:
            _uuid(run_id)
        except (ValueError, TypeError):
            raise HostedRunsError(status_code=404) from None
        index, _ = self._index(principal)
        entry = next((entry for entry in index["runs"] if entry["intent"]["id"] == run_id), None)
        if entry is None:
            raise HostedRunsError(status_code=404)
        stored = self._store.read(self._root(principal) + run_id)
        if stored is None:
            if entry["generation"] != 0:
                raise HostedRunsError()
            return _initial(entry["intent"]), None
        record, version = stored
        self._validate_record(record, principal)
        if (record["generation"] < entry["generation"]
            or any(record[name] != entry["intent"][name] for name in _INTENT)):
            raise HostedRunsError()
        return record, version

    def _save(self, principal, record, version):
        record = {**record, "generation": record["generation"] + 1}
        self._validate_record(record, principal)
        version = self._store.write(self._root(principal) + record["id"], record, version)
        if record["status"] in {"succeeded", "failed", "stopped"}:
            self._usage.capture(principal.object_id, "research:" + record["id"], "research",
                                success=record["status"] == "succeeded")
        for _attempt in range(_ATTEMPTS):
            index, index_version = self._index(principal)
            entry = next(item for item in index["runs"] if item["intent"]["id"] == record["id"])
            if entry["generation"] > record["generation"]:
                return record, version
            entry.update(summary=_summary(record), generation=record["generation"])
            self._bounded(index)
            try:
                self._store.write(self._root(principal) + "index", index, index_version)
                return record, version
            except StoreConflict:
                continue
        raise StoreConflict

    def _request(self, payload, metadata, base=None):
        try:
            if type(payload) is not dict:
                raise ValueError
            payload = deepcopy(payload)
            if type(payload.get("data_vendors")) is dict:
                payload["data_vendors"] = {**metadata["data_vendors"], **payload["data_vendors"]}
            request = contracts.validate_request({**(base or {}), "asset_type": metadata["asset_type"],
                "data_vendors": metadata["data_vendors"], **payload})
            if any(request[name] != metadata[name] for name in ("asset_type", "data_vendors")):
                raise ValueError
            return request
        except (ValueError, TypeError):
            raise HostedRunsError(status_code=400) from None

    @_safe
    def list(self, session_id):
        principal = self._principal_for(session_id)
        return sorted((deepcopy(entry["summary"]) for entry in self._index(principal)[0]["runs"]),
                      key=lambda record: (record["created_at"], record["id"]), reverse=True)

    @_safe
    def get(self, session_id, run_id):
        return _public(self._read(self._principal_for(session_id), run_id)[0])

    @_safe
    def event_snapshot(self, session_id, run_id):
        principal = self._principal_for(session_id)
        record, _ = self._read(principal, run_id)
        source, version = self._read_source(principal, run_id)
        provenance = source["latest"] if version is not None or not record["events"] else "mixed"
        return {"run_id": run_id, "events": deepcopy(record["events"]), "source": provenance}

    @_safe
    def resume(self, session_id, run_id):
        principal = self._principal_for(session_id)
        record, _ = self._read(principal, run_id)
        if (record["status"] != "prepared" or not record["prepare_attempted"]
                or record["stop_requested"] or record["start_attempted"]):
            return _public(record)
        return self._start_ready(session_id, run_id)

    @_safe
    def create(self, session_id, payload, submission_id):
        return self._create(session_id, payload, submission_id, background=False)

    @_safe
    def submit(self, session_id, payload, submission_id):
        return self._create(session_id, payload, submission_id, background=True)

    def _create(self, session_id, payload, submission_id, *, background):
        principal = self._principal_for(session_id)
        try:
            _uuid(submission_id)
        except (ValueError, TypeError):
            raise HostedRunsError(status_code=400) from None
        for _attempt in range(_ATTEMPTS):
            index, version = self._index(principal)
            previous = next((item for item in index["runs"] if item["intent"]["submission_id"] == submission_id), None)
            if previous is not None:
                intent = previous["intent"]
                if _signature(self._request(payload, intent["settings"], intent["request"])) != intent["signature"]:
                    raise HostedRunsError(status_code=409)
                return self.get(session_id, intent["id"]) if background else self._begin(session_id, intent["id"])
            if len(index["runs"]) >= _LIMIT:
                raise HostedRunsError(status_code=429)
            metadata = self._settings.get(principal)
            run_id = str(uuid4())
            snapshot = self._settings.pin(principal, run_id)
            if snapshot.revision != metadata["revision"]:
                raise HostedRunsError(status_code=409)
            if metadata["mode"] == "managed_identity":
                self._execution.admission_ready(principal, snapshot)
            request = self._request(payload, metadata)
            intent = {
                "id": run_id, "submission_id": submission_id, "signature": _signature(request),
                "settings_snapshot": asdict(snapshot), "request": request,
                "created_at": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
                "settings": {name: metadata[name] for name in (
                    "revision", "mode", "provider", "quick_model", "deep_model", "asset_type", "data_vendors",
                )},
            }
            entry = {"intent": intent, "summary": _summary(_initial(intent)), "generation": 0}
            if background:
                _, registry, _ = self._auth._session_record(session_id)
                entry["automatic"] = {"generation": registry["users"][principal.object_id]["generation"],
                                      "email": principal.email, "owner_id": registry["owner_id"],
                                      "deadline": self._clock() + 4200}
            index["runs"].append(entry)
            self._bounded(index)
            try:
                self._store.write(self._root(principal) + "index", index, version)
            except StoreConflict:
                continue
            return self.get(session_id, run_id) if background else self._begin(session_id, run_id)
        raise StoreConflict

    def _begin(self, session_id, run_id):
        principal = self._principal_for(session_id)
        record, version = self._read(principal, run_id)
        if record["stop_requested"]:
            return _public(record)
        if record["prepare_attempted"]:
            if record["status"] == "reconciliation_required":
                self._poll(session_id, run_id, force=True)
                record, version = self._read(principal, run_id)
            if record["status"] == "prepared" and not record["start_attempted"]:
                return self._start_ready(session_id, run_id)
            return _public(record)
        record["prepare_attempted"] = True
        record, version = self._save(principal, record, version)
        try:
            self._execution.prepare(run_id, principal, SettingsSnapshot(**record["settings_snapshot"]))
        except ExecutionError:
            return self._uncertain(principal, run_id)
        return self._start_ready(session_id, run_id)

    def _start_ready(self, session_id, run_id):
        principal = self._principal_for(session_id)
        record, version = self._read(principal, run_id)
        if record["stop_requested"] or record["start_attempted"]:
            return _public(record)
        record.update(status="prepared", start_attempted=True)
        record, version = self._save(principal, record, version)
        principal = self._principal_for(session_id)
        try:
            view = self._execution.start(run_id, principal, record["request"])
        except ExecutionError:
            return self._uncertain(principal, run_id)
        record, version = self._read(principal, run_id)
        self._apply_view(record, view, self._clock())
        record, _ = self._save(principal, record, version)
        return _public(record)

    def _uncertain(self, principal, run_id):
        record, version = self._read(principal, run_id)
        if record["cloud_status"] in _TERMINAL or record["status"] == "stopped" or record["error_code"] in _AUTOMATIC_ERRORS:
            return _public(record)
        record.update(status="reconciliation_required", start_uncertain=True,
                      error_code="reconciliation_required", error=_ERRORS["reconciliation_required"])
        with suppress(ExecutionError):
            view = self._execution.get(run_id, principal)
            if view["status"] == "terminal" or view["error_code"] is not None:
                self._apply_view(record, view, self._clock())
        record, _ = self._save(principal, record, version)
        return _public(record)

    def _events(self, events):
        if type(events) is not list or len(events) > 2 * _LIMIT:
            raise ValueError
        combined = {}
        for event in contracts.scrub_secrets(events):
            if (type(event) is not dict or set(event) != {"kind", "payload", "timestamp"}
                    or event["kind"] not in contracts.EVENT_KINDS or type(event["payload"]) is not dict
                    or type(event["timestamp"]) is not str or len(event["timestamp"]) > 64
                    or datetime.fromisoformat(event["timestamp"]).tzinfo is None):
                raise ValueError
            if event["kind"] == "error":
                code = event["payload"].get("code")
                code = code if type(code) is str and code in _WORKER_CODES else "worker_error"
                event["payload"] = {"code": code, "message": _ERRORS[code]}
            if event["kind"] == "result" and not _valid_result(event["payload"]):
                raise ValueError
            combined[_json_bytes(event)] = event
            if len(combined) > _LIMIT:
                raise ValueError
        if sum(map(len, combined)) > min(MAX_LOG_BYTES, self._store.max_bytes // 3):
            raise ValueError
        return sorted(combined.values(), key=lambda event: (datetime.fromisoformat(event["timestamp"]), _json_bytes(event)))

    def _apply_view(self, record, view, now):
        if record["error_code"] in _AUTOMATIC_ERRORS:
            return
        if view is not None:
            if (set(view) != {"run_id", "status", "execution_name", "cloud_status", "revoked",
                              "authorization_expires_at", "error_code"}
                    or view["run_id"] != record["id"] or view["cloud_status"] not in _CLOUD
                    or view["status"] not in {"reserved", "prepared", "running", "stop_attempted",
                                               "terminal", "reconciliation_required"}
                    or type(view["revoked"]) is not bool
                    or view["error_code"] not in {None, "authorization_expired", "cancelled_before_start"}
                    or view["authorization_expires_at"] is not None and (
                        type(view["authorization_expires_at"]) is not int or view["authorization_expires_at"] <= 0)):
                raise ValueError
            if view["error_code"] is not None:
                record["error_code"] = view["error_code"]
            if record["cloud_status"] not in _TERMINAL and record["status"] != "stopped":
                record["cloud_status"] = view["cloud_status"]
                record["execution_name"] = view["execution_name"]
                if view["status"] == "stop_attempted":
                    record.update(stop_requested=True, stop_pending=True)
                if view["status"] == "terminal" and view["execution_name"] is None:
                    record.update(status="stopped", start_pending=False, start_uncertain=False,
                                  stop_pending=False, stop_accepted=True)
                elif (view["status"] == "reconciliation_required"
                      or view["status"] == "stop_attempted" and view["execution_name"] is None):
                    record.update(status="reconciliation_required", start_uncertain=True)
                else:
                    record["status"] = "starting" if view["status"] == "reserved" else (
                        "prepared" if view["status"] == "prepared" else "running")
                    record["start_uncertain"] = False
            if record["execution_name"] is not None:
                record["start_pending"] = False
        errors = [event["payload"]["code"] for event in record["events"] if event["kind"] == "error"]
        code = "budget_exhausted" if "budget_exhausted" in errors else errors[-1] if errors else None
        if code is None and record["error_code"] in {"authorization_expired", "cancelled_before_start"}:
            code = record["error_code"]
        if record["log_warning"]:
            code = code or "event_limit"
        cloud = record["cloud_status"]
        if cloud in _TERMINAL:
            if record["collection_started_at"] is None:
                record["collection_started_at"] = now
            record.update(start_pending=False, start_uncertain=False, stop_pending=False)
            if cloud == "Stopped" or record["stop_requested"]:
                record["status"] = "stopped"
            elif code is not None or cloud == "Failed":
                record["status"] = "failed"
                code = code or "cloud_failed"
            elif record["result"] is not None:
                result = record["result"]
                usable = (bool(result["decision"] and result["decision"].strip()) and bool(result["reports"]))
                if record["request"]["mode"] == "connectivity":
                    usable = bool(result["connectivity"]) and all(
                        row["status"] in {"ok", "skipped"}
                        or (row["provider"] == "alpha_vantage" and row["status"] == "unconfigured"
                            and "alpha_vantage" not in record["request"]["data_vendors"]["core_stock_apis"].split(","))
                        for row in result["connectivity"])
                record["status"] = "succeeded" if usable else "failed"
                code = None if usable else "partial_result"
            elif now - record["collection_started_at"] >= INGESTION_SECONDS:
                record["status"], code = "failed", "no_result"
            else:
                record["status"] = "collecting"
        elif record["start_uncertain"]:
            code = code or "reconciliation_required"
        record.update(error_code=code, error=_ERRORS.get(code))

    def _poll(self, session_id, run_id, *, force=False):
        principal = self._principal_for(session_id)
        record, version = self._read(principal, run_id)
        if record["error_code"] in _AUTOMATIC_ERRORS:
            return _public(record)
        now = self._clock()
        expired = (record["collection_started_at"] is not None
                   and now - record["collection_started_at"] >= INGESTION_SECONDS)
        if (not record["prepare_attempted"] or expired and not force
                or not force and record["last_poll_at"] is not None
                and now - record["last_poll_at"] < POLL_SECONDS):
            if expired and record["status"] == "collecting":
                self._apply_view(record, None, now)
                record, _ = self._save(principal, record, version)
            return _public(record)
        record["last_poll_at"] = now
        self._save(principal, record, version)
        view, events, poll_error, log_warning = None, [], None, None
        try:
            view = self._execution.reconcile(run_id, principal)
        except ExecutionError:
            poll_error = _POLL_ERROR
            with suppress(ExecutionError):
                view = self._execution.get(run_id, principal)
        if not expired and (view or record)["execution_name"] is not None:
            try:
                events = self._events(self._execution.get_events(run_id, principal))
            except ExecutionError:
                poll_error = _POLL_ERROR
            except (ValueError, TypeError, KeyError, OverflowError):
                log_warning = _LOG_LIMIT
        for _attempt in range(_ATTEMPTS):
            principal = self._principal_for(session_id)
            record, version = self._read(principal, run_id)
            previous_events = record["events"]
            try:
                record["events"] = self._events(record["events"] + events)
            except (ValueError, TypeError):
                log_warning = _LOG_LIMIT
            for event in record["events"]:
                if event["kind"] == "result":
                    record["result"] = event["payload"]
            record.update(poll_error=poll_error, log_warning=log_warning or record["log_warning"])
            self._apply_view(record, view, now)
            try:
                if record["events"] != previous_events:
                    unknown_prior_source = bool(previous_events) and self._read_source(principal, run_id)[1] is None
                    self._save_source(principal, run_id, "mixed" if unknown_prior_source else "logs")
                record, _ = self._save(principal, record, version)
                return _public(record)
            except StoreConflict:
                continue
        raise StoreConflict

    def ingest_worker_events(self, capability, lines):
        try:
            run_id, principal = self._execution.worker_access(capability)
            if (
                type(lines) is not list
                or not lines
                or len(lines) > contracts.MAX_PARTS
                or any(type(line) is not str for line in lines)
            ):
                raise ValueError
            parts = [json.loads(line[6:]) for line in lines if line.startswith("TAWB1 ")
                     and len(line.encode("utf-8")) <= contracts.MAX_LINE_BYTES]
            if len(parts) != len(lines) or any(type(part) is not dict for part in parts):
                raise ValueError
            first = parts[0]
            if (first.get("run_id") != run_id or type(first.get("total")) is not int
                    or first["total"] != len(parts) or len({part.get("part") for part in parts}) != len(parts)
                    or any(part.get("run_id") != run_id or part.get("message_id") != first.get("message_id")
                           or part.get("total") != len(parts) or part.get("sha256") != first.get("sha256")
                           for part in parts)):
                raise ValueError
            events = contracts.decode_events(lines, run_id)
            if len(events) != 1:
                raise ValueError
            events = _redact_worker_capability(events, capability)
        except GatewayError as error:
            raise HostedRunsError(status_code=error.status_code) from None
        except ExecutionError as error:
            raise HostedRunsError(status_code=error.status_code) from None
        except (ValueError, TypeError, KeyError):
            raise HostedRunsError(status_code=400) from None
        for _attempt in range(_ATTEMPTS):
            try:
                run_id, principal = self._execution.worker_access(capability)
            except ExecutionError as error:
                raise HostedRunsError(status_code=error.status_code) from None
            record, version = self._read(principal, run_id)
            try:
                merged = self._events(record["events"] + events)
            except (ValueError, TypeError):
                raise HostedRunsError(status_code=400) from None
            if merged == record["events"]:
                return {"accepted": True}
            unknown_prior_source = bool(record["events"]) and self._read_source(principal, run_id)[1] is None
            record["events"] = merged
            for event in record["events"]:
                if event["kind"] == "result":
                    record["result"] = event["payload"]
            self._apply_view(record, None, self._clock())
            try:
                self._save_source(principal, run_id, "mixed" if unknown_prior_source else "direct")
                self._save(principal, record, version)
                return {"accepted": True}
            except StoreConflict:
                continue
        raise HostedRunsError(status_code=409)

    @_safe
    def refresh(self, session_id, run_id):
        return self._poll(session_id, run_id)

    @_safe
    def reconcile(self, session_id, run_id):
        return self._poll(session_id, run_id, force=True)

    @_safe
    def stop(self, session_id, run_id):
        principal = self._principal_for(session_id)
        record, version = self._read(principal, run_id)
        if record["cloud_status"] in _TERMINAL or record["status"] == "stopped":
            return _public(record)
        record.update(stop_requested=True, stop_pending=True)
        try:
            record, version = self._save(principal, record, version)
        except Exception:
            if record["prepare_attempted"]:
                self._execution.stop(run_id, self._principal_for(session_id))
            raise
        if not record["prepare_attempted"]:
            record.update(status="stopped", stop_pending=False, start_pending=False)
            record, _ = self._save(principal, record, version)
            return _public(record)
        try:
            view = self._execution.stop(run_id, principal)
        except ExecutionError:
            return self._uncertain(principal, run_id)
        record, version = self._read(principal, run_id)
        record["stop_accepted"] = True
        self._apply_view(record, view, self._clock())
        record, _ = self._save(principal, record, version)
        return _public(record)

    def _advance(self, access):
        principal = self._principal_for(access)
        run_id = access.run_id
        record, version = self._read(principal, run_id)
        if record["stop_requested"]:
            if record["prepare_attempted"]:
                with suppress(ExecutionError):
                    self._execution.reconcile(run_id, principal)
            return self.stop(access, run_id)
        if record["status"] in {"succeeded", "failed", "stopped"}:
            return _public(record)
        if not record["start_attempted"]:
            if not record["prepare_attempted"]:
                record["prepare_attempted"] = True
                self._save(principal, record, version)
            try:
                self._execution.prepare(run_id, self._principal_for(access), SettingsSnapshot(**record["settings_snapshot"]))
            except ExecutionError:
                return self._uncertain(principal, run_id)
            return self._start_ready(access, run_id)
        view = self._execution.get(run_id, principal)
        if view["status"] == "prepared":
            record, version = self._read(principal, run_id)
            if record["stop_requested"]:
                return self.stop(access, run_id)
            view = self._execution.start(run_id, self._principal_for(access), record["request"])
            record, version = self._read(principal, run_id)
            self._apply_view(record, view, self._clock())
            record, _ = self._save(principal, record, version)
            return _public(record)
        return self._poll(access, run_id, force=True)

    def _end_automatic(self, principal, run_id, code):
        record, version = self._read(principal, run_id)
        if record["status"] in {"succeeded", "failed", "stopped"}:
            return
        record.update(stop_requested=True, stop_pending=True)
        self._save(principal, record, version)
        confirmed = not record["prepare_attempted"]
        if record["prepare_attempted"]:
            with suppress(ExecutionError):
                self._execution.reconcile(run_id, principal)
            with suppress(ExecutionError):
                confirmed = self._execution.stop(run_id, principal)["status"] == "terminal"
        record, version = self._read(principal, run_id)
        if record["status"] in {"succeeded", "failed", "stopped"}:
            return
        record.update(status="failed" if confirmed else "reconciliation_required", error_code=code, error=_ERRORS[code],
                      start_pending=False, start_uncertain=not confirmed, stop_pending=not confirmed)
        self._save(principal, record, version)

    def advance_pending(self):
        registry, _ = self._auth._registry()
        advanced = 0
        for user_id in registry["users"]:
            principal = self._auth._principal(registry, user_id)
            try:
                index, _ = self._index(principal)
            except (HostedRunsError, ValueError):
                continue
            for entry in index["runs"]:
                grant = entry.get("automatic")
                if grant is None or entry["summary"]["status"] in {"succeeded", "failed", "stopped"}:
                    continue
                run_id = entry["intent"]["id"]
                key = self._root(principal) + "work/" + run_id
                now = self._clock()
                stored = self._store.read(key)
                work, version = stored if stored is not None else ({"attempts": 0, "next_at": 0}, None)
                if (type(work) is not dict or set(work) != {"attempts", "next_at"}
                        or type(work["attempts"]) is not int or work["attempts"] < 0
                        or type(work["next_at"]) not in (int, float)
                        or not math.isfinite(work["next_at"]) or work["next_at"] < 0):
                    continue
                if work["next_at"] > now:
                    continue
                claimed = {"attempts": work["attempts"] + 1, "next_at": now + 120}
                try:
                    claim_version = self._store.write(key, claimed, version)
                except StoreConflict:
                    continue
                access = _RunAuthorization(user_id, run_id, grant)
                delay = min(30, 5 * 2 ** min(work["attempts"], 3))
                try:
                    if now >= grant["deadline"]:
                        self._end_automatic(principal, run_id, "automatic_expired")
                    else:
                        result = self._advance(access)
                        if result["status"] not in {"starting", "prepared", "reconciliation_required"}:
                            claimed["attempts"] = 0
                            delay = 5
                except AccountError:
                    self._end_automatic(principal, run_id, "authorization_changed")
                except (HostedRunsError, ExecutionError, StoreConflict):
                    pass
                finally:
                    claimed["next_at"] = self._clock() + delay
                    with suppress(StoreConflict):
                        self._store.write(key, claimed, claim_version)
                advanced += 1
                if advanced >= 16:
                    return advanced
        return advanced

    @_safe
    def export(self, session_id, run_id, *, output_format="markdown"):
        principal = self._principal_for(session_id)
        record = _public(self._read(principal, run_id)[0])
        if type(output_format) is not str or output_format not in {"markdown", "json"}:
            raise HostedRunsError(status_code=400)
        ticker = record["request"]["ticker"]
        suffix = "md" if output_format == "markdown" else "json"
        return {"content": _markdown(record).encode("utf-8") if suffix == "md" else _json_bytes(record),
                "filename": f"{ticker}-{run_id}.{suffix}",
                "media_type": "text/markdown" if suffix == "md" else "application/json"}
