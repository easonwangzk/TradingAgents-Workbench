"""Owner-bound, immutable previews for explicitly confirmed Azure initialization."""

import base64
import copy
import hashlib
import re
import secrets
import time
from contextlib import suppress
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from tradingagents.workbench.public_auth import AuthenticationRequired, canonical_uuid
from tradingagents.workbench.public_profiles import (
    AzureResources,
    Profiles,
    ResourceError,
    resource_id,
    resource_name,
)
from tradingagents.workbench.public_source import WorkerSource
from tradingagents.workbench.public_store import StoreConflict

PREVIEW_SECONDS = 3600
BUILD_VERSION = "2019-06-01-preview"
STEP_SECONDS = 900
TYPES = {
    "registry": ("Microsoft.ContainerRegistry/registries", "2023-07-01"),
    "workspace": ("Microsoft.OperationalInsights/workspaces", "2023-09-01"),
    "environment": ("Microsoft.App/managedEnvironments", "2024-03-01"),
    "identity": ("Microsoft.ManagedIdentity/userAssignedIdentities", "2023-01-31"),
    "job": ("Microsoft.App/jobs", "2024-03-01"),
}
ROLES = {"AcrPull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
         "Cognitive Services OpenAI User": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"}


def initialization_input(value):
    if type(value) is not dict or set(value) != {
        "subscription_id", "resource_group", "location", "prefix", "foundry_account_id", "deployment_name",
    }:
        raise ResourceError("Only explicit Azure resource selections are accepted.")
    try:
        canonical_uuid(value["subscription_id"])
    except ValueError:
        raise ResourceError("Invalid subscription identifier.") from None
    resource_name(value["resource_group"], group=True)
    resource_id(value["foundry_account_id"], "foundry")
    resource_name(value["deployment_name"])
    if not isinstance(value["prefix"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,18}[a-z0-9]", value["prefix"]):
        raise ResourceError("Use a lowercase resource prefix of 3 to 20 letters, digits or hyphens.")
    if not isinstance(value["location"], str) or not re.fullmatch(r"[a-z][a-z0-9]{1,39}", value["location"]):
        raise ResourceError("Invalid Azure region.")
    return copy.deepcopy(value)


class Provisioning:
    def __init__(self, *, auth, resource_factory=AzureResources, source=None, clock=time.time):
        self.auth = auth
        self.store = auth.store
        self.resource_factory = resource_factory
        self.source = source
        self.clock = clock

    def _key(self, principal, operation_id):
        try:
            canonical_uuid(operation_id)
        except ValueError:
            raise ResourceError("Initialization not found.", 404) from None
        return principal.owner_key + "/operations/" + operation_id

    def _read(self, principal, operation_id):
        stored = self.store.read(self._key(principal, operation_id))
        if stored is None:
            raise ResourceError("Initialization not found.", 404)
        return stored

    def _index(self, principal):
        return self.store.read(principal.owner_key + "/operation-index") or ({"ids": []}, None)

    def get(self, session_id, operation_id):
        return self._public(self._read(self.auth.session(session_id).principal, operation_id)[0])

    def list(self, session_id):
        principal = self.auth.session(session_id).principal
        return [self._public(self._read(principal, operation_id)[0]) for operation_id in self._index(principal)[0]["ids"]]

    def _public(self, record):
        return copy.deepcopy({key: value for key, value in record.items() if key != "internal"})

    def _optional_resource(self, cloud, identifier, version):
        try:
            return cloud.read(identifier, version)
        except ResourceError as error:
            if error.status_code == 404:
                return None
            raise

    def preview(self, session_id, payload):
        session = self.auth.session(session_id)
        selection = initialization_input(payload)
        if self.source is not None and 4 * ((len(self.source.content) + 2) // 3) + 32 > self.store.max_bytes:
            raise ResourceError("The worker source exceeds secure storage capacity.", 503)
        index, index_version = self._index(session.principal)
        if len(index["ids"]) >= 50:
            raise ResourceError("The initialization history limit is 50 per user.")
        operation_id = str(uuid4())
        prefix = selection["prefix"]
        group_id = f"/subscriptions/{selection['subscription_id']}/resourceGroups/{selection['resource_group']}"
        registry_name = "taw" + hashlib.sha256((session.principal.owner_key + operation_id).encode()).hexdigest()[:20]
        names = {"registry": registry_name, "workspace": prefix + "-logs", "environment": prefix + "-env",
                 "identity": prefix + "-identity", "job": prefix + "-job"}
        resources = [{"kind": kind, "name": names[kind], "id": group_id + "/providers/" + resource_type + "/" + names[kind],
                      "api_version": version} for kind, (resource_type, version) in TYPES.items()]
        cloud = self.resource_factory(session.principal, self.auth.credential(session_id))
        try:
            cloud.subscription(selection["subscription_id"])
            locations = cloud.collection(f"/subscriptions/{selection['subscription_id']}/locations", "2022-12-01")
            if selection["location"] not in {item.get("name") for item in locations}:
                raise ResourceError("The selected region is unavailable in this subscription.")
            foundry = cloud.foundry(selection["foundry_account_id"], selection["deployment_name"])
            group = self._optional_resource(cloud, group_id, "2021-04-01")
            for resource in resources:
                if self._optional_resource(cloud, resource["id"], resource["api_version"]) is not None:
                    raise ResourceError("A requested resource already exists. Choose a different prefix or attach it as a profile.", 409)
        finally:
            cloud.close()
        identifiers = {resource["kind"]: resource["id"] for resource in resources}
        roles = []
        for role, scope in (("AcrPull", identifiers["registry"]),
                            ("Cognitive Services OpenAI User", selection["foundry_account_id"])):
            role_id = str(uuid5(NAMESPACE_URL, identifiers["identity"].casefold() + scope.casefold() + role))
            roles.append({"role": role, "scope": scope, "identity_id": identifiers["identity"],
                          "id": scope + "/providers/Microsoft.Authorization/roleAssignments/" + role_id,
                          "role_definition_id": "/subscriptions/" + scope.split("/")[2]
                                                + "/providers/Microsoft.Authorization/roleDefinitions/" + ROLES[role]})
        plan = {"selection": selection, "foundry": foundry, "resources": resources, "roles": roles,
                "group": {"id": group_id, "create": group is None}, "image_tag": "operation-" + operation_id,
                "charges": ["Azure Container Registry Basic storage and one image build",
                            "Container Apps research execution: 1 CPU, 2 GiB, maximum 3600 seconds, no automatic retry",
                            "Log Analytics ingestion and retention", "Existing Foundry deployment model usage"],
                "tags": {"tawbOperation": operation_id, "tawbOwner": hashlib.sha256(session.principal.owner_key.encode()).hexdigest()}}
        if self.source is not None:
            source_key = "sources/" + self.source.sha256
            if self.store.read(source_key) is None:
                with suppress(StoreConflict):
                    self.store.write(source_key, {"content": base64.b64encode(self.source.content).decode("ascii")})
            plan["source_sha256"] = self.source.sha256
        record = {"id": operation_id, "plan": plan, "status": "preview", "created_at": self.clock(),
                  "expires_at": self.clock() + PREVIEW_SECONDS,
                  "confirmation": "CREATE " + selection["resource_group"] + "/" + prefix,
                  "confirmed_at": None, "completed_steps": [], "error": None,
                  "internal": {"attempts": {}, "busy_until": 0}}
        operation_key = self._key(session.principal, operation_id)
        operation_version = self.store.write(operation_key, record)
        try:
            for _attempt in range(3):
                index, index_version = self._index(session.principal)
                if operation_id in index["ids"]:
                    break
                if len(index["ids"]) >= 50:
                    raise ResourceError("The initialization history limit is 50 per user.")
                index["ids"].insert(0, operation_id)
                try:
                    self.store.write(session.principal.owner_key + "/operation-index", index, index_version)
                    break
                except StoreConflict:
                    continue
            else:
                raise StoreConflict("Initialization history changed. Refresh before creating a preview.")
        except (ResourceError, StoreConflict):
            if operation_id not in self._index(session.principal)[0]["ids"]:
                self.store.delete(operation_key, operation_version)
            raise
        return self._public(record)

    def confirm(self, session_id, operation_id, confirmation):
        principal = self.auth.session(session_id).principal
        record, version = self._read(principal, operation_id)
        if not isinstance(confirmation, str) or not secrets.compare_digest(record["confirmation"], confirmation):
            raise ResourceError("The confirmation must exactly match the reviewed initialization.")
        if record["status"] == "preview":
            if record["expires_at"] <= self.clock():
                raise ResourceError("The preview expired. Review a new preview before creating resources.", 409)
            record.update(status="confirmed", confirmed_at=self.clock())
            self.store.write(self._key(principal, operation_id), record, version)
        return self._public(record)

    def _save(self, principal, record, version):
        if record["internal"].get("claim_expires_at", float("inf")) <= self.clock():
            raise StoreConflict("The initialization step expired. Resume with a new request.")
        return self.store.write(self._key(principal, record["id"]), record, version)

    def _matches(self, actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(self._matches(actual.get(key), item) for key, item in expected.items())
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(
                self._matches(item, desired) for item, desired in zip(actual, expected, strict=True))
        return actual == expected

    def _ready(self, value, step, principal):
        properties = value.get("properties", {})
        status = properties.get("provisioningState")
        if status is None and step == "identity":
            try:
                canonical_uuid(properties["principalId"])
                canonical_uuid(properties["clientId"])
                if canonical_uuid(properties["tenantId"]) == principal.tenant_id:
                    return True
            except (KeyError, ValueError, TypeError):
                pass
        if status is None and step.startswith("role-"):
            return True
        if status is None:
            raise ResourceError("Azure did not confirm resource provisioning readiness.", 502)
        if status in {"Failed", "Canceled", "Cancelled", "Deleting"}:
            raise ResourceError("Azure resource provisioning failed. Review the resource in Azure before resuming.", 409)
        return status == "Succeeded"

    def _ensure(self, cloud, principal, record, version, step, identifier, api_version, body, *, role=False, existing_group=False):
        existing = self._optional_resource(cloud, identifier, api_version)
        if existing is not None:
            if not isinstance(existing.get("id"), str) or existing["id"].casefold() != identifier.casefold():
                raise ResourceError("Azure returned a different resource than requested.", 502)
            if not existing_group:
                if role:
                    properties = existing.get("properties", {})
                    if any(str(properties.get(field, "")).casefold() != str(value).casefold()
                           for field, value in body["properties"].items()):
                        raise ResourceError("An existing role assignment differs from the reviewed assignment.", 409)
                elif any(existing.get("tags", {}).get(key) != value for key, value in record["plan"]["tags"].items()):
                    raise ResourceError("A requested resource belongs to another initialization. It was not modified.", 409)
                else:
                    expected = record["internal"].get("expected", {}).get(step)
                    if not expected or not self._matches(existing, expected):
                        raise ResourceError("The resource configuration changed from this initialization. It was not modified.", 409)
            return self._ready(existing, step, principal), version
        if existing_group:
            raise ResourceError("The selected existing resource group was removed. Review a new preview.", 409)
        attempts = record["internal"]["attempts"]
        if step in attempts:
            raise ResourceError("A previous resource creation is unconfirmed. Review Azure before resuming; it will not be repeated.", 409)
        expected = copy.deepcopy(body)
        if step == "environment":
            expected["properties"]["appLogsConfiguration"]["logAnalyticsConfiguration"].pop("sharedKey")
        record["internal"].setdefault("expected", {})[step] = expected
        attempts[step] = "unconfirmed"
        version = self._save(principal, record, version)
        try:
            cloud.write(identifier, api_version, body, create_only=True)
        except (AuthenticationRequired, ResourceError) as error:
            if isinstance(error, AuthenticationRequired) or error.status_code in {400, 401, 403, 404}:
                attempts.pop(step, None)
                self._save(principal, record, version)
            raise
        attempts[step] = "accepted"
        return False, version

    def _resource_body(self, cloud, record, kind):
        plan = record["plan"]
        resources = {resource["kind"]: resource for resource in plan["resources"]}
        body = {"location": plan["selection"]["location"], "tags": plan["tags"]}
        if kind == "registry":
            return {**body, "sku": {"name": "Basic"}, "properties": {"adminUserEnabled": False, "anonymousPullEnabled": False}}
        if kind == "workspace":
            return {**body, "properties": {"sku": {"name": "PerGB2018"}, "retentionInDays": 30}}
        if kind == "identity":
            return body
        if kind == "environment":
            workspace = cloud.read(resources["workspace"]["id"], resources["workspace"]["api_version"])
            customer_id = canonical_uuid(workspace["properties"]["customerId"])
            keys = cloud.write(resources["workspace"]["id"] + "/sharedKeys", resources["workspace"]["api_version"], {}, method="POST")
            shared_key = keys["primarySharedKey"]
            if not isinstance(shared_key, str) or not shared_key:
                raise ResourceError("Azure did not return the log workspace configuration.", 502)
            return {**body, "properties": {"appLogsConfiguration": {"destination": "log-analytics",
                    "logAnalyticsConfiguration": {"customerId": customer_id, "sharedKey": shared_key}},
                    "workloadProfiles": [{"name": "Consumption", "workloadProfileType": "Consumption"}]}}
        if kind == "job":
            identity = cloud.read(resources["identity"]["id"], resources["identity"]["api_version"])
            client_id = canonical_uuid(identity["properties"]["clientId"])
            identity_id = resources["identity"]["id"]
            image = record["internal"]["image"]
            return {**body, "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}},
                    "properties": {"environmentId": resources["environment"]["id"],
                    "configuration": {"triggerType": "Manual", "replicaTimeout": 3600, "replicaRetryLimit": 0,
                                      "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                                      "registries": [{"server": image.split("/")[0], "identity": identity_id}]},
                    "template": {"containers": [{"name": "worker", "image": image,
                                 "resources": {"cpu": 1, "memory": "2Gi"}, "env": [
                                     {"name": "AZURE_CLIENT_ID", "value": client_id},
                                     {"name": "AZURE_OPENAI_AUTH_MODE", "value": "managed_identity"},
                                     {"name": "AZURE_OPENAI_ENDPOINT", "value": plan["foundry"]["endpoint"]},
                                     {"name": "AZURE_OPENAI_DEPLOYMENT_NAME", "value": plan["foundry"]["model"]},
                                     {"name": "TRADINGAGENTS_LLM_PROVIDER", "value": "azure"}]}]}}}
        return body

    def _build(self, cloud, principal, record, version):
        plan = record["plan"]
        registry = next(resource for resource in plan["resources"] if resource["kind"] == "registry")
        details = cloud.read(registry["id"], registry["api_version"])
        server = details["properties"]["loginServer"]
        if not isinstance(server, str) or not re.fullmatch(r"[a-z0-9-]+\.azurecr\.io", server):
            raise ResourceError("Azure returned an invalid registry server.", 502)
        task_id = registry["id"] + "/taskRuns/build-" + record["id"]
        task = self._optional_resource(cloud, task_id, BUILD_VERSION)
        internal = record["internal"]
        if task is not None:
            if task.get("id", "").casefold() != task_id.casefold():
                raise ResourceError("Azure returned a different build resource.", 502)
            self._ready(task, "build", principal)
            request = task.get("properties", {}).get("runRequest", {})
            expected = internal.get("build_request")
            defaults = {"noCache": False, "isArchiveEnabled": False}
            additional = {key: value for key, value in request.items() if key not in (expected or {})}
            if (not expected or any(request.get(key) != value for key, value in expected.items())
                    or any(key not in defaults or value != defaults[key] for key, value in additional.items())
                    or task.get("properties", {}).get("forceUpdateTag")):
                raise ResourceError("The registry build does not match the reviewed source.", 409)
            properties = task.get("properties", {}).get("runResult", {}).get("properties", {})
            status = properties.get("status")
            if status in {"Failed", "Canceled", "Error", "Timeout"}:
                raise ResourceError("The image build failed. Review its Azure build log; no automatic rebuild will occur.", 409)
            if status != "Succeeded":
                return False, version
            images = [image for image in properties.get("outputImages", [])
                      if image.get("registry") == server and image.get("repository") == "tradingagents-workbench"
                      and image.get("tag") == plan["image_tag"]]
            if len(images) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", images[0].get("digest", "")):
                raise ResourceError("The build did not return a verified worker image digest.", 502)
            internal["image"] = server + "/tradingagents-workbench@" + images[0]["digest"]
            return True, version
        if "build" in internal["attempts"]:
            raise ResourceError("The image build request is unconfirmed. It will not be submitted again.", 409)
        if "source_relative_path" not in internal:
            stored = self.store.read("sources/" + plan["source_sha256"])
            if stored is None:
                raise ResourceError("The reviewed worker source is unavailable.", 503)
            source = WorkerSource(base64.b64decode(stored[0]["content"], validate=True))
            if source.sha256 != plan["source_sha256"]:
                raise ResourceError("The reviewed worker source failed its digest check.", 503)
            upload = cloud.write(registry["id"] + "/listBuildSourceUploadUrl", BUILD_VERSION, {}, method="POST")
            parts = urlsplit(upload["uploadUrl"])
            relative = upload["relativePath"]
            if (parts.scheme != "https" or not parts.hostname
                    or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parts.hostname)
                    or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment
                    or not isinstance(relative, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", relative)
                    or relative.startswith("/") or ".." in relative.split("/")):
                raise ResourceError("Azure returned an invalid source upload location.", 502)
            try:
                with cloud.client.stream("PUT", upload["uploadUrl"], content=source.content,
                                         headers={"x-ms-blob-type": "BlockBlob"},
                                         timeout=60, follow_redirects=False) as response:
                    if response.status_code != 201:
                        raise ResourceError("Source upload was not confirmed.", 502)
            except httpx.HTTPError:
                raise ResourceError("Source upload was not confirmed.", 502) from None
            internal["source_relative_path"] = relative
            version = self._save(principal, record, version)
        build_request = {"type": "DockerBuildRequest", "dockerFilePath": "infra/Dockerfile.workbench",
                         "platform": {"os": "Linux", "architecture": "amd64"},
                         "sourceLocation": internal["source_relative_path"], "timeout": 1800,
                         "imageNames": [server + "/tradingagents-workbench:" + plan["image_tag"]], "isPushEnabled": True}
        internal["build_request"] = build_request
        internal["attempts"]["build"] = "unconfirmed"
        version = self._save(principal, record, version)
        try:
            cloud.write(task_id, BUILD_VERSION, {"location": plan["selection"]["location"],
                                               "properties": {"runRequest": build_request}}, create_only=True)
        except (AuthenticationRequired, ResourceError) as error:
            if isinstance(error, AuthenticationRequired) or error.status_code in {400, 401, 403, 404}:
                internal["attempts"].pop("build", None)
                self._save(principal, record, version)
            raise
        internal["attempts"]["build"] = "accepted"
        return False, version

    def _finish_profile(self, cloud, principal, record):
        resources = {resource["kind"]: resource for resource in record["plan"]["resources"]}
        selection = record["plan"]["selection"]
        profile_input = {"name": selection["prefix"], "job_id": resources["job"]["id"],
                         "workspace_resource_id": resources["workspace"]["id"],
                         "foundry_account_id": selection["foundry_account_id"], "deployment_name": selection["deployment_name"]}
        profile_id = str(uuid5(NAMESPACE_URL, record["id"] + "/profile"))
        profiles = Profiles(self.store)
        index, version = profiles._record(principal)
        if not any(item["id"] == profile_id for item in index["profiles"]):
            if len(index["profiles"]) >= 20:
                raise ResourceError("The profile limit is 20 per user. Remove an unused profile before resuming.", 409)
            snapshot = cloud.validate(profile_input)
            index["profiles"].append({**profile_input, **snapshot, "id": profile_id, "revision": str(uuid4())})
            self.store.write(principal.owner_key + "/profiles", index, version)
        record["profile_id"] = profile_id

    def apply(self, session_id, operation_id):
        session = self.auth.session(session_id)
        principal = session.principal
        record, version = self._read(principal, operation_id)
        if record["confirmed_at"] is None or record["status"] == "preview":
            raise ResourceError("Review and explicitly confirm initialization before creating resources.", 409)
        if record["status"] == "succeeded":
            return self._public(record)
        if "source_sha256" not in record["plan"]:
            raise ResourceError("This preview has no reviewed worker source. Generate a new preview.", 409)
        if record["internal"]["busy_until"] > self.clock():
            raise ResourceError("An initialization step is already in progress.", 409)
        claim = str(uuid4())
        record["internal"].update(claim=claim, busy_until=self.clock() + STEP_SECONDS,
                      claim_expires_at=self.clock() + STEP_SECONDS)
        record.update(status="running", error=None)
        version = self._save(principal, record, version)
        cloud = self.resource_factory(principal, self.auth.credential(session_id))
        try:
            plan = record["plan"]
            resources = {resource["kind"]: resource for resource in plan["resources"]}
            steps = ["group", "registry", "workspace", "identity", "environment", "role-0", "role-1", "build", "job", "profile"]
            step = next((step for step in steps if step not in record["completed_steps"]), None)
            record["current_step"] = step
            ready = False
            if step == "group":
                ready, version = self._ensure(cloud, principal, record, version, step, plan["group"]["id"], "2021-04-01",
                    {"location": plan["selection"]["location"], "tags": plan["tags"]}, existing_group=not plan["group"]["create"])
            elif step in resources:
                resource = resources[step]
                existing = self._optional_resource(cloud, resource["id"], resource["api_version"])
                body = {"tags": plan["tags"]} if existing is not None else self._resource_body(cloud, record, step)
                ready, version = self._ensure(cloud, principal, record, version, step, resource["id"], resource["api_version"], body)
            elif step in {"role-0", "role-1"}:
                role = plan["roles"][int(step[-1])]
                identity = cloud.read(resources["identity"]["id"], resources["identity"]["api_version"])
                principal_id = canonical_uuid(identity["properties"]["principalId"])
                body = {"properties": {"principalId": principal_id, "principalType": "ServicePrincipal",
                                       "roleDefinitionId": role["role_definition_id"]}}
                ready, version = self._ensure(cloud, principal, record, version, step, role["id"], "2022-04-01", body, role=True)
            elif step == "build":
                ready, version = self._build(cloud, principal, record, version)
            elif step == "profile":
                self._finish_profile(cloud, principal, record)
                ready = True
            if ready and step is not None:
                record["completed_steps"].append(step)
            record["status"] = "succeeded" if len(record["completed_steps"]) == len(steps) else "running"
            record["internal"]["busy_until"] = 0
            self._save(principal, record, version)
            return self._public(record)
        except (AuthenticationRequired, ResourceError, httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            latest, latest_version = self._read(principal, operation_id)
            if latest["internal"].get("claim") == claim and latest["internal"]["busy_until"] > self.clock():
                latest.update(status="blocked", current_step=record.get("current_step"),
                              error="Initialization paused. Check Azure permissions, resource state and the current step before resuming.")
                latest["internal"]["busy_until"] = 0
                self._save(principal, latest, latest_version)
            if isinstance(error, (AuthenticationRequired, ResourceError)):
                raise
            raise ResourceError("Azure initialization could not confirm the current step.", 502) from None
        finally:
            cloud.close()
