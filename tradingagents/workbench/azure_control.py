"""Bounded Azure Job execution and Log Analytics access for the local workbench."""

import base64
import copy
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from azure.identity import AzureCliCredential

from tradingagents.workbench import contracts

MODEL = "gpt-5.6-sol"
API_VERSION = "2024-03-01"
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_HTTP_BYTES = 16 * 1024 * 1024
MAX_LOG_LINES = 10000
MAX_LOG_WINDOW = timedelta(hours=1, minutes=10)
ARM_SCOPE = "https://management.azure.com/.default"
LOG_SCOPE = "https://api.loganalytics.io/.default"


class CloudError(RuntimeError):
    """A public-safe cloud failure, never containing a service response."""

    def __init__(self, message, *, execution_may_exist=False, status_code=None):
        super().__init__(message)
        self.execution_may_exist = execution_may_exist
        self.status_code = status_code


def validate_run_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Invalid run identifier") from None
    return value


def _execution_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", value):
        raise ValueError("Invalid execution identifier")
    return value


def _log_time_filter(since, until):
    if since is None and until is None:
        return "TimeGenerated > ago(2d)"
    try:
        bounds = []
        for value in (since, until):
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError
            bounds.append(value.astimezone(timezone.utc))
        start, end = bounds
        if not timedelta(0) < end - start <= MAX_LOG_WINDOW:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid log time window") from None
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = end.isoformat().replace("+00:00", "Z")
    return f"TimeGenerated between (datetime({start_text}) .. datetime({end_text}))"


def validate_config(config: dict, *, allow_custom_model=False) -> dict:
    try:
        if type(config) is not dict or set(config) != {
            "subscription_id", "resource_group", "job_name", "workspace_id", "model", "endpoint",
        }:
            raise ValueError
        validate_run_id(config["subscription_id"])
        validate_run_id(config["workspace_id"])
        if not re.fullmatch(r"[A-Za-z0-9_().-]{1,90}", config["resource_group"]):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", config["job_name"]):
            raise ValueError
        endpoint = urlsplit(config["endpoint"])
        if ((not allow_custom_model and config["model"] != MODEL)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", config["model"])
            or endpoint.scheme != "https" or not endpoint.hostname
                or not endpoint.hostname.endswith((".services.ai.azure.com", ".openai.azure.com",
                                                   ".cognitiveservices.azure.com"))
                or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
                or endpoint.query or endpoint.fragment):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid deployment configuration") from None
    return dict(config)


class AzureController:
    def __init__(self, config: dict, *, credential=None, client=None, allow_custom_model=False):
        self.config = validate_config(config, allow_custom_model=allow_custom_model)
        self.credential = credential if credential is not None else AzureCliCredential()
        self.client = client if client is not None else httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None
        self._owns_credential = credential is None
        self.job_url = (
            "https://management.azure.com/subscriptions/" + self.config["subscription_id"]
            + "/resourceGroups/" + quote(self.config["resource_group"], safe="")
            + "/providers/Microsoft.App/jobs/" + self.config["job_name"]
        )

    def close(self):
        if self._owns_client:
            self.client.close()
        if self._owns_credential:
            self.credential.close()

    def _token(self, scope):
        try:
            return self.credential.get_token(scope).token
        except Exception:
            raise CloudError("Azure request failed. Check deployment and Azure CLI access.") from None

    def _request(self, method, url, *, scope=ARM_SCOPE, params=None, body=None, allow_partial=False):
        token = self._token(scope)
        try:
            kwargs = {"json": body} if body is not None else {}
            with self.client.stream(method, url, params=params,
                                    headers={"Authorization": "Bearer " + token},
                                    timeout=30, follow_redirects=False, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise CloudError("Azure request failed. Check deployment and Azure CLI access.",
                                     status_code=response.status_code)
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_HTTP_BYTES:
                        raise CloudError("Azure response exceeds the local size limit.")
                    content.extend(chunk)
                result = json.loads(content) if content else {}
                if type(result) is not dict:
                    raise CloudError("Azure returned an invalid response.")
                if "error" in result and not (
                    allow_partial and scope == LOG_SCOPE and isinstance(result["error"], dict)
                    and result["error"].get("code") == "PartialError"
                ):
                    raise CloudError("Azure returned an invalid response.")
                return result
        except CloudError:
            raise
        except Exception:
            raise CloudError("Azure request failed. Check deployment and Azure CLI access.") from None

    def _execution_environment(self, run_id, request, job):
        encoded = base64.b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
        return [{"name": "TRADINGAGENTS_RUN_REQUEST", "value": encoded},
                {"name": "TRADINGAGENTS_RUN_ID", "value": run_id}]

    def start_run(self, run_id: str, request: dict) -> str:
        validate_run_id(run_id)
        normalized = contracts.validate_request(request)
        job = self._request("GET", self.job_url, params={"api-version": API_VERSION})
        try:
            template = job["properties"]["template"]
            containers = copy.deepcopy(template["containers"])
            if not isinstance(containers, list) or not containers:
                raise ValueError
            workers = [container for container in containers if container.get("name") == "worker"]
            worker = containers[0] if len(containers) == 1 else workers[0] if len(workers) == 1 else None
            if worker is None:
                raise ValueError
            environment = self._execution_environment(run_id, normalized, job)
            overridden = {entry["name"] for entry in environment}
            worker["env"] = [entry for entry in (worker.get("env") or []) if entry["name"] not in overridden] + environment
            body = {"containers": containers}
            if template.get("initContainers") is not None:
                body["initContainers"] = copy.deepcopy(template["initContainers"])
            for group in body.values():
                for container in group:
                    for field in ("provisioningState", "provisioningError", "runningState"):
                        container.pop(field, None)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CloudError("Azure Job has no valid worker execution template.") from None
        try:
            response = self._request("POST", self.job_url + "/start",
                                     params={"api-version": API_VERSION}, body=body)
            return _execution_name(response.get("name"))
        except (ValueError, CloudError):
            raise CloudError(
                "Azure did not return a confirmed execution name. Check Job executions before retrying.",
                execution_may_exist=True,
            ) from None

    def get_status(self, execution_name: str) -> str:
        response = self._request("GET", self.job_url + "/executions/" + _execution_name(execution_name),
                                 params={"api-version": API_VERSION})
        properties = response.get("properties")
        status = properties.get("status") if isinstance(properties, dict) else None
        if not isinstance(status, str) or status not in {"Running", "Processing", "Pending", "Succeeded", "Failed", "Stopped", "Stopping", "Unknown"}:
            raise CloudError("Azure returned an unknown execution status.")
        return status

    def stop_run(self, execution_name: str) -> None:
        self._request("POST", self.job_url + "/executions/" + _execution_name(execution_name) + "/stop",
                      params={"api-version": API_VERSION})

    def get_events(self, run_id: str, *, warnings: list[str] | None = None,
                   since: datetime | str | None = None,
                   until: datetime | str | None = None) -> list[dict]:
        """Read logs with optional aware bounds: one-hour authorization plus ten-minute slack."""
        validate_run_id(run_id)
        time_filter = _log_time_filter(since, until)
        job = self.config["job_name"]
        query = (
            f"ContainerAppConsoleLogs_CL | where {time_filter} "
            "| extend WorkbenchJob = coalesce(tostring(column_ifexists('JobName_s', '')), "
            "tostring(column_ifexists('ContainerJobName_s', '')), "
            "tostring(column_ifexists('ContainerAppName_s', ''))) "
            f"| where WorkbenchJob == '{job}' "
            "| extend Log_s = tostring(column_ifexists('Log_s', '')) "
            f"| where Log_s has '{run_id}' and Log_s startswith 'TAWB1 ' "
            "| order by TimeGenerated asc | project Log_s | limit 10000"
        )
        response = self._request(
            "POST", "https://api.loganalytics.azure.com/v1/workspaces/" + self.config["workspace_id"] + "/query",
            scope=LOG_SCOPE, body={"query": query}, allow_partial=True,
        )
        try:
            groups = {}
            total_bytes = 0
            count = 0
            if not isinstance(response["tables"], list):
                raise ValueError
            for table in response["tables"]:
                if (not isinstance(table, dict) or not isinstance(table["columns"], list)
                        or not isinstance(table["rows"], list)
                        or any(not isinstance(column, dict) or not isinstance(column.get("name"), str)
                               for column in table["columns"])):
                    raise ValueError
                columns = [column["name"] for column in table["columns"]]
                if columns.count("Log_s") != 1:
                    raise ValueError
                index = columns.index("Log_s")
                count += len(table["rows"])
                if count > MAX_LOG_LINES:
                    raise CloudError("Run logs exceed the local size limit.")
                for row in table["rows"]:
                    if not isinstance(row, list) or len(row) != len(columns):
                        raise ValueError
                    line = row[index]
                    if not isinstance(line, str):
                        raise ValueError
                    total_bytes += len(line.encode("utf-8"))
                    if total_bytes > MAX_LOG_BYTES:
                        raise CloudError("Run logs exceed the local size limit.")
                    if not line.startswith("TAWB1 ") or len(line) > contracts.MAX_LINE_BYTES:
                        continue
                    try:
                        message_id = json.loads(line[6:]).get("message_id")
                        if isinstance(message_id, str):
                            groups.setdefault(message_id, []).append(line)
                    except (ValueError, AttributeError):
                        continue
            events = []
            decoded_bytes = 0
            for lines in groups.values():
                decoded = contracts.decode_events(lines, run_id)
                decoded_bytes += len(json.dumps(decoded, ensure_ascii=True).encode())
                if decoded_bytes > MAX_LOG_BYTES:
                    raise CloudError("Decoded run events exceed the local size limit.")
                events.extend(decoded)
            if "error" in response and warnings is not None:
                warnings.append("Azure returned partial query results. Available verified events are retained; refresh to retry.")
            return sorted(events, key=lambda event: event["timestamp"])
        except (KeyError, TypeError, ValueError, IndexError):
            raise CloudError("Run logs are not available yet. Retry after ingestion.") from None
