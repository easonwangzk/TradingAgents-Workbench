# Modified for public distribution: installation identifiers replaced with examples.
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from tests.test_public_accounts import GUEST_PASSWORD, ORIGIN, OWNER_PASSWORD
from tests.test_public_store import MemoryBackend
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.workbench.public_store import EncryptedStore

FIXTURE_NOW = 1800000000


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, events=None, error=None):
        self._chunks = list(chunks)
        self._events = [] if events is None else events
        self._error = error

    def __iter__(self):
        for chunk in self._chunks:
            self._events.append(("chunk", len(chunk)))
            yield chunk
        if self._error is not None:
            raise self._error

    def close(self):
        self._events.append(("close",))


def foundry_installation(
    *,
    foundry_enabled=True,
    deployment="deployment-prod-1",
    api_version="2025-03-01-preview",
    model_family="gpt-5.6-sol",
    model_version="2026-09-10",
    context_window_tokens=32,
    max_output_tokens=32,
    input_rate=10_000_000,
    cached_input_rate=3_000_000,
    output_rate=20_000_000,
    verification_status="verified",
):
    if not foundry_enabled:
        return {"foundry": None}
    return {
        "foundry": {
            "endpoint": "https://example-foundry.services.ai.azure.com/openai/v1/",
            "deployment": deployment,
            "api_version": api_version,
            "model_family": model_family,
            "model_version": model_version,
            "context_window_tokens": context_window_tokens,
            "max_output_tokens": max_output_tokens,
            "pricing": {
                "version": "fixture-v2",
                "input_microusd_per_million_tokens": input_rate,
                "cached_input_microusd_per_million_tokens": cached_input_rate,
                "output_microusd_per_million_tokens": output_rate,
            },
            "verification": {
                "status": verification_status,
                "evidence": "fixture-contract-v1",
                "recorded_at": "2026-09-10",
            },
        }
    }


def test_managed_model_expiry_accepts_utc_suffix_on_python310(monkeypatch):
    from datetime import datetime

    from tradingagents.workbench import public_gateway
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_settings import UserSettings

    class Python310DateTime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            if value.endswith("Z"):
                raise ValueError("Python 3.10 requires an explicit UTC offset")
            return super().fromisoformat(value)

    monkeypatch.setattr(public_gateway, "datetime", Python310DateTime)
    installation = foundry_installation()
    installation["foundry"]["verification"]["valid_until"] = "2099-01-01T00:00:00Z"
    now = FIXTURE_NOW
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    gateway = public_gateway.ProviderGateway(
        store, settings=UserSettings(store), budget=BudgetLedger(store, clock=lambda: now),
        managed_credential=None, installation=installation, clock=lambda: now,
    )
    assert gateway.managed_identity_available()
    now = datetime.fromisoformat("2099-01-01T00:00:00+00:00").timestamp()
    assert not gateway.managed_identity_available()


def _json_request_body(request: httpx.Request):
    content = request.content
    if isinstance(content, bytes):
        return json.loads(content.decode("utf-8"))
    return json.loads(content)


def test_managed_identity_catalog_includes_verified_additional_deployments():
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    installation = foundry_installation(deployment="gpt-5.6-sol")
    installation["foundry_models"] = [
        foundry_installation(deployment=name, model_family=name)["foundry"]
        for name in ("gpt-5.6-luna", "gpt-5.6-terra")
    ]
    gateway = ProviderGateway(
        store, settings=UserSettings(store), budget=BudgetLedger(store),
        managed_credential=None, installation=installation,
    )
    assert [model["deployment"] for model in gateway.managed_identity_configurations()] == [
        "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    ]
    assert gateway.managed_identity_configuration()["deployment"] == "gpt-5.6-sol"


def test_research_assistant_gateway_pins_luna_disables_tools_and_settles_budget():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invite = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invite.token, invite.email, GUEST_PASSWORD)
    settings = UserSettings(store)
    installation = foundry_installation(deployment="gpt-5.6-sol", model_family="gpt-5.6-sol")
    luna = foundry_installation(
        deployment="gpt-5.6-luna", model_family="gpt-5.6-luna",
        context_window_tokens=1_000_000, max_output_tokens=4096,
    )["foundry"]
    installation["foundry_models"] = [luna]
    budget = BudgetLedger(store)

    class Credential:
        def get_token(self, scope):
            assert scope == "https://cognitiveservices.azure.com/.default"
            return type("Token", (), {"token": "fixture-managed-token"})()

    outbound = []

    def upstream(request):
        body = _json_request_body(request)
        outbound.append(body)
        assert budget.snapshot()["reserved_microusd"] < 100_000
        return httpx.Response(200, json={
            "model": "gpt-5.6-luna",
            "model_version": "2026-09-10",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "历史报告记录 MSFT 市场上涨 2%。"}]}],
            "usage": {
                "input_tokens": 40, "output_tokens": 12, "total_tokens": 52,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        })

    gateway = ProviderGateway(
        store, settings=settings, budget=budget, managed_credential=Credential(),
        installation=installation, http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    assert gateway.assistant_available() is True
    answer = gateway.assistant_reply(
        guest.principal,
        str(uuid4()),
        [{"role": "user", "content": "What did MSFT history report?"}],
        context="RUN 11111111-1111-4111-8111-111111111111 | MSFT market rose 2 percent.",
    )

    assert answer == "历史报告记录 MSFT 市场上涨 2%。"
    assert len(outbound) == 1
    assert outbound[0]["model"] == "gpt-5.6-luna"
    assert "tools" not in outbound[0]
    assert outbound[0]["input"][0]["role"] == "developer"
    assert "RUN 11111111-1111-4111-8111-111111111111" not in outbound[0]["input"][0]["content"]
    assert outbound[0]["input"][1]["role"] == "user"
    assert "RUN 11111111-1111-4111-8111-111111111111" in outbound[0]["input"][1]["content"]
    assert "What did MSFT history report?" in outbound[0]["input"][1]["content"]
    assert budget.snapshot()["spent_microusd"] > 0
    assert budget.snapshot()["reserved_microusd"] == 0


def test_research_assistant_gateway_rejects_missing_luna_without_http_or_budget():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invite = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invite.token, invite.email, GUEST_PASSWORD)
    budget = BudgetLedger(store)
    requests = []
    gateway = ProviderGateway(
        store, settings=UserSettings(store), budget=budget, managed_credential=None,
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request))),
    )
    assert gateway.assistant_available() is False
    with pytest.raises(GatewayError) as denied:
        gateway.assistant_reply(guest.principal, str(uuid4()), [{"role": "user", "content": "hi"}], context="")
    assert denied.value.status_code == 409
    with pytest.raises(GatewayError) as unavailable:
        gateway.assistant_reply(owner.principal, str(uuid4()), [{"role": "user", "content": "hi"}], context="")
    assert unavailable.value.status_code == 409
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    with pytest.raises(GatewayError) as disabled:
        gateway.assistant_reply(guest.principal, str(uuid4()), [{"role": "user", "content": "hi"}], context="")
    assert disabled.value.status_code == 403
    assert requests == []
    assert budget.snapshot()["reserved_microusd"] == 0


@pytest.mark.parametrize("wrong_response_model", [False, True])
def test_selected_managed_models_route_and_settle_their_own_prices(wrong_response_model):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    owner = EmailAuth(store, origin=ORIGIN).bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity", "managed_models": {
        "quick_model": "gpt-5.6-luna", "deep_model": "gpt-5.6-terra",
    }}, expected_version=None)
    installation = foundry_installation(deployment="gpt-5.6-sol")
    installation["foundry_models"] = []
    rates = {"gpt-5.6-luna": (400_000, 1_800_000), "gpt-5.6-terra": (4_000_000, 18_000_000)}
    for name, (input_rate, output_rate) in rates.items():
        model = foundry_installation(deployment=name, model_family=name, input_rate=input_rate,
                                     cached_input_rate=input_rate, output_rate=output_rate)["foundry"]
        model["pricing"]["version"] = name.replace(".", "") + "-fixture"
        installation["foundry_models"].append(model)
    calls = []
    tokens = []
    budget = BudgetLedger(store)

    class Credential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "fixture-token"})()

    def upstream(request):
        body = _json_request_body(request)
        calls.append(body["model"])
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        input_rate, output_rate = rates[body["model"]]
        assert budget.snapshot()["reserved_microusd"] == (
            (32 * input_rate + 999_999) // 1_000_000 + (2 * output_rate + 999_999) // 1_000_000
        )
        return httpx.Response(200, json={"model": "gpt-5.6-sol" if wrong_response_model else body["model"], "usage": {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }})

    gateway = ProviderGateway(store, settings=settings, budget=budget, managed_credential=Credential(),
                              installation=installation, http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=int(gateway._clock()) + 60)
    worker = gateway.worker_config(capability)
    assert worker["quick_model"] == "gpt-5.6-luna"
    assert worker["deep_model"] == "gpt-5.6-terra"
    if wrong_response_model:
        with pytest.raises(GatewayError):
            gateway.dispatch(capability, method="POST", path="/responses", headers={}, body={
                "model": "gpt-5.6-luna", "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 2,
            })
        assert budget.snapshot()["spent_microusd"] == 0
        assert budget.snapshot()["reserved_microusd"] == 17
        assert len(calls) == len(tokens) == 1
        return
    for name in rates:
        gateway.dispatch(capability, method="POST", path="/responses", headers={}, body={
            "model": name, "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 2,
        })
    assert budget.snapshot()["spent_microusd"] == 84
    assert budget.snapshot()["reserved_microusd"] == 0
    with pytest.raises(GatewayError):
        gateway.dispatch(capability, method="POST", path="/responses", headers={}, body={
            "model": "gpt-5.6-sol", "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 2,
        })
    assert len(calls) == len(tokens) == 2


@pytest.mark.parametrize("offset,available", [(-1, True), (0, False)])
def test_owner_promotional_price_expires_before_new_admission(offset, available):
    from datetime import datetime, timezone

    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    now = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp() + offset
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: now)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    installation = foundry_installation(model_version="2026-07-09")
    installation["foundry"]["pricing"]["version"] = "gpt56sol-20260901-upper-bound"
    budget = BudgetLedger(store, clock=lambda: now)
    gateway = ProviderGateway(
        store, settings=settings, budget=budget, managed_credential=None,
        installation=installation, clock=lambda: now,
    )
    assert gateway.managed_identity_available() is available
    assert gateway.managed_identity_configuration() == ({
        "deployment": "deployment-prod-1",
        "endpoint": "https://example-foundry.services.ai.azure.com/openai/v1/",
        "model_family": "gpt-5.6-sol",
        "model_version": "2026-07-09",
    } if available else None)
    run_id = str(uuid4())
    snapshot = settings.pin(owner.principal, run_id)
    if available:
        gateway.admission_ready(owner.principal, snapshot)
    else:
        with pytest.raises(GatewayError) as failure:
            gateway.authorize(run_id, owner.principal, snapshot, expires_at=int(now) + 3600)
        assert failure.value.status_code == 409
    assert budget.snapshot()["reserved_microusd"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("additional", [False, True])
@pytest.mark.parametrize(
    "direct,expired_before_dispatch",
    [(False, False), (True, False), (True, True)],
    ids=["dispatch-crosses-deadline", "direct-crosses-deadline", "direct-already-expired"],
)
def test_owner_promotional_price_expiry_blocks_foundry_dispatch(direct, expired_before_dispatch, additional):
    from datetime import datetime, timezone

    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import LEDGER_KEY, BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    deadline = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
    now = deadline - 1
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: now)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: now)
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            nonlocal now
            tokens.append(scope)
            assert budget.snapshot("2026-11")["reserved_microusd"] > 0
            now = deadline
            return type("Token", (), {"token": "managed-secret-token"})()

    installation = foundry_installation(model_version="2026-07-09")
    if additional:
        model = foundry_installation(deployment="gpt-5.6-luna", model_family="gpt-5.6-luna")["foundry"]
        model["verification"]["valid_until"] = "2026-12-01T00:00:00Z"
        installation["foundry_models"] = [model]
        settings.update(owner.principal, {"managed_models": {
            "quick_model": "gpt-5.6-luna", "deep_model": "gpt-5.6-luna",
        }}, expected_version=settings.get(owner.principal)["version"])
    else:
        installation["foundry"]["pricing"]["version"] = "gpt56sol-20260901-upper-bound"
    gateway = ProviderGateway(
        store, settings=settings, budget=budget, managed_credential=ManagedCredential(),
        installation=installation, clock=lambda: now,
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500),
        )),
    )
    run_id = str(uuid4())
    snapshot = settings.pin(owner.principal, run_id)
    capability = gateway.authorize(run_id, owner.principal, snapshot, expires_at=int(now) + 3600)
    resolved = settings.resolve(owner.principal, snapshot)
    if expired_before_dispatch:
        now = deadline
    body = {"input": [{"role": "user", "content": "hi"}], "max_output_tokens": 1}

    with (
        patch.object(budget, "cancel", wraps=budget.cancel) as cancel,
        pytest.raises(GatewayError) as failure,
    ):
        if direct:
            gateway._dispatch_foundry(run_id, resolved, "POST", "/responses", {}, body)
        else:
            gateway.dispatch(capability, method="POST", path="/responses", headers={}, body=body)

    assert requests == []
    assert failure.value.status_code == 409
    assert str(failure.value) == ("Selected managed model is unavailable." if additional and expired_before_dispatch
                                  else "Provider mode is not available yet.")
    assert "managed-secret-token" not in str(failure.value)
    assert len(tokens) == (0 if expired_before_dispatch else 1)
    assert cancel.call_count == (0 if expired_before_dispatch else 1)
    if expired_before_dispatch:
        assert store.read(LEDGER_KEY) is None
    else:
        record, _ = store.read(LEDGER_KEY)
        assert len(record["calls"]) == 1
        call = next(iter(record["calls"].values()))
        assert call["month"] == "2026-11"
        assert call["state"] == "cancelled"
        assert call["amount_microusd"] > 0
    for month in ("2026-11", "2026-12"):
        snapshot = budget.snapshot(month)
        assert snapshot["reserved_microusd"] == 0
        assert snapshot["spent_microusd"] == 0


def _gateway_delegate_transport(gateway, capability):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in {"/v1/responses", "/v1/responses/parse"}:
            path = "/responses"
        result = gateway.dispatch(
            capability,
            method=request.method,
            path=path,
            headers={},
            body=_json_request_body(request),
        )
        return httpx.Response(result["status_code"], json=result["body"])

    return httpx.MockTransport(handler)


def test_example_model_contracts_are_not_operator_verified():
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parents[1] / "config/owner-foundry.json").read_text())
    assert config["environment"]["TAWB_FOUNDRY_VERIFICATION_STATUS"] == "pending"
    assert all(model["verification"]["status"] == "pending" for model in config["additional_models"])


def test_verified_example_configuration_reserves_conservative_full_context_bound():
    from pathlib import Path

    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_runtime import _foundry_installation
    from tradingagents.workbench.public_settings import UserSettings

    config = json.loads((Path(__file__).resolve().parents[1] / "config/owner-foundry.json").read_text())
    environment = {**config["environment"], "TAWB_FOUNDRY_VERIFICATION_STATUS": "verified"}
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    budget = BudgetLedger(store)
    gateway = ProviderGateway(
        store, settings=UserSettings(store), budget=budget, managed_credential=None,
        installation=_foundry_installation(environment),
    )
    request, _, reservation = gateway._prepare_foundry_request(
        str(uuid4()), None, {"input": [{"role": "user", "content": "hi"}], "max_output_tokens": 128000},
    )
    assert reservation.amount_microusd == 12_240_000
    assert request["prompt_cache_options"] == {"mode": "explicit"}
    assert budget.snapshot()["available_microusd"] == 287_760_000


def test_example_additional_model_contracts_use_verified_bounds_and_expire():
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_runtime import _foundry_installation
    from tradingagents.workbench.public_settings import UserSettings

    config = json.loads((Path(__file__).resolve().parents[1] / "config/owner-foundry.json").read_text())
    for model in config["additional_models"]:
        model["verification"]["status"] = "verified"
    environment = {**config["environment"], "TAWB_FOUNDRY_VERIFICATION_STATUS": "verified",
                   "TAWB_FOUNDRY_MODELS_JSON": json.dumps(config["additional_models"])}
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    budget = BudgetLedger(store, clock=lambda: now)
    gateway = ProviderGateway(store, settings=UserSettings(store), budget=budget, managed_credential=None,
                              installation=_foundry_installation(environment), clock=lambda: now)
    resolved = SimpleNamespace(quick_model="gpt-5.6-luna", deep_model="gpt-5.6-terra")
    for deployment, amount in [("gpt-5.6-luna", 650_400), ("gpt-5.6-terra", 6_504_000)]:
        request, _, reservation = gateway._prepare_foundry_request(str(uuid4()), resolved, {
            "model": deployment, "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 128000,
        })
        assert request["model"] == deployment
        assert request["prompt_cache_options"] == {"mode": "explicit"}
        assert reservation.amount_microusd == amount
        budget.cancel(reservation)
    now = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    assert gateway.managed_identity_configurations() == []
    with pytest.raises(GatewayError):
        gateway._prepare_foundry_request(str(uuid4()), resolved, {
            "model": "gpt-5.6-luna", "input": [{"role": "user", "content": "hi"}], "max_output_tokens": 16,
        })


class _GatewayDataBridgeResponse:
    def __init__(self, result):
        self.status_code = result["status_code"]
        self._body = result["body"]
        self.text = self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"gateway returned {self.status_code}")


def _gateway_query_params(request: httpx.Request):
    return dict(request.url.params.multi_items())


def _make_data_gateway(*, data_keys=None, data_vendors=None, asset_type=None, mode="api_key", upstream=None):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    if mode == "managed_identity":
        payload = {"mode": "managed_identity"}
    else:
        payload = {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-model-key",
        }
    if data_keys is not None:
        payload["data_keys"] = data_keys
    if data_vendors is not None:
        payload["data_vendors"] = data_vendors
    if asset_type is not None:
        payload["asset_type"] = asset_type
    settings.update(owner.principal, payload, expected_version=None)

    upstream_requests = []

    def capture(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if upstream is None:
            return httpx.Response(200, json={"ok": True})
        return upstream(request)

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    return {"gateway": gateway, "capability": capability, "requests": upstream_requests}


def _bridge_alpha_vantage_requests_get(gateway, capability):
    def fake_get(url, params=None, **_kwargs):
        payload = dict(params or {})
        function_name = payload.pop("function", None)
        payload.pop("apikey", None)
        payload.pop("source", None)
        result = gateway.dispatch(
            capability,
            method="POST",
            path="/data/alpha_vantage",
            headers={},
            body={"function": function_name, "params": payload},
        )
        return _GatewayDataBridgeResponse(result)

    return fake_get


def _bridge_fred_requests_get(gateway, capability):
    def fake_get(url, params=None, **_kwargs):
        payload = dict(params or {})
        payload.pop("api_key", None)
        payload.pop("file_type", None)
        parsed = httpx.URL(url)
        if not parsed.path.startswith("/fred/"):
            raise AssertionError(f"unexpected FRED URL: {url}")
        result = gateway.dispatch(
            capability,
            method="POST",
            path="/data/fred" + parsed.path.removeprefix("/fred"),
            headers={},
            body={"params": payload},
        )
        return _GatewayDataBridgeResponse(result)

    return fake_get


def capture_openai_responses_sdk_payload(*, langchain=False, structured=False):
    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        text = '{"answer":"ok"}' if structured else "ok"
        records.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": {
                    name.lower(): value
                    for name, value in request.headers.items()
                    if name.lower() in {"authorization", "accept", "content-type"}
                },
                "body": _json_request_body(request),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "model": "gpt-5",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    if langchain:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model="gpt-5",
            api_key="dummy-openai",
            max_retries=0,
            use_responses_api=True,
            http_client=client,
        )
        if structured:
            from pydantic import BaseModel

            class Answer(BaseModel):
                answer: str

            llm.with_structured_output(Answer, method="json_schema").invoke("hi")
        else:
            llm.bind_tools(
                [
                    {
                        "type": "function",
                        "name": "lookup",
                        "description": "Find quote",
                        "parameters": {
                            "type": "object",
                            "properties": {"ticker": {"type": "string"}},
                            "required": ["ticker"],
                        },
                        "strict": True,
                    }
                ]
            ).invoke(
                [
                    HumanMessage(content="hi"),
                    AIMessage(
                        content="",
                        additional_kwargs={
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"ticker": "MSFT"}'},
                                }
                            ]
                        },
                    ),
                    ToolMessage(content="done", tool_call_id="call_1"),
                ]
            )
    else:
        from openai import OpenAI

        sdk = OpenAI(api_key="dummy-openai", max_retries=0, http_client=client)
        sdk.responses.create(
            model="gpt-5",
            input=[
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call_output", "call_id": "call_1", "output": "done"},
            ],
            max_output_tokens=7,
            tools=[
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Find quote",
                    "parameters": {
                        "type": "object",
                        "properties": {"ticker": {"type": "string"}},
                        "required": ["ticker"],
                    },
                    "strict": True,
                }
            ],
        )
    assert len(records) == 1
    return records[0]


def capture_anthropic_sdk_payload():
    from anthropic import Anthropic

    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        records.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": {
                    name.lower(): value
                    for name, value in request.headers.items()
                    if name.lower() in {"x-api-key", "accept", "content-type", "anthropic-version"}
                },
                "body": _json_request_body(request),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    Anthropic(
        api_key="dummy-anthropic",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).messages.create(
        model="claude-sonnet-5",
        max_tokens=6,
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "name": "lookup",
                "description": "Find quote",
                "input_schema": {
                    "type": "object",
                    "properties": {"ticker": {"type": "string"}},
                },
            }
        ],
    )
    assert len(records) == 1
    return records[0]


def capture_google_sdk_payload():
    from google.genai import Client as GoogleClient, types as google_types

    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        records.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": {
                    name.lower(): value
                    for name, value in request.headers.items()
                    if name.lower() in {"x-goog-api-key", "accept", "content-type"}
                },
                "body": _json_request_body(request),
            }
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
                "modelVersion": "gemini-3.5-flash",
            },
        )

    http_options = google_types.HttpOptions(
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        client_args={"transport": httpx.MockTransport(handler)},
    )
    sdk = GoogleClient(api_key="dummy-google", http_options=http_options)
    sdk.models.generate_content(
        model="gemini-3.5-flash",
        contents="hi",
        config=google_types.GenerateContentConfig(
            max_output_tokens=7,
            tools=[
                google_types.Tool(
                    function_declarations=[
                        google_types.FunctionDeclaration(
                            name="lookup",
                            description="Find quote",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"ticker": {"type": "string"}},
                                "required": ["ticker"],
                            },
                        )
                    ]
                )
            ],
            response_mime_type="application/json",
            response_json_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
    )
    assert len(records) == 1
    return records[0]


def capture_azure_sdk_payload():
    from openai import AzureOpenAI

    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        records.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": {
                    name.lower(): value
                    for name, value in request.headers.items()
                    if name.lower() in {"api-key", "accept", "content-type"}
                },
                "body": _json_request_body(request),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "model": "gpt-4.1-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    AzureOpenAI(
        api_key="dummy-azure",
        api_version="2025-03-01-preview",
        azure_endpoint="https://fixture.openai.azure.com",
        azure_deployment="quick-deploy",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).chat.completions.create(
        model="ignored-by-sdk",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=4,
    )
    assert len(records) == 1
    return records[0]


def capture_openai_chat_sdk_payload():
    from openai import OpenAI

    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        records.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": {
                    name.lower(): value
                    for name, value in request.headers.items()
                    if name.lower() in {"authorization", "accept", "content-type"}
                },
                "body": _json_request_body(request),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "model": "gpt-4.1-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    OpenAI(
        api_key="dummy-openai",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=4,
    )
    assert len(records) == 1
    return records[0]


@pytest.mark.unit
def test_budget_denial_happens_before_any_managed_identity_or_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetError, BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    budget.reserve(
        str(uuid4()),
        run_id=str(uuid4()),
        amount_microusd=300_000_000,
        price_version="fixture-v1",
    )
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "resp_123", "usage": {"input_tokens": 1, "output_tokens": 1}})

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(input_rate=1_000_000, cached_input_rate=1_000_000, output_rate=1_000_000),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(
        run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600
    )

    with pytest.raises(BudgetError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={
                "model": "gpt-5.6-sol",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "max_output_tokens": 1,
            },
        )

    assert requests == []

    assert tokens == []


@pytest.mark.unit
def test_gateway_rejects_regular_httpx_clients_without_pinned_or_mock_transport():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)

    with pytest.raises(GatewayError):
        ProviderGateway(
            store,
            settings=settings,
            budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
            managed_credential=object(),
            installation=foundry_installation(),
            http_client=httpx.Client(),
            clock=lambda: FIXTURE_NOW,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "client",
    [
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), auth=("user", "pass")),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), headers={"x-leak": "1"}),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), params={"leak": "1"}),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), base_url="https://api.example.com"),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), follow_redirects=True),
    ],
)
def test_gateway_rejects_mock_clients_with_unsafe_default_state(client):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)

    with pytest.raises(GatewayError):
        ProviderGateway(
            store,
            settings=settings,
            budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
            managed_credential=object(),
            installation=foundry_installation(),
            http_client=client,
            clock=lambda: FIXTURE_NOW,
        )

    client.close()


@pytest.mark.unit
def test_dispatch_rejects_stale_settings_snapshot_before_any_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    saved = settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "grok-4-fast-reasoning",
            "deep_model": "grok-4-fast-reasoning",
            "api_key": "fixture-user-key",
        },
        expected_version=None,
    )
    run_id = str(uuid4())
    snapshot = settings.pin(owner.principal, run_id)
    settings.update(owner.principal, {"api_key": "fixture-rotated-key"}, expected_version=saved["version"])
    requests = []

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))),
        clock=lambda: FIXTURE_NOW,
    )
    with pytest.raises(GatewayError):
        gateway.authorize(run_id, owner.principal, snapshot, expires_at=1800003600)

    assert requests == []


OPENAI_COMPATIBLE_KEY_PROVIDERS = [
    name for name, env in PROVIDER_API_KEY_ENV.items() if env is not None and name not in {"azure", "anthropic", "google"}
]


@pytest.mark.unit
@pytest.mark.parametrize("provider", OPENAI_COMPATIBLE_KEY_PROVIDERS)
def test_api_key_dispatch_uses_only_user_key_for_openai_compatible_family(provider):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    payload = {
        "mode": "api_key",
        "provider": provider,
        "quick_model": "fixture-quick",
        "deep_model": "fixture-deep",
        "api_key": f"fixture-{provider}-key",
    }
    if provider == "openai_compatible":
        payload["base_url"] = "https://relay.example.com/v1"
    elif provider == "openai":
        payload["quick_model"] = payload["deep_model"] = "gpt-4.1-mini"
    settings.update(owner.principal, payload, expected_version=None)
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "upstream rejected key fixture"}})

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(
        run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600
    )

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={
                "model": payload["deep_model"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4,
            },
        )

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer fixture-{provider}-key"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert "fixture" not in str(failure.value)
    assert tokens == []


@pytest.mark.unit
@pytest.mark.parametrize("model_identity,accepted", [
    ({"model": "gpt-5.6-sol", "model_version": "2026-09-10"}, True),
    ({"model": "gpt-5.6-sol-2026-09-10"}, True),
    ({}, False),
    ({"model": None, "model_version": None}, False),
    ({"model": "gpt-5.6-sol"}, True),
    ({"model": "gpt-5.6-sol", "model_version": None}, True),
    ({"model": "other-model"}, False),
    ({"model": "gpt-5.6-sol", "model_version": ""}, False),
    ({"model": "gpt-5.6-sol", "model_version": False}, False),
    ({"model_version": "2026-09-10"}, False),
    ({"model": "gpt-5.6-sol", "model_version": "2026-08-01"}, False),
    ({"model": "gpt-5.6-sol-2026-08-01"}, False),
    ({"model": "gpt-5.6-sol-2026-09-10", "model_version": "2026-08-01"}, False),
])
def test_managed_identity_dispatch_pins_deployment_and_settles_usage_without_retry(model_identity, accepted):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params:
            return httpx.Response(400, json={"error": {"message": "API version not supported"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_ok",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "total_tokens": 6,
                    "input_tokens_details": {"cached_tokens": 1},
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
                **model_identity,
                "output": [{"type": "output_text", "text": "ok"}],
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(api_version="2025-04-01-preview", max_output_tokens=7),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(
        run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600
    )

    if not accepted:
        with pytest.raises(GatewayError) as failure:
            gateway.dispatch(
                capability, method="POST", path="/responses", headers={},
                body={"input": [{"role": "user", "content": "hi"}], "max_output_tokens": 7},
            )
        assert failure.value.status_code == 502
        assert len(requests) == 1
        assert budget.snapshot()["spent_microusd"] == 0
        assert budget.snapshot()["reserved_microusd"] == 460
        return

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/responses",
        headers={},
        body={
            "model": "gpt-5.6-sol",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "max_output_tokens": 7,
        },
    )

    assert result["status_code"] == 200
    assert len(requests) == 1
    assert requests[0].url == "https://example-foundry.services.ai.azure.com/openai/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer managed-secret-token"
    assert requests[0].read().decode("utf-8") == (
        '{"input":[{"role":"user","content":[{"type":"input_text","text":"hi"}]}],'
        '"max_output_tokens":7,"model":"deployment-prod-1","prompt_cache_options":{"mode":"explicit"}}'
    )
    assert tokens == ["https://cognitiveservices.azure.com/.default"]
    assert budget.snapshot()["spent_microusd"] == 73
    assert budget.snapshot()["reserved_microusd"] == 0


@pytest.mark.unit
def test_dispatch_rechecks_current_account_state_before_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invitation = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invitation.token, invitation.email, "fixture-guest-password-2026")
    settings = UserSettings(store)
    settings.update(
        guest.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-guest-key",
        },
        expected_version=None,
    )
    requests = []

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(
        run_id, guest.principal, settings.pin(guest.principal, run_id), expires_at=1800003600
    )
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    assert requests == []


@pytest.mark.unit
def test_dispatch_revalidates_mutated_http_client_before_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-xai-key",
        },
        expected_version=None,
    )
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True})))
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=client,
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    client.event_hooks["request"].append(lambda request: None)

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    assert failure.value.status_code == 503
    assert requests == []
    client.close()


@pytest.mark.unit
def test_foundry_rejects_oversized_serialized_request_before_budget_or_credential_use():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(context_window_tokens=256, max_output_tokens=16),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "x" * (1024 * 1024)}]}],
                "max_output_tokens": 1,
            },
        )

    assert requests == []
    assert tokens == []
    assert budget.snapshot()["reserved_microusd"] == 0
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
def test_foundry_rejects_oversized_content_length_without_reading_stream_and_keeps_reserve():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    events = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": str(8 * 1024 * 1024 + 1),
            },
            stream=ChunkStream([b'{"usage": {}}'], events=events),
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 1},
        )

    assert events == [("close",)]
    assert budget.snapshot()["reserved_microusd"] > 0
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
def test_foundry_rejects_chunked_oversize_timeout_and_redirect_without_leaking_or_settling():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    scenarios = [
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ChunkStream([b"x" * (8 * 1024 * 1024), b"x"]),
        ),
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ChunkStream([], error=httpx.ReadTimeout("fixture timeout")),
        ),
        httpx.Response(302, headers={"location": "https://evil.example.com/next", "content-type": "application/json"}, json={"error": "fixture-redirect"}),
    ]

    for response in scenarios:
        budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
        gateway = ProviderGateway(
            store,
            settings=settings,
            budget=budget,
            managed_credential=ManagedCredential(),
            installation=foundry_installation(),
            http_client=httpx.Client(transport=httpx.MockTransport(lambda request, response=response: response)),
            clock=lambda: FIXTURE_NOW,
        )
        run_id = str(uuid4())
        capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

        with pytest.raises(GatewayError) as failure:
            gateway.dispatch(
                capability,
                method="POST",
                path="/responses",
                headers={},
                body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 1},
            )

        assert "fixture timeout" not in str(failure.value)
        assert budget.snapshot()["reserved_microusd"] > 0
        assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "application/json", "content-encoding": "gzip"},
        {"content-type": "text/plain"},
    ],
)
def test_openai_compatible_rejects_unsupported_success_content_encoding_and_type(headers):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {"mode": "api_key", "provider": "xai", "quick_model": "fixture-quick", "deep_model": "fixture-deep", "api_key": "fixture-xai-key"},
        expected_version=None,
    )
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers=headers,
                    stream=ChunkStream([b'{"ok":true}']),
                )
            )
        ),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )


@pytest.mark.unit
def test_openai_compatible_rejects_invalid_json_and_redacts_success_body_key_echo():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {"mode": "api_key", "provider": "xai", "quick_model": "fixture-quick", "deep_model": "fixture-deep", "api_key": "fixture-xai-key"},
        expected_version=None,
    )

    invalid_gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=b"not-json"))),
        clock=lambda: FIXTURE_NOW,
    )
    invalid_run_id = str(uuid4())
    invalid_capability = invalid_gateway.authorize(
        invalid_run_id, owner.principal, settings.pin(owner.principal, invalid_run_id), expires_at=1800003600
    )
    with pytest.raises(GatewayError):
        invalid_gateway.dispatch(
            invalid_capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json={"output": {"nested": ["fixture-xai-key", {"value": "fixture-xai-key"}]}}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/chat/completions",
        headers={},
        body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
    )

    assert result["body"] == {"output": {"nested": ["[redacted]", {"value": "[redacted]"}]}}


@pytest.mark.unit
def test_openai_compatible_rejects_oversized_cached_response_fixture():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {"mode": "api_key", "provider": "xai", "quick_model": "fixture-quick", "deep_model": "fixture-deep", "api_key": "fixture-xai-key"},
        expected_version=None,
    )
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=b"x" * (8 * 1024 * 1024 + 1),
                )
            )
        ),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )


@pytest.mark.unit
def test_authorize_returns_hidden_capability_and_rejects_duplicate_or_disabled_identity():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    guest_token = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(guest_token.token, guest_token.email, "fixture-guest-password-2026")
    settings = UserSettings(store)
    settings.update(
        guest.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-guest-key",
        },
        expected_version=None,
    )
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    snapshot = settings.pin(guest.principal, run_id)

    capability = gateway.authorize(run_id, guest.principal, snapshot, expires_at=1800003600)

    assert "fixture-guest-key" not in repr(capability)
    assert "guest-password" not in repr(capability)
    with pytest.raises(GatewayError):
        gateway.authorize(run_id, guest.principal, snapshot, expires_at=1800003600)

    disabled_run_id = str(uuid4())
    disabled_snapshot = settings.pin(guest.principal, disabled_run_id)
    auth.disable(owner.session_id, guest.principal.object_id,
                 expected_version=auth.accounts(owner.session_id)['version'])
    with pytest.raises(GatewayError):
        gateway.authorize(disabled_run_id, guest.principal, disabled_snapshot, expires_at=1800003600)


@pytest.mark.unit
def test_authorize_rejects_unavailable_managed_identity_before_grant_write():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())

    with pytest.raises(GatewayError) as caught:
        gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    assert caught.value.status_code == 409
    assert store.read("email-v1/gateway/runs/" + run_id) is None


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["disable", "reset", "change_password"])
def test_dispatch_rechecks_account_generation_after_authorization(mutation):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invitation = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invitation.token, invitation.email, "fixture-guest-password-2026")
    settings = UserSettings(store)
    settings.update(
        guest.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-guest-key",
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(
        run_id,
        guest.principal,
        settings.pin(guest.principal, run_id),
        expires_at=1800003600,
    )

    if mutation == "disable":
        auth.disable(owner.session_id, guest.principal.object_id,
                     expected_version=auth.accounts(owner.session_id)['version'])
    elif mutation == "reset":
        recovery = auth.recovery(owner.session_id, guest.principal.object_id)
        auth.reset(recovery.token, guest.principal.email, "fixture-reset-password-2026")
    else:
        auth.change_password(guest.session_id, "fixture-guest-password-2026", "fixture-reset-password-2026")

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    assert requests == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("installation", "body"),
    [
        (foundry_installation(foundry_enabled=False), {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4}),
        (foundry_installation(), {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4, "previous_response_id": "resp_remote"}),
        (foundry_installation(), {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4, "priority": "high"}),
        (foundry_installation(), {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4, "tools": [{"type": "web_search"}]}),
        (foundry_installation(), {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.com/img.png"}]}], "max_output_tokens": 4}),
    ],
)
def test_managed_identity_rejects_disabled_unverified_or_billable_request_shapes_before_dispatch(installation, body):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=installation,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())

    if installation["foundry"] is None:
        with pytest.raises(GatewayError) as caught:
            gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
        assert caught.value.status_code == 409
        assert store.read("email-v1/gateway/runs/" + run_id) is None
        assert requests == []
        assert tokens == []
        assert budget.snapshot()["reserved_microusd"] == 0
        assert budget.snapshot()["spent_microusd"] == 0
        return

    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(capability, method="POST", path="/responses", headers={}, body=body)

    assert requests == []
    assert tokens == []
    assert budget.snapshot()["reserved_microusd"] == 0
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
def test_managed_identity_requires_explicit_verified_installation_contract():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)

    with pytest.raises(GatewayError):
        ProviderGateway(
            store,
            settings=settings,
            budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
            managed_credential=object(),
            installation=foundry_installation(verification_status="pending"),
            http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
            clock=lambda: FIXTURE_NOW,
        )


@pytest.mark.unit
def test_token_failure_cancels_only_undispatched_foundry_reservation():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []

    class ManagedCredential:
        def get_token(self, scope):
            raise RuntimeError(f"private token failure for {scope}")

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(context_window_tokens=11, max_output_tokens=5, input_rate=1_000_001, output_rate=2_000_001),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 5},
        )

    assert "private token failure" not in str(failure.value)
    assert requests == []
    assert budget.snapshot()["reserved_microusd"] == 0
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("usage", "expected_reserved"),
    [
        (None, 12),
        ({"input_tokens": 3, "output_tokens": 2}, 12),
        ({"input_tokens": 3, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 1}}, 12),
        ({"input_tokens": 3, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 1}, "output_tokens_details": {"reasoning_tokens": 3}}, 12),
        ({"input_tokens": 3, "output_tokens": 2, "cached_input_tokens": 1, "reasoning_tokens": 1}, 12),
    ],
)
def test_unknown_or_malformed_usage_keeps_full_dispatched_reservation(usage, expected_reserved):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request):
        requests.append(request)
        body = {"id": "resp_partial", "output": [{"type": "output_text", "text": "ok"}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(
            context_window_tokens=10,
            max_output_tokens=4,
            input_rate=1_000_000,
            cached_input_rate=1_000_000,
            output_rate=500_000,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4},
        )

    assert len(requests) == 1
    assert budget.snapshot()["reserved_microusd"] == expected_reserved
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 1, "cache_write_tokens": 1},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 1, "unknown_tokens": 1},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    ],
)
def test_foundry_priced_or_unknown_usage_components_fail_closed_and_keep_dispatched_reserve(usage):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(context_window_tokens=10, max_output_tokens=4),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "resp_partial", "model": "gpt-5.6-sol", "model_version": "2026-09-10", "usage": usage, "output": [{"type": "output_text", "text": "ok"}]}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4},
        )

    assert failure.value.status_code == 502
    assert budget.snapshot()["reserved_microusd"] > 0
    assert budget.snapshot()["spent_microusd"] == 0


@pytest.mark.unit
def test_foundry_scrubs_bearer_token_and_secret_keys_after_usage_validation():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(max_output_tokens=4),
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=(
                        b'{"model":"gpt-5.6-sol","model_version":"2026-09-10","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5,'
                        b'"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":1}},'
                        b'"output":{"Bearer managed-secret-token":{"echo":"Bearer managed-secret-token"}}}'
                    ),
                )
            )
        ),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/responses",
        headers={},
        body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4},
    )

    encoded = json.dumps(result["body"], sort_keys=True)
    assert "managed-secret-token" not in encoded
    assert "Bearer managed-secret-token" not in encoded
    assert "[redacted]" in encoded


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        b'{"output":NaN}',
        b'{"usage":{"input_tokens":1},"usage":{"output_tokens":1}}',
    ],
)
def test_openai_compatible_rejects_nonfinite_or_duplicate_json_without_leaking_raw_response(content):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {"mode": "api_key", "provider": "xai", "quick_model": "fixture-quick", "deep_model": "fixture-deep", "api_key": "fixture-xai-key"},
        expected_version=None,
    )
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=content))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/chat/completions",
            headers={},
            body={"model": "fixture-deep", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    assert failure.value.status_code == 502
    assert "NaN" not in str(failure.value)
    assert "input_tokens" not in str(failure.value)


@pytest.mark.unit
def test_native_dispatch_normalizes_request_errors_without_leaking_url():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "azure",
            "quick_model": "quick-deploy",
            "deep_model": "deep-deploy",
            "api_key": "fixture-azure-key",
            "base_url": "https://fixture.openai.azure.com/",
            "api_version": "2025-03-01-preview",
        },
        expected_version=None,
    )
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("fixture connect error", request=request)))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError) as failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/openai/deployments/quick-deploy/chat/completions",
            headers={},
            body={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        )

    assert failure.value.status_code == 502
    assert "fixture.openai.azure.com" not in str(failure.value)
    assert "fixture connect error" not in str(failure.value)


@pytest.mark.unit
def test_authorize_rejects_non_email_principal_and_expiry_beyond_one_hour():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    snapshot = settings.pin(owner.principal, run_id)

    with pytest.raises(GatewayError) as wrong_principal:
        gateway.authorize(run_id, object(), snapshot, expires_at=1800000100)
    with pytest.raises(GatewayError) as wrong_expiry:
        gateway.authorize(run_id, owner.principal, snapshot, expires_at=FIXTURE_NOW + 3601)

    assert wrong_principal.value.status_code == 403
    assert wrong_expiry.value.status_code == 403


@pytest.mark.unit
def test_corrupt_grant_snapshot_and_unknown_store_errors_fail_closed_and_revoke_is_idempotent():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway, _grant_key
    from tradingagents.workbench.public_settings import UserSettings
    from tradingagents.workbench.public_store import StoreUnavailable

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    record, version = store.read(_grant_key(run_id))
    record["snapshot"]["run_id"] = str(uuid4())
    store.write(_grant_key(run_id), record, version)

    with pytest.raises(GatewayError) as corrupt_failure:
        gateway.dispatch(
            capability,
            method="POST",
            path="/responses",
            headers={},
            body={"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 1},
        )

    class BrokenStore:
        def read(self, key):
            raise RuntimeError("backend details should not leak")

        def write(self, key, content, expected_version=None):
            raise StoreUnavailable("write failed")

    broken_gateway = ProviderGateway(
        BrokenStore(),
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )

    with pytest.raises(GatewayError) as store_failure:
        broken_gateway.revoke(run_id)

    assert corrupt_failure.value.status_code == 503
    assert store_failure.value.status_code == 503

    clean_run_id = str(uuid4())
    gateway.authorize(clean_run_id, owner.principal, settings.pin(owner.principal, clean_run_id), expires_at=1800003600)
    gateway.revoke(clean_run_id)
    gateway.revoke(clean_run_id)


@pytest.mark.unit
def test_foundry_reserve_uses_verified_context_bound_and_fractional_microusd_ceiling():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request):
        return httpx.Response(
            200,
            json={
                "id": "resp_ok",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 1},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
                "output": [{"type": "output_text", "text": "ok"}],
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(
            context_window_tokens=9,
            max_output_tokens=2,
            input_rate=333_334,
            cached_input_rate=111_112,
            output_rate=250_001,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    gateway.dispatch(
        capability,
        method="POST",
        path="/responses",
        headers={},
        body={
            "model": "ignored-user-model",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "tiny"}]}],
            "max_output_tokens": 2,
        },
    )

    assert budget.snapshot()["spent_microusd"] == 3
    assert budget.snapshot()["reserved_microusd"] == 0


@pytest.mark.unit
def test_managed_identity_accepts_langchain_openai_responses_history_and_total_tokens_usage():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_openai_responses_sdk_payload(langchain=True)
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_ok",
                "object": "response",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(max_output_tokens=32),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method=captured["method"],
        path="/responses",
        headers={},
        body=captured["body"] | {"max_output_tokens": 7},
    )

    assert result["body"]["usage"]["total_tokens"] == 7
    assert len(requests) == 1
    assert json.loads(requests[0].content.decode("utf-8"))["input"] == captured["body"]["input"]


@pytest.mark.unit
def test_api_key_dispatch_supports_native_anthropic_messages_sdk_shape():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_anthropic_sdk_payload()
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "anthropic",
            "quick_model": "claude-sonnet-5",
            "deep_model": "claude-fable-5",
            "api_key": "fixture-anthropic-key",
        },
        expected_version=None,
    )
    requests = []

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"id": "msg_123", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 2}}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(capability, method="POST", path="/v1/messages", headers={}, body=captured["body"])

    assert result["status_code"] == 200
    assert len(requests) == 1
    assert str(requests[0].url) == captured["url"]
    assert requests[0].headers["x-api-key"] == "fixture-anthropic-key"
    assert json.loads(requests[0].content.decode("utf-8")) == captured["body"]


@pytest.mark.unit
def test_api_key_dispatch_supports_native_google_generate_content_sdk_shape():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_google_sdk_payload()
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "google",
            "quick_model": "gemini-3.5-flash",
            "deep_model": "gemini-3.1-pro-preview",
            "api_key": "fixture-google-key",
        },
        expected_version=None,
    )
    requests = []

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}, "modelVersion": "gemini-3.5-flash"}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/v1beta/models/gemini-3.5-flash:generateContent",
        headers={},
        body=captured["body"],
    )

    assert result["status_code"] == 200
    assert len(requests) == 1
    assert str(requests[0].url) == captured["url"]
    assert requests[0].headers["x-goog-api-key"] == "fixture-google-key"
    assert "key=" not in str(requests[0].url)


@pytest.mark.unit
def test_api_key_dispatch_supports_azure_chat_completions_sdk_shape():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_azure_sdk_payload()
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "azure",
            "quick_model": "quick-deploy",
            "deep_model": "deep-deploy",
            "api_key": "fixture-azure-key",
            "base_url": "https://fixture.openai.azure.com/",
            "api_version": "2025-03-01-preview",
        },
        expected_version=None,
    )
    requests = []

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"id": "chatcmpl-123", "object": "chat.completion", "model": "gpt-4.1-mini", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/openai/deployments/quick-deploy/chat/completions",
        headers={},
        body=captured["body"],
    )

    assert result["status_code"] == 200
    assert len(requests) == 1
    assert str(requests[0].url) == captured["url"]
    assert requests[0].headers["api-key"] == "fixture-azure-key"
    assert json.loads(requests[0].content.decode("utf-8"))["max_completion_tokens"] == 4


@pytest.mark.unit
def test_native_anthropic_langchain_tool_history_round_trips_through_gateway():
    from langchain_core.messages import HumanMessage, ToolMessage

    from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "anthropic",
            "quick_model": "claude-sonnet-5",
            "deep_model": "claude-fable-5",
            "api_key": "fixture-anthropic-key",
        },
        expected_version=None,
    )
    upstream_requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if len(upstream_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [
                        {"type": "text", "text": "need tool"},
                        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"ticker": "MSFT"}},
                    ],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "final answer"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    tool = {
        "name": "lookup",
        "description": "Find quote",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    }

    with patch(
        "langchain_anthropic.chat_models._get_default_httpx_client",
        return_value=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    ):
        llm = NormalizedChatAnthropic(model="claude-sonnet-5", api_key="dummy-anthropic", max_retries=0)
        first = llm.bind_tools([tool]).invoke([HumanMessage(content="hi")])
        second = llm.bind_tools([tool]).invoke([
            HumanMessage(content="hi"),
            first,
            ToolMessage(content="done", tool_call_id="toolu_1"),
        ])

    assert first.tool_calls == [{"name": "lookup", "args": {"ticker": "MSFT"}, "id": "toolu_1", "type": "tool_call"}]
    assert second.content == "final answer"
    assert len(upstream_requests) == 2
    assert upstream_requests[0].headers["x-api-key"] == "fixture-anthropic-key"
    assert tokens == []
    forwarded = json.loads(upstream_requests[1].content.decode("utf-8"))
    assert forwarded["messages"][1]["content"] == [
        {"type": "text", "text": "need tool"},
        {"type": "tool_use", "name": "lookup", "input": {"ticker": "MSFT"}, "id": "toolu_1"},
    ]
    assert forwarded["messages"][2]["content"] == [
        {"type": "tool_result", "content": "done", "tool_use_id": "toolu_1", "is_error": False}
    ]


@pytest.mark.unit
def test_native_azure_langchain_tool_history_round_trips_through_gateway():
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    from tradingagents.llm_clients.azure_client import NormalizedAzureChatOpenAI
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "azure",
            "quick_model": "quick-deploy",
            "deep_model": "deep-deploy",
            "api_key": "fixture-azure-key",
            "base_url": "https://fixture.openai.azure.com/",
            "api_version": "2025-03-01-preview",
        },
        expected_version=None,
    )
    upstream_requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if len(upstream_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "model": "gpt-4.1-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": '{"ticker":"MSFT"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "object": "chat.completion",
                "model": "gpt-4.1-mini",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "final answer"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Find quote",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    }
    llm = NormalizedAzureChatOpenAI(
        model="ignored-by-sdk",
        azure_deployment="quick-deploy",
        api_key="dummy-azure",
        api_version="2025-03-01-preview",
        azure_endpoint="https://fixture.openai.azure.com",
        max_retries=0,
        http_client=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    )

    first = llm.bind_tools([tool]).invoke([SystemMessage(content="You are helpful."), HumanMessage(content="hi")])
    second = llm.bind_tools([tool]).invoke([
        SystemMessage(content="You are helpful."),
        HumanMessage(content="hi"),
        first,
        ToolMessage(content="done", tool_call_id="call_1"),
    ])

    assert first.tool_calls == [{"name": "lookup", "args": {"ticker": "MSFT"}, "id": "call_1", "type": "tool_call"}]
    assert second.content == "final answer"
    assert len(upstream_requests) == 2
    assert upstream_requests[0].headers["api-key"] == "fixture-azure-key"
    assert tokens == []
    forwarded = json.loads(upstream_requests[1].content.decode("utf-8"))
    assert forwarded["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert forwarded["messages"][2]["tool_calls"] == [
        {"type": "function", "id": "call_1", "function": {"name": "lookup", "arguments": '{"ticker": "MSFT"}'}}
    ]
    assert forwarded["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    assert forwarded.get("model") != "ignored-by-sdk"


@pytest.mark.unit
def test_native_responses_langchain_tool_history_round_trips_through_gateway():
    from langchain_core.messages import HumanMessage, ToolMessage

    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    upstream_requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if len(upstream_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "object": "response",
                    "model": "gpt-5.6-sol",
                    "model_version": "2026-09-10",
                    "output": [
                        {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"ticker":"MSFT"}'},
                        {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "need tool", "annotations": []}]},
                    ],
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_2",
                "object": "response",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "output": [
                    {"id": "msg_2", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final answer", "annotations": []}]}
                ],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 2,
                    "total_tokens": 9,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(max_output_tokens=32),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    tool = {
        "type": "function",
        "name": "lookup",
        "description": "Find quote",
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
        "strict": True,
    }
    llm = NormalizedChatOpenAI(
        model="gpt-5.6-sol",
        api_key="dummy-openai",
        max_tokens=16,
        max_retries=0,
        use_responses_api=True,
        http_client=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    )

    first = llm.bind_tools([tool]).invoke([HumanMessage(content="hi")])
    second = llm.bind_tools([tool]).invoke([
        HumanMessage(content="hi"),
        first,
        ToolMessage(content="done", tool_call_id="call_1"),
    ])

    assert first.tool_calls == [{"name": "lookup", "args": {"ticker": "MSFT"}, "id": "call_1", "type": "tool_call"}]
    assert second.content == "final answer"
    assert len(upstream_requests) == 2
    assert upstream_requests[0].headers["authorization"] == "Bearer managed-secret-token"
    assert tokens == ["https://cognitiveservices.azure.com/.default", "https://cognitiveservices.azure.com/.default"]
    forwarded = json.loads(upstream_requests[1].content.decode("utf-8"))
    assert forwarded["input"][1] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "need tool", "annotations": []}],
    }
    assert forwarded["input"][2] == {
        "type": "function_call",
        "name": "lookup",
        "arguments": '{"ticker": "MSFT"}',
        "call_id": "call_1",
    }
    assert forwarded["input"][3] == {"type": "function_call_output", "output": "done", "call_id": "call_1"}


@pytest.mark.unit
def test_native_anthropic_structured_output_round_trips_through_gateway():
    from pydantic import BaseModel

    from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    class Answer(BaseModel):
        answer: str

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "anthropic",
            "quick_model": "claude-sonnet-5",
            "deep_model": "claude-fable-5",
            "api_key": "fixture-anthropic-key",
        },
        expected_version=None,
    )
    upstream_requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "toolu_schema", "name": "Answer", "input": {"answer": "ok"}}],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with patch(
        "langchain_anthropic.chat_models._get_default_httpx_client",
        return_value=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    ):
        llm = NormalizedChatAnthropic(model="claude-sonnet-5", api_key="dummy-anthropic", max_retries=0)
        result = llm.with_structured_output(Answer).invoke("hi")

    assert result == Answer(answer="ok")
    assert len(upstream_requests) == 1
    forwarded = json.loads(upstream_requests[0].content.decode("utf-8"))
    assert forwarded["tool_choice"] == {"type": "tool", "name": "Answer"}
    assert forwarded["tools"][0]["name"] == "Answer"


@pytest.mark.unit
def test_native_azure_structured_output_round_trips_through_gateway():
    from pydantic import BaseModel

    from tradingagents.llm_clients.azure_client import NormalizedAzureChatOpenAI
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    class Answer(BaseModel):
        answer: str

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "azure",
            "quick_model": "quick-deploy",
            "deep_model": "deep-deploy",
            "api_key": "fixture-azure-key",
            "base_url": "https://fixture.openai.azure.com/",
            "api_version": "2025-03-01-preview",
        },
        expected_version=None,
    )
    upstream_requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_schema",
                                    "type": "function",
                                    "function": {"name": "Answer", "arguments": '{"answer":"ok"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    llm = NormalizedAzureChatOpenAI(
        model="ignored-by-sdk",
        azure_deployment="quick-deploy",
        api_key="dummy-azure",
        api_version="2025-03-01-preview",
        azure_endpoint="https://fixture.openai.azure.com",
        max_retries=0,
        http_client=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    )

    result = llm.with_structured_output(Answer, method="function_calling").invoke("hi")

    assert result == Answer(answer="ok")
    assert len(upstream_requests) == 1
    assert tokens == []
    forwarded = json.loads(upstream_requests[0].content.decode("utf-8"))
    assert forwarded["tool_choice"] == {"type": "function", "function": {"name": "Answer"}}
    assert forwarded["tools"][0]["function"]["name"] == "Answer"
    assert forwarded.get("model") != "ignored-by-sdk"


@pytest.mark.unit
def test_azure_v1_connectivity_response_reaches_sdk_and_settles_verified_usage():
    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    upstream_requests = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request):
        upstream_requests.append(request)
        return httpx.Response(200, json={
            "id": "resp_azure_v1",
            "object": "response",
            "model": "gpt-5.6-sol",
            "status": "completed",
            "output": [{
                "id": "msg_azure_v1", "type": "message", "status": "completed",
                "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": "OK"}],
            }],
            "usage": {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        })

    gateway = ProviderGateway(
        store, settings=settings, budget=budget, managed_credential=ManagedCredential(),
        installation=foundry_installation(
            input_rate=8_000_000, cached_input_rate=8_000_000, output_rate=30_000_000,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)), clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    llm = NormalizedChatOpenAI(
        model="gpt-5.6-sol", api_key="dummy-openai", max_tokens=16, max_retries=0,
        use_responses_api=True,
        http_client=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    )

    result = llm.invoke("Reply only OK.")

    assert result.content == "OK"
    assert result.usage_metadata["input_tokens"] == 10
    assert result.usage_metadata["output_tokens"] == 5
    assert len(upstream_requests) == 1
    assert budget.snapshot()["spent_microusd"] == 230
    assert budget.snapshot()["reserved_microusd"] == 0


@pytest.mark.unit
def test_native_responses_structured_output_round_trips_through_gateway():
    from pydantic import BaseModel

    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    class Answer(BaseModel):
        answer: str

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    upstream_requests = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "model": "gpt-5.6-sol",
                "model_version": "2026-09-10",
                "output": [
                    {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}]}
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(max_output_tokens=32),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    llm = NormalizedChatOpenAI(
        model="gpt-5.6-sol",
        api_key="dummy-openai",
        max_tokens=16,
        max_retries=0,
        use_responses_api=True,
        http_client=httpx.Client(transport=_gateway_delegate_transport(gateway, capability)),
    )

    result = llm.with_structured_output(Answer, method="json_schema").invoke("hi")

    assert result == Answer(answer="ok")
    assert len(upstream_requests) == 1
    forwarded = json.loads(upstream_requests[0].content.decode("utf-8"))
    assert forwarded["text"]["format"]["type"] == "json_schema"
    assert forwarded["text"]["format"]["name"] == "Answer"


@pytest.mark.unit
def test_native_google_sdk_tool_history_round_trips_through_gateway():
    from google.genai import Client as GoogleClient, types as google_types

    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "google",
            "quick_model": "gemini-3.5-flash",
            "deep_model": "gemini-3.1-pro-preview",
            "api_key": "fixture-google-key",
        },
        expected_version=None,
    )
    upstream_requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if len(upstream_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {"ticker": "MSFT"}}}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
                    "modelVersion": "gemini-3.5-flash",
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "final answer"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
                "modelVersion": "gemini-3.5-flash",
            },
        )

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)
    sdk = GoogleClient(
        api_key="dummy-google",
        http_options=google_types.HttpOptions(
            base_url="https://generativelanguage.googleapis.com",
            api_version="v1beta",
            client_args={"transport": _gateway_delegate_transport(gateway, capability)},
        ),
    )

    first = sdk.models.generate_content(
        model="gemini-3.5-flash",
        contents=[google_types.Content(role="user", parts=[google_types.Part(text="hi")])],
        config=google_types.GenerateContentConfig(
            max_output_tokens=16,
            tools=[
                google_types.Tool(
                    function_declarations=[
                        google_types.FunctionDeclaration(
                            name="lookup",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"ticker": {"type": "string"}},
                                "required": ["ticker"],
                            },
                        )
                    ]
                )
            ],
        ),
    )
    second = sdk.models.generate_content(
        model="gemini-3.5-flash",
        contents=[
            google_types.Content(role="user", parts=[google_types.Part(text="hi")]),
            google_types.Content(role="model", parts=first.candidates[0].content.parts),
            google_types.Content(
                role="user",
                parts=[google_types.Part(function_response=google_types.FunctionResponse(name="lookup", response={"result": "done"}))],
            ),
        ],
        config=google_types.GenerateContentConfig(
            max_output_tokens=16,
            tools=[
                google_types.Tool(
                    function_declarations=[
                        google_types.FunctionDeclaration(
                            name="lookup",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"ticker": {"type": "string"}},
                                "required": ["ticker"],
                            },
                        )
                    ]
                )
            ],
        ),
    )

    assert first.candidates[0].content.parts[0].function_call.name == "lookup"
    assert second.candidates[0].content.parts[0].text == "final answer"
    assert len(upstream_requests) == 2
    assert upstream_requests[0].headers["x-goog-api-key"] == "fixture-google-key"
    assert tokens == []
    forwarded = json.loads(upstream_requests[1].content.decode("utf-8"))
    assert forwarded["contents"][1]["parts"] == [{"functionCall": {"name": "lookup", "args": {"ticker": "MSFT"}}}]
    assert forwarded["contents"][2]["parts"] == [{"functionResponse": {"name": "lookup", "response": {"result": "done"}}}]


@pytest.mark.unit
def test_google_gateway_rejects_file_parts_before_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "google",
            "quick_model": "gemini-3.5-flash",
            "deep_model": "gemini-3.1-pro-preview",
            "api_key": "fixture-google-key",
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(
            capability,
            method="POST",
            path="/v1beta/models/gemini-3.5-flash:generateContent",
            headers={},
            body={
                "contents": [{"role": "user", "parts": [{"fileData": {"mimeType": "text/plain", "fileUri": "https://example.com/file.txt"}}]}],
                "generationConfig": {"maxOutputTokens": 16},
            },
        )

    assert requests == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "path, body",
    [
        (
            "/responses",
            {
                "model": "gpt-4.1-mini",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "max_output_tokens": 7,
                "priority": "high",
            },
        ),
        (
            "/responses",
            {
                "model": "gpt-4.1-mini",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "max_output_tokens": 7,
                "tools": [{"type": "web_search"}],
            },
        ),
        (
            "/chat/completions",
            {
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}]}],
                "max_tokens": 4,
            },
        ),
        (
            "/chat/completions",
            {
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4,
                "metadata": {"leak": True},
            },
        ),
    ],
)
def test_openai_gateway_rejects_unknown_or_remote_request_shapes_before_upstream(path, body):
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "openai",
            "quick_model": "gpt-4.1-mini",
            "deep_model": "gpt-4.1",
            "api_key": "fixture-openai-key",
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError):
        gateway.dispatch(capability, method="POST", path=path, headers={}, body=body)

    assert requests == []


@pytest.mark.unit
def test_managed_identity_accepts_responses_json_schema_text_format():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_openai_responses_sdk_payload(langchain=True, structured=True)
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: FIXTURE_NOW)
    requests = []

    class ManagedCredential:
        def get_token(self, scope):
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=ManagedCredential(),
        installation=foundry_installation(max_output_tokens=32),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"id": "resp_ok", "object": "response", "model": "gpt-5.6-sol", "model_version": "2026-09-10", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{\"answer\":\"ok\"}", "annotations": []}]}], "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})) ),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    gateway.dispatch(capability, method="POST", path="/responses", headers={}, body=captured["body"] | {"max_output_tokens": 7})

    assert len(requests) == 1
    forwarded = json.loads(requests[0].content.decode("utf-8"))
    assert forwarded["text"] == captured["body"]["text"]


@pytest.mark.unit
def test_openai_chat_completions_accepts_modern_max_completion_tokens():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    captured = capture_openai_chat_sdk_payload()
    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "openai",
            "quick_model": "gpt-4.1-mini",
            "deep_model": "gpt-4.1",
            "api_key": "fixture-openai-key",
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"id": "chatcmpl-123", "object": "chat.completion", "model": "gpt-4.1-mini", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(capability, method="POST", path="/chat/completions", headers={}, body=captured["body"])

    assert result["status_code"] == 200
    assert len(requests) == 1
    assert str(requests[0].url) == captured["url"]
    assert json.loads(requests[0].content.decode("utf-8"))["max_completion_tokens"] == 4


@pytest.mark.unit
def test_data_alpha_vantage_route_rejects_unhashable_function_before_upstream_call():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import GatewayError, ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-model-key",
            "data_keys": {"alpha_vantage": "fixture-alpha-key"},
            "data_vendors": {"technical_indicators": "alpha_vantage"},
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"ok": True}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    with pytest.raises(GatewayError, match="Provider request is invalid or unavailable"):
        gateway.dispatch(
            capability,
            method="POST",
            path="/data/alpha_vantage",
            headers={},
            body={"function": [], "params": {}},
        )

    assert requests == []


@pytest.mark.unit
def test_data_alpha_vantage_route_injects_server_key_and_respects_vendor_selection():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-model-key",
            "data_keys": {"alpha_vantage": "fixture-alpha-key"},
            "data_vendors": {"technical_indicators": "alpha_vantage"},
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, headers={"content-type": "text/csv"}, text="time,SMA\n2026-09-09,100\n"))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/data/alpha_vantage",
        headers={},
        body={
            "function": "SMA",
            "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"},
        },
    )

    assert result["body"] == "time,SMA\n2026-09-09,100\n"
    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://www.alphavantage.co/query?")
    assert "apikey=fixture-alpha-key" in str(requests[0].url)
    assert "function=SMA" in str(requests[0].url)


@pytest.mark.unit
def test_data_fred_route_injects_server_key_and_returns_json_body():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "api_key",
            "provider": "xai",
            "quick_model": "fixture-quick",
            "deep_model": "fixture-deep",
            "api_key": "fixture-model-key",
            "data_keys": {"fred": "fixture-fred-key"},
        },
        expected_version=None,
    )
    requests = []
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=object(),
        installation=foundry_installation(foundry_enabled=False),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, headers={"content-type": "application/json"}, json={"observations": [{"date": "2026-09-09", "value": "4.3"}]}))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/data/fred/series/observations",
        headers={},
        body={"params": {"series_id": "DGS10", "observation_start": "2026-09-01", "observation_end": "2026-09-09", "sort_order": "asc", "realtime_start": "2026-09-09", "realtime_end": "2026-09-09"}},
    )

    assert result["body"] == {"observations": [{"date": "2026-09-09", "value": "4.3"}]}
    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://api.stlouisfed.org/fred/series/observations?")
    assert "api_key=fixture-fred-key" in str(requests[0].url)


@pytest.mark.unit
def test_data_alpha_vantage_bridge_runs_native_stock_formatter():
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_stock as av_stock

    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"core_stock_apis": "alpha_vantage"},
        upstream=lambda request: httpx.Response(
            200,
            headers={"content-type": "text/csv"},
            text=(
                "timestamp,open,high,low,close,adjusted_close,volume,dividend_amount,split_coefficient\n"
                "2026-09-11,101,103,100,102,102,1000,0,1\n"
                "2026-09-09,99,100,98,99,99,900,0,1\n"
                "2026-09-01,95,96,94,95,95,800,0,1\n"
            ),
        ),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_stock.get_stock("MSFT", "2026-09-02", "2026-09-10")

    assert result.splitlines() == [
        "timestamp,open,high,low,close,adjusted_close,volume,dividend_amount,split_coefficient",
        "2026-09-09,99,100,98,99,99,900,0,1",
    ]
    assert len(bridge["requests"]) == 1
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == "TIME_SERIES_DAILY_ADJUSTED"
    assert params["apikey"] == "fixture-alpha-key"
    assert params["source"] == "trading_agents"
    assert params["outputsize"] == "compact"


@pytest.mark.unit
@pytest.mark.parametrize(
    "indicator, expected_function, response_text, expected_params, expected_value",
    [
        ("close_50_sma", "SMA", "time,SMA\n2026-09-09,100\n", {"time_period": "50", "series_type": "close"}, "2026-09-09: 100"),
        ("close_200_sma", "SMA", "time,SMA\n2026-09-09,250\n", {"time_period": "200", "series_type": "close"}, "2026-09-09: 250"),
        ("close_10_ema", "EMA", "time,EMA\n2026-09-09,101\n", {"time_period": "10", "series_type": "close"}, "2026-09-09: 101"),
        ("macd", "MACD", "time,MACD,MACD_Signal,MACD_Hist\n2026-09-09,1.5,1.0,0.5\n", {"series_type": "close"}, "2026-09-09: 1.5"),
        ("macds", "MACD", "time,MACD,MACD_Signal,MACD_Hist\n2026-09-09,1.5,1.0,0.5\n", {"series_type": "close"}, "2026-09-09: 1.0"),
        ("macdh", "MACD", "time,MACD,MACD_Signal,MACD_Hist\n2026-09-09,1.5,1.0,0.5\n", {"series_type": "close"}, "2026-09-09: 0.5"),
        ("rsi", "RSI", "time,RSI\n2026-09-09,65\n", {"time_period": "14", "series_type": "close"}, "2026-09-09: 65"),
        ("boll", "BBANDS", "time,Real Upper Band,Real Middle Band,Real Lower Band\n2026-09-09,11,10,9\n", {"time_period": "20", "series_type": "close"}, "2026-09-09: 10"),
        ("boll_ub", "BBANDS", "time,Real Upper Band,Real Middle Band,Real Lower Band\n2026-09-09,11,10,9\n", {"time_period": "20", "series_type": "close"}, "2026-09-09: 11"),
        ("boll_lb", "BBANDS", "time,Real Upper Band,Real Middle Band,Real Lower Band\n2026-09-09,11,10,9\n", {"time_period": "20", "series_type": "close"}, "2026-09-09: 9"),
        ("atr", "ATR", "time,ATR\n2026-09-09,3.2\n", {"time_period": "14"}, "2026-09-09: 3.2"),
    ],
)
def test_data_alpha_vantage_bridge_runs_native_indicator_requests_and_formatting(indicator, expected_function, response_text, expected_params, expected_value):
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_indicator as av_indicator

    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"technical_indicators": "alpha_vantage"},
        upstream=lambda request, response_text=response_text: httpx.Response(
            200,
            headers={"content-type": "text/csv"},
            text=response_text,
        ),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_indicator.get_indicator("MSFT", indicator, "2026-09-10", 30)

    assert expected_value in result
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == expected_function
    assert params["symbol"] == "MSFT"
    assert params["interval"] == "daily"
    assert params["datatype"] == "csv"
    assert params["apikey"] == "fixture-alpha-key"
    assert params["source"] == "trading_agents"
    for name, value in expected_params.items():
        assert params[name] == value


@pytest.mark.unit
def test_data_alpha_vantage_bridge_runs_native_overview_formatter():
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_fundamentals as av_fundamentals

    overview = json.dumps({"Symbol": "MSFT", "Name": "Microsoft Corp"})
    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"fundamental_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=json.loads(overview)),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_fundamentals.get_fundamentals("MSFT", curr_date="2026-09-10")

    assert json.loads(result) == {"Symbol": "MSFT", "Name": "Microsoft Corp"}
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == "OVERVIEW"
    assert params["symbol"] == "MSFT"
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
@pytest.mark.parametrize(
    "method_name, expected_function",
    [
        ("get_balance_sheet", "BALANCE_SHEET"),
        ("get_cashflow", "CASH_FLOW"),
        ("get_income_statement", "INCOME_STATEMENT"),
    ],
)
def test_data_alpha_vantage_bridge_runs_native_statement_date_filters(method_name, expected_function):
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_fundamentals as av_fundamentals

    response_body = {
        "symbol": "MSFT",
        "annualReports": [
            {"fiscalDateEnding": "2026-12-31", "value": "future"},
            {"fiscalDateEnding": "2025-12-31", "value": "keep"},
        ],
        "quarterlyReports": [
            {"fiscalDateEnding": "2026-03-31", "value": "future"},
            {"fiscalDateEnding": "2025-09-30", "value": "keep"},
        ],
    }
    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"fundamental_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=response_body),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = getattr(av_fundamentals, method_name)("MSFT", curr_date="2026-01-01")

    parsed = json.loads(result)
    assert [row["fiscalDateEnding"] for row in parsed["annualReports"]] == ["2025-12-31"]
    assert [row["fiscalDateEnding"] for row in parsed["quarterlyReports"]] == ["2025-09-30"]
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == expected_function
    assert params["symbol"] == "MSFT"
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_data_alpha_vantage_bridge_runs_native_news_request():
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_news as av_news

    payload = {"feed": [{"title": "MSFT launch"}]}
    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"news_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=payload),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_news.get_news("MSFT", "2026-09-01", "2026-09-10")

    assert json.loads(result) == payload
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == "NEWS_SENTIMENT"
    assert params["tickers"] == "MSFT"
    assert params["time_from"] == "20260901T0000"
    assert params["time_to"] == "20260910T0000"
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_data_alpha_vantage_bridge_runs_native_global_news_request():
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_news as av_news

    payload = {"feed": [{"title": "Macro roundup"}]}
    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"news_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=payload),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_news.get_global_news("2026-09-10", look_back_days=7, limit=25)

    assert json.loads(result) == payload
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == "NEWS_SENTIMENT"
    assert params["topics"] == "financial_markets,economy_macro,economy_monetary"
    assert params["time_from"] == "20260903T0000"
    assert params["time_to"] == "20260910T0000"
    assert params["limit"] == "25"
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_data_alpha_vantage_bridge_runs_native_insider_request():
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_news as av_news

    payload = {"data": [{"symbol": "MSFT", "shares": "10"}]}
    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"news_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=payload),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])):
        result = av_news.get_insider_transactions("MSFT")

    assert json.loads(result) == payload
    params = _gateway_query_params(bridge["requests"][0])
    assert params["function"] == "INSIDER_TRANSACTIONS"
    assert params["symbol"] == "MSFT"
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_data_fred_bridge_runs_native_formatter_and_preserves_pit_bounds():
    from tradingagents.dataflows import fred

    meta = {
        "seriess": [
            {
                "title": "Unemployment Rate",
                "units_short": "%",
                "frequency": "Monthly",
                "seasonal_adjustment_short": "SA",
            }
        ]
    }
    observations = {
        "observations": [
            {"date": "2026-07-01", "value": "4.1"},
            {"date": "2026-08-01", "value": "4.2"},
            {"date": "2026-09-01", "value": "4.4"},
        ]
    }

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/series"):
            return httpx.Response(200, headers={"content-type": "application/json"}, json=meta)
        if request.url.path.endswith("/series/observations"):
            return httpx.Response(200, headers={"content-type": "application/json"}, json=observations)
        raise AssertionError(f"unexpected FRED path: {request.url.path}")

    bridge = _make_data_gateway(
        data_keys={"fred": "fixture-fred-key"},
        data_vendors={"macro_data": "fred"},
        upstream=upstream,
    )

    with patch.object(fred, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(fred.requests, "get", side_effect=_bridge_fred_requests_get(bridge["gateway"], bridge["capability"])), \
            patch.object(fred, "_fred_today", return_value="2026-08-31"):
        result = fred.get_macro_data("unemployment", "2026-09-01", 90)

    assert "## FRED: Unemployment Rate (UNRATE)" in result
    assert "**Latest:** 4.4 (2026-09-01)" in result
    assert len(bridge["requests"]) == 2
    meta_params = _gateway_query_params(bridge["requests"][0])
    obs_params = _gateway_query_params(bridge["requests"][1])
    assert meta_params["api_key"] == "fixture-fred-key"
    assert obs_params["api_key"] == "fixture-fred-key"
    assert meta_params["file_type"] == "json"
    assert obs_params["file_type"] == "json"
    assert meta_params["realtime_start"] == "2026-08-31"
    assert meta_params["realtime_end"] == "2026-08-31"
    assert obs_params["realtime_start"] == "2026-08-31"
    assert obs_params["realtime_end"] == "2026-08-31"
    assert obs_params["observation_start"] == "2026-06-03"
    assert obs_params["observation_end"] == "2026-09-01"


@pytest.mark.unit
@pytest.mark.parametrize(
    "response_json, expected_exception",
    [
        ({"Information": "the parameter apikey is invalid or missing. Please claim your free API key."}, "AlphaVantageNotConfiguredError"),
        ({"Note": "API call frequency is 5 calls per minute."}, "AlphaVantageRateLimitError"),
    ],
)
def test_data_alpha_vantage_bridge_preserves_native_vendor_errors(response_json, expected_exception):
    import tradingagents.dataflows.alpha_vantage_common as av
    import tradingagents.dataflows.alpha_vantage_stock as av_stock

    bridge = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha-key"},
        data_vendors={"core_stock_apis": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, headers={"content-type": "application/json"}, json=response_json),
    )

    with patch.object(av, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(av.requests, "get", side_effect=_bridge_alpha_vantage_requests_get(bridge["gateway"], bridge["capability"])), \
            pytest.raises(getattr(av, expected_exception)):
        av_stock.get_stock("MSFT", "2026-09-01", "2026-09-10")

    params = _gateway_query_params(bridge["requests"][0])
    assert params["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_data_fred_bridge_preserves_native_actionable_400():
    from tradingagents.dataflows import fred

    bridge = _make_data_gateway(
        data_keys={"fred": "fixture-fred-key"},
        data_vendors={"macro_data": "fred"},
        upstream=lambda request: httpx.Response(
            400,
            headers={"content-type": "application/json"},
            json={"error_message": "Bad Request. Unknown series id: BADSERIES"},
        ),
    )

    with patch.object(fred, "get_api_key", return_value="dummy-worker-key"), \
            patch.object(fred.requests, "get", side_effect=_bridge_fred_requests_get(bridge["gateway"], bridge["capability"])), \
            pytest.raises(ValueError, match="FRED request failed: Bad Request. Unknown series id: BADSERIES"):
        fred.get_macro_data("BADSERIES", "2026-09-01", 90)

    assert len(bridge["requests"]) == 1
    params = _gateway_query_params(bridge["requests"][0])
    assert params["api_key"] == "fixture-fred-key"


@pytest.mark.unit
@pytest.mark.parametrize(
    "path, body, data_keys, data_vendors, status_code",
    [
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {}, {"technical_indicators": "alpha_vantage"}, 409),
        ("/data/fred/series", {"params": {"series_id": "UNRATE"}}, {}, {"macro_data": "fred"}, 409),
        ("/data/alpha_vantage", {"function": "OVERVIEW", "params": {"symbol": "MSFT"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv", "apikey": "forged"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv", "host": "evil.example.com"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/fred/series/observations", {"params": {"series_id": "UNRATE", "observation_start": "2026-09-01", "observation_end": "2026-09-10", "sort_order": "asc", "realtime_start": "2026-09-10", "realtime_end": "2026-09-10", "file_type": "xml"}}, {"fred": "fixture-fred-key"}, {"macro_data": "fred"}, 400),
        ("/data/fred/series", {"params": {"series_id": "UNRATE", "provider": "evil"}}, {"fred": "fixture-fred-key"}, {"macro_data": "fred"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": True, "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": ["MSFT"], "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/fred/series", {"params": {"series_id": {"nested": "UNRATE"}}}, {"fred": "fixture-fred-key"}, {"macro_data": "fred"}, 400),
        ("/data/yahoo", {"params": {}}, {"fred": "fixture-fred-key", "alpha_vantage": "fixture-alpha-key"}, {"macro_data": "fred", "technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"symbol": "X" * 257, "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "sma", "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
        ("/data/alpha_vantage", {"function": "SMA", "params": {"Symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}}, {"alpha_vantage": "fixture-alpha-key"}, {"technical_indicators": "alpha_vantage"}, 400),
    ],
)
def test_data_routes_reject_invalid_or_bypass_payloads_without_upstream(path, body, data_keys, data_vendors, status_code):
    from tradingagents.workbench.public_gateway import GatewayError

    bridge = _make_data_gateway(data_keys=data_keys, data_vendors=data_vendors)

    with pytest.raises(GatewayError, match="Provider (request is invalid or unavailable|credentials are unavailable)") as exc:
        bridge["gateway"].dispatch(
            bridge["capability"],
            method="POST",
            path=path,
            headers={},
            body=body,
        )

    assert exc.value.status_code == status_code
    assert bridge["requests"] == []


@pytest.mark.unit
def test_data_route_skips_managed_identity_tokens_for_native_vendor_calls():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_budget import BudgetLedger
    from tradingagents.workbench.public_gateway import ProviderGateway
    from tradingagents.workbench.public_settings import UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(
        owner.principal,
        {
            "mode": "managed_identity",
            "data_keys": {"alpha_vantage": "fixture-alpha-key"},
            "data_vendors": {"technical_indicators": "alpha_vantage"},
        },
        expected_version=None,
    )
    requests = []
    tokens = []

    class ManagedCredential:
        def get_token(self, scope):
            tokens.append(scope)
            return type("Token", (), {"token": "managed-secret-token"})()

    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=BudgetLedger(store, clock=lambda: FIXTURE_NOW),
        managed_credential=ManagedCredential(),
        installation=foundry_installation(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, headers={"content-type": "text/csv"}, text="time,SMA\n2026-09-09,100\n"))),
        clock=lambda: FIXTURE_NOW,
    )
    run_id = str(uuid4())
    capability = gateway.authorize(run_id, owner.principal, settings.pin(owner.principal, run_id), expires_at=1800003600)

    result = gateway.dispatch(
        capability,
        method="POST",
        path="/data/alpha_vantage",
        headers={},
        body={"function": "SMA", "params": {"symbol": "MSFT", "interval": "daily", "time_period": "50", "series_type": "close", "datatype": "csv"}},
    )

    assert result["body"] == "time,SMA\n2026-09-09,100\n"
    assert len(requests) == 1
    assert tokens == []
    assert _gateway_query_params(requests[0])["apikey"] == "fixture-alpha-key"


@pytest.mark.unit
def test_etf_alpha_vantage_fundamentals_are_rejected_before_any_gateway_dispatch():
    from tradingagents.workbench.public_accounts import EmailAuth
    from tradingagents.workbench.public_settings import SettingsError, UserSettings

    store = EncryptedStore(MemoryBackend(), b"g" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: FIXTURE_NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)

    with pytest.raises(SettingsError, match="Invalid settings input"):
        settings.update(
            owner.principal,
            {
                "mode": "api_key",
                "provider": "xai",
                "quick_model": "fixture-quick",
                "deep_model": "fixture-deep",
                "api_key": "fixture-model-key",
                "asset_type": "etf",
                "data_keys": {"alpha_vantage": "fixture-alpha-key"},
                "data_vendors": {"fundamental_data": "alpha_vantage"},
            },
            expected_version=None,
        )
