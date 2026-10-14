import pytest
from fastapi.testclient import TestClient

from tests.test_email_server import GUEST_PASSWORD, app_case as app_case, login
from tests.test_public_accounts import ORIGIN, OWNER_PASSWORD
from tradingagents.workbench.public_accounts import REGISTRY_KEY, AccountError
from tradingagents.workbench.public_store import StoreUnavailable


def test_admin_account_contract_authority_and_conditional_mutation(app_case):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        assert client.get('/api/admin/accounts').status_code == 401
        login(client, 'guest@example.com', GUEST_PASSWORD)
        assert client.get('/api/admin/accounts').status_code == 403
        session = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        response = client.get('/api/admin/accounts')
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {'accounts', 'version'}
        assert isinstance(payload['version'], str)
        assert client.get('/api/me').json()['permissions'] == dict.fromkeys(
            ('research', 'assistant', 'market'), True)
        guest = next(row for row in payload['accounts'] if not row['is_admin'])
        assert set(guest) == {'object_id', 'email', 'name', 'is_admin', 'disabled', 'permissions'}
        body = {'disabled': False, 'permissions': {'research': False, 'assistant': True, 'market': True}}
        path = f"/api/admin/accounts/{guest['object_id']}"
        headers = {'Origin': ORIGIN, 'x-workbench-token': session['csrf_token']}
        assert client.patch(path, json=body, headers=headers).status_code in (409, 412)
        assert client.patch(path, json=body, headers={**headers, 'If-Match': 'stale'}).status_code in (409, 412)
        saved = client.patch(path, json=body, headers={**headers, 'If-Match': payload['version']})
        assert saved.status_code == 200
        assert saved.json()['account']['permissions'] == body['permissions']
        owner_path = f'/api/admin/accounts/{app_case.owner.principal.object_id}'
        assert client.patch(owner_path, json=body, headers={**headers, 'If-Match': saved.json()['version']}).status_code == 403
        disable_owner = {'disabled': True, 'permissions': dict.fromkeys(('research', 'assistant', 'market'), True)}
        assert client.patch(owner_path, json=disable_owner, headers={**headers, 'If-Match': saved.json()['version']}).status_code == 403
        assert client.patch(path, json=body, headers={**headers, 'If-Match': payload['version']}).status_code == 409
        assert client.patch(path, json=body, headers={'If-Match': saved.json()['version']}).status_code == 403


@pytest.mark.parametrize('body', [
    {'disabled': 0, 'permissions': dict.fromkeys(('research', 'assistant', 'market'), True)},
    {'disabled': False, 'permissions': {'research': True}},
    {'disabled': False, 'permissions': {'research': True, 'assistant': True, 'market': 'yes'}},
    {'disabled': False, 'permissions': dict.fromkeys(('research', 'assistant', 'market'), True), 'is_admin': True},
])
def test_admin_patch_strict_input(app_case, body):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        session = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        version = client.get('/api/admin/accounts').json()['version']
        response = client.patch(f'/api/admin/accounts/{app_case.guest.principal.object_id}', json=body,
                                headers={'Origin': ORIGIN, 'x-workbench-token': session['csrf_token'], 'If-Match': version})
        assert response.status_code == 400


def test_disable_enable_does_not_restore_sessions_and_audit_is_atomic(app_case, monkeypatch):
    auth = app_case.auth
    identity = app_case.guest.principal.object_id
    payload = {'disabled': True, 'permissions': dict.fromkeys(('research', 'assistant', 'market'), True)}
    original = auth.accounts(app_case.owner.session_id)
    write = app_case.store.write

    def fail_registry(key, *args, **kwargs):
        if key == REGISTRY_KEY:
            raise StoreUnavailable()
        return write(key, *args, **kwargs)

    monkeypatch.setattr(app_case.store, 'write', fail_registry)
    with pytest.raises(AccountError) as error:
        auth.update_account(app_case.owner.session_id, identity, payload, expected_version=original['version'])
    assert error.value.status_code == 503
    assert auth.accounts(app_case.owner.session_id) == original
    assert not any(event['action'] == 'account.update' for event in auth.audit(app_case.owner.session_id)['events'])
    monkeypatch.setattr(app_case.store, 'write', write)
    disabled = auth.update_account(app_case.owner.session_id, identity, payload, expected_version=original['version'])
    payload['disabled'] = False
    auth.update_account(app_case.owner.session_id, identity, payload, expected_version=disabled['version'])
    with pytest.raises(AccountError):
        auth.session(app_case.guest.session_id)
    assert auth.login('guest@example.com', GUEST_PASSWORD, source='fixture').principal == app_case.guest.principal
    audit = auth.audit(app_case.owner.session_id)
    assert audit['events'][0]['after']['disabled'] is False
    assert 'password' not in repr(audit)
    assert app_case.guest.session_id not in repr(audit)


def test_legacy_disable_requires_current_registry_version(app_case):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        session = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        identity = app_case.guest.principal.object_id
        path = f'/api/admin/users/{identity}/disable'
        snapshot = client.get('/api/admin/accounts').json()
        headers = {'Origin': ORIGIN, 'x-workbench-token': session['csrf_token']}
        for conditional in ({}, {'If-Match': 'stale'}):
            assert client.post(path, json={}, headers={**headers, **conditional}).status_code == 409
            assert client.get('/api/admin/accounts').json() == snapshot
        assert client.post(path, json={}, headers={'If-Match': snapshot['version']}).status_code == 403
        assert client.post(path, json={}, headers={**headers, 'If-Match': snapshot['version']}).status_code == 200
        disabled = client.get('/api/admin/accounts').json()
        assert next(row for row in disabled['accounts'] if row['object_id'] == identity)['disabled'] is True
        body = {'disabled': False, 'permissions': dict.fromkeys(('research', 'assistant', 'market'), True)}
        enabled = client.patch(f'/api/admin/accounts/{identity}', json=body,
                               headers={**headers, 'If-Match': disabled['version']})
        assert enabled.status_code == 200
        assert client.post(path, json={}, headers={**headers, 'If-Match': disabled['version']}).status_code == 409
        assert client.get('/api/admin/accounts').json()['version'] == enabled.json()['version']
        owner_path = f'/api/admin/users/{app_case.owner.principal.object_id}/disable'
        assert client.post(owner_path, json={}, headers={**headers, 'If-Match': enabled.json()['version']}).status_code == 403


def test_legacy_disable_revision_and_audit_are_atomic(app_case, monkeypatch):
    auth = app_case.auth
    snapshot = auth.accounts(app_case.owner.session_id)
    identity = app_case.guest.principal.object_id
    with pytest.raises(AccountError) as missing:
        auth.disable(app_case.owner.session_id, identity)
    assert missing.value.status_code == 409
    write = app_case.store.write

    def fail_registry(key, *args, **kwargs):
        if key == REGISTRY_KEY:
            raise StoreUnavailable()
        return write(key, *args, **kwargs)

    monkeypatch.setattr(app_case.store, 'write', fail_registry)
    with pytest.raises(AccountError) as unavailable:
        auth.disable(app_case.owner.session_id, identity, expected_version=snapshot['version'])
    assert unavailable.value.status_code == 503
    assert auth.accounts(app_case.owner.session_id) == snapshot
    assert not any(event['action'] == 'account.disable' for event in auth.audit(app_case.owner.session_id)['events'])


def test_monitoring_contract_and_unknown_metrics(app_case):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        login(client, 'guest@example.com', GUEST_PASSWORD)
        for route in ('overview', 'usage', 'audit'):
            assert client.get(f'/api/admin/{route}').status_code == 403
        login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        overview = client.get('/api/admin/overview')
        assert overview.status_code == 200
        assert set(overview.json()) == {'accounts', 'runs', 'budget', 'recent_errors', 'coverage'}
        assert overview.json()['accounts'] == {'total': 2, 'enabled': 2, 'disabled': 0}
        usage = client.get('/api/admin/usage?days=7').json()
        assert set(usage) == {'days', 'rows', 'coverage'}
        assert set(usage['coverage']) == {'since', 'notes', 'truncated'}
        assert len(usage['rows']) == 2
        assert set(usage['rows'][0]) == {'account_id', 'email', 'research_runs', 'research_failed', 'assistant_calls',
                                      'input_tokens', 'output_tokens', 'platform_cost_usd', 'external_cost_usd', 'last_activity'}
        assert usage['rows'][0]['input_tokens'] is None
        assert usage['rows'][0]['external_cost_usd'] is None
        assert client.get('/api/admin/usage?days=8').status_code == 400
        assert client.get('/api/admin/usage?account_id=invalid').status_code == 400


def test_usage_durable_idempotent_bounded_and_redacted(app_case):
    from tradingagents.workbench.public_usage import UsageLedger

    ledger = UsageLedger(app_case.store, clock=lambda: app_case.clock[0], max_events=2)
    identity = app_case.guest.principal.object_id
    ledger.record(identity, 'operation-1', 'assistant', success=False)
    ledger.record(identity, 'operation-1', 'assistant', success=True, input_tokens=99)
    ledger.record(identity, 'operation-2', 'research', success=True, input_tokens=10, output_tokens=5, platform_cost_usd=0.01)
    assert len(ledger.read(identity)['events']) == 2
    assert ledger.read(identity)['events'][0]['input_tokens'] is None
    ledger.record(identity, 'operation-3', 'assistant', success=True)
    stored = UsageLedger(app_case.store).read(identity)
    assert len(stored['events']) == 2
    assert stored['truncated'] is True
    assert stored['since'] == stored['events'][0]['occurred_at']
    assert all(set(event) == {'operation_id', 'kind', 'occurred_at', 'success', 'input_tokens', 'output_tokens', 'platform_cost_usd'}
               for event in stored['events'])


def test_legacy_permissions_and_audit_retention(app_case, monkeypatch):
    from tradingagents.workbench import public_accounts

    monkeypatch.setattr(public_accounts, 'MAX_AUDIT', 3)
    registry, version = app_case.store.read(REGISTRY_KEY)
    del registry['users'][app_case.guest.principal.object_id]['permissions']
    app_case.store.write(REGISTRY_KEY, registry, version)
    assert app_case.auth.feature_permissions(app_case.guest.principal) == dict.fromkeys(('research', 'assistant', 'market'), True)
    for _ in range(4):
        app_case.auth.recovery(app_case.owner.session_id, app_case.guest.principal.object_id)
    audit = app_case.auth.audit(app_case.owner.session_id)
    assert len(audit['events']) == 3
    assert audit['truncated'] is True
    assert all(event['action'] == 'recovery.create' for event in audit['events'])


def test_monitoring_bound_and_no_report_reads(app_case, monkeypatch):
    from datetime import datetime, timezone

    from tradingagents.workbench.public_admin import PublicAdmin

    now = datetime.fromtimestamp(app_case.clock[0], timezone.utc).isoformat()
    identity = app_case.guest.principal.object_id
    index_key = f'email-v1/users/{identity}/hosted-runs/index'
    app_case.store.write(index_key, {'runs': [{'summary': {'id': str(number), 'status': 'failed', 'created_at': now,
                                                       'error_code': 'private-secret-error', 'result': {'reports': 'secret'}}}
                                            for number in range(513)]})
    read = app_case.store.read
    reads = []

    def bounded_read(key):
        reads.append(key)
        assert '/hosted-runs/' not in key or key.endswith('/index')
        return read(key)

    monkeypatch.setattr(app_case.store, 'read', bounded_read)
    admin = PublicAdmin(auth=app_case.auth, budget=app_case.budget)
    result = admin.overview(app_case.owner.session_id)
    assert result['runs']['total'] == 512
    assert result['coverage']['truncated'] is True
    assert len(result['recent_errors']) == 20
    assert 'secret' not in repr(result)
    assert all(item['code'] == 'worker_error' for item in result['recent_errors'])


def test_overview_includes_sanitized_usage_failures_without_runs(app_case):
    from tradingagents.workbench.public_usage import UsageLedger

    ledger = UsageLedger(app_case.store, clock=lambda: app_case.clock[0])
    identity = app_case.guest.principal.object_id
    for kind in ('assistant', 'assistant_provider', 'research_provider'):
        app_case.clock[0] += 1
        ledger.record(identity, f'{kind}:private-payload-secret', kind, success=False)
    ledger.record(identity, 'successful-assistant', 'assistant', success=True)
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        result = client.get('/api/admin/overview').json()
    assert result['runs']['total'] == 0
    assert [item['code'] for item in result['recent_errors']] == ['provider_failed', 'provider_failed', 'assistant_failed']
    assert [item['occurred_at'] for item in result['recent_errors']] == [
        event['occurred_at'] for event in reversed(ledger.read(identity)['events'][:3])]
    assert all(item['account_email'] == 'guest@example.com' and item['run_id'] == '' for item in result['recent_errors'])
    assert all(set(item) == {'account_email', 'run_id', 'code', 'occurred_at'} for item in result['recent_errors'])
    assert 'secret' not in repr(result) and 'private-payload' not in repr(result)
    assert result['coverage']['since'] == ledger.read(identity)['since']


def test_overview_usage_reads_are_per_account_bounded_and_coverage_is_honest(app_case, monkeypatch):
    from tradingagents.workbench.public_admin import PublicAdmin
    from tradingagents.workbench.public_usage import UsageLedger

    ledger = UsageLedger(app_case.store, clock=lambda: app_case.clock[0], max_events=2)
    identity = app_case.guest.principal.object_id
    for number in range(3):
        ledger.record(identity, f'failure-{number}', 'assistant', success=False)
    read = app_case.store.read
    usage_reads = []

    def bounded_read(key):
        if key.endswith('/usage'):
            usage_reads.append(key)
        assert not key.endswith('/messages')
        return read(key)

    monkeypatch.setattr(app_case.store, 'read', bounded_read)
    result = PublicAdmin(auth=app_case.auth, budget=app_case.budget).overview(app_case.owner.session_id)
    assert len(usage_reads) == 2 and len(set(usage_reads)) == 2
    assert len(result['recent_errors']) == 2
    assert result['coverage']['truncated'] is True
    assert any('512' in note and 'per account' in note for note in result['coverage']['notes'])
    assert any('event times' in note for note in result['coverage']['notes'])


def test_invitation_redemption_and_recovery_audit_excludes_secrets(app_case):
    invitation = app_case.auth.invite(app_case.owner.session_id, 'new-member@example.com')
    member = app_case.auth.redeem(invitation.token, invitation.email, 'fixture-new-member-password')
    recovery = app_case.auth.recovery(app_case.owner.session_id, member.principal.object_id)
    app_case.auth.reset(recovery.token, member.principal.email, 'fixture-replaced-password')
    events = app_case.auth.audit(app_case.owner.session_id)['events']
    actions = {event['action'] for event in events}
    assert {'invitation.redeem', 'recovery.redeem'} <= actions
    assert events[0]['actor_email'] == member.principal.email
    for secret in (invitation.token, recovery.token, 'fixture-new-member-password', 'fixture-replaced-password'):
        assert secret not in repr(events)
