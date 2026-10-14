"""Fixed-installation hosted execution; no user-selectable Azure resources."""

import base64
import hashlib
import hmac
import json
import re
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import httpx

from tradingagents.workbench.azure_control import API_VERSION, AzureController
from tradingagents.workbench.contracts import validate_request
from tradingagents.workbench.public_accounts import EmailPrincipal
from tradingagents.workbench.public_gateway import GatewayError, RunCapability
from tradingagents.workbench.public_settings import SettingsSnapshot
from tradingagents.workbench.public_store import StoreConflict
from tradingagents.workbench.public_worker import _https_origin, hosted_public_config_for_settings

_SECRET = "tawb-run-capability"
_PREFIX = "email-v1/executions/"
_TERMINAL = {"Succeeded", "Failed", "Stopped"}
_CLOUD_STATUSES = _TERMINAL | {"Running", "Processing", "Pending", "Stopping", "Unknown"}
_STATES = {"reserved", "authorization_attempted", "authorized", "secret_attempted",
           "prepared", "start_attempted", "running", "stop_attempted", "terminal", "quarantined"}
_FLAGS = {"authorization_attempted", "secret_attempted", "secret_confirmed", "start_attempted",
          "stop_attempted", "stop_sent", "revoked"}
_FIELDS = _FLAGS | {"run_id", "owner", "installation", "snapshot", "status", "capability",
                   "worker_config", "request", "execution_name", "cloud_status",
                   "authorization_expires_at", "error_code"}
_ERRORS = {None, "authorization_expired", "cancelled_before_start"}


def _safe(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as error:
            code = error.status_code if isinstance(error, ExecutionError) else 409 if isinstance(error, StoreConflict) else 503
        raise ExecutionError(status_code=code)
    return invoke


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _ExecutionRecord:
    value: dict = field(repr=False)
    version: str = field(repr=False)


class ExecutionError(RuntimeError):
    def __init__(self, *, status_code=503):
        super().__init__({
            400: "Invalid hosted execution request.",
            403: "Hosted execution is not permitted.",
            404: "Hosted execution was not found.",
            409: "Hosted execution requires reconciliation.",
            503: "Hosted execution is unavailable.",
        }[status_code])
        self.status_code = status_code


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _location_key(value):
    if type(value) is not str:
        raise ValueError
    return "".join(value.split()).casefold()


def _installation(config):
    try:
        if type(config) is not dict or set(config) != {
            "subscription_id", "resource_group", "job_name", "job_resource_id",
            "environment_id", "worker_identity_id", "image", "backend_origin",
            "workspace_id", "location",
        }:
            raise ValueError
        result = dict(config)
        for name in ("subscription_id", "workspace_id"):
            result[name] = _uuid(config[name].lower())
        if (not re.fullmatch(r"[A-Za-z0-9_().-]{1,90}", config["resource_group"])
            or config["resource_group"].endswith(".")):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,30}[A-Za-z0-9]", config["job_name"]):
            raise ValueError
        if config["job_name"].casefold() == "job-research-example":
            raise ValueError
        result["resource_group"] = config["resource_group"].lower()
        result["job_name"] = config["job_name"].lower()
        root = "/subscriptions/" + result["subscription_id"] + "/resourcegroups/"
        for name, resource_type in (
            ("job_resource_id", "microsoft.app/jobs"),
            ("environment_id", "microsoft.app/managedenvironments"),
            ("worker_identity_id", "microsoft.managedidentity/userassignedidentities"),
        ):
            value = config[name].lower()
            if not re.fullmatch(re.escape(root) + r"[a-z0-9_().-]{1,90}/providers/"
                                + re.escape(resource_type) + r"/[a-z0-9][a-z0-9-]{0,63}", value):
                raise ValueError
            if value.split("/")[4].endswith("."):
                raise ValueError
            result[name] = value
        if result["job_resource_id"] != (root + result["resource_group"]
                + "/providers/microsoft.app/jobs/" + result["job_name"]):
            raise ValueError
        if not re.fullmatch(r"[a-z0-9]+\.azurecr\.io/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}", config["image"]):
            raise ValueError
        if not re.fullmatch(r"[a-z][a-z0-9]{1,31}", config["location"]):
            raise ValueError
        result["backend_origin"] = _https_origin(config["backend_origin"])
        return MappingProxyType(result)
    except (ValueError, TypeError, AttributeError, RuntimeError):
        raise ExecutionError() from None


class _FixedController:
    _request = AzureController._request
    get_events = AzureController.get_events

    def __init__(self, config, credential, client):
        self.config = config
        self.credential = credential
        self.client = client
        self.job_url = "https://management.azure.com" + config["job_resource_id"]

    def _token(self, scope):
        try:
            return self.credential.get_token(scope).token
        except Exception:
            raise ExecutionError() from None


class HostedExecution:
    def __init__(self, store, *, gateway, installation, credential, client=None, clock=time.time, previous_images=()):
        self._installation = _installation(installation)
        if credential is None or not callable(getattr(credential, "get_token", None)):
            raise ExecutionError()
        self._store = store
        self._gateway = gateway
        self._clock = clock
        self._fingerprint = _digest(dict(self._installation))
        self._images = {self._fingerprint: self._installation["image"]}
        if type(previous_images) not in (list, tuple) or len(previous_images) > 8:
            raise ExecutionError()
        for image in previous_images:
            previous = _installation({**self._installation, "image": image})
            fingerprint = _digest(dict(previous))
            if (fingerprint in self._images
                    or image.split("@", 1)[0] != self._installation["image"].split("@", 1)[0]):
                raise ExecutionError()
            self._images[fingerprint] = image
        self._reservation_key = "email-v1/execution-jobs/" + hashlib.sha256(
            self._installation["job_resource_id"].casefold().encode("ascii")
        ).hexdigest()
        self._owns_client = client is None
        self._cloud = _FixedController(
            self._installation, credential,
            client if client is not None else httpx.Client(trust_env=False, follow_redirects=False),
        )

    def close(self):
        if self._owns_client:
            self._cloud.client.close()

    def worker_access(self, capability):
        try:
            grant, principal, _, _ = self._gateway.resolve_grant_access(capability)
            owner = self._identity(grant["run_id"], principal)
            self._record(grant["run_id"], owner)
        except GatewayError as error:
            raise ExecutionError(status_code=error.status_code) from None
        return grant["run_id"], principal

    def _identity(self, run_id, principal):
        try:
            _uuid(run_id)
            if type(principal) is not EmailPrincipal:
                raise ValueError
            _uuid(principal.object_id)
            if principal.owner_key != "email-v1/users/" + principal.object_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ExecutionError(status_code=403) from None
        return principal.owner_key

    def _record(self, run_id, owner):
        stored = self._store.read(_PREFIX + run_id)
        if stored is None:
            raise ExecutionError(status_code=404)
        value, version = stored
        if value.get("owner") != owner:
            raise ExecutionError(status_code=403)
        try:
            if set(value) == _FIELDS - {"authorization_expires_at", "error_code"}:
                value = {**value, "error_code": None, "authorization_expires_at": (
                    self._gateway.authorization_expires_at(run_id) if value["authorization_attempted"] else None)}
            if (set(value) != _FIELDS or value["run_id"] != run_id
                    or value["installation"] not in self._images or value["status"] not in _STATES
                    or any(type(value[name]) is not bool for name in _FLAGS)
                    or value["error_code"] not in _ERRORS
                    or value["authorization_expires_at"] is not None and (
                        type(value["authorization_expires_at"]) is not int
                        or value["authorization_expires_at"] <= 0)
                    or not value["authorization_attempted"] and value["authorization_expires_at"] is not None
                    or value["authorization_expires_at"] is None and (
                        value["capability"] is not None or value["secret_attempted"])):
                raise ValueError
            snapshot = value["snapshot"]
            if (set(snapshot) != {"owner_key", "run_id", "revision", "signature"}
                    or snapshot["owner_key"] != owner or snapshot["run_id"] != run_id
                    or not re.fullmatch(r"[0-9a-f]{64}", snapshot["signature"])):
                raise ValueError
            _uuid(snapshot["revision"])
            capability = value["capability"]
            if capability is not None and (type(capability) is not str or not re.fullmatch(
                    re.escape(run_id) + r"\.[A-Za-z0-9_-]{43}", capability)):
                raise ValueError
            config = value["worker_config"]
            if config is not None:
                expected = hosted_public_config_for_settings(
                    mode=config["mode"], provider=config.get("provider", "azure"),
                    quick_model=config["quick_model"], deep_model=config["deep_model"],
                    asset_type=config["asset_type"], data_vendors=config["data_vendors"],
                    provider_config=config.get("provider_config"), data_key_configured=config["dataKeyConfigured"],
                )
                if config != expected or capability is None:
                    raise ValueError
            if (value["execution_name"] is None) != (value["cloud_status"] is None):
                raise ValueError
            if value["execution_name"] is not None:
                self._execution_name(value["execution_name"])
                if value["cloud_status"] not in _CLOUD_STATUSES:
                    raise ValueError
            if value["request"] is not None and validate_request(value["request"]) != value["request"]:
                raise ValueError
            if (value["start_attempted"] and (not value["secret_confirmed"] or value["request"] is None)
                    or value["secret_attempted"] and (config is None or not value["authorization_attempted"])
                    or value["secret_confirmed"] and not value["secret_attempted"]
                    or value["status"] == "prepared" and not value["secret_confirmed"]
                    or value["status"] == "terminal" and not value["revoked"]):
                raise ValueError
        except Exception:
            raise ExecutionError() from None
        return _ExecutionRecord(value, version)

    def _save(self, record, **changes):
        value = {**record.value, **changes}
        version = self._store.write(_PREFIX + value["run_id"], value, record.version)
        record.value, record.version = value, version

    def _reservation(self):
        stored = self._store.read(self._reservation_key)
        if stored is not None:
            value = stored[0]
            if (set(value) != {"job_id", "installation", "run_id", "owner", "previous", "released"}
                    or value["job_id"] != self._installation["job_resource_id"]
                    or value["installation"] not in self._images
                    or type(value["released"]) is not bool):
                raise ExecutionError()
        return stored

    def _held(self, record):
        stored = self._reservation()
        if (stored is None or stored[0]["released"] or stored[0]["run_id"] != record.value["run_id"]
            or stored[0]["owner"] != record.value["owner"]
            or stored[0]["installation"] != record.value["installation"]):
            raise ExecutionError(status_code=409)
        return stored

    def _reserve(self, run_id, owner):
        stored = self._reservation()
        if stored is not None and not stored[0]["released"]:
            if stored[0]["run_id"] != run_id or stored[0]["owner"] != owner:
                raise ExecutionError(status_code=409)
            return
        previous = None
        if stored is not None:
            completed = self._record(stored[0]["run_id"], stored[0]["owner"]).value
            if completed["status"] != "terminal" or not completed["revoked"]:
                raise ExecutionError()
            previous = completed["run_id"] if completed["secret_confirmed"] else stored[0]["previous"]
        self._store.write(self._reservation_key, {
            "job_id": self._installation["job_resource_id"], "installation": self._fingerprint,
            "run_id": run_id, "owner": owner, "previous": previous, "released": False,
        }, stored[1] if stored is not None else None)

    def _arm(self, method, suffix="", *, body=None):
        return self._cloud._request(method, self._cloud.job_url + suffix,
                                    params={"api-version": API_VERSION}, body=body)

    def _environment(self, config=None):
        entries = [{"name": "TAWB_WORKER_MODE", "value": "hosted"},
                   {"name": "TAWB_BACKEND_BASE_URL", "value": self._installation["backend_origin"]}]
        if config is not None:
            entries.insert(0, {"name": "TAWB_RUN_CAPABILITY", "secretRef": _SECRET})
            entries.append({"name": "TAWB_PUBLIC_CONFIG", "value": json.dumps(config, separators=(",", ":"))})
        return entries

    def _worker(self, config=None, *, image=None):
        return {"name": "worker", "image": image or self._installation["image"],
                "resources": {"cpu": 1, "memory": "2Gi"}, "env": self._environment(config)}

    def _job_body(self, record):
        return {
            "location": self._installation["location"],
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {
                self._installation["worker_identity_id"]: {},
            }},
            "properties": {
                "environmentId": self._installation["environment_id"],
                "workloadProfileName": "Consumption",
                "configuration": {
                    "triggerType": "Manual", "replicaTimeout": 3600, "replicaRetryLimit": 0,
                    "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                    "registries": [{"server": self._installation["image"].split("/")[0],
                                    "identity": self._installation["worker_identity_id"]}],
                    "secrets": [{"name": _SECRET, "value": record.value["capability"]}],
                },
                "template": {"containers": [self._worker(record.value["worker_config"],
                    image=self._images[record.value["installation"]])], "initContainers": []},
            },
        }

    def _metadata_fields(self, properties):
        allowed = {"eventStreamEndpoint", "outboundIpAddresses"}
        extra = set(properties) - {
            "environmentId", "configuration", "template", "provisioningState", "workloadProfileName",
        } - allowed
        if extra:
            raise ValueError
        endpoint = properties.get("eventStreamEndpoint")
        if endpoint is not None and type(endpoint) is not str:
            raise ValueError
        outbound = properties.get("outboundIpAddresses")
        if outbound is not None and (
            type(outbound) is not list or any(type(item) is not str for item in outbound)
        ):
            raise ValueError

    def _normalize_configuration(self, configuration, config):
        for name in ("dapr", "eventTriggerConfig", "scheduleTriggerConfig"):
            if name in configuration:
                if configuration[name] is not None:
                    raise ValueError
                configuration.pop(name)
        registries = configuration["registries"]
        if len(registries) != 1:
            raise ValueError
        registry = registries[0]
        registry["identity"] = registry["identity"].casefold()
        for name in ("username", "passwordSecretRef"):
            if name in registry:
                if registry[name] != "":
                    raise ValueError
                registry.pop(name)
        expected = self._job_body(_ExecutionRecord({"capability": "", "worker_config": config,
            "installation": self._fingerprint}, ""))["properties"]["configuration"]
        expected["secrets"] = [{"name": _SECRET}] if config is not None else []
        if config is None and configuration.get("secrets") is None and "secrets" in configuration:
            configuration["secrets"] = []
        if configuration != expected:
            raise ValueError

    def _normalize_template(self, template, config, image):
        resources = template["containers"][0]["resources"]
        if type(resources["cpu"]) not in (int, float):
            raise ValueError
        if "ephemeralStorage" in resources:
            if resources["ephemeralStorage"] != "":
                raise ValueError
            resources.pop("ephemeralStorage")
        for name in ("initContainers", "volumes"):
            if name in template:
                if template[name] not in (None, []):
                    raise ValueError
                template.pop(name)
        if template != {"containers": [self._worker(config, image=image)]}:
            raise ValueError

    def _validate_job(self, job, config=None, *, image=None):
        try:
            fixed = self._installation
            if (job["id"].casefold() != fixed["job_resource_id"]
                    or job["name"].casefold() != fixed["job_name"]
                    or job["type"].casefold() != "microsoft.app/jobs"
                    or _location_key(job["location"]) != _location_key(fixed["location"])):
                raise ValueError
            identity = job["identity"]
            identities = identity["userAssignedIdentities"]
            if (set(identity) != {"type", "userAssignedIdentities"} or identity["type"] != "UserAssigned"
                    or len(identities) != 1
                    or next(iter(identities)).casefold() != fixed["worker_identity_id"]):
                raise ValueError
            properties = job["properties"]
            self._metadata_fields(properties)
            if (properties["environmentId"].casefold() != fixed["environment_id"]
                    or properties.get("workloadProfileName", "Consumption") != "Consumption"):
                raise ValueError
            configuration = deepcopy(properties["configuration"])
            if any(type(configuration[name]) is not int for name in ("replicaTimeout", "replicaRetryLimit")):
                raise ValueError
            if any(type(configuration["manualTriggerConfig"][name]) is not int
                   for name in ("parallelism", "replicaCompletionCount")):
                raise ValueError
            self._normalize_configuration(configuration, config)
            template = deepcopy(properties["template"])
            if image is None and config is None:
                image = template["containers"][0]["image"]
                if image not in self._images.values():
                    raise ValueError
            self._normalize_template(template, config, image)
            if properties["provisioningState"] != "Succeeded":
                raise ExecutionError(status_code=409)
        except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
            raise ExecutionError(status_code=409) from None

    def _confirm_secret(self, record):
        self._validate_job(self._arm("GET"), record.value["worker_config"],
                           image=self._images[record.value["installation"]])
        secrets = self._arm("POST", "/listSecrets", body={})
        if (set(secrets) != {"value"} or type(secrets["value"]) is not list or len(secrets["value"]) != 1
                or set(secrets["value"][0]) != {"name", "value"}
                or secrets["value"][0]["name"] != _SECRET
                or type(secrets["value"][0]["value"]) is not str
                or not hmac.compare_digest(secrets["value"][0]["value"], record.value["capability"])):
            raise ExecutionError(status_code=409)

    @_safe
    def get(self, run_id, principal):
        record = self._record(run_id, self._identity(run_id, principal)).value
        status = record["status"]
        if status in {"authorization_attempted", "authorized", "secret_attempted", "start_attempted", "quarantined"}:
            status = "reconciliation_required"
        return {"run_id": run_id, "status": status,
                "execution_name": record["execution_name"], "cloud_status": record["cloud_status"],
                "revoked": record["revoked"], "authorization_expires_at": record["authorization_expires_at"],
                "error_code": record["error_code"] or (
                    "authorization_expired" if status != "terminal" and self._expired(record) else None)}

    def _expired(self, value):
        return (value["authorization_expires_at"] is not None
                and value["authorization_expires_at"] <= int(self._clock()))

    def admission_ready(self, principal, snapshot):
        try:
            self._gateway.admission_ready(principal, snapshot)
        except GatewayError as error:
            raise ExecutionError(status_code=error.status_code) from None

    @_safe
    def prepare(self, run_id, principal, snapshot):
        owner = self._identity(run_id, principal)
        if (type(snapshot) is not SettingsSnapshot or snapshot.run_id != run_id
                or snapshot.owner_key != owner):
            raise ExecutionError(status_code=403)
        try:
            record = self._record(run_id, owner)
        except ExecutionError as error:
            if error.status_code != 404:
                raise
            self.admission_ready(principal, snapshot)
            self._reserve(run_id, owner)
            value = {
                "run_id": run_id, "owner": owner, "installation": self._fingerprint,
                "snapshot": asdict(snapshot), "status": "reserved", "capability": None,
                "worker_config": None, "request": None, "execution_name": None, "cloud_status": None,
                "authorization_expires_at": None, "error_code": None,
                "authorization_attempted": False, "secret_attempted": False, "secret_confirmed": False,
                "start_attempted": False, "stop_attempted": False, "stop_sent": False, "revoked": False,
            }
            record = _ExecutionRecord(value, self._store.write(_PREFIX + run_id, value))
        self._held(record)
        if record.value["snapshot"] != asdict(snapshot):
            raise ExecutionError(status_code=403)
        if self._expired(record.value):
            self._reconcile(record)
            raise ExecutionError(status_code=409)
        if record.value["status"] == "reserved":
            self.admission_ready(principal, snapshot)
            expires_at = int(self._clock()) + 3600
            self._save(record, status="authorization_attempted", authorization_attempted=True,
                       authorization_expires_at=expires_at)
            capability = self._gateway.authorize(run_id, principal, snapshot, expires_at=expires_at)
            if type(capability) is not RunCapability or capability.run_id != run_id:
                raise ExecutionError()
            config = self._gateway.worker_config(capability)
            self._save(record, status="authorized", capability=capability.serialize(), worker_config=config)
        if record.value["status"] == "authorized":
            reservation = self._held(record)[0]
            previous_record = None
            if reservation["previous"] is not None:
                previous = self._store.read(_PREFIX + reservation["previous"])
                if previous is None or previous[0]["status"] != "terminal" or not previous[0]["revoked"]:
                    raise ExecutionError()
                previous_record = self._record(reservation["previous"], previous[0]["owner"])
            if previous_record is not None and previous_record.value["secret_confirmed"]:
                self._confirm_secret(previous_record)
            else:
                self._validate_job(self._arm("GET"))
            self._check_idle()
            if self._expired(record.value):
                self._reconcile(record)
                raise ExecutionError(status_code=409)
            self._save(record, status="secret_attempted", secret_attempted=True)
            properties = self._job_body(record)["properties"]
            result = self._arm("PATCH", body={"properties": {
                "configuration": {"secrets": properties["configuration"]["secrets"]},
                "template": {"containers": properties["template"]["containers"]},
            }})
            if result.get("properties", {}).get("provisioningState") != "Succeeded":
                raise ExecutionError(status_code=409)
        if record.value["status"] == "secret_attempted":
            self._confirm_secret(record)
            self._save(record, status="prepared", secret_confirmed=True)
        if record.value["status"] != "prepared":
            raise ExecutionError(status_code=409)
        if self._gateway.worker_config(record.value["capability"]) != record.value["worker_config"]:
            raise ExecutionError(status_code=409)
        return deepcopy(record.value["worker_config"])

    def _payload(self, record):
        raw = json.dumps(record.value["request"], separators=(",", ":")).encode("ascii")
        if len(raw) > 16 * 1024:
            raise ExecutionError(status_code=400)
        worker = self._worker(record.value["worker_config"], image=self._images[record.value["installation"]])
        worker["env"].extend([
            {"name": "TRADINGAGENTS_RUN_REQUEST", "value": base64.b64encode(raw).decode("ascii")},
            {"name": "TRADINGAGENTS_RUN_ID", "value": record.value["run_id"]},
        ])
        return {"containers": [worker]}

    def _execution_name(self, value):
        if (type(value) is not str or not re.fullmatch(
                re.escape(self._installation["job_name"]) + r"-[a-z0-9][a-z0-9-]{0,63}", value)):
            raise ExecutionError(status_code=409)
        return value

    def _execution(self, value):
        try:
            name = self._execution_name(value["name"])
            if (value["id"].casefold() != self._installation["job_resource_id"] + "/executions/" + name
                    or value["type"].casefold() != "microsoft.app/jobs/executions"
                    or value["properties"]["status"] not in _CLOUD_STATUSES):
                raise ValueError
            template = deepcopy(value["properties"]["template"])
            if template.get("initContainers") in (None, []):
                template.pop("initContainers", None)
            containers = template["containers"]
            if len(containers) != 1:
                raise ValueError
            for container in containers:
                for field_name in ("provisioningState", "provisioningError", "runningState"):
                    container.pop(field_name, None)
                if "imageType" in container and container.pop("imageType") != "ContainerImage":
                    raise ValueError
                resources = container["resources"]
                if "ephemeralStorage" in resources and resources.pop("ephemeralStorage") != "":
                    raise ValueError
            env = containers[0]["env"]
            for entry in env:
                if (entry.get("name") == "TAWB_RUN_CAPABILITY"
                        and entry.get("secretRef") == "cappjob-" + self._installation["job_name"]):
                    entry["secretRef"] = _SECRET
            run_ids = [entry["value"] for entry in env if entry["name"] == "TRADINGAGENTS_RUN_ID"]
            if len(run_ids) != 1:
                raise ValueError
            _uuid(run_ids[0])
            return name, run_ids[0], template, value["properties"]["status"]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExecutionError(status_code=409) from None

    def _executions(self):
        url = self._cloud.job_url + "/executions"
        params = {"api-version": API_VERSION}
        seen, executions = set(), []
        for _ in range(10):
            if url in seen:
                raise ExecutionError(status_code=409)
            seen.add(url)
            result = self._cloud._request("GET", url, params=params)
            if (set(result) - {"value", "nextLink"} or type(result.get("value")) is not list
                    or len(executions) + len(result["value"]) > 1000):
                raise ExecutionError(status_code=409)
            executions.extend(result["value"])
            next_link = result.get("nextLink")
            if next_link is None or next_link == "":
                return executions
            try:
                if type(next_link) is not str or len(next_link) > 4096:
                    raise ValueError
                parsed = urlsplit(next_link)
                pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
                query = dict(pairs)
                if (parsed.scheme != "https" or parsed.netloc != "management.azure.com"
                        or parsed.fragment or parsed.path.casefold() != self._installation["job_resource_id"] + "/executions"
                        or len(pairs) != len(query) or set(query) - {"api-version", "$skipToken"}
                        or query.get("api-version") != API_VERSION):
                    raise ValueError
            except (ValueError, TypeError):
                raise ExecutionError(status_code=409) from None
            url, params = next_link, None
        raise ExecutionError(status_code=409)

    def _check_idle(self):
        if any(item.get("properties", {}).get("status") not in _TERMINAL for item in self._executions()):
            raise ExecutionError(status_code=409)

    def _release(self, record):
        stored = self._reservation()
        if stored is None:
            raise ExecutionError()
        value, version = stored
        if value["run_id"] != record.value["run_id"] or value["released"]:
            return
        if record.value["status"] != "terminal" or not record.value["revoked"]:
            raise ExecutionError(status_code=409)
        self._store.write(self._reservation_key, {**value, "released": True}, version)

    def _finish(self, record, status=None):
        self._gateway.revoke(record.value["run_id"])
        self._save(record, status="terminal", cloud_status=status, revoked=True)
        self._release(record)

    def _request_stop(self, record):
        if not record.value["stop_attempted"]:
            try:
                self._save(record, stop_attempted=True,
                           status="quarantined" if record.value["status"] == "quarantined" else "stop_attempted",
                           error_code=record.value["error_code"] if record.value["start_attempted"] else (
                               "authorization_expired" if self._expired(record.value) else "cancelled_before_start"))
            except Exception:
                durable = self._record(record.value["run_id"], record.value["owner"])
                if durable.value["stop_attempted"]:
                    self._gateway.revoke(record.value["run_id"])
                raise
        self._gateway.revoke(record.value["run_id"])
        if not record.value["revoked"]:
            self._save(record, revoked=True)

    def _reconcile(self, record):
        if record.value["status"] == "terminal":
            self._release(record)
            return
        self._held(record)
        if record.value["status"] == "quarantined":
            raise ExecutionError(status_code=409)
        if not record.value["start_attempted"]:
            if (record.value["stop_attempted"] or self._expired(record.value)
                    or record.value["authorization_attempted"] and record.value["capability"] is None):
                self._request_stop(record)
            if record.value["secret_attempted"] and not record.value["secret_confirmed"]:
                self._confirm_secret(record)
                self._save(record, secret_confirmed=True,
                           status="stop_attempted" if record.value["stop_attempted"] else "prepared")
            if record.value["stop_attempted"]:
                self._finish(record)
            elif record.value["status"] != "prepared":
                raise ExecutionError(status_code=409)
            return
        matches = []
        try:
            for item in self._executions():
                details = self._execution(item)
                if details[1] == record.value["run_id"]:
                    self._confirm_secret(record)
                    if details[2] != self._payload(record):
                        raise ExecutionError(status_code=409)
                    matches.append(details)
            if len(matches) > 1:
                raise ExecutionError(status_code=409)
        except ExecutionError:
            self._save(record, status="quarantined")
            raise
        if not matches:
            raise ExecutionError(status_code=409)
        name = matches[0][0]
        details = self._execution(self._arm("GET", "/executions/" + name))
        if (details[0] != name or details[1] != record.value["run_id"]
                or details[2] != self._payload(record)
                or record.value["execution_name"] not in (None, name)):
            self._save(record, status="quarantined")
            raise ExecutionError(status_code=409)
        self._save(record, execution_name=name, cloud_status=details[3],
                   status="stop_attempted" if record.value["stop_attempted"] else "running")
        if details[3] in _TERMINAL:
            self._finish(record, details[3])

    @_safe
    def start(self, run_id, principal, request):
        record = self._record(run_id, self._identity(run_id, principal))
        try:
            normalized = validate_request(request)
        except (ValueError, TypeError):
            raise ExecutionError(status_code=400) from None
        config = record.value["worker_config"]
        if (config is None or normalized["asset_type"] != config["asset_type"]
                or normalized["data_vendors"] != config["data_vendors"]
                or record.value["request"] not in (None, normalized)):
            raise ExecutionError(status_code=409)
        if record.value["start_attempted"]:
            self._reconcile(record)
            return self.get(run_id, principal)
        if record.value["status"] == "terminal":
            self._release(record)
            return self.get(run_id, principal)
        self._held(record)
        if self._expired(record.value):
            self._reconcile(record)
            return self.get(run_id, principal)
        if record.value["status"] != "prepared" or record.value["stop_attempted"]:
            raise ExecutionError(status_code=409)
        if self._gateway.worker_config(record.value["capability"]) != config:
            raise ExecutionError(status_code=409)
        self._confirm_secret(record)
        self._check_idle()
        if self._expired(record.value):
            self._reconcile(record)
            return self.get(run_id, principal)
        record.value["request"] = normalized
        payload = self._payload(record)
        self._save(record, status="start_attempted", start_attempted=True)
        self._arm("POST", "/start", body=payload)
        self._reconcile(record)
        return self.get(run_id, principal)

    @_safe
    def reconcile(self, run_id, principal):
        record = self._record(run_id, self._identity(run_id, principal))
        self._reconcile(record)
        return self.get(run_id, principal)

    @_safe
    def stop(self, run_id, principal):
        record = self._record(run_id, self._identity(run_id, principal))
        if record.value["status"] == "terminal":
            self._release(record)
            return self.get(run_id, principal)
        self._held(record)
        self._request_stop(record)
        if record.value["status"] == "quarantined":
            raise ExecutionError(status_code=409)
        if not record.value["start_attempted"]:
            if record.value["secret_attempted"] and not record.value["secret_confirmed"]:
                raise ExecutionError(status_code=409)
            self._finish(record)
            return self.get(run_id, principal)
        self._reconcile(record)
        if record.value["status"] != "terminal" and not record.value["stop_sent"]:
            self._save(record, stop_sent=True)
            self._arm("POST", "/executions/" + record.value["execution_name"] + "/stop")
        return self.get(run_id, principal)

    @_safe
    def get_events(self, run_id, principal):
        record = self._record(run_id, self._identity(run_id, principal))
        expires_at = record.value["authorization_expires_at"]
        if expires_at is None:
            return []
        expiry = datetime.fromtimestamp(expires_at, timezone.utc)
        events = self._cloud.get_events(run_id, since=expiry - timedelta(seconds=3600),
                                        until=expiry + timedelta(seconds=600))
        capability = record.value["capability"]

        def redact(value):
            if isinstance(value, str) and capability:
                return value.replace(capability, "[REDACTED]").replace(capability.split(".")[1], "[REDACTED]")
            if isinstance(value, dict):
                return {redact(name): redact(content) for name, content in value.items()}
            if isinstance(value, list):
                return [redact(content) for content in value]
            return value

        return redact(events)
