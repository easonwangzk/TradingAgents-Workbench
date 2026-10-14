"""Write-only provider credentials scoped to the user's selected research Job."""

import copy
import hashlib
import re
from contextlib import contextmanager
from uuid import uuid4

from tradingagents.workbench.public_auth import AuthenticationRequired
from tradingagents.workbench.public_profiles import VERSIONS, AzureResources, ResourceError
from tradingagents.workbench.public_store import StoreConflict

PROVIDERS = {"alpha_vantage": ("ALPHA_VANTAGE_API_KEY", "tawb-alpha-vantage"),
             "fred": ("FRED_API_KEY", "tawb-fred")}


class ProviderKeys:
    def __init__(self, *, auth, profiles, runs, resource_factory=AzureResources):
        self.auth = auth
        self.profiles = profiles
        self.runs = runs
        self.resource_factory = resource_factory

    @contextmanager
    def _cloud(self, session):
        cloud = self.resource_factory(session.principal, self.auth.credential(session.session_id))
        try:
            yield cloud
        finally:
            cloud.close()

    def _job(self, cloud, profile):
        job = cloud._resource(profile["job_id"], "job")
        try:
            properties = job["properties"]
            containers = properties["template"]["containers"]
            if (properties["environmentId"].casefold() != profile["environment_id"].casefold()
                    or properties["configuration"]["triggerType"] != "Manual"
                    or len(containers) != 1 or containers[0]["name"] != "worker"
                    or containers[0]["image"] != profile["image"]):
                raise ValueError
            return job
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ResourceError("The research Job changed. Revalidate the profile before configuring providers.", 409) from None

    def _secrets(self, cloud, profile):
        result = cloud.write(profile["job_id"] + "/listSecrets", VERSIONS["job"], {}, method="POST")
        items = result.get("value")
        if (not isinstance(items, list) or any(type(item) is not dict
                or not isinstance(item.get("name"), str)
                or not (isinstance(item.get("value"), str) and item["value"]
                    or isinstance(item.get("keyVaultUrl"), str) and item["keyVaultUrl"])
                for item in items)
                or len({item["name"] for item in items}) != len(items)):
            raise ResourceError("Azure did not return valid Job secret configuration.", 502)
        return copy.deepcopy(items)

    def _bindings(self, job):
        return {entry["name"]: entry for entry in job["properties"]["template"]["containers"][0].get("env", [])}

    def _references(self, value, secret_name):
        if isinstance(value, list):
            return any(self._references(item, secret_name) for item in value)
        if isinstance(value, dict):
            return any((key.lower().endswith("secretref") and item == secret_name)
                       or self._references(item, secret_name) for key, item in value.items())
        return False

    def get(self, session_id, profile_id):
        session = self.auth.session(session_id)
        profile = self.profiles.get(session.principal, profile_id)
        key = self.runs._reservation_key(profile)
        stored = self.auth.store.read(key)
        with self._cloud(session) as cloud:
            job = self._job(cloud, profile)
            bindings = self._bindings(job)
            secret_names = {item["name"] for item in job["properties"]["configuration"].get("secrets", [])}
            configured = {provider: bool(bindings.get(environment, {}).get("value")
                          or bindings.get(environment, {}).get("secretRef") in secret_names)
                          for provider, (environment, _) in PROVIDERS.items()}
            pending = bool(stored and "key_update" in stored[0])
            if pending and stored[0].get("owner") == session.principal.owner_key:
                reservation, version = stored
                settled = reservation.get("request_finished") or reservation["busy_until"] <= self.auth.clock()
                if settled and not reservation["attempted"]:
                    self.auth.store.delete(key, version)
                    pending = False
                elif settled and job["properties"].get("provisioningState") == "Succeeded":
                    values = {item["name"]: item.get("value") for item in self._secrets(cloud, profile)}
                    matches = True
                    for provider, digest in reservation["expected"].items():
                        environment, secret_name = PROVIDERS[provider]
                        if digest is None:
                            matches = matches and environment not in bindings
                        else:
                            value = values.get(secret_name)
                            matches = (matches and bindings.get(environment, {}).get("secretRef") == secret_name
                                       and isinstance(value, str) and hashlib.sha256(value.encode()).hexdigest() == digest)
                    if matches:
                        self.auth.store.delete(key, version)
                        pending = False
        return {"configured": configured, "pending": pending}

    def update(self, session_id, profile_id, payload):
        session = self.auth.session(session_id)
        profile = self.profiles.get(session.principal, profile_id)
        if (type(payload) is not dict or not payload or not set(payload) <= PROVIDERS.keys()
                or any(value is not None and (not isinstance(value, str)
                       or not re.fullmatch(r"[A-Za-z0-9_./+=-]{1,512}", value)) for value in payload.values())):
            raise ResourceError("Submit only nonempty Alpha Vantage or FRED keys; null disables a provider binding.")
        if not self.runs._recover_reservation(profile):
            raise ResourceError("This Job has an active research run or pending key update.", 409)
        key = self.runs._reservation_key(profile)
        reservation = {"owner": session.principal.owner_key, "key_update": str(uuid4()),
                       "attempted": False, "request_finished": False, "busy_until": self.auth.clock() + 180,
                       "expected": {provider: hashlib.sha256(value.encode()).hexdigest() if value is not None else None
                                    for provider, value in payload.items()}}
        try:
            version = self.auth.store.write(key, reservation)
        except StoreConflict:
            raise ResourceError("This Job became busy. Refresh its status before retrying.", 409) from None
        try:
            with self._cloud(session) as cloud:
                job = self._job(cloud, profile)
                if job["properties"].get("provisioningState") != "Succeeded":
                    raise ResourceError("Wait for the Job's Azure configuration to finish before changing providers.", 409)
                configuration = copy.deepcopy(job["properties"]["configuration"])
                template = copy.deepcopy(job["properties"]["template"])
                secrets = self._secrets(cloud, profile)
                original_secrets = copy.deepcopy(secrets)
                environment = template["containers"][0].setdefault("env", [])
                for provider, value in payload.items():
                    environment_name, secret_name = PROVIDERS[provider]
                    other_entries = [entry for entry in environment if entry["name"] != environment_name]
                    environment[:] = other_entries
                    if self._references(template, secret_name) or self._references(configuration, secret_name):
                        raise ResourceError("A provider secret is shared with another setting. Resolve it in Azure before changing it.", 409)
                    secrets = [item for item in secrets if item["name"] != secret_name]
                    if value is not None:
                        secrets.append({"name": secret_name, "value": value})
                        environment.append({"name": environment_name, "secretRef": secret_name})
                configuration["secrets"] = secrets
                current = self._job(cloud, profile)
                if (current["properties"]["configuration"] != job["properties"]["configuration"]
                        or current["properties"]["template"] != job["properties"]["template"]
                        or self._secrets(cloud, profile) != original_secrets):
                    raise ResourceError("The Job was changed outside this request. Refresh before changing providers.", 409)
                if reservation["busy_until"] <= self.auth.clock():
                    raise ResourceError("The provider update expired before writing. Refresh before retrying.", 409)
                reservation["attempted"] = True
                version = self.auth.store.write(key, reservation, version)
                cloud.write(profile["job_id"], VERSIONS["job"],
                            {"properties": {"configuration": configuration, "template": template}}, method="PATCH")
                reservation["request_finished"] = True
                self.auth.store.write(key, reservation, version)
        except (AuthenticationRequired, ResourceError) as error:
            if not reservation["attempted"] or isinstance(error, AuthenticationRequired) or error.status_code in {400, 403, 404}:
                self.auth.store.delete(key, version)
            else:
                reservation["request_finished"] = True
                self.auth.store.write(key, reservation, version)
            raise
        return self.get(session_id, profile_id)
