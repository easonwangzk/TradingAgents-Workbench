import copy

import pytest

from tests.test_public_store import MemoryBackend

TENANT = "11111111-1111-4111-8111-111111111111"
ALICE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ARM = "https://management.azure.com/.default"


class IdentityProvider:
    def __init__(self):
        self.claims = {"tid": TENANT, "oid": ALICE, "name": "Alice"}
        self.accounts = [{"local_account_id": ALICE, "realm": TENANT}]
        self.token_result = {"access_token": "private-downstream-token", "expires_in": 3600}

    def initiate_auth_code_flow(self, scopes, redirect_uri):
        assert len(scopes) == 1 and scopes[0] in {
            ARM, "https://api.loganalytics.io/.default", "https://cognitiveservices.azure.com/.default",
        }
        assert redirect_uri == "https://workbench.example.com/auth/callback"
        return {
            "auth_uri": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
            "state": "provider-state",
            "nonce": "provider-nonce",
            "code_verifier": "private-pkce-verifier",
        }

    def acquire_token_by_auth_code_flow(self, flow, response):
        assert flow["nonce"] == "provider-nonce"
        assert response["code"] == "one-time-code"
        return {"id_token_claims": copy.deepcopy(self.claims), "access_token": "private-login-token"}

    def get_accounts(self):
        return copy.deepcopy(self.accounts)

    def acquire_token_silent_with_error(self, scopes, account):
        assert len(scopes) == 1 and scopes[0] in {
            ARM, "https://api.loganalytics.io/.default", "https://cognitiveservices.azure.com/.default",
        }
        assert account in self.accounts
        return copy.deepcopy(self.token_result)


@pytest.fixture(name="harness")
def auth_harness():
    from tradingagents.workbench.public_auth import AuthManager
    from tradingagents.workbench.public_store import EncryptedStore

    provider = IdentityProvider()
    clock = [1800000000]
    backend = MemoryBackend()
    store = EncryptedStore(backend, b"a" * 32)
    auth = AuthManager(
        tenant_id=TENANT,
        origin="https://workbench.example.com",
        store=store,
        client_factory=lambda cache: provider,
        clock=lambda: clock[0],
    )
    return auth, provider, clock, backend


def sign_in(auth):
    flow = auth.begin_login()
    session = auth.complete_login(flow.flow_id, {"state": "provider-state", "code": "one-time-code"})
    return flow, session


def test_successful_login_uses_verified_identity_and_hides_credentials(harness):
    auth, _, _, backend = harness
    flow, session = sign_in(auth)
    assert session.principal.tenant_id == TENANT
    assert session.principal.object_id == ALICE
    assert session.principal.name == "Alice"
    assert auth.session(session.session_id) == session
    assert "private-" not in repr(session)
    assert all(b"private-" not in content for content, _ in backend.records.values())
    assert all(session.session_id not in key and flow.flow_id not in key for key in backend.records)


def test_callback_is_one_time_and_wrong_state_is_rejected(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, _, _, _ = harness
    flow, _ = sign_in(auth)
    with pytest.raises(AuthenticationRequired):
        auth.complete_login(flow.flow_id, {"state": "provider-state", "code": "one-time-code"})
    another = auth.begin_login()
    with pytest.raises(AuthenticationRequired):
        auth.complete_login(another.flow_id, {"state": "attacker-state", "code": "one-time-code"})


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "22222222-2222-4222-8222-222222222222", "oid": ALICE},
        {"tid": TENANT, "oid": "../../bob"},
        {"tid": TENANT},
        {"oid": ALICE},
    ],
)
def test_untrusted_or_incomplete_identity_cannot_create_session(harness, claims):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, provider, _, _ = harness
    provider.claims = claims
    with pytest.raises(AuthenticationRequired):
        sign_in(auth)


def test_expired_flow_and_session_require_fresh_login(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, _, clock, _ = harness
    flow = auth.begin_login()
    clock[0] += 601
    with pytest.raises(AuthenticationRequired):
        auth.complete_login(flow.flow_id, {"state": "provider-state", "code": "one-time-code"})
    _, session = sign_in(auth)
    clock[0] += 8 * 3600 + 1
    with pytest.raises(AuthenticationRequired):
        auth.session(session.session_id)


def test_delegated_credential_uses_own_account_and_logout_revokes_access(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, _, _, _ = harness
    _, session = sign_in(auth)
    credential = auth.credential(session.session_id)
    assert credential.get_token(ARM).token == "private-downstream-token"
    auth.logout(session.session_id)
    with pytest.raises(AuthenticationRequired):
        credential.get_token(ARM)


def test_missing_user_account_never_uses_another_cached_account(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, provider, _, _ = harness
    _, session = sign_in(auth)
    provider.accounts = [{"local_account_id": BOB, "realm": TENANT}]
    with pytest.raises(AuthenticationRequired):
        auth.credential(session.session_id).get_token(ARM)


def test_missing_consent_is_actionable_without_exposing_provider_error(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, provider, _, _ = harness
    _, session = sign_in(auth)
    provider.token_result = {"error": "interaction_required", "error_description": "private-details"}
    with pytest.raises(AuthenticationRequired) as failure:
        auth.credential(session.session_id).get_token(ARM)
    assert failure.value.scope == ARM
    assert "private-details" not in str(failure.value)


def test_arbitrary_audience_cannot_receive_a_user_token(harness):
    auth, _, _, _ = harness
    _, session = sign_in(auth)
    with pytest.raises(ValueError):
        auth.credential(session.session_id).get_token("https://attacker.example/.default")


def test_csrf_is_bound_to_the_current_session(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, _, _, _ = harness
    _, first = sign_in(auth)
    _, second = sign_in(auth)
    auth.check_csrf(first, first.csrf_token)
    with pytest.raises(AuthenticationRequired):
        auth.check_csrf(first, second.csrf_token)


def test_delegated_credential_accepts_standard_sdk_defaults(harness):
    auth, _, _, _ = harness
    _, session = sign_in(auth)
    result = auth.credential(session.session_id).get_token(
        ARM, claims=None, tenant_id=TENANT, enable_cae=False
    )
    assert result.token == "private-downstream-token"


@pytest.mark.parametrize(
    "options",
    [{"tenant_id": "22222222-2222-4222-8222-222222222222"}, {"claims": '{"access_token":{}}'}, {"enable_cae": True}],
)
def test_unsupported_tenant_or_claims_challenges_never_return_a_token(harness, options):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, _, _, _ = harness
    _, session = sign_in(auth)
    with pytest.raises(AuthenticationRequired) as failure:
        auth.credential(session.session_id).get_token(ARM, **options)
    assert failure.value.scope == ARM


def test_incremental_login_reuses_latest_cache_and_rotates_session(harness):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, provider, _, _ = harness
    _, previous = sign_in(auth)
    flow = auth.begin_login(session_id=previous.session_id)
    key = auth._key("sessions", previous.session_id)
    record, version = auth.store.read(key)
    record["cache"] = '{"AppMetadata":{"existing":{"client_id":"existing-client"}}}'
    auth.store.write(key, record, version)
    observed = []
    auth.client_factory = lambda cache: (observed.append(cache.serialize()), provider)[1]
    current = auth.complete_login(
        flow.flow_id, {"state": "provider-state", "code": "one-time-code"},
        session_id=previous.session_id,
    )
    assert current.principal == previous.principal
    assert current.session_id != previous.session_id
    assert "existing-client" in observed[0]
    with pytest.raises(AuthenticationRequired):
        auth.session(previous.session_id)


@pytest.mark.parametrize("change", ["account", "logout", "cookie"])
def test_incremental_consent_cannot_switch_or_resurrect_identity(harness, change):
    from tradingagents.workbench.public_auth import AuthenticationRequired

    auth, provider, _, _ = harness
    _, previous = sign_in(auth)
    flow = auth.begin_login(session_id=previous.session_id)
    if change == "account":
        provider.claims["oid"] = BOB
    if change == "logout":
        auth.logout(previous.session_id)
    with pytest.raises(AuthenticationRequired):
        auth.complete_login(
            flow.flow_id, {"state": "provider-state", "code": "one-time-code"},
            session_id=None if change == "cookie" else previous.session_id,
        )
