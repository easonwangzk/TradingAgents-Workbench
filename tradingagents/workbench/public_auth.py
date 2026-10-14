"""Single-tenant server-side Entra sessions and delegated Azure credentials."""

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import msal
from azure.core.credentials import AccessToken

from tradingagents.workbench.public_store import EncryptedStore, StoreConflict

ARM_SCOPE = "https://management.azure.com/.default"
LOG_SCOPE = "https://api.loganalytics.io/.default"
FOUNDRY_SCOPE = "https://cognitiveservices.azure.com/.default"
ALLOWED_SCOPES = frozenset({ARM_SCOPE, LOG_SCOPE, FOUNDRY_SCOPE})
FLOW_SECONDS = 600
SESSION_SECONDS = 8 * 3600


class LoginThrottled(RuntimeError):
    """The anonymous login admission window is full."""


class AuthenticationRequired(RuntimeError):
    def __init__(self, *, scope=None, status_code=401):
        super().__init__("Sign in again or complete the required resource consent.")
        self.scope = scope
        self.status_code = status_code


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid identity identifier")
    return value


def public_origin(value):
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", parts.hostname)
    ):
        raise ValueError("A fixed public HTTPS origin is required")
    return f"https://{parts.hostname}"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    object_id: str
    name: str

    @property
    def owner_key(self):
        return f"users/{self.tenant_id}/{self.object_id}"


@dataclass(frozen=True)
class Session:
    session_id: str = field(repr=False)
    principal: Principal
    csrf_token: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class LoginFlow:
    flow_id: str = field(repr=False)
    authorize_url: str


class MsalClientFactory:
    def __init__(self, *, tenant_id, client_id, assertion_credential):
        self.tenant_id = canonical_uuid(tenant_id)
        self.client_id = canonical_uuid(client_id)
        self.assertion_credential = assertion_credential

    def _assertion(self):
        return self.assertion_credential.get_token("api://AzureADTokenExchange/.default").token

    def __call__(self, cache):
        return msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential={"client_assertion": self._assertion},
            token_cache=cache,
            instance_discovery=False,
            timeout=30,
        )


class AuthManager:
    def __init__(self, *, tenant_id, origin, store: EncryptedStore, client_factory, clock=time.time):
        self.tenant_id = canonical_uuid(tenant_id)
        self.origin = public_origin(origin)
        self.store = store
        self.client_factory = client_factory
        self.clock = clock
        self.login_attempts = deque()
        self.login_lock = threading.Lock()

    def _client(self, serialized=""):
        cache = msal.SerializableTokenCache()
        if serialized:
            cache.deserialize(serialized)
        return self.client_factory(cache), cache

    def _principal(self, claims):
        try:
            tenant = canonical_uuid(claims["tid"])
            object_id = canonical_uuid(claims["oid"])
            if tenant != self.tenant_id:
                raise ValueError
            name = claims.get("name")
            return Principal(tenant, object_id, name[:200] if isinstance(name, str) else "")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AuthenticationRequired() from None

    def _key(self, kind, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", identifier):
            raise AuthenticationRequired()
        digest = hashlib.sha256(identifier.encode("ascii")).hexdigest()
        return f"{kind}/{digest}"

    def begin_login(self, *, scope=ARM_SCOPE, session_id=None):
        if scope not in ALLOWED_SCOPES:
            raise ValueError("Unsupported delegated scope")
        with self.login_lock:
            now = self.clock()
            while self.login_attempts and self.login_attempts[0] <= now - 60:
                self.login_attempts.popleft()
            if len(self.login_attempts) >= 60:
                raise LoginThrottled
            self.login_attempts.append(now)
        previous_key = None
        serialized = ""
        if session_id is not None:
            _, record, _ = self._session_record(session_id)
            previous_key = self._key("sessions", session_id)
            serialized = record["cache"]
        client, cache = self._client(serialized)
        flow = client.initiate_auth_code_flow(
            scopes=[scope], redirect_uri=self.origin + "/auth/callback"
        )
        authorize = urlsplit(flow.get("auth_uri", ""))
        if authorize.scheme != "https" or authorize.hostname != "login.microsoftonline.com":
            raise AuthenticationRequired()
        flow_id = secrets.token_urlsafe(32)
        self.store.write(
            self._key("flows", flow_id),
            {"flow": flow, "cache": cache.serialize(), "expires_at": int(self.clock()) + FLOW_SECONDS,
             "previous_key": previous_key},
        )
        return LoginFlow(flow_id, flow["auth_uri"])

    def complete_login(self, flow_id, response, *, session_id=None):
        try:
            key = self._key("flows", flow_id)
            stored = self.store.read(key)
            if stored is None:
                raise AuthenticationRequired()
            pending, version = stored
            if pending["expires_at"] <= self.clock():
                raise AuthenticationRequired()
            expected_state = pending["flow"]["state"]
            supplied_state = response.get("state", "")
            if not isinstance(supplied_state, str) or not secrets.compare_digest(
                expected_state, supplied_state
            ):
                raise AuthenticationRequired()
            previous = None
            serialized = pending["cache"]
            if pending.get("previous_key") is not None:
                if pending["previous_key"] != self._key("sessions", session_id):
                    raise AuthenticationRequired()
                previous, previous_record, previous_version = self._session_record(session_id)
                serialized = previous_record["cache"]
            self.store.delete(key, version)
            client, cache = self._client(serialized)
            result = client.acquire_token_by_auth_code_flow(pending["flow"], response)
            principal = self._principal(result.get("id_token_claims", {}))
            if previous is not None:
                if principal.owner_key != previous.principal.owner_key:
                    raise AuthenticationRequired()
                self.store.delete(pending["previous_key"], previous_version)
            session_id = secrets.token_urlsafe(32)
            record = {
                "principal": {"tid": principal.tenant_id, "oid": principal.object_id, "name": principal.name},
                "csrf_token": secrets.token_urlsafe(32),
                "expires_at": int(self.clock()) + SESSION_SECONDS,
                "cache": cache.serialize(),
            }
            self.store.write(self._key("sessions", session_id), record)
            return self._session_value(session_id, record)
        except (StoreConflict, ValueError, KeyError, TypeError):
            raise AuthenticationRequired() from None

    def _session_value(self, session_id, record):
        if record["expires_at"] <= self.clock():
            raise AuthenticationRequired()
        principal = self._principal(record["principal"])
        return Session(session_id, principal, record["csrf_token"], record["expires_at"])

    def _session_record(self, session_id):
        stored = self.store.read(self._key("sessions", session_id))
        if stored is None:
            raise AuthenticationRequired()
        record, version = stored
        try:
            return self._session_value(session_id, record), record, version
        except (ValueError, KeyError, TypeError):
            raise AuthenticationRequired() from None

    def session(self, session_id):
        return self._session_record(session_id)[0]

    def check_csrf(self, session, supplied):
        if not isinstance(supplied, str) or not secrets.compare_digest(session.csrf_token, supplied):
            raise AuthenticationRequired(status_code=403)

    def logout(self, session_id):
        with suppress(AuthenticationRequired):
            self.store.take(self._key("sessions", session_id))

    def credential(self, session_id):
        self.session(session_id)
        return DelegatedCredential(self, session_id)

    def get_token(self, session_id, scope):
        if scope not in ALLOWED_SCOPES:
            raise ValueError("Unsupported delegated scope")
        session, record, version = self._session_record(session_id)
        client, cache = self._client(record["cache"])
        accounts = [
            account for account in client.get_accounts()
            if account.get("local_account_id") == session.principal.object_id
            and account.get("realm") == session.principal.tenant_id
        ]
        if len(accounts) != 1:
            raise AuthenticationRequired(scope=scope)
        result = client.acquire_token_silent_with_error(scopes=[scope], account=accounts[0])
        if not result or not isinstance(result.get("access_token"), str):
            raise AuthenticationRequired(scope=scope)
        try:
            expires_at = int(result.get("expires_on") or self.clock() + int(result["expires_in"]))
            if expires_at <= self.clock():
                raise ValueError
            record["cache"] = cache.serialize()
            self.store.write(self._key("sessions", session_id), record, version)
        except (ValueError, KeyError, TypeError, StoreConflict):
            raise AuthenticationRequired(scope=scope) from None
        return AccessToken(result["access_token"], expires_at)


class DelegatedCredential:
    def __init__(self, manager, session_id):
        self.manager = manager
        self.session_id = session_id

    def get_token(self, *scopes, claims=None, tenant_id=None, enable_cae=False, **kwargs):
        if len(scopes) != 1 or scopes[0] not in ALLOWED_SCOPES or kwargs:
            raise ValueError("Exactly one delegated resource scope is required")
        if tenant_id not in (None, self.manager.tenant_id):
            raise AuthenticationRequired(scope=scopes[0], status_code=403)
        if claims or enable_cae:
            raise AuthenticationRequired(scope=scopes[0])
        return self.manager.get_token(self.session_id, scopes[0])
