from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_email_server import GUEST_PASSWORD, app_case as app_case, login
from tests.test_public_accounts import ORIGIN
from tests.test_public_assistant import assistant_case as assistant_case
from tests.test_public_gateway import foundry_installation
from tradingagents.workbench.public_admin import PublicAdmin
from tradingagents.workbench.public_assistant import AssistantError, ResearchAssistant
from tradingagents.workbench.public_budget import BudgetLedger
from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
from tradingagents.workbench.public_hosted_runs import HostedRunsError
from tradingagents.workbench.public_market import MarketData, MarketError
from tradingagents.workbench.public_usage import UsageLedger


def revoke(app_case, feature):
    payload = {'disabled': False, 'permissions': {'research': True, 'assistant': True, 'market': True}}
    payload['permissions'][feature] = False
    app_case.auth.update_account(app_case.owner.session_id, app_case.guest.principal.object_id, payload,
                                expected_version=app_case.auth.accounts(app_case.owner.session_id)['version'])


@pytest.mark.parametrize('feature,route', [('research', '/api/runs'), ('assistant', '/api/assistant/sessions'),
                                         ('market', '/api/market/watchlist')])
def test_feature_denied_on_history_http_and_service(app_case, feature, route):
    revoke(app_case, feature)
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        login(client, 'guest@example.com', GUEST_PASSWORD)
        assert client.get(route).status_code == 403
    if feature == 'research':
        with pytest.raises(HostedRunsError) as error:
            app_case.runs.list(app_case.guest.session_id)
        assert error.value.status_code == 403
    elif feature == 'assistant':
        assistant = ResearchAssistant(store=app_case.store, auth=app_case.auth, runs=app_case.runs, gateway=app_case.gateway)
        with pytest.raises(AssistantError) as error:
            assistant.create(app_case.guest.session_id)
        assert error.value.status_code == 403
    else:
        market = MarketData(store=app_case.store, settings=app_case.settings)
        with pytest.raises(MarketError) as error:
            market.watchlist(app_case.guest.principal)
        assert error.value.status_code == 403


@pytest.mark.parametrize('during_token', [False, True])
def test_assistant_gateway_rechecks_permission_before_http(app_case, during_token):
    installation = foundry_installation(deployment='gpt-5.6-sol')
    installation['foundry_models'] = [foundry_installation(deployment='gpt-5.6-luna', model_family='gpt-5.6-luna',
                                                          context_window_tokens=100000, max_output_tokens=4096)['foundry']]
    calls = []

    class Credential:
        def get_token(self, scope):
            if during_token:
                revoke(app_case, 'assistant')
            return SimpleNamespace(token='fixture-token')

    gateway = ProviderGateway(app_case.store, settings=app_case.settings, budget=app_case.budget,
                              installation=installation, managed_credential=Credential(),
                              http_client=httpx.Client(transport=httpx.MockTransport(lambda request: (calls.append(request), httpx.Response(500))[1])),
                              clock=lambda: app_case.clock[0])
    if not during_token:
        revoke(app_case, 'assistant')
    with pytest.raises(GatewayError) as error:
        gateway.assistant_reply(app_case.guest.principal, str(uuid4()), [{'role': 'user', 'content': 'fixture'}], context='')
    assert error.value.status_code == 403
    assert calls == []
    assert app_case.budget.snapshot()['reserved_microusd'] == 0


def test_automatic_research_grant_cannot_bypass_permission(app_case):
    from tradingagents.workbench.public_hosted_runs import _RunAuthorization

    revoke(app_case, 'research')
    registry = app_case.auth._registry()[0]
    grant = {'generation': registry['users'][app_case.guest.principal.object_id]['generation'],
             'email': app_case.guest.principal.email, 'owner_id': registry['owner_id'],
             'deadline': app_case.clock[0] + 100}
    with pytest.raises(Exception) as error:
        app_case.runs._principal_for(_RunAuthorization(app_case.guest.principal.object_id, str(uuid4()), grant))
    assert error.value.status_code == 403


def test_assistant_usage_survives_deletion_and_provider_errors_are_sanitized(assistant_case):
    assistant = assistant_case['assistant']
    owner = assistant_case['owner']
    session = assistant.create(owner.session_id)
    assistant.send(owner.session_id, session['id'], str(uuid4()), 'fixture prompt private')
    assistant.delete(owner.session_id, session['id'])
    ledger = UsageLedger(assistant._store)
    assert len(ledger.read(owner.principal.object_id)['events']) == 1
    assert 'fixture prompt private' not in repr(ledger.read(owner.principal.object_id))
    session = assistant.create(owner.session_id)

    def provider_error(*args, **kwargs):
        raise RuntimeError('secret upstream key and prompt')

    assistant_case['gateway'].assistant_reply = provider_error
    with pytest.raises(AssistantError) as error:
        assistant.send(owner.session_id, session['id'], str(uuid4()), 'fixture')
    assert 'secret' not in str(error.value)
    events = ledger.read(owner.principal.object_id)['events']
    assert len(events) == 2 and events[-1]['success'] is False
    assert events[-1]['platform_cost_usd'] is None
    overview = PublicAdmin(auth=assistant._auth, budget=BudgetLedger(assistant._store)).overview(owner.session_id)
    assert overview['runs']['total'] == 0
    assert overview['recent_errors'] == [{'account_email': owner.principal.email, 'run_id': '',
                                         'code': 'assistant_failed', 'occurred_at': events[-1]['occurred_at']}]
    assert 'secret' not in repr(overview) and 'fixture prompt private' not in repr(overview)


def test_failed_provider_usage_is_visible_without_hosted_runs(app_case):
    run_id = str(uuid4())
    principal = app_case.guest.principal
    capability = app_case.gateway.authorize(run_id, principal, app_case.settings.pin(principal, run_id),
                                           expires_at=int(app_case.clock[0]) + 100)
    app_case.gateway._http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(500, json={'error': 'private upstream key and prompt'})))
    with pytest.raises(GatewayError):
        app_case.gateway.dispatch(capability.serialize(), method='POST', path='/chat/completions', headers={},
                                  body={'model': 'gpt-5', 'messages': [{'role': 'user', 'content': 'fixture private prompt'}],
                                        'max_completion_tokens': 16})
    events = UsageLedger(app_case.store).read(principal.object_id)['events']
    assert len(events) == 1 and events[0]['success'] is False
    overview = PublicAdmin(auth=app_case.auth, budget=app_case.budget).overview(app_case.owner.session_id)
    assert overview['runs']['total'] == 0
    assert overview['recent_errors'] == [{'account_email': principal.email, 'run_id': '',
                                         'code': 'provider_failed', 'occurred_at': events[0]['occurred_at']}]
    assert 'private' not in repr(overview) and run_id not in repr(overview)


def test_usage_failure_after_assistant_success_does_not_replay(assistant_case, monkeypatch):
    assistant = assistant_case['assistant']
    owner = assistant_case['owner']
    session = assistant.create(owner.session_id)
    write = assistant._store.write

    def fail_usage(key, *args, **kwargs):
        if key.endswith('/usage'):
            raise RuntimeError('storage unavailable')
        return write(key, *args, **kwargs)

    monkeypatch.setattr(assistant._store, 'write', fail_usage)
    turn = str(uuid4())
    first = assistant.send(owner.session_id, session['id'], turn, 'fixture')
    assert assistant.send(owner.session_id, session['id'], turn, 'fixture') == first
    assert len(assistant_case['gateway'].calls) == 1


def test_research_gateway_revocation_and_byok_usage(app_case):
    run_id = str(uuid4())
    principal = app_case.guest.principal
    capability = app_case.gateway.authorize(run_id, principal, app_case.settings.pin(principal, run_id),
                                           expires_at=int(app_case.clock[0]) + 100)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={'model': 'gpt-5', 'usage': {'prompt_tokens': 11, 'completion_tokens': 7},
                                         'choices': [{'message': {'role': 'assistant', 'content': 'fixture'}}]})

    app_case.gateway._http = httpx.Client(transport=httpx.MockTransport(upstream))
    body = {'model': 'gpt-5', 'messages': [{'role': 'user', 'content': 'fixture'}], 'max_completion_tokens': 16}
    app_case.gateway.dispatch(capability.serialize(), method='POST', path='/chat/completions', headers={}, body=body)
    events = UsageLedger(app_case.store).read(principal.object_id)['events']
    assert len(events) == 1
    assert events[0]['input_tokens'] == 11
    assert events[0]['output_tokens'] == 7
    assert events[0]['platform_cost_usd'] is None
    revoke(app_case, 'research')
    with pytest.raises(GatewayError) as error:
        app_case.gateway.dispatch(capability.serialize(), method='POST', path='/chat/completions', headers={}, body=body)
    assert error.value.status_code == 403
    assert len(calls) == 1


def test_research_gateway_recheck_after_request_validation(app_case, monkeypatch):
    from tradingagents.workbench import public_gateway

    run_id = str(uuid4())
    principal = app_case.guest.principal
    capability = app_case.gateway.authorize(run_id, principal, app_case.settings.pin(principal, run_id),
                                           expires_at=int(app_case.clock[0]) + 100)
    calls = []
    app_case.gateway._http = httpx.Client(transport=httpx.MockTransport(lambda request: (calls.append(request), httpx.Response(500))[1]))
    validate = public_gateway.validate_gateway_http_client

    def validating(client):
        revoke(app_case, 'research')
        return validate(client)

    monkeypatch.setattr(public_gateway, 'validate_gateway_http_client', validating)
    with pytest.raises(GatewayError) as error:
        app_case.gateway.dispatch(capability.serialize(), method='POST', path='/chat/completions', headers={},
                                  body={'model': 'gpt-5', 'messages': [{'role': 'user', 'content': 'fixture'}], 'max_completion_tokens': 16})
    assert error.value.status_code == 403
    assert calls == []


def test_research_export_requires_permission(app_case):
    revoke(app_case, 'research')
    with pytest.raises(HostedRunsError) as error:
        app_case.runs.export(app_case.guest.session_id, str(uuid4()))
    assert error.value.status_code == 403


def test_market_revocation_during_settings_read_is_not_masked(app_case, monkeypatch):
    market = MarketData(store=app_case.store, settings=app_case.settings)
    calls = []

    def data_key(*args):
        revoke(app_case, 'market')
        return 'fixture-key'

    monkeypatch.setattr(app_case.settings, 'data_key', data_key)
    monkeypatch.setattr(market, '_alpha', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(MarketError) as error:
        market.snapshot(app_case.guest.principal, {'symbols': ['MSFT'], 'selected': 'MSFT', 'range': '1m', 'source': 'alpha_vantage'})
    assert error.value.status_code == 403
    assert calls == []


@pytest.mark.parametrize('logging_failure', [False, True])
def test_managed_assistant_usage_matches_settlement_without_logging_replay(app_case, monkeypatch, logging_failure):
    installation = foundry_installation(deployment='gpt-5.6-sol')
    installation['foundry_models'] = [foundry_installation(deployment='gpt-5.6-luna', model_family='gpt-5.6-luna',
                                                          context_window_tokens=100000, max_output_tokens=4096)['foundry']]
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={'model': 'gpt-5.6-luna', 'model_version': '2026-09-10',
                                         'usage': {'input_tokens': 15, 'output_tokens': 4, 'total_tokens': 19,
                                                   'input_tokens_details': {'cached_tokens': 0},
                                                   'output_tokens_details': {'reasoning_tokens': 0}},
                                         'output': [{'type': 'message', 'role': 'assistant',
                                                     'content': [{'type': 'output_text', 'text': 'fixture answer'}]}]})

    gateway = ProviderGateway(app_case.store, settings=app_case.settings, budget=app_case.budget,
                              installation=installation,
                              managed_credential=SimpleNamespace(get_token=lambda scope: SimpleNamespace(token='fixture-token')),
                              http_client=httpx.Client(transport=httpx.MockTransport(upstream)), clock=lambda: app_case.clock[0])
    if logging_failure:
        write = app_case.store.write

        def fail_usage(key, *args, **kwargs):
            if key.endswith('/usage'):
                raise RuntimeError('usage storage unavailable')
            return write(key, *args, **kwargs)

        monkeypatch.setattr(app_case.store, 'write', fail_usage)
    result = gateway.assistant_reply(app_case.guest.principal, str(uuid4()), [{'role': 'user', 'content': 'fixture'}], context='')
    assert result == 'fixture answer'
    assert len(calls) == 1
    budget = app_case.budget.snapshot()
    assert budget['reserved_microusd'] == 0
    assert budget['spent_microusd'] > 0
    events = UsageLedger(app_case.store).read(app_case.guest.principal.object_id)['events']
    if logging_failure:
        assert events == []
    else:
        assert len(events) == 1
        assert events[0]['kind'] == 'assistant_provider'
        assert events[0]['input_tokens'] == 15
        assert events[0]['output_tokens'] == 4
        assert events[0]['platform_cost_usd'] == budget['spent_microusd'] / 1000000
