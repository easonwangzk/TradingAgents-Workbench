# Modified for public distribution: installation identifiers replaced with examples.
import hashlib
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_accounts import AccountError, EmailAuth
from tradingagents.workbench.public_store import EncryptedStore, StoreConflict, StoreUnavailable

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
        self.remaining = 0
        self.reached = threading.Event()
        self.release = threading.Event()

    def arm(self, phase, prefix, occurrence=1):
        self.target = (phase, prefix)
        self.remaining = occurrence
        self.reached.clear()
        self.release.clear()

    def checkpoint(self, phase, key):
        if (
            threading.current_thread().name == "account-racer" and self.target
            and self.target[0] == phase and key.startswith(self.target[1])
        ):
            self.remaining -= 1
            if self.remaining == 0:
                self.reached.set()
                if not self.release.wait(15):
                    raise RuntimeError("Test race was not released")

    def read(self, key):
        self.checkpoint("before-read", key)
        result = super().read(key)
        self.checkpoint("read", key)
        return result

    def write(self, key, content, expected_version=None):
        self.checkpoint("write", key)
        result = super().write(key, content, expected_version)
        self.checkpoint("written", key)
        return result


@contextmanager
def paused_call(backend, operation, phase, prefix, occurrence=1):
    outcomes = []
    backend.arm(phase, prefix, occurrence)

    def run():
        try:
            outcomes.append(operation())
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=run, name="account-racer", daemon=True)
    thread.start()
    try:
        assert backend.reached.wait(15), "Operation did not reach the selected storage boundary"
        yield outcomes
    finally:
        backend.release.set()
        thread.join(15)
        assert not thread.is_alive()


@pytest.fixture
def env():
    backend = PausingBackend()
    store = EncryptedStore(backend, b"a" * 32)
    clock = [1800000000]
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
    return auth, store, backend, clock


@pytest.fixture
def owner(env):
    return env[0].bootstrap(OWNER_PASSWORD)


def add_guest(auth, owner, email="guest@example.com"):
    invitation = auth.invite(owner.session_id, email)
    return auth.redeem(invitation.token, email, GUEST_PASSWORD)


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


def test_bootstrap_invitation_login_isolation_and_secret_handling():
    from argon2 import PasswordHasher, Type, extract_parameters

    from tradingagents.workbench.public_accounts import AccountError, EmailAuth

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"a" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: 1800000000)
    owner = auth.bootstrap(OWNER_PASSWORD)
    assert owner.principal.email == OWNER_EMAIL
    assert owner.principal.is_admin is True
    assert UUID(owner.principal.object_id).version == 4
    assert owner.principal.owner_key == f"email-v1/users/{owner.principal.object_id}"
    link = auth.invite(owner.session_id, "Guest@Example.com")
    guest = auth.redeem(link.token, "guest@example.com", GUEST_PASSWORD)
    assert guest.principal.email == "guest@example.com"
    assert guest.principal.name == "guest@example.com"
    assert guest.principal.is_admin is False
    assert guest.principal.owner_key != owner.principal.owner_key
    assert auth.session(guest.session_id) == guest
    logged_in = auth.login(" GUEST@example.com ", GUEST_PASSWORD, source="trusted-peer")
    assert logged_in.principal == guest.principal
    assert logged_in.session_id != guest.session_id
    with pytest.raises(AccountError):
        auth.invite(guest.session_id, "other@example.com")
    with pytest.raises(AccountError):
        auth.bootstrap(OWNER_PASSWORD)
    with pytest.raises(AccountError):
        auth.redeem(link.token, "guest@example.com", GUEST_PASSWORD)
    with pytest.raises(FrozenInstanceError):
        link.token = "replacement"
    with pytest.raises(FrozenInstanceError):
        guest.principal.object_id = owner.principal.object_id
    plaintext = repr([store.read(key)[0] for key in backend.records])
    ciphertext = repr(backend.records)
    for secret in (OWNER_PASSWORD, GUEST_PASSWORD, link.token, guest.session_id, guest.csrf_token):
        assert secret not in ciphertext
        assert secret not in repr((owner, link, guest, logged_in))
    for secret in (OWNER_PASSWORD, GUEST_PASSWORD, link.token, guest.session_id):
        assert secret not in plaintext
    registry = store.read("email-v1/registry")[0]
    assert registry["owner_id"] == owner.principal.object_id
    stored_hash = registry["users"][guest.principal.object_id]["password_hash"]
    assert PasswordHasher().verify(stored_hash, GUEST_PASSWORD)
    parameters = extract_parameters(stored_hash)
    assert parameters.type is Type.ID
    assert parameters.time_cost >= 3
    assert parameters.memory_cost >= 65536


def test_recovery_is_single_use_and_reset_invalidates_all_old_sessions(env, owner):
    auth, store, _, clock = env
    guest = add_guest(auth, owner)
    old_login = auth.login(guest.principal.email, GUEST_PASSWORD, source="peer")
    recovery = auth.recovery(owner.session_id, guest.principal.object_id)
    other = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
    with pytest.raises(AccountError):
        other.reset(recovery.token, "someone@example.com", NEW_PASSWORD)
    fresh = other.reset(recovery.token, guest.principal.email, NEW_PASSWORD)
    assert fresh.principal == guest.principal
    assert auth.session(fresh.session_id) == fresh
    for stale in (guest, old_login):
        with pytest.raises(AccountError):
            auth.session(stale.session_id)
    with pytest.raises(AccountError):
        auth.reset(recovery.token, guest.principal.email, NEW_PASSWORD)
    with pytest.raises(AccountError):
        auth.login(guest.principal.email, GUEST_PASSWORD, source="peer")
    assert auth.login(guest.principal.email, NEW_PASSWORD, source="peer").principal == guest.principal
    assert recovery.token not in repr(recovery)
    with pytest.raises(FrozenInstanceError):
        recovery.email = OWNER_EMAIL


def test_password_change_requires_current_password_and_revokes_sessions_and_recoveries(env, owner):
    auth = env[0]
    guest = add_guest(auth, owner)
    recovery = auth.recovery(owner.session_id, guest.principal.object_id)
    with pytest.raises(AccountError) as failure:
        auth.change_password(guest.session_id, "fixture-wrong-password", NEW_PASSWORD)
    assert "fixture-wrong-password" not in str(failure.value)
    assert auth.session(guest.session_id) == guest
    with pytest.raises(AccountError):
        auth.change_password(guest.session_id, GUEST_PASSWORD, "short")
    fresh = auth.change_password(guest.session_id, GUEST_PASSWORD, NEW_PASSWORD)
    assert fresh.principal == guest.principal
    with pytest.raises(AccountError):
        auth.session(guest.session_id)
    with pytest.raises(AccountError):
        auth.reset(recovery.token, guest.principal.email, GUEST_PASSWORD)
    assert auth.login(guest.principal.email, NEW_PASSWORD, source="peer").principal == guest.principal


def test_disable_preserves_audit_identity_and_owner_cannot_be_disabled_or_recreated(env, owner):
    auth, store, _, _ = env
    guest = add_guest(auth, owner)
    recovery = auth.recovery(owner.session_id, guest.principal.object_id)
    with pytest.raises(AccountError):
        auth.disable(owner.session_id, owner.principal.object_id,
                     expected_version=auth.accounts(owner.session_id)['version'])
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    with pytest.raises(AccountError):
        auth.session(guest.session_id)
    with pytest.raises(AccountError):
        auth.login(guest.principal.email, GUEST_PASSWORD, source="peer")
    with pytest.raises(AccountError):
        auth.invite(owner.session_id, guest.principal.email)
    with pytest.raises(AccountError):
        auth.recovery(owner.session_id, guest.principal.object_id)
    with pytest.raises(AccountError):
        auth.reset(recovery.token, guest.principal.email, NEW_PASSWORD)
    entries = {entry["object_id"]: entry for entry in auth.users(owner.session_id)}
    assert entries[guest.principal.object_id]["disabled"] is True
    assert entries[owner.principal.object_id]["is_admin"] is True
    assert "password" not in repr(entries)
    assert "generation" not in repr(entries)
    registry = store.read("email-v1/registry")[0]
    assert guest.principal.object_id in registry["users"]
    code = auth.recovery(owner.session_id, owner.principal.object_id)
    fresh_owner = auth.reset(code.token, OWNER_EMAIL, NEW_PASSWORD)
    assert fresh_owner.principal == owner.principal
    with pytest.raises(AccountError):
        auth.bootstrap(OWNER_PASSWORD)
    with pytest.raises(AccountError):
        auth.invite(fresh_owner.session_id, OWNER_EMAIL.upper())


def test_csrf_uses_persisted_capability_and_revalidates_generation(env, owner):
    auth = env[0]
    guest = add_guest(auth, owner)
    auth.check_csrf(guest, guest.csrf_token)
    for invalid in (None, "", "wrong", "\u00e9" * 43):
        with pytest.raises(AccountError) as failure:
            auth.check_csrf(guest, invalid)
        assert failure.value.status_code == 403
    forged = replace(guest, csrf_token="attacker-token")
    with pytest.raises(AccountError):
        auth.check_csrf(forged, forged.csrf_token)
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    with pytest.raises(AccountError):
        auth.check_csrf(guest, guest.csrf_token)


def test_logout_is_durable_idempotent_and_session_expires(env, owner):
    auth, store, _, clock = env
    guest = add_guest(auth, owner)
    other = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
    surviving = other.login(guest.principal.email, GUEST_PASSWORD, source="peer")
    auth.logout(guest.session_id)
    auth.logout(guest.session_id)
    auth.logout(None)
    with pytest.raises(AccountError):
        other.session(guest.session_id)
    assert auth.session(surviving.session_id) == surviving
    clock[0] = owner.expires_at
    with pytest.raises(AccountError):
        other.session(owner.session_id)
    with pytest.raises(AccountError):
        auth.invite(owner.session_id, "late@example.com")


def test_revoked_expired_and_email_substituted_invitations_cannot_create_accounts(env, owner):
    auth, _, _, clock = env
    revoked = auth.invite(owner.session_id, "revoked@example.com")
    auth.revoke_invite(owner.session_id, revoked.token_id)
    with pytest.raises(AccountError):
        auth.redeem(revoked.token, revoked.email, GUEST_PASSWORD)
    invitation = auth.invite(owner.session_id, "invited@example.com")
    with pytest.raises(AccountError):
        auth.redeem(invitation.token, OWNER_EMAIL, GUEST_PASSWORD)
    with pytest.raises(AccountError):
        auth.redeem(invitation.token, "substitute@example.com", GUEST_PASSWORD)
    clock[0] = invitation.expires_at
    with pytest.raises(AccountError):
        auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD)
    owner = auth.login(OWNER_EMAIL, OWNER_PASSWORD, source="peer")
    assert len(auth.users(owner.session_id)) == 1


def test_expired_recovery_and_superseded_codes_are_rejected(env, owner):
    auth, _, _, clock = env
    guest = add_guest(auth, owner)
    first = auth.recovery(owner.session_id, guest.principal.object_id)
    second = auth.recovery(owner.session_id, guest.principal.object_id)
    with pytest.raises(AccountError):
        auth.reset(first.token, guest.principal.email, NEW_PASSWORD)
    clock[0] = second.expires_at
    with pytest.raises(AccountError):
        auth.reset(second.token, guest.principal.email, NEW_PASSWORD)


def test_guest_cannot_manage_users_and_role_kwargs_cannot_escalate(env, owner):
    auth = env[0]
    guest = add_guest(auth, owner)
    link = auth.invite(owner.session_id, "another@example.com")
    operations = (
        lambda: auth.users(guest.session_id),
        lambda: auth.disable(guest.session_id, owner.principal.object_id),
        lambda: auth.recovery(guest.session_id, owner.principal.object_id),
        lambda: auth.revoke_invite(guest.session_id, link.token_id),
        lambda: auth.invite(owner.session_id, "elevate@example.com", is_admin=True),
        lambda: auth.redeem(link.token, link.email, GUEST_PASSWORD, is_admin=True),
        lambda: auth.bootstrap(OWNER_PASSWORD, email="new-owner@example.com"),
    )
    for operation in operations:
        with pytest.raises(AccountError):
            operation()
    assert len(auth.users(owner.session_id)) == 2
    assert auth.redeem(link.token, link.email, GUEST_PASSWORD).principal.is_admin is False


@pytest.mark.parametrize("email", [
    "missing-at", "", "name <guest@example.com>", "guest@example.com\nBcc:other@example.com",
    "a..b@example.com", ".guest@example.com", "guest@-example.com", "guest@example-.com",
    "guest@a..com", "a" * 65 + "@example.com", "guest@example.com:443", "a" * 255,
])
def test_invalid_email_rejected_without_creating_invites(env, owner, email):
    auth, store, _, _ = env
    before = store.read("email-v1/registry")
    with pytest.raises(AccountError):
        auth.invite(owner.session_id, email)
    assert store.read("email-v1/registry") == before


@pytest.mark.parametrize("password", ["", "a" * 11, "a" * 129, "\ud800" * 12, None, b"a" * 12])
def test_invalid_password_rejected_without_bootstrap_state(env, password):
    auth, store, _, _ = env
    with pytest.raises(AccountError) as failure:
        auth.bootstrap(password)
    assert failure.value.status_code == 400
    assert store.read("email-v1/registry") is None


@pytest.mark.parametrize("password", ["a" * 12, "a" * 128, "\U0001f512" * 128])
def test_password_character_and_utf8_boundaries_work(env, password):
    auth = env[0]
    owner = auth.bootstrap(password)
    assert auth.login(OWNER_EMAIL, password, source="peer").principal == owner.principal


def test_old_entra_sessions_and_namespace_are_not_accepted(env, owner):
    auth, store, _, _ = env
    legacy_id = secrets.token_urlsafe(32)
    email_record = store.read(
        "email-v1/sessions/" + hashlib.sha256(owner.session_id.encode()).hexdigest()
    )[0]
    store.write("sessions/" + hashlib.sha256(legacy_id.encode()).hexdigest(), email_record)
    for identifier in (legacy_id, "users/tenant/owner", "../email-v1/registry", None):
        with pytest.raises(AccountError):
            auth.session(identifier)
    assert auth.session(owner.session_id) == owner


@pytest.mark.parametrize("scope,limit", [("account", 10), ("source", 30), ("global", 120)])
def test_admission_is_durable_bounded_and_precedes_hashing(env, owner, scope, limit):
    auth, store, backend, clock = env
    with hashing_calls() as calls:
        for attempt in range(limit):
            email = OWNER_EMAIL if scope == "account" else f"unknown-{attempt}@example.com"
            source = "trusted-peer" if scope == "source" else f"trusted-peer-{attempt}"
            with pytest.raises(AccountError) as failure:
                auth.login(email, "fixture-wrong-password", source=source)
            assert failure.value.status_code == 401
        before = len(calls)
        other = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
        for attempt in range(180):
            email = OWNER_EMAIL if scope == "account" else f"new-{attempt}@example.com"
            source = "trusted-peer" if scope == "source" else f"new-peer-{attempt}"
            with pytest.raises(AccountError) as failure:
                other.login(email, OWNER_PASSWORD, source=source)
            assert failure.value.status_code == 429
        assert len(calls) == before
    admission = store.read("email-v1/admission")[0]
    assert len(admission["accounts"]) <= 128
    assert len(admission["sources"]) <= 128
    assert len(backend.records) == 3
    assert OWNER_EMAIL not in repr(admission)
    assert "trusted-peer" not in repr(admission)
    clock[0] += 61
    fresh = other.login(OWNER_EMAIL, OWNER_PASSWORD, source="after-window")
    assert fresh.principal == owner.principal
    cleaned = store.read("email-v1/admission")[0]
    assert len(cleaned["accounts"]) == len(cleaned["sources"]) == 1


def test_unknown_wrong_disabled_and_invalid_email_errors_are_generic(env, owner):
    auth = env[0]
    guest = add_guest(auth, owner)
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    messages = []
    with hashing_calls() as calls:
        for email, password in (
            ("unknown@example.com", GUEST_PASSWORD),
            (OWNER_EMAIL, GUEST_PASSWORD),
            (guest.principal.email, GUEST_PASSWORD),
        ):
            before = calls.count("verify")
            with pytest.raises(AccountError) as failure:
                auth.login(email, password, source="peer")
            assert calls.count("verify") == before + 1
            messages.append((failure.value.status_code, str(failure.value)))
        with pytest.raises(AccountError) as failure:
            auth.login("invalid-email", GUEST_PASSWORD, source="peer")
        messages.append((failure.value.status_code, str(failure.value)))
    assert len(set(messages)) == 1
    assert messages[0][0] == 401


@pytest.mark.parametrize("source", [None, "", " " * 8, "a" * 257, "\ud800", 42])
def test_invalid_trusted_source_fails_before_hashing_or_anonymous_storage(env, owner, source):
    auth, _, backend, _ = env
    before = backend.read("email-v1/registry")
    with hashing_calls() as calls, pytest.raises(AccountError):
        auth.login(OWNER_EMAIL, OWNER_PASSWORD, source=source)
    assert calls == []
    assert len(backend.records) == 2
    assert backend.read("email-v1/registry") == before


def test_redeem_and_reset_share_anonymous_admission_before_hashing(env, owner):
    auth, _, _, _ = env
    guest = add_guest(auth, owner)
    recovery = auth.recovery(owner.session_id, guest.principal.object_id)
    invitation = auth.invite(owner.session_id, "new@example.com")
    for attempt in range(120):
        with pytest.raises(AccountError):
            auth.login(f"unknown-{attempt}@example.com", "fixture-wrong-password", source=str(attempt))
    with hashing_calls() as calls:
        for operation in (
            lambda: auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD),
            lambda: auth.reset(recovery.token, recovery.email, NEW_PASSWORD),
        ):
            with pytest.raises(AccountError) as failure:
                operation()
            assert failure.value.status_code == 429
    assert calls == []


def test_expired_invites_and_recoveries_are_cleaned_without_removing_identities(env, owner):
    auth, store, _, clock = env
    from tradingagents.workbench.public_accounts import MAX_INVITES, MAX_RECOVERIES

    guest = add_guest(auth, owner)
    registry, version = store.read("email-v1/registry")
    identities = registry["users"].copy()
    registry["invites"] = {
        hashlib.sha256(str(index).encode()).hexdigest(): {
            "email": f"expired-{index}@example.com", "expires_at": clock[0],
        } for index in range(MAX_INVITES)
    }
    registry["recoveries"] = {
        hashlib.sha256(str(index).encode()).hexdigest(): {
            "user_id": str(uuid4()), "email": f"expired-{index}@example.com",
            "expires_at": clock[0], "generation": 1,
        } for index in range(MAX_RECOVERIES)
    }
    store.write("email-v1/registry", registry, version)
    invitation = auth.invite(owner.session_id, "fresh@example.com")
    recovery = auth.recovery(owner.session_id, guest.principal.object_id)
    updated = store.read("email-v1/registry")[0]
    assert updated["users"] == identities
    assert len(updated["invites"]) == len(updated["recoveries"]) == 1
    assert auth.reset(recovery.token, recovery.email, NEW_PASSWORD).principal == guest.principal
    assert auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD).principal.is_admin is False


@pytest.mark.parametrize("collection", ["users", "invites", "recoveries"])
def test_registry_limits_fail_closed_and_keep_disabled_account_identities(env, owner, collection):
    from tradingagents.workbench.public_accounts import MAX_INVITES, MAX_RECOVERIES, MAX_USERS

    auth, store, _, clock = env
    guest = add_guest(auth, owner)
    registry, version = store.read("email-v1/registry")
    maximum = {"users": MAX_USERS, "invites": MAX_INVITES, "recoveries": MAX_RECOVERIES}[collection]
    assert 1 <= maximum <= 1024
    if collection == "users":
        for index in range(maximum - len(registry["users"])):
            registry["users"][str(uuid4())] = {
                **registry["users"][guest.principal.object_id],
                "email": f"disabled-{index}@example.com", "disabled": True,
            }
    else:
        for index in range(maximum):
            registry[collection][hashlib.sha256(str(index).encode()).hexdigest()] = {
                "user_id": str(uuid4()), "email": f"pending-{index}@example.com",
                "generation": 1, "expires_at": clock[0] + 1000,
            }
    store.write("email-v1/registry", registry, version)
    before = store.read("email-v1/registry")
    with pytest.raises(AccountError):
        if collection == "recoveries":
            auth.recovery(owner.session_id, guest.principal.object_id)
        else:
            auth.invite(owner.session_id, "new@example.com")
    assert store.read("email-v1/registry") == before


def peer_auth(env):
    _, store, _, clock = env
    return EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])


def test_concurrent_bootstrap_creates_only_one_immutable_owner(env):
    auth, _, backend, _ = env
    peer = peer_auth(env)
    with paused_call(backend, lambda: auth.bootstrap(OWNER_PASSWORD), "write", "email-v1/registry") as result:
        winner = peer.bootstrap(NEW_PASSWORD)
    assert isinstance(result[0], AccountError)
    assert len(peer.users(winner.session_id)) == 1
    assert peer.login(OWNER_EMAIL, NEW_PASSWORD, source="peer").principal == winner.principal


@pytest.mark.parametrize("competing", ["redeem", "revoke", "duplicate-email"])
def test_inflight_invitation_cannot_overwrite_a_competing_registry_change(env, owner, competing):
    auth, _, backend, _ = env
    peer = peer_auth(env)
    invitation = auth.invite(owner.session_id, "guest@example.com")
    another = auth.invite(owner.session_id, invitation.email) if competing == "duplicate-email" else invitation
    with paused_call(
        backend, lambda: auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD),
        "write", "email-v1/registry",
    ) as result:
        if competing == "revoke":
            peer.revoke_invite(owner.session_id, invitation.token_id)
        else:
            winner = peer.redeem(another.token, another.email, NEW_PASSWORD)
    assert isinstance(result[0], AccountError)
    entries = peer.users(owner.session_id)
    assert len(entries) == (1 if competing == "revoke" else 2)
    if competing != "revoke":
        assert peer.login(invitation.email, NEW_PASSWORD, source="peer").principal == winner.principal
    with pytest.raises(AccountError):
        peer.redeem(invitation.token, invitation.email, GUEST_PASSWORD)


@pytest.mark.parametrize("kind", ["invite", "recovery"])
def test_token_expiring_during_real_hashing_is_not_consumed(env, owner, kind):
    auth, store, _, clock = env
    if kind == "invite":
        token = auth.invite(owner.session_id, "guest@example.com")
    else:
        guest = add_guest(auth, owner)
        token = auth.recovery(owner.session_id, guest.principal.object_id)
    before = store.read("email-v1/registry")
    with (
        hashing_calls(lambda: clock.__setitem__(0, token.expires_at)) as calls,
        pytest.raises(AccountError),
    ):
        if kind == "invite":
            auth.redeem(token.token, token.email, NEW_PASSWORD)
        else:
            auth.reset(token.token, token.email, NEW_PASSWORD)
    assert "hash" in calls
    assert store.read("email-v1/registry") == before


@pytest.mark.parametrize("mutation", ["reset", "disable"])
@pytest.mark.parametrize("phase,prefix,occurrence", [
    ("read", "email-v1/registry", 1),
    ("read", "email-v1/registry", 2),
    ("write", "email-v1/sessions/", 1),
    ("written", "email-v1/sessions/", 1),
])
def test_login_never_returns_a_valid_stale_session_across_account_changes(
    env, owner, mutation, phase, prefix, occurrence,
):
    auth, _, backend, _ = env
    guest = add_guest(auth, owner)
    peer = peer_auth(env)
    code = auth.recovery(owner.session_id, guest.principal.object_id)
    with paused_call(
        backend, lambda: auth.login(guest.principal.email, GUEST_PASSWORD, source="peer"),
        phase, prefix, occurrence,
    ) as result:
        if mutation == "reset":
            fresh = peer.reset(code.token, code.email, NEW_PASSWORD)
        else:
            peer.disable(owner.session_id, guest.principal.object_id,
                         expected_version=peer.accounts(owner.session_id)['version'])
        issued_before_release = {key for key in backend.records if key.startswith("email-v1/sessions/")}
    assert isinstance(result[0], AccountError)
    if phase == "read" and occurrence == 1:
        assert {key for key in backend.records if key.startswith("email-v1/sessions/")} == issued_before_release
    with pytest.raises(AccountError):
        auth.session(guest.session_id)
    if mutation == "reset":
        assert auth.session(fresh.session_id) == fresh


@pytest.mark.parametrize("operation", ["reset", "change"])
def test_concurrent_password_updates_cannot_overwrite_winner(env, owner, operation):
    auth, _, backend, _ = env
    guest = add_guest(auth, owner)
    peer = peer_auth(env)
    code = auth.recovery(owner.session_id, guest.principal.object_id)

    def change():
        if operation == "reset":
            return auth.reset(code.token, code.email, "fixture-losing-password")
        return auth.change_password(guest.session_id, GUEST_PASSWORD, "fixture-losing-password")

    with paused_call(backend, change, "write", "email-v1/registry") as result:
        winner = peer.reset(code.token, code.email, NEW_PASSWORD)
    assert isinstance(result[0], AccountError)
    assert auth.login(guest.principal.email, NEW_PASSWORD, source="peer").principal == winner.principal
    with pytest.raises(AccountError):
        auth.reset(code.token, code.email, "fixture-losing-password")


@pytest.mark.parametrize("mutation", ["reset", "logout"])
@pytest.mark.parametrize("operation", ["invite", "change"])
def test_completed_session_revocation_fences_inflight_registry_writes(env, owner, mutation, operation):
    auth, store, backend, _ = env
    peer = peer_auth(env)
    recovery = auth.recovery(owner.session_id, owner.principal.object_id)

    def in_flight():
        if operation == "invite":
            return auth.invite(owner.session_id, "late@example.com")
        return auth.change_password(owner.session_id, OWNER_PASSWORD, "fixture-losing-password")

    with paused_call(backend, in_flight, "write", "email-v1/registry") as result:
        if mutation == "reset":
            peer.reset(recovery.token, recovery.email, NEW_PASSWORD)
        else:
            peer.logout(owner.session_id)
    assert isinstance(result[0], AccountError)
    registry = store.read("email-v1/registry")[0]
    assert not registry["invites"]
    expected = NEW_PASSWORD if mutation == "reset" else OWNER_PASSWORD
    assert auth.login(OWNER_EMAIL, expected, source="peer").principal == owner.principal


def test_logout_between_session_and_registry_reads_cannot_authorize_an_invite(env, owner):
    auth, store, backend, _ = env
    peer = peer_auth(env)
    with paused_call(
        backend, lambda: auth.invite(owner.session_id, "late@example.com"),
        "before-read", "email-v1/registry",
    ) as result:
        peer.logout(owner.session_id)
    assert isinstance(result[0], AccountError)
    assert not store.read("email-v1/registry")[0]["invites"]


def test_inflight_admin_role_change_and_forged_role_never_grant_access(env, owner):
    auth, store, backend, _ = env
    guest = add_guest(auth, owner)
    registry, version = store.read("email-v1/registry")
    registry["users"][guest.principal.object_id]["is_admin"] = True
    store.write("email-v1/registry", registry, version)
    assert auth.session(guest.session_id).principal.is_admin is False
    with paused_call(
        backend, lambda: auth.invite(owner.session_id, "late@example.com"),
        "write", "email-v1/registry",
    ) as result:
        registry, version = store.read("email-v1/registry")
        registry["owner_id"] = guest.principal.object_id
        store.write("email-v1/registry", registry, version)
    assert isinstance(result[0], AccountError)
    for old_session in (owner, guest):
        with pytest.raises(AccountError):
            peer_auth(env).session(old_session.session_id)


def test_last_admission_slot_is_atomic_across_instances_before_hashing(env, owner):
    auth, store, backend, _ = env
    peer = peer_auth(env)
    for _ in range(9):
        with pytest.raises(AccountError):
            auth.login(OWNER_EMAIL, "fixture-wrong-password", source="peer")

    def attempt():
        with hashing_calls() as calls:
            try:
                auth.login(OWNER_EMAIL, OWNER_PASSWORD, source="peer")
            except AccountError as error:
                assert calls == []
                return error
        pytest.fail("A losing admission must not hash or issue a session")

    with paused_call(backend, attempt, "write", "email-v1/admission") as result:
        admitted = peer.login(OWNER_EMAIL, OWNER_PASSWORD, source="peer")
    assert isinstance(result[0], AccountError)
    assert result[0].status_code == 429
    assert auth.session(admitted.session_id).principal == owner.principal
    assert store.read("email-v1/admission")[0]["global"]["count"] == 10


@pytest.mark.parametrize("fault", ["conflict", "unavailable"])
def test_admission_storage_failure_never_hashes_and_is_sanitized(fault):
    class FailingBackend(LockedBackend):
        failures = 0

        def write(self, key, content, expected_version=None):
            if key == "email-v1/admission":
                self.failures += 1
                error = StoreConflict if fault == "conflict" else StoreUnavailable
                raise error("fixture-backend-sensitive-detail")
            return super().write(key, content, expected_version)

    backend = FailingBackend()
    auth = EmailAuth(EncryptedStore(backend, b"a" * 32), origin=ORIGIN)
    auth.bootstrap(OWNER_PASSWORD)
    with hashing_calls() as calls, pytest.raises(AccountError) as failure:
        auth.login(OWNER_EMAIL, OWNER_PASSWORD, source="peer")
    assert failure.value.status_code == 503
    assert "sensitive" not in str(failure.value)
    assert calls == []
    assert 1 <= backend.failures <= 8


@pytest.mark.parametrize("category", ["accounts", "sources"])
def test_full_anonymous_index_rejects_new_identities_without_hashing(env, owner, category):
    auth, store, _, clock = env
    store.write("email-v1/admission", {
        "global": {"count": 0, "until": clock[0] + 60},
        category: {
            hashlib.sha256(f"prior-window-{index}".encode()).hexdigest(): {
                "count": 1, "until": clock[0] + 50,
            } for index in range(128)
        },
    })
    before = store.read("email-v1/admission")
    with hashing_calls() as calls, pytest.raises(AccountError) as failure:
        auth.login(OWNER_EMAIL, OWNER_PASSWORD, source="new-peer")
    assert failure.value.status_code == 429
    assert calls == []
    assert store.read("email-v1/admission") == before


def test_user_capacity_is_rechecked_before_redemption_hashing(env, owner):
    from tradingagents.workbench.public_accounts import MAX_USERS

    auth, store, _, _ = env
    invitation = auth.invite(owner.session_id, "pending@example.com")
    registry, version = store.read("email-v1/registry")
    for index in range(MAX_USERS - 1):
        registry["users"][str(uuid4())] = {
            **registry["users"][owner.principal.object_id],
            "email": f"retained-{index}@example.com", "disabled": True,
        }
    store.write("email-v1/registry", registry, version)
    before = store.read("email-v1/registry")
    with hashing_calls() as calls, pytest.raises(AccountError):
        auth.redeem(invitation.token, invitation.email, GUEST_PASSWORD)
    assert calls == []
    assert store.read("email-v1/registry") == before
