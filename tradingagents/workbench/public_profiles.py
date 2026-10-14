"""Delegated Azure discovery and owner-scoped, validated resource profiles."""

import copy
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from tradingagents.workbench.public_auth import ARM_SCOPE, AuthenticationRequired, canonical_uuid
from tradingagents.workbench.public_store import StoreConflict

ARM = "https://management.azure.com"
RESOURCE_TYPES = {
    "job": "Microsoft.App/jobs", "environment": "Microsoft.App/managedEnvironments",
    "workspace": "Microsoft.OperationalInsights/workspaces",
    "foundry": "Microsoft.CognitiveServices/accounts",
}
VERSIONS = {"job": "2024-03-01", "environment": "2024-03-01",
            "workspace": "2023-09-01", "foundry": "2024-10-01"}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResourceError(RuntimeError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def resource_name(value, *, group=False):
    pattern = r"[A-Za-z0-9_().-]{1,90}" if group else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or value in (".", ".."):
        raise ResourceError("Invalid Azure resource name.")
    return value


def resource_id(value, kind):
    if not isinstance(value, str):
        raise ResourceError("Invalid Azure resource identifier.")
    match = re.fullmatch(
        r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/]+)/providers/"
        + re.escape(RESOURCE_TYPES[kind]) + r"/([^/]+)", value, re.IGNORECASE,
    )
    if match is None:
        raise ResourceError("Invalid Azure resource identifier.")
    subscription, group, name = match.groups()
    try:
        canonical_uuid(subscription)
    except ValueError:
        raise ResourceError("Invalid subscription identifier.") from None
    resource_name(group, group=True)
    resource_name(name)
    return subscription, group, name


def profile_input(value):
    if type(value) is not dict or set(value) != {
        "name", "job_id", "workspace_resource_id", "foundry_account_id", "deployment_name",
    }:
        raise ResourceError("Only named Azure resource selections are accepted.")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"].strip()) <= 80:
        raise ResourceError("Profile name must contain 1 to 80 characters.")
    resource_id(value["job_id"], "job")
    resource_id(value["workspace_resource_id"], "workspace")
    resource_id(value["foundry_account_id"], "foundry")
    resource_name(value["deployment_name"])
    return {**value, "name": value["name"].strip()}


class AzureResources:
    def __init__(self, principal, credential, *, client=None):
        if credential is None:
            raise ValueError("Delegated credentials are required")
        self.principal = principal
        self.credential = credential
        self.client = client if client is not None else httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def read(self, path, version):
        return self._request("GET", path, version)

    def write(self, path, version, body, *, method="PUT", create_only=False):
        if method not in {"PUT", "POST", "PATCH"} or type(body) is not dict:
            raise ResourceError("Invalid Azure write request.")
        if not isinstance(path, str) or urlsplit(path).query or urlsplit(path).fragment:
            raise ResourceError("Invalid Azure write path.")
        if len(json.dumps(body, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ResourceError("Azure request exceeds the size limit.")
        return self._request(method, path, version, body=body, create_only=create_only)

    def _request(self, method, path, version, *, body=None, create_only=False):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ResourceError("Invalid Azure request path.")
        token = self.credential.get_token(ARM_SCOPE).token
        try:
            url = httpx.URL(ARM + path).copy_merge_params({"api-version": version})
            headers = {"Authorization": "Bearer " + token}
            if create_only:
                headers["If-None-Match"] = "*"
            options = {"json": body} if body is not None else {}
            with self.client.stream(method, url, headers=headers,
                                    timeout=30, follow_redirects=False, **options) as response:
                if response.status_code == 401:
                    raise AuthenticationRequired(scope=ARM_SCOPE)
                if response.status_code == 403:
                    raise ResourceError("Your Azure identity lacks permission for this resource.", 403)
                if response.status_code == 404:
                    raise ResourceError("The selected Azure resource was not found.", 404)
                if response.status_code in {409, 412}:
                    raise ResourceError("The Azure resource changed or is already in use.", 409)
                if not 200 <= response.status_code < 300:
                    raise ResourceError("Azure could not complete the resource request.", 502)
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ResourceError("Azure response exceeds the size limit.", 502)
                    content.extend(chunk)
                result = json.loads(content) if content else {}
                if type(result) is not dict or "error" in result:
                    raise ValueError
                return result
        except (httpx.HTTPError, ValueError, TypeError):
            raise ResourceError("Azure returned an unavailable or invalid response.", 502) from None

    def collection(self, path, version):
        items = []
        next_path = path
        for _page in range(20):
            result = self.read(next_path, version)
            batch = result.get("value")
            if not isinstance(batch, list) or any(type(item) is not dict for item in batch):
                raise ResourceError("Azure returned an invalid resource list.", 502)
            items.extend(batch)
            if len(items) > 1000:
                raise ResourceError("Select a smaller resource group to narrow the results.")
            next_link = result.get("nextLink")
            if not next_link:
                return items
            parts = urlsplit(next_link)
            if (parts.scheme != "https" or parts.netloc != "management.azure.com"
                    or parts.path != path or parts.fragment):
                raise ResourceError("Azure returned an invalid continuation link.", 502)
            next_path = parts.path + ("?" + parts.query if parts.query else "")
        raise ResourceError("Azure resource pagination exceeds the limit.", 502)

    def subscriptions(self):
        return [{"id": item["subscriptionId"], "name": item.get("displayName", item["subscriptionId"])}
                for item in self.collection("/subscriptions", "2022-12-01")
                if item.get("tenantId") == self.principal.tenant_id and item.get("state") == "Enabled"]

    def subscription(self, subscription_id):
        try:
            canonical_uuid(subscription_id)
        except ValueError:
            raise ResourceError("Invalid subscription identifier.") from None
        subscription = self.read("/subscriptions/" + subscription_id, "2022-12-01")
        if subscription.get("tenantId") != self.principal.tenant_id or subscription.get("state") != "Enabled":
            raise ResourceError("Select an enabled subscription in the signed-in tenant.", 403)

    def groups(self, subscription_id):
        self.subscription(subscription_id)
        path = f"/subscriptions/{subscription_id}/resourcegroups"
        return [{"name": item["name"], "location": item.get("location", "")}
                for item in self.collection(path, "2021-04-01")]

    def catalog(self, subscription_id, group):
        self.subscription(subscription_id)
        resource_name(group, group=True)
        base = f"/subscriptions/{subscription_id}/resourceGroups/{group}/providers/"
        return {kind: [{"id": item["id"], "name": item["name"], "location": item.get("location", "")}
                       for item in self.collection(base + RESOURCE_TYPES[kind], VERSIONS[kind])]
                for kind in ("job", "workspace", "foundry")}

    def deployments(self, account_id):
        subscription, _, _ = resource_id(account_id, "foundry")
        self.subscription(subscription)
        return [{"name": item["name"], "model": item.get("properties", {}).get("model", {}).get("name", ""),
                 "status": item.get("properties", {}).get("provisioningState", "Unknown")}
                for item in self.collection(account_id + "/deployments", VERSIONS["foundry"])]

    def _resource(self, identifier, kind):
        resource_id(identifier, kind)
        result = self.read(identifier, VERSIONS[kind])
        if not isinstance(result.get("id"), str) or result["id"].casefold() != identifier.casefold():
            raise ResourceError("Azure returned a different resource than requested.", 502)
        return result

    def _model_selection(self, account_id, account, deployment_name):
        resource_name(deployment_name)
        try:
            subdomain = account["properties"]["customSubDomainName"]
            if not isinstance(subdomain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", subdomain):
                raise ResourceError("Foundry must have a valid custom subdomain.")
            suffix = {"AIServices": "services.ai.azure.com", "OpenAI": "openai.azure.com"}.get(account["kind"])
            if suffix is None:
                raise ResourceError("Select an Azure OpenAI or Foundry account.")
            deployment_id = account_id + "/deployments/" + deployment_name
            deployment = self.read(deployment_id, VERSIONS["foundry"])
            if (deployment["id"].casefold() != deployment_id.casefold()
                    or deployment["properties"]["provisioningState"] != "Succeeded"
                    or deployment["properties"]["model"]["format"] != "OpenAI"):
                raise ResourceError("Select a ready OpenAI model deployment.")
            return {"endpoint": f"https://{subdomain}.{suffix}/", "model": deployment_name,
                    "deployment_model": deployment["properties"]["model"]["name"]}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ResourceError("Foundry did not return a valid OpenAI deployment.") from None

    def foundry(self, account_id, deployment_name):
        subscription, _, _ = resource_id(account_id, "foundry")
        resource_name(deployment_name)
        self.subscription(subscription)
        return self._model_selection(account_id, self._resource(account_id, "foundry"), deployment_name)

    def validate(self, payload):
        selection = profile_input(payload)
        identifiers = ((selection["job_id"], "job"), (selection["workspace_resource_id"], "workspace"),
                       (selection["foundry_account_id"], "foundry"))
        for subscription in sorted({resource_id(identifier, kind)[0] for identifier, kind in identifiers}):
            self.subscription(subscription)
        job, workspace, account = [self._resource(identifier, kind) for identifier, kind in identifiers]
        try:
            properties = job["properties"]
            environment_id = properties["environmentId"]
            environment = self._resource(environment_id, "environment")
            workspace_id = canonical_uuid(workspace["properties"]["customerId"])
            log_configuration = environment["properties"]["appLogsConfiguration"]
            if (log_configuration["destination"] != "log-analytics"
                    or log_configuration["logAnalyticsConfiguration"]["customerId"] != workspace_id):
                raise ResourceError("The workspace does not receive logs from this Job environment.")
            containers = properties["template"]["containers"]
            if (properties["configuration"]["triggerType"] != "Manual" or len(containers) != 1
                    or containers[0]["name"] != "worker" or "Assigned" not in job["identity"]["type"]):
                raise ResourceError("Select a dedicated manual research Job with a managed identity.")
            environment_values = {entry["name"]: entry.get("value") for entry in containers[0].get("env", [])}
            if environment_values.get("AZURE_OPENAI_AUTH_MODE") != "managed_identity":
                raise ResourceError("The worker must use managed identity for Foundry.")
            foundry = self._model_selection(selection["foundry_account_id"], account, selection["deployment_name"])
            subscription, group, job_name = resource_id(selection["job_id"], "job")
            return {"config": {"subscription_id": subscription, "resource_group": group,
                               "job_name": job_name, "workspace_id": workspace_id,
                               "model": foundry["model"], "endpoint": foundry["endpoint"]},
                    "environment_id": environment_id, "image": containers[0]["image"],
                    "deployment_model": foundry["deployment_model"]}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ResourceError("The selected resources do not form a compatible research environment.") from None


class Profiles:
    def __init__(self, store):
        self.store = store

    def _record(self, principal):
        return self.store.read(principal.owner_key + "/profiles") or ({"profiles": []}, None)

    def list(self, principal):
        return self._record(principal)[0]["profiles"]

    def get(self, principal, profile_id):
        try:
            canonical_uuid(profile_id)
        except ValueError:
            raise ResourceError("Profile not found.", 404) from None
        for profile in self.list(principal):
            if profile["id"] == profile_id:
                return profile
        raise ResourceError("Profile not found.", 404)

    def save(self, principal, payload, resources, profile_id=None, revision=None):
        selection = profile_input(payload)
        record, version = self._record(principal)
        previous = None
        if profile_id is not None:
            previous = next((item for item in record["profiles"] if item["id"] == profile_id), None)
            if previous is None:
                raise ResourceError("Profile not found.", 404)
            if revision != previous["revision"]:
                raise StoreConflict("Profile changed.")
        elif len(record["profiles"]) >= 20:
            raise ResourceError("The profile limit is 20 per user.")
        if resources.principal.owner_key != principal.owner_key:
            raise ResourceError("The resource identity does not match the profile owner.", 403)
        snapshot = resources.validate(selection)
        result = {**selection, **snapshot, "id": profile_id or str(uuid4()), "revision": str(uuid4())}
        record["profiles"] = [item for item in record["profiles"] if item is not previous] + [result]
        self.store.write(principal.owner_key + "/profiles", record, version)
        return copy.deepcopy(result)

    def delete(self, principal, profile_id, revision):
        record, version = self._record(principal)
        target = next((item for item in record["profiles"] if item["id"] == profile_id), None)
        if target is None:
            raise ResourceError("Profile not found.", 404)
        if revision != target["revision"]:
            raise StoreConflict("Profile changed.")
        record["profiles"].remove(target)
        self.store.write(principal.owner_key + "/profiles", record, version)
