# Modified for public distribution: installation identifiers replaced with examples.
"""Invitation-only email identities, independent of delegated Azure credentials."""

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from tradingagents.workbench.public_auth import SESSION_SECONDS, public_origin
from tradingagents.workbench.public_store import EncryptedStore, StoreConflict, StoreUnavailable

OWNER_EMAIL = "owner@example.com"
REGISTRY_KEY = "email-v1/registry"
INVITE_SECONDS = 24 * 3600
RECOVERY_SECONDS = 15 * 60
MAX_USERS = 256
MAX_INVITES = 128
MAX_RECOVERIES = 128
MAX_PASSWORD_BYTES = 512
ADMISSION_KEY = "email-v1/admission"
ADMISSION_SECONDS = 60
ACCOUNT_ATTEMPTS = 10
SOURCE_ATTEMPTS = 30
GLOBAL_ATTEMPTS = 120
MAX_ANONYMOUS_IDENTITIES = 128
CAS_ATTEMPTS = 8
FEATURES = frozenset({"research", "assistant", "market"})
MAX_AUDIT = 512


def permissions(user):
    stored = user.get("permissions", dict.fromkeys(FEATURES, True))
    if type(stored) is not dict or set(stored) != FEATURES or any(type(value) is not bool for value in stored.values()):
        raise AccountError(status_code=503)
    return dict(stored)


class AccountError(RuntimeError):
    def __init__(self, *, status_code=401):
        super().__init__({
            400: "Invalid account input.",
            401: "Invalid credentials or expired authorization.",
            403: "Account operation is not permitted.",
            409: "Account state changed or operation is unavailable.",
            429: "Too many account attempts. Try again later.",
            503: "Account storage is unavailable.",
        }.get(status_code, "Account operation failed."))
        self.status_code = status_code


def _operation(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except StoreConflict:
            raise AccountError(status_code=409) from None
        except StoreUnavailable:
            raise AccountError(status_code=503) from None
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise AccountError() from None
    return guarded


@dataclass(frozen=True)
class EmailPrincipal:
    object_id: str
    email: str
    name: str
    is_admin: bool

    @property
    def owner_key(self):
        return f"email-v1/users/{self.object_id}"


@dataclass(frozen=True)
class EmailSession:
    session_id: str = field(repr=False)
    principal: EmailPrincipal
    csrf_token: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class EmailToken:
    token_id: str
    token: str = field(repr=False)
    email: str
    expires_at: int


def _email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise AccountError(status_code=400)
    value = value.strip().lower()
    if not re.fullmatch(
        r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}",
        value,
    ):
        raise AccountError(status_code=400)
    local, domain = value.split("@")
    if local.startswith(".") or local.endswith(".") or ".." in value or any(
        len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in domain.split(".")
    ):
        raise AccountError(status_code=400)
    return value


def _password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise AccountError(status_code=400)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise AccountError(status_code=400) from None
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise AccountError(status_code=400)
    return value


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capability(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise AccountError()
    return _digest(value)


class EmailAuth:
    def __init__(self, store: EncryptedStore, *, origin, clock=time.time):
        self.store = store
        self.origin = public_origin(origin)
        self.clock = clock
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)
        self._dummy_hash = None

    def _registry(self):
        stored = self.store.read(REGISTRY_KEY)
        if stored is None:
            raise AccountError()
        return stored

    def _principal(self, registry, user_id):
        user = registry["users"][user_id]
        return EmailPrincipal(user_id, user["email"], user["name"], user_id == registry["owner_id"])

    def _session_record(self, session_id):
        key = f"email-v1/sessions/{_capability(session_id)}"
        stored = self.store.read(key)
        if stored is None:
            raise AccountError()
        record, _ = stored
        registry, version = self._registry()
        confirmed = self.store.read(key)
        if confirmed is None or confirmed[1] != stored[1]:
            raise AccountError()
        user = registry["users"].get(record["user_id"])
        if (
            record["expires_at"] <= self.clock()
            or user is None
            or user["disabled"]
            or record["generation"] != user["generation"]
            or record["owner_id"] != registry["owner_id"]
            or record["email"] != user["email"]
        ):
            raise AccountError()
        session = EmailSession(
            session_id, self._principal(registry, record["user_id"]),
            record["csrf_token"], record["expires_at"],
        )
        return session, registry, version

    def _admin(self, session_id):
        session, registry, version = self._session_record(session_id)
        if not session.principal.is_admin:
            raise AccountError(status_code=403)
        return registry, version

    def _issue(self, registry, user_id):
        user = registry["users"][user_id]
        current, _ = self._registry()
        if current["owner_id"] != registry["owner_id"] or current["users"].get(user_id) != user:
            raise AccountError()
        if user["disabled"]:
            raise AccountError()
        session_id = secrets.token_urlsafe(32)
        self.store.write(f"email-v1/sessions/{_capability(session_id)}", {
            "user_id": user_id, "email": user["email"], "owner_id": registry["owner_id"],
            "generation": user["generation"], "csrf_token": secrets.token_urlsafe(32),
            "expires_at": int(self.clock()) + SESSION_SECONDS,
        })
        return self.session(session_id)

    def _new_user(self, email, password_hash):
        return {
            "email": email, "name": email, "password_hash": password_hash,
            "generation": 1, "disabled": False, "permissions": dict.fromkeys(FEATURES, True),
        }

    def _account(self, registry, user_id):
        user = registry["users"][user_id]
        return {"object_id": user_id, "email": user["email"], "name": user["name"],
                "is_admin": user_id == registry["owner_id"], "disabled": user["disabled"],
                "permissions": permissions(user)}

    def _audit(self, registry, target_email, action, before=None, after=None, *, actor_email=None):
        events = registry.setdefault("audit", [])
        events.append({"id": str(uuid4()), "occurred_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                       "actor_email": actor_email or registry["users"][registry["owner_id"]]["email"],
                       "target_email": target_email, "action": action, "before": before,
                       "after": after, "result": "success"})
        if len(events) > MAX_AUDIT:
            del events[:-MAX_AUDIT]
            registry["audit_truncated"] = True

    @_operation
    def accounts(self, admin_session_id):
        registry, version = self._admin(admin_session_id)
        return {"accounts": [self._account(registry, user_id) for user_id in sorted(
            registry["users"], key=lambda identity: registry["users"][identity]["email"])], "version": version}

    @_operation
    def audit(self, admin_session_id):
        registry, _ = self._admin(admin_session_id)
        return {"events": list(reversed(registry.get("audit", []))),
                "truncated": bool(registry.get("audit_truncated", False))}

    @_operation
    def feature_permissions(self, principal):
        registry, _ = self._registry()
        user = registry["users"].get(principal.object_id)
        if user is None or user["disabled"] or user["email"] != principal.email:
            raise AccountError(status_code=403)
        return permissions(user)

    def require_feature(self, principal, feature):
        if feature not in FEATURES or not self.feature_permissions(principal)[feature]:
            raise AccountError(status_code=403)

    @_operation
    def update_account(self, admin_session_id, user_id, payload, *, expected_version):
        registry, version = self._admin(admin_session_id)
        if not expected_version or expected_version != version:
            raise AccountError(status_code=409)
        if (type(payload) is not dict or set(payload) != {"disabled", "permissions"}
                or type(payload["disabled"]) is not bool or type(payload["permissions"]) is not dict
                or set(payload["permissions"]) != FEATURES
                or any(type(value) is not bool for value in payload["permissions"].values())):
            raise AccountError(status_code=400)
        user = registry["users"].get(user_id)
        if user is None:
            raise AccountError(status_code=400)
        if user_id == registry["owner_id"] and (payload["disabled"] or not all(payload["permissions"].values())):
            raise AccountError(status_code=403)
        before = self._account(registry, user_id)
        if payload["disabled"] and not user["disabled"]:
            user["generation"] += 1
            self._clear_recoveries(registry, user_id)
        user["disabled"] = payload["disabled"]
        user["permissions"] = dict(payload["permissions"])
        after = self._account(registry, user_id)
        self._audit(registry, user["email"], "account.update", before, after)
        new_version = self.store.write(REGISTRY_KEY, registry, version)
        return {"account": after, "version": new_version}

    def _verify(self, password_hash, password):
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def _admit(self, email, source):
        if (
            not isinstance(source, str) or not source.strip() or len(source) > 256
            or not source.isprintable() or len(source.encode("utf-8")) > 512
        ):
            raise AccountError(status_code=400)
        try:
            email = _email(email)
        except AccountError:
            email = None
        identities = {"accounts": _digest(email or "invalid-email"), "sources": _digest(source)}
        for _ in range(CAS_ATTEMPTS):
            now = int(self.clock())
            stored = self.store.read(ADMISSION_KEY)
            admission, version = stored if stored else ({}, None)
            global_window = admission.get("global", {})
            if global_window.get("until", 0) <= now:
                global_window = {"until": now + ADMISSION_SECONDS, "count": 0}
            if global_window["count"] >= GLOBAL_ATTEMPTS:
                raise AccountError(status_code=429)
            admission["global"] = global_window
            for category, limit in (("accounts", ACCOUNT_ATTEMPTS), ("sources", SOURCE_ATTEMPTS)):
                entries = {
                    key: value for key, value in admission.get(category, {}).items()
                    if value["until"] > now
                }
                identity = identities[category]
                if identity not in entries and len(entries) >= MAX_ANONYMOUS_IDENTITIES:
                    raise AccountError(status_code=429)
                window = entries.setdefault(identity, {"until": now + ADMISSION_SECONDS, "count": 0})
                if window["count"] >= limit:
                    raise AccountError(status_code=429)
                window["count"] += 1
                admission[category] = entries
            global_window["count"] += 1
            try:
                self.store.write(ADMISSION_KEY, admission, version)
            except StoreConflict:
                continue
            if email is None:
                raise AccountError()
            return email
        raise AccountError(status_code=503)

    def _cleanup_tokens(self, registry):
        now = self.clock()
        for collection in ("invites", "recoveries"):
            registry[collection] = {
                key: value for key, value in registry[collection].items()
                if value["expires_at"] > now
            }

    @_operation
    def bootstrap(self, password):
        """Operator-only initialization. This method must never be exposed as an HTTP route."""
        if self.store.read(REGISTRY_KEY) is not None:
            raise AccountError(status_code=409)
        password_hash = self._hasher.hash(_password(password))
        user_id = str(uuid4())
        registry = {
            "owner_id": user_id, "users": {user_id: self._new_user(OWNER_EMAIL, password_hash)},
            "invites": {}, "recoveries": {},
        }
        self.store.write(REGISTRY_KEY, registry)
        return self._issue(registry, user_id)

    @_operation
    def session(self, session_id):
        return self._session_record(session_id)[0]

    @_operation
    def logout(self, session_id):
        """Delete the capability, then fence already-authorized registry writes with CAS."""
        try:
            key = f"email-v1/sessions/{_capability(session_id)}"
        except AccountError:
            return
        stored = self.store.read(key)
        if stored is not None:
            try:
                self.store.delete(key, stored[1])
            except StoreConflict:
                if self.store.read(key) is not None:
                    raise
            for _ in range(CAS_ATTEMPTS):
                registry, version = self._registry()
                try:
                    self.store.write(REGISTRY_KEY, registry, version)
                except StoreConflict:
                    continue
                return
            raise AccountError(status_code=503)

    @_operation
    def check_csrf(self, session, token):
        current = self.session(session.session_id)
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            or not secrets.compare_digest(current.csrf_token, token)
        ):
            raise AccountError(status_code=403)

    @_operation
    def login(self, email, password, *, source):
        """The server caller must supply a trusted peer identifier, not a client header."""
        email = self._admit(email, source)
        try:
            password = _password(password)
        except AccountError:
            raise AccountError() from None
        registry, _ = self._registry()
        user_id = next((key for key, user in registry["users"].items() if user["email"] == email), None)
        user = registry["users"].get(user_id)
        if user is None:
            if self._dummy_hash is None:
                self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))
            password_hash = self._dummy_hash
        else:
            password_hash = user["password_hash"]
        valid = self._verify(password_hash, password)
        if user is None or user["disabled"] or not valid:
            raise AccountError()
        return self._issue(registry, user_id)

    @_operation
    def invite(self, admin_session_id, email):
        email = _email(email)
        registry, version = self._admin(admin_session_id)
        self._cleanup_tokens(registry)
        if any(user["email"] == email for user in registry["users"].values()):
            raise AccountError(status_code=409)
        if len(registry["users"]) >= MAX_USERS or len(registry["invites"]) >= MAX_INVITES:
            raise AccountError(status_code=409)
        token = secrets.token_urlsafe(32)
        token_id = _capability(token)
        expires_at = int(self.clock()) + INVITE_SECONDS
        registry["invites"][token_id] = {"email": email, "expires_at": expires_at}
        self._audit(registry, email, "invitation.create")
        self.store.write(REGISTRY_KEY, registry, version)
        return EmailToken(token_id, token, email, expires_at)

    @_operation
    def revoke_invite(self, admin_session_id, token_id):
        registry, version = self._admin(admin_session_id)
        if not isinstance(token_id, str) or not re.fullmatch(r"[a-f0-9]{64}", token_id):
            raise AccountError(status_code=400)
        if token_id not in registry["invites"]:
            raise AccountError(status_code=409)
        self._audit(registry, registry["invites"][token_id]["email"], "invitation.revoke")
        del registry["invites"][token_id]
        self.store.write(REGISTRY_KEY, registry, version)

    @_operation
    def users(self, admin_session_id):
        registry, _ = self._admin(admin_session_id)
        return [{
            "object_id": user_id, "email": user["email"], "name": user["name"],
            "is_admin": user_id == registry["owner_id"], "disabled": user["disabled"],
        } for user_id, user in sorted(registry["users"].items(), key=lambda item: item[1]["email"])]

    def _clear_recoveries(self, registry, user_id):
        registry["recoveries"] = {
            key: value for key, value in registry["recoveries"].items()
            if value["user_id"] != user_id
        }

    @_operation
    def disable(self, admin_session_id, user_id, *, expected_version=None):
        registry, version = self._admin(admin_session_id)
        if not expected_version or expected_version != version:
            raise AccountError(status_code=409)
        if user_id == registry["owner_id"]:
            raise AccountError(status_code=403)
        user = registry["users"].get(user_id)
        if user is None:
            raise AccountError(status_code=400)
        if not user["disabled"]:
            before = self._account(registry, user_id)
            user["disabled"] = True
            user["generation"] += 1
            self._clear_recoveries(registry, user_id)
            self._audit(registry, user["email"], "account.disable", before, self._account(registry, user_id))
            self.store.write(REGISTRY_KEY, registry, version)

    @_operation
    def recovery(self, admin_session_id, user_id):
        registry, version = self._admin(admin_session_id)
        self._cleanup_tokens(registry)
        user = registry["users"].get(user_id)
        if user is None or user["disabled"]:
            raise AccountError(status_code=400)
        self._clear_recoveries(registry, user_id)
        if len(registry["recoveries"]) >= MAX_RECOVERIES:
            raise AccountError(status_code=409)
        token = secrets.token_urlsafe(32)
        token_id = _capability(token)
        expires_at = int(self.clock()) + RECOVERY_SECONDS
        registry["recoveries"][token_id] = {
            "user_id": user_id, "email": user["email"],
            "generation": user["generation"], "expires_at": expires_at,
        }
        self._audit(registry, user["email"], "recovery.create")
        self.store.write(REGISTRY_KEY, registry, version)
        return EmailToken(token_id, token, user["email"], expires_at)

    def _replace_password(self, registry, user_id, password_hash):
        user = registry["users"][user_id]
        user["password_hash"] = password_hash
        user["generation"] += 1
        self._clear_recoveries(registry, user_id)

    @_operation
    def reset(self, token, email, password):
        email = self._admit(email, "token-reset")
        registry, version = self._registry()
        code = registry["recoveries"].get(_capability(token))
        if code is None or code["email"] != email or code["expires_at"] <= self.clock():
            raise AccountError()
        user_id = code["user_id"]
        user = registry["users"].get(user_id)
        if user is None or user["disabled"] or user["generation"] != code["generation"]:
            raise AccountError()
        password_hash = self._hasher.hash(_password(password))
        if code["expires_at"] <= self.clock():
            raise AccountError()
        self._replace_password(registry, user_id, password_hash)
        self._audit(registry, email, "recovery.redeem", actor_email=email)
        self.store.write(REGISTRY_KEY, registry, version)
        return self._issue(registry, user_id)

    @_operation
    def change_password(self, session_id, current_password, new_password):
        session, snapshot, _ = self._session_record(session_id)
        user_id = session.principal.object_id
        user = snapshot["users"][user_id]
        self._admit(user["email"], "session-password-change")
        _password(new_password)
        if not self._verify(user["password_hash"], _password(current_password)):
            raise AccountError()
        password_hash = self._hasher.hash(new_password)
        _, registry, version = self._session_record(session_id)
        if registry["users"][user_id] != user or registry["owner_id"] != snapshot["owner_id"]:
            raise AccountError()
        self._replace_password(registry, user_id, password_hash)
        self.store.write(REGISTRY_KEY, registry, version)
        return self._issue(registry, user_id)

    @_operation
    def redeem(self, token, email, password):
        email = self._admit(email, "token-redemption")
        token_id = _capability(token)
        registry, version = self._registry()
        invitation = registry["invites"].get(token_id)
        if invitation is None or invitation["email"] != email or invitation["expires_at"] <= self.clock():
            raise AccountError()
        if len(registry["users"]) >= MAX_USERS or any(
            user["email"] == email for user in registry["users"].values()
        ):
            raise AccountError(status_code=409)
        password_hash = self._hasher.hash(_password(password))
        if invitation["expires_at"] <= self.clock():
            raise AccountError()
        user_id = str(uuid4())
        registry["users"][user_id] = self._new_user(email, password_hash)
        del registry["invites"][token_id]
        self._audit(registry, email, "invitation.redeem", actor_email=email)
        self.store.write(REGISTRY_KEY, registry, version)
        return self._issue(registry, user_id)
