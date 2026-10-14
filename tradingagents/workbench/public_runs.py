"""Delegated execution against immutable research resource snapshots."""

import copy
import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from tradingagents.workbench import contracts
from tradingagents.workbench.azure_control import (
    API_VERSION,
    ARM_SCOPE,
    LOG_SCOPE,
    MAX_LOG_BYTES,
    AzureController,
    CloudError,
    _execution_name,
)
from tradingagents.workbench.public_auth import AuthenticationRequired, canonical_uuid
from tradingagents.workbench.public_profiles import AzureResources, ResourceError
from tradingagents.workbench.public_store import StoreConflict
from tradingagents.workbench.server import (
    INGESTION_SECONDS,
    POLL_SECONDS,
    WORKER_ERROR,
    _json_bytes,
    _valid_result,
)

TERMINAL = frozenset({"succeeded", "failed", "stopped"})
AZURE_TERMINAL = frozenset({"Succeeded", "Failed", "Stopped"})
START_CONFIRM_SECONDS = 180


class DelegatedAzureController(AzureController):
    def __init__(self, profile, *, credential, client=None):
        if credential is None:
            raise ValueError("Delegated credentials are required")
        super().__init__(profile["config"], credential=credential, client=client, allow_custom_model=True)
        self.profile = copy.deepcopy(profile)

    def _token(self, scope):
        try:
            return self.credential.get_token(scope).token
        except AuthenticationRequired:
            raise
        except Exception:
            raise CloudError("User authorization could not be acquired.") from None

    def _request(self, method, url, *, scope=ARM_SCOPE, **kwargs):
        try:
            return super()._request(method, url, scope=scope, **kwargs)
        except CloudError as error:
            if error.status_code == 401:
                raise AuthenticationRequired(scope=scope) from None
            if error.status_code == 403:
                raise ResourceError("Your Azure identity lacks permission for this operation.", 403) from None
            raise

    def _execution_environment(self, run_id, request, job):
        try:
            properties = job["properties"]
            containers = properties["template"]["containers"]
            if (properties["configuration"]["triggerType"] != "Manual" or len(containers) != 1
                    or containers[0]["name"] != "worker" or "Assigned" not in job["identity"]["type"]
                    or properties["environmentId"].casefold() != self.profile["environment_id"].casefold()
                    or containers[0]["image"] != self.profile["image"]):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ResourceError("The Job changed. Validate the profile again before starting.", 409) from None
        return super()._execution_environment(run_id, request, job) + [
            {"name": "AZURE_OPENAI_DEPLOYMENT_NAME", "value": self.config["model"]},
            {"name": "AZURE_OPENAI_ENDPOINT", "value": self.config["endpoint"]},
            {"name": "AZURE_OPENAI_AUTH_MODE", "value": "managed_identity"},
            {"name": "TRADINGAGENTS_LLM_PROVIDER", "value": "azure"},
        ]

    def reconcile_execution(self, run_id, execution_name):
        try:
            name = _execution_name(execution_name)
        except ValueError:
            raise ResourceError("Invalid execution name.") from None
        execution = self._request("GET", self.job_url + "/executions/" + name,
                                  params={"api-version": API_VERSION})
        try:
            containers = execution["properties"]["template"]["containers"]
            workers = [container for container in containers if container["name"] == "worker"]
            identifiers = [entry.get("value") for entry in workers[0]["env"]
                           if entry["name"] == "TRADINGAGENTS_RUN_ID"] if len(workers) == 1 else []
            if identifiers != [run_id]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ResourceError("The Azure execution does not match this research run.", 409) from None
        return name


class PublicRuns:
    def __init__(self, *, auth, profiles, controller_factory=DelegatedAzureController,
                 resource_factory=AzureResources, clock=time.time):
        self.auth = auth
        self.store = auth.store
        self.profiles = profiles
        self.controller_factory = controller_factory
        self.resource_factory = resource_factory
        self.clock = clock

    def _key(self, principal, run_id):
        try:
            canonical_uuid(run_id)
        except ValueError:
            raise ResourceError("Run not found.", 404) from None
        return principal.owner_key + "/runs/" + run_id

    def _read(self, principal, run_id):
        stored = self.store.read(self._key(principal, run_id))
        if stored is None:
            raise ResourceError("Run not found.", 404)
        return stored

    def _index(self, principal):
        return self.store.read(principal.owner_key + "/run-index") or ({"runs": []}, None)

    def _summary(self, record):
        summary = {key: value for key, value in record.items() if key not in {"events", "result", "profile_snapshot"}}
        return {**summary, "events": [], "result": None, "has_result": record["result"] is not None}

    def _update_index(self, principal, record):
        for _attempt in range(3):
            index, version = self._index(principal)
            index["runs"] = [self._summary(record) if item["id"] == record["id"] else item for item in index["runs"]]
            try:
                self.store.write(principal.owner_key + "/run-index", index, version)
                return
            except StoreConflict:
                continue
        raise StoreConflict("Run history changed.")

    def _save(self, principal, record, version):
        version = self.store.write(self._key(principal, record["id"]), contracts.scrub_secrets(record), version)
        self._update_index(principal, record)
        return version

    def _reservation_key(self, profile):
        digest = hashlib.sha256(profile["job_id"].casefold().encode("ascii")).hexdigest()
        return "reservations/" + digest

    def _releasable(self, record):
        return (record["status"] in TERMINAL and not record["start_pending"]
                and not record["start_uncertain"] and not record["stop_pending"]
                and (record["cloud_status"] in AZURE_TERMINAL or record["execution_name"] is None))

    def _recover_reservation(self, profile):
        key = self._reservation_key(profile)
        stored = self.store.read(key)
        if stored is None:
            return True
        reservation, version = stored
        try:
            parts = reservation["owner"].split("/")
            if len(parts) != 3 or parts[:2] != ["users", self.auth.tenant_id]:
                return False
            canonical_uuid(parts[2])
            canonical_uuid(reservation["run_id"])
            previous = self.store.read(reservation["owner"] + "/runs/" + reservation["run_id"])
            if (previous is None or not self._releasable(previous[0])
                    or self._reservation_key(previous[0]["profile_snapshot"]) != key):
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        self.store.delete(key, version)
        return True

    def _release(self, principal, record):
        key = self._reservation_key(record["profile_snapshot"])
        stored = self.store.read(key)
        if stored is not None:
            reservation, version = stored
            if reservation == {"owner": principal.owner_key, "run_id": record["id"]}:
                self.store.delete(key, version)

    @contextmanager
    def _controller(self, session, record):
        controller = self.controller_factory(record["profile_snapshot"],
                                             credential=self.auth.credential(session.session_id))
        try:
            yield controller
        finally:
            controller.close()

    def list(self, session_id):
        principal = self.auth.session(session_id).principal
        return sorted(self._index(principal)[0]["runs"], key=lambda record: record["created_at"], reverse=True)

    def get(self, session_id, run_id):
        return self._read(self.auth.session(session_id).principal, run_id)[0]

    def create(self, session_id, profile_id, payload, submission_id):
        session = self.auth.session(session_id)
        principal = session.principal
        try:
            canonical_uuid(submission_id)
            request = contracts.validate_request(payload)
        except (ValueError, TypeError):
            raise ResourceError("A valid request and unique submission identifier are required.") from None
        signature = hashlib.sha256(_json_bytes({"profile_id": profile_id, "request": request})).hexdigest()
        index, index_version = self._index(principal)
        previous = next((item for item in index["runs"] if item["submission_id"] == submission_id), None)
        if previous is not None:
            if previous["signature"] != signature:
                raise ResourceError("The submission identifier belongs to a different request.", 409)
            record, version = self._read(principal, previous["id"])
            if record["status"] == "starting" and not record["start_attempted"]:
                return self._start(session, record, version)
            return record
        if len(index["runs"]) >= 1000:
            raise ResourceError("The history limit is 1000 runs per user.")
        profile = self.profiles.get(principal, profile_id)
        if not self._recover_reservation(profile):
            raise ResourceError("This Job has an active or unconfirmed operation.", 409)
        credential = self.auth.credential(session_id)
        resource_client = self.resource_factory(principal, credential)
        try:
            selection = {field: profile[field] for field in PAYLOAD_FIELDS}
            verified = resource_client.validate(selection)
        finally:
            resource_client.close()
        if any(profile.get(field) != value for field, value in verified.items()):
            raise ResourceError("The selected resources changed. Validate and save the profile again.", 409)
        credential.get_token(LOG_SCOPE)
        record = {
            "id": str(uuid4()), "submission_id": submission_id, "signature": signature,
            "profile_id": profile_id, "profile_snapshot": copy.deepcopy(profile), "request": request,
            "status": "starting", "created_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "execution_name": None, "events": [], "result": None, "error": None,
            "start_pending": True, "start_attempted": False, "start_attempted_at": None, "start_uncertain": False,
            "stop_pending": False, "stop_requested": False, "stop_accepted": False,
            "last_poll_at": None, "poll_error": None, "log_warning": None,
            "collection_started_at": None, "cloud_status": None,
        }
        version = self.store.write(self._key(principal, record["id"]), record)
        index["runs"].append(self._summary(record))
        self.store.write(principal.owner_key + "/run-index", index, index_version)
        return self._start(session, record, version)

    def _start(self, session, record, version):
        principal = session.principal
        reservation_key = self._reservation_key(record["profile_snapshot"])
        reservation = {"owner": principal.owner_key, "run_id": record["id"]}
        stored = self.store.read(reservation_key)
        try:
            if stored is None:
                self.store.write(reservation_key, reservation)
            elif stored[0] != reservation:
                raise StoreConflict
        except StoreConflict:
            record.update(status="failed", start_pending=False, error="The Job is reserved by another operation.")
            self._save(principal, record, version)
            raise ResourceError("This Job has an active or unconfirmed operation.", 409) from None
        record["start_attempted"] = True
        record["start_attempted_at"] = self.clock()
        version = self.store.write(self._key(principal, record["id"]), record, version)
        try:
            with self._controller(session, record) as controller:
                execution = controller.start_run(record["id"], record["request"])
        except Exception as error:
            definite = isinstance(error, (AuthenticationRequired, ResourceError))
            uncertain = False if definite else getattr(error, "execution_may_exist", True)
            record.update(status="failed", start_pending=False, start_uncertain=uncertain,
                          error="Azure start is unconfirmed; the Job remains reserved." if uncertain
                          else "Azure did not start this run. Check resource access and configuration.")
            self._save(principal, record, version)
            if not uncertain:
                self._release(principal, record)
            if definite:
                raise
            return record
        record.update(status="running", execution_name=execution, start_pending=False)
        self._save(principal, record, version)
        return record

    def reconcile(self, session_id, run_id, execution_name):
        session = self.auth.session(session_id)
        record, version = self._read(session.principal, run_id)
        if record["execution_name"] or not record["start_attempted"]:
            raise ResourceError("This run does not require execution reconciliation.", 409)
        with self._controller(session, record) as controller:
            name = controller.reconcile_execution(run_id, execution_name)
        record.update(execution_name=name, status="running", start_uncertain=False,
                      start_pending=False, error=None, last_poll_at=None)
        self._save(session.principal, record, version)
        return record

    def stop(self, session_id, run_id):
        session = self.auth.session(session_id)
        record, version = self._read(session.principal, run_id)
        if self._releasable(record):
            self._release(session.principal, record)
            return record
        if not record["execution_name"]:
            if record["start_attempted"]:
                raise ResourceError("Start is unconfirmed. Reconcile the Azure execution before stopping.", 409)
            record.update(status="stopped", start_pending=False)
            self._save(session.principal, record, version)
            self._release(session.principal, record)
            return record
        if record["cloud_status"] in AZURE_TERMINAL:
            return record
        record.update(stop_requested=True, stop_pending=True)
        version = self._save(session.principal, record, version)
        with self._controller(session, record) as controller:
            if not record["stop_accepted"]:
                controller.stop_run(record["execution_name"])
                record["stop_accepted"] = True
                self._save(session.principal, record, version)
        return record

    def refresh(self, session_id, run_id):
        session = self.auth.session(session_id)
        principal = session.principal
        record, version = self._read(principal, run_id)
        now = self.clock()
        if (record["start_pending"] and record["start_attempted"] and not record["execution_name"]
                and now - record["start_attempted_at"] >= START_CONFIRM_SECONDS):
            record.update(status="failed", start_pending=False, start_uncertain=True,
                          error="Azure start is unconfirmed; reconcile its execution before retrying.")
            self._save(principal, record, version)
            return record
        if self._releasable(record):
            self._release(principal, record)
            if (not record["execution_name"] or (record["result"] is not None and not record["log_warning"])
                    or (record["collection_started_at"] is not None
                        and now - record["collection_started_at"] >= INGESTION_SECONDS)):
                return record
        if not record["execution_name"] or (record["last_poll_at"] is not None
                                            and now - record["last_poll_at"] < POLL_SECONDS):
            return record
        record["last_poll_at"] = now
        self.store.write(self._key(principal, run_id), record, version)
        with self._controller(session, record) as controller:
            status, events, warnings = None, [], []
            try:
                status = controller.get_status(record["execution_name"])
                events = controller.get_events(run_id, warnings=warnings)
                poll_error = None
            except CloudError:
                poll_error = "Azure status or logs are temporarily unavailable. Refresh to retry."
        record, version = self._read(principal, run_id)
        combined = {_json_bytes(event): event for event in record["events"]}
        for event in contracts.scrub_secrets(events):
            if event["kind"] == "error":
                event["payload"] = {"message": WORKER_ERROR}
            combined[_json_bytes(event)] = event
        if sum(len(key) for key in combined) > min(MAX_LOG_BYTES, self.store.max_bytes // 3):
            poll_error = "Run events exceed the storage size limit."
        else:
            record["events"] = sorted(combined.values(), key=lambda event: event["timestamp"])
        for event in record["events"]:
            if event["kind"] == "result" and _valid_result(event["payload"]):
                record["result"] = event["payload"]
        record.update(poll_error=poll_error, log_warning=warnings[0] if warnings else None)
        if status is not None:
            record["cloud_status"] = status
        status = record["cloud_status"]
        if status in AZURE_TERMINAL:
            if record["collection_started_at"] is None:
                record["collection_started_at"] = now
            record["stop_pending"] = False
            if status == "Stopped" or record["stop_requested"]:
                record.update(status="stopped", error=None)
            elif status == "Failed":
                record.update(status="failed", error="Azure Job execution failed. Review available reports.")
            elif record["result"] is not None:
                record.update(status="succeeded", error=None)
            else:
                record["status"] = "collecting"
                if now - record["collection_started_at"] >= INGESTION_SECONDS:
                    record.update(status="failed", error="No validated result arrived after 10 minutes of log ingestion.")
        self._save(principal, record, version)
        if record["status"] in TERMINAL and record["cloud_status"] in AZURE_TERMINAL:
            self._release(principal, record)
        return record


PAYLOAD_FIELDS = ("name", "job_id", "workspace_resource_id", "foundry_account_id", "deployment_name")
