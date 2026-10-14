# Modified for public distribution: installation identifiers replaced with examples.
import hashlib
import sys
import threading
from contextlib import contextmanager

import pytest

from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_accounts import REGISTRY_KEY, AccountError, EmailAuth
from tradingagents.workbench.public_store import EncryptedStore

ORIGIN = "https://workbench.example.com"
OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "fixture-owner-password-2026"
GUEST_PASSWORD = "fixture-guest-password-2026"
NEW_PASSWORD = "fixture-new-password-2026"


class LockedBackend(MemoryBackend):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()

    def read(self, key):
        with self.lock:
            return super().read(key)

    def write(self, key, content, expected_version=None):
        with self.lock:
            return super().write(key, content, expected_version)

    def delete(self, key, expected_version):
        with self.lock:
            return super().delete(key, expected_version)


class PausingBackend(LockedBackend):
    def __init__(self):
        super().__init__()
        self.target = None
        self.reached = threading.Event()
        self.release = threading.Event()

    def arm(self, phase, prefix):
        self.target = (phase, prefix)
        self.reached.clear()
        self.release.clear()

    def checkpoint(self, phase, key):
        if (
            threading.current_thread().name == "registration-racer" and self.target
            and self.target[0] == phase and key.startswith(self.target[1])
        ):
            self.reached.set()
            if not self.release.wait(15):
                raise RuntimeError("Test race was not released")

    def read(self, key):
        result = super().read(key)
        self.checkpoint("read", key)
        return result

    def write(self, key, content, expected_version=None):
        self.checkpoint("write", key)
        return super().write(key, content, expected_version)


@contextmanager
def paused_call(backend, operation, phase, prefix):
    outcomes = []
    backend.arm(phase, prefix)

    def run():
        try:
            outcomes.append(operation())
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=run, name="registration-racer", daemon=True)
    thread.start()
    try:
        assert backend.reached.wait(15), "Operation did not reach the selected storage boundary"
        yield outcomes
    finally:
        backend.release.set()
        thread.join(15)
        assert not thread.is_alive()


class Sender:
    def __init__(self):
        self.deliveries = []
        self.failures = 0

    def __call__(self, email, code):
        self.deliveries.append((email, code))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("provider-sensitive-failure")

    @property
    def last_code(self):
        return self.deliveries[-1][1]


@contextmanager
def hashing_calls(after_hash=None):
    calls = []
    previous = sys.getprofile()

    def observe(frame, event, arg):
        if (
            event == "call" and frame.f_code.co_name in {"hash", "verify"}
            and frame.f_globals.get("__name__") == "argon2._password_hasher"
        ):
            calls.append(frame.f_code.co_name)
        if (
            after_hash is not None and event == "return" and frame.f_code.co_name == "hash"
            and frame.f_globals.get("__name__") == "argon2._password_hasher"
        ):
            after_hash()

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


@pytest.fixture
def env():
    backend = PausingBackend()
    store = EncryptedStore(backend, b"a" * 32)
    clock = [1800000000]
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
    owner = auth.bootstrap(OWNER_PASSWORD)
    sender = Sender()
    return auth, store, backend, clock, owner, sender


def registration(env, sender=None):
    from tradingagents.workbench.public_registration import EmailRegistration

    auth, _, _, _, _, default_sender = env
    return EmailRegistration(auth, sender=default_sender if sender is None else sender)


def registry(store):
    return store.read(REGISTRY_KEY)[0]


def challenge_for(store, email):
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return registry(store)["registrations"][digest]


def test_request_code_requires_configured_sender_and_returns_only_public_metadata(env):
    auth, store, _, _, _, sender = env
    from tradingagents.workbench.public_registration import EmailRegistration

    assert EmailRegistration(auth).available is False
    service = EmailRegistration(auth, sender=sender)
    assert service.available is True
    response = service.request_code(" Guest@Example.com ", source="trusted-peer")
    assert response == {"accepted": True, "expires_in": 600, "retry_after": 60}
    assert len(sender.deliveries) == 1
    email, code = sender.deliveries[0]
    assert email == "guest@example.com"
    assert code.isdecimal() and len(code) == 6
    stored = challenge_for(store, email)
    assert stored["email"] == email
    assert stored["expires_at"] == 1800000600
    assert stored["attempts"] == 0
    assert len(stored["salt"]) >= 64
    assert stored["code_hash"] == hashlib.sha256((stored["salt"] + code).encode("ascii")).hexdigest()
    plaintext = repr(store.read(REGISTRY_KEY)[0])
    ciphertext = repr(store.backend.records)
    assert code not in plaintext
    assert code not in ciphertext
    assert "registrations" in registry(store)
    assert "registration_limits" in registry(store)


@pytest.mark.parametrize("email", [OWNER_EMAIL, "guest@example.com", "disabled@example.com"])
def test_existing_disabled_and_owner_addresses_get_same_public_response_without_user_changes(env, email):
    auth, store, _, _, owner, sender = env
    invitation = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD)
    disabled_invitation = auth.invite(owner.session_id, "disabled@example.com")
    disabled = auth.redeem(disabled_invitation.token, disabled_invitation.email, GUEST_PASSWORD)
    auth.disable(owner.session_id, disabled.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    before_users = registry(store)["users"].copy()
    response = registration(env).request_code(email.upper(), source="trusted-peer")
    assert response == {"accepted": True, "expires_in": 600, "retry_after": 60}
    assert sender.deliveries == []
    assert registry(store)["users"] == before_users
    assert auth.login(guest.principal.email, GUEST_PASSWORD, source="login-peer").principal == guest.principal


def test_successful_registration_consumes_code_and_creates_only_ordinary_user(env):
    auth, store, _, _, _, sender = env
    service = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    session = service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    assert session.principal.email == "guest@example.com"
    assert session.principal.name == "guest@example.com"
    assert session.principal.is_admin is False
    assert auth.session(session.session_id) == session
    assert auth.login("guest@example.com", NEW_PASSWORD, source="login-peer").principal == session.principal
    assert registry(store)["registrations"] == {}
    with pytest.raises(AccountError):
        service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    assert len(registry(store)["users"]) == 2


def test_resend_requires_cooldown_and_invalidates_previous_code(env):
    _, store, _, clock, _, sender = env
    service = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    first = sender.last_code
    with pytest.raises(AccountError) as too_soon:
        service.request_code("guest@example.com", source="trusted-peer")
    assert too_soon.value.status_code == 429
    clock[0] += 60
    service.request_code("guest@example.com", source="trusted-peer")
    second = sender.last_code
    assert second != first
    with pytest.raises(AccountError):
        service.register("guest@example.com", first, NEW_PASSWORD, source="trusted-peer")
    assert service.register("guest@example.com", second, NEW_PASSWORD, source="trusted-peer").principal.email == "guest@example.com"
    assert registry(store)["registrations"] == {}


def test_wrong_expired_and_exhausted_codes_are_persistently_rejected(env):
    _, store, _, clock, _, sender = env
    service = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    for attempt in range(5):
        with pytest.raises(AccountError):
            service.register("guest@example.com", f"00000{attempt}", NEW_PASSWORD, source="trusted-peer")
    stored = challenge_for(store, "guest@example.com")
    assert stored["attempts"] == 5
    with pytest.raises(AccountError):
        service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    assert registry(store)["users"].keys() == {registry(store)["owner_id"]}
    clock[0] += 60
    service.request_code("late@example.com", source="trusted-peer")
    clock[0] += 600
    with pytest.raises(AccountError):
        service.register("late@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    assert "late@example.com" not in repr(registry(store)["users"])


def test_sender_failure_counts_against_limits_but_does_not_activate_challenge(env):
    _, store, _, clock, _, sender = env
    service = registration(env)
    sender.failures = 1
    with pytest.raises(AccountError) as failure:
        service.request_code("guest@example.com", source="trusted-peer")
    assert failure.value.status_code == 503
    assert "provider-sensitive" not in str(failure.value)
    assert not challenge_for(store, "guest@example.com")["active"]
    with pytest.raises(AccountError):
        service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    limits = registry(store)["registration_limits"]
    assert limits["global"]["minute"]["count"] == 1
    assert limits["global"]["hour"]["count"] == 1
    with pytest.raises(AccountError) as held:
        service.request_code("guest@example.com", source="trusted-peer")
    assert held.value.status_code == 429
    clock[0] += 60
    service.request_code("guest@example.com", source="trusted-peer")
    assert sender.deliveries[-1][0] == "guest@example.com"


def test_other_account_write_during_delivery_does_not_invalidate_delivered_code(env):
    auth, store, _, _, owner, sender = env

    def concurrent_sender(email, code):
        sender(email, code)
        auth.invite(owner.session_id, "another@example.com")
        with pytest.raises(AccountError):
            registration(env).register(email, code, NEW_PASSWORD, source="trusted-peer")

    service = registration(env, sender=concurrent_sender)
    assert service.request_code("guest@example.com", source="trusted-peer")["accepted"]
    assert service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer").principal.email == "guest@example.com"
    assert len(registry(store)["invites"]) == 1


@pytest.mark.parametrize("scope,limit", [("global-minute", 5), ("global-hour", 10), ("email-hour", 3), ("source-hour", 5)])
def test_send_limits_are_persistent_bounded_and_apply_to_unknown_addresses(env, scope, limit):
    _, store, _, clock, _, _ = env
    service = registration(env)
    for attempt in range(limit):
        email = "same@example.com" if scope == "email-hour" else f"unknown-{attempt}@example.com"
        source = "same-peer" if scope == "source-hour" else f"peer-{attempt}"
        service.request_code(email, source=source)
        clock[0] += 60 if scope != "global-minute" else 0
    with pytest.raises(AccountError) as failure:
        email = "same@example.com" if scope == "email-hour" else "next@example.com"
        service.request_code(email, source="same-peer" if scope == "source-hour" else "next-peer")
    assert failure.value.status_code == 429
    limits = registry(store)["registration_limits"]
    assert len(limits["emails"]) <= 128
    assert len(limits["sources"]) <= 128
    clock[0] += 3600
    service.request_code("fresh@example.com", source="fresh-peer")


@pytest.mark.parametrize("source", [None, "", " " * 8, "a" * 257, "\ud800", 42])
def test_invalid_trusted_source_fails_without_sender_or_registry_change(env, source):
    _, store, _, _, _, sender = env
    before = store.read(REGISTRY_KEY)
    with pytest.raises(AccountError) as failure:
        registration(env).request_code("guest@example.com", source=source)
    assert failure.value.status_code == 400
    assert sender.deliveries == []
    assert store.read(REGISTRY_KEY) == before


def test_registration_rechecks_expiry_after_password_hashing(env):
    _, store, _, clock, _, sender = env
    service = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    before = store.read(REGISTRY_KEY)
    with hashing_calls(lambda: clock.__setitem__(0, 1800000600)) as calls, pytest.raises(AccountError):
        service.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer")
    assert "hash" in calls
    assert store.read(REGISTRY_KEY) == before


def test_concurrent_verification_consumes_code_once_without_duplicate_users(env):
    auth, store, backend, _, _, sender = env
    service = registration(env)
    peer = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    code = sender.last_code
    with paused_call(
        backend,
        lambda: service.register("guest@example.com", code, NEW_PASSWORD, source="trusted-peer"),
        "write",
        REGISTRY_KEY,
    ) as result:
        winner = peer.register("guest@example.com", code, GUEST_PASSWORD, source="trusted-peer")
    assert isinstance(result[0], AccountError)
    assert auth.login("guest@example.com", GUEST_PASSWORD, source="login-peer").principal == winner.principal
    assert len([user for user in registry(store)["users"].values() if user["email"] == "guest@example.com"]) == 1


def test_restart_persistence_and_capacity_limits(env):
    auth, store, _, _, owner, sender = env
    service = registration(env)
    service.request_code("guest@example.com", source="trusted-peer")
    restarted = registration(env)
    assert restarted.register("guest@example.com", sender.last_code, NEW_PASSWORD, source="trusted-peer").principal.email == "guest@example.com"
    from tradingagents.workbench.public_accounts import MAX_USERS
    from tradingagents.workbench.public_registration import MAX_CHALLENGES

    registry_value, version = store.read(REGISTRY_KEY)
    template = registry_value["users"][owner.principal.object_id]
    for index in range(MAX_USERS - len(registry_value["users"])):
        registry_value["users"][f"fixture-user-{index}"] = {
            **template, "email": f"retained-{index}@example.com", "disabled": True,
        }
    store.write(REGISTRY_KEY, registry_value, version)
    with pytest.raises(AccountError):
        service.register("full@example.com", "123456", NEW_PASSWORD, source="trusted-peer")
    registry_value, version = store.read(REGISTRY_KEY)
    registry_value["users"] = {owner.principal.object_id: template}
    registry_value["registrations"] = {
        hashlib.sha256(f"pending-{index}@example.com".encode()).hexdigest(): {
            "email": f"pending-{index}@example.com", "salt": "a" * 64,
            "code_hash": "b" * 64, "expires_at": 1800000600, "attempts": 0,
            "created_at": 1800000000, "retry_after": 1800000060,
        } for index in range(MAX_CHALLENGES)
    }
    store.write(REGISTRY_KEY, registry_value, version)
    with pytest.raises(AccountError):
        service.request_code("overflow@example.com", source="trusted-peer")
