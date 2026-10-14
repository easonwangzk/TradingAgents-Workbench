"""Public email self-registration with emailed one-time codes."""

import hashlib
import re
import secrets
from uuid import uuid4

from tradingagents.workbench.public_accounts import (
    CAS_ATTEMPTS,
    MAX_USERS,
    OWNER_EMAIL,
    REGISTRY_KEY,
    AccountError,
    StoreConflict,
    StoreUnavailable,
    _email,
    _operation,
    _password,
)

CODE_SECONDS = 10 * 60
RETRY_SECONDS = 60
MAX_ATTEMPTS = 5
MAX_CHALLENGES = 128
MAX_LIMIT_IDENTITIES = 128
GLOBAL_MINUTE_SENDS = 5
GLOBAL_HOUR_SENDS = 10
EMAIL_HOUR_SENDS = 3
SOURCE_HOUR_SENDS = 5
PUBLIC_RESPONSE = {"accepted": True, "expires_in": CODE_SECONDS, "retry_after": RETRY_SECONDS}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(value):
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 256
        or not value.isprintable() or len(value.encode("utf-8")) > 512
    ):
        raise AccountError(status_code=400)
    return value


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise AccountError()
    return value


def _defaults(registry):
    registrations = registry.setdefault("registrations", {})
    limits = registry.setdefault("registration_limits", {})
    limits.setdefault("global", {})
    limits.setdefault("emails", {})
    limits.setdefault("sources", {})
    return registrations, limits


def _clean(registry, now):
    registrations, limits = _defaults(registry)
    registry["registrations"] = {
        key: value for key, value in registrations.items()
        if value.get("expires_at", 0) > now and value.get("attempts", MAX_ATTEMPTS) < MAX_ATTEMPTS
    }
    for category in ("emails", "sources"):
        limits[category] = {
            key: value for key, value in limits.get(category, {}).items()
            if value.get("until", 0) > now
        }


def _window(entry, name, now, seconds):
    window = entry.get(name, {})
    if window.get("until", 0) <= now:
        window = {"until": now + seconds, "count": 0}
    entry[name] = window
    return window


def _identity_window(collection, identity, now):
    live = {
        key: value for key, value in collection.items()
        if value.get("until", 0) > now
    }
    if identity not in live and len(live) >= MAX_LIMIT_IDENTITIES:
        raise AccountError(status_code=429)
    window = live.setdefault(identity, {"until": now + 3600, "count": 0})
    return live, window


class EmailRegistration:
    def __init__(self, auth, sender=None):
        self.auth = auth
        self.sender = sender

    @property
    def available(self):
        return callable(self.sender)

    def _registry(self):
        return self.auth._registry()

    def _admit_send(self, registry, email, source, now):
        _clean(registry, now)
        registrations, limits = _defaults(registry)
        email_id = _digest(email)
        source_id = _digest(source)
        global_limits = limits["global"]
        minute = _window(global_limits, "minute", now, 60)
        hour = _window(global_limits, "hour", now, 3600)
        if minute["count"] >= GLOBAL_MINUTE_SENDS or hour["count"] >= GLOBAL_HOUR_SENDS:
            raise AccountError(status_code=429)
        limits["emails"], email_window = _identity_window(limits["emails"], email_id, now)
        limits["sources"], source_window = _identity_window(limits["sources"], source_id, now)
        if email_window.get("retry_after", 0) > now:
            raise AccountError(status_code=429)
        if email_window["count"] >= EMAIL_HOUR_SENDS or source_window["count"] >= SOURCE_HOUR_SENDS:
            raise AccountError(status_code=429)
        challenge = registrations.get(email_id)
        if challenge is not None and challenge.get("retry_after", 0) > now:
            raise AccountError(status_code=429)
        minute["count"] += 1
        hour["count"] += 1
        email_window["count"] += 1
        email_window["retry_after"] = now + RETRY_SECONDS
        source_window["count"] += 1
        return email_id

    def _public_existing(self, registry, email):
        return email == OWNER_EMAIL or any(user["email"] == email for user in registry["users"].values())

    @_operation
    def request_code(self, email, *, source):
        if not self.available:
            raise AccountError(status_code=503)
        email = _email(email)
        source = _source(source)
        code = f"{secrets.randbelow(1000000):06d}"
        salt = secrets.token_hex(32)
        for _ in range(CAS_ATTEMPTS):
            now = int(self.auth.clock())
            registry, version = self._registry()
            email_id = self._admit_send(registry, email, source, now)
            existing = self._public_existing(registry, email)
            registrations = registry["registrations"]
            if not existing and email_id not in registrations and len(registrations) >= MAX_CHALLENGES:
                raise AccountError(status_code=409)
            if not existing:
                registrations[email_id] = {
                    "email": email, "salt": salt, "code_hash": _digest(salt + code),
                    "expires_at": now + CODE_SECONDS, "attempts": 0, "created_at": now,
                    "retry_after": now + RETRY_SECONDS, "active": False,
                }
            try:
                self.auth.store.write(REGISTRY_KEY, registry, version)
                break
            except StoreConflict:
                continue
            except StoreUnavailable:
                raise AccountError(status_code=503) from None
        else:
            raise AccountError(status_code=503)
        if existing:
            return dict(PUBLIC_RESPONSE)
        try:
            self.sender(email, code)
        except Exception:
            raise AccountError(status_code=503) from None
        for _ in range(CAS_ATTEMPTS):
            registry, version = self._registry()
            challenge = registry.get("registrations", {}).get(email_id)
            if (challenge is None or challenge["salt"] != salt
                    or challenge["expires_at"] <= self.auth.clock() or self._public_existing(registry, email)):
                raise AccountError(status_code=409)
            challenge["active"] = True
            try:
                self.auth.store.write(REGISTRY_KEY, registry, version)
            except StoreConflict:
                continue
            return dict(PUBLIC_RESPONSE)
        raise AccountError(status_code=503)

    def _bad_code(self, registry, version, email_id, challenge):
        challenge["attempts"] = min(MAX_ATTEMPTS, challenge.get("attempts", 0) + 1)
        try:
            self.auth.store.write(REGISTRY_KEY, registry, version)
        except StoreConflict:
            raise AccountError(status_code=409) from None
        except StoreUnavailable:
            raise AccountError(status_code=503) from None
        raise AccountError()

    @_operation
    def register(self, email, code, password, *, source):
        if not self.available:
            raise AccountError(status_code=503)
        email = self.auth._admit(email, _source(source))
        code = _code(code)
        password = _password(password)
        email_id = _digest(email)
        for _ in range(CAS_ATTEMPTS):
            registry, version = self._registry()
            now = int(self.auth.clock())
            _clean(registry, now)
            registrations, _ = _defaults(registry)
            challenge = registrations.get(email_id)
            if challenge is None or challenge.get("email") != email or not challenge.get("active", False):
                raise AccountError()
            if challenge.get("expires_at", 0) <= now or challenge.get("attempts", MAX_ATTEMPTS) >= MAX_ATTEMPTS:
                raise AccountError()
            expected = _digest(challenge["salt"] + code)
            if not secrets.compare_digest(expected, challenge["code_hash"]):
                self._bad_code(registry, version, email_id, challenge)
            if len(registry["users"]) >= MAX_USERS or self._public_existing(registry, email):
                raise AccountError(status_code=409)
            password_hash = self.auth._hasher.hash(password)
            if challenge.get("expires_at", 0) <= self.auth.clock():
                raise AccountError()
            user_id = str(uuid4())
            registry["users"][user_id] = self.auth._new_user(email, password_hash)
            del registrations[email_id]
            try:
                self.auth.store.write(REGISTRY_KEY, registry, version)
            except StoreConflict:
                continue
            except StoreUnavailable:
                raise AccountError(status_code=503) from None
            return self.auth._issue(registry, user_id)
        raise AccountError(status_code=503)
