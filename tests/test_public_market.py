from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from tests.test_public_accounts import ORIGIN, OWNER_PASSWORD
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_accounts import EmailAuth
from tradingagents.workbench.public_settings import UserSettings
from tradingagents.workbench.public_store import EncryptedStore

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def no_live_market_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live market network is forbidden in tests")

    monkeypatch.setattr("yfinance.Ticker", forbidden)
    monkeypatch.setattr("tradingagents.workbench.public_transport.PinnedTransport.handle_request", forbidden)


@pytest.fixture
def case():
    from tradingagents.workbench.public_market import MarketData

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"m" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invite = auth.invite(owner.session_id, "market@example.com")
    guest = auth.redeem(invite.token, invite.email, "market-fixture-password")
    settings = UserSettings(store)
    market = MarketData(store=store, settings=settings, clock=lambda: NOW)
    return SimpleNamespace(**locals())


def payload(symbols=None, **changes):
    return {"symbols": ["MSFT"] if symbols is None else symbols,
            "selected": "MSFT", "range": "1m", "source": "yfinance", **changes}


def yahoo(symbol):
    assert symbol in {"MSFT", "AAPL", "BAD"}
    if symbol == "BAD":
        raise RuntimeError("private upstream detail")
    return SimpleNamespace(
        get_info=lambda: {"shortName": "Microsoft", "regularMarketPrice": 105,
                          "regularMarketPreviousClose": 100, "currency": "USD",
                          "exchange": "NMS", "regularMarketTime": 1791561600},
        history=lambda **kwargs: pd.DataFrame(
            {"Open": [101, float("nan")], "High": [106, 107], "Low": [100, 102],
             "Close": [105, 106], "Volume": [1000, 1200]},
            index=pd.to_datetime(["2026-10-08", "2026-10-09"])),
    )


def test_watchlist_account_isolation_normalization_and_cas(case):
    from tradingagents.workbench.public_market import MarketError

    assert case.market.watchlist(case.guest.principal) == {"symbols": [], "version": None}
    saved = case.market.update_watchlist(case.guest.principal, {"symbols": ["msft", "0700.hk", "^gspc"]}, expected_version=None)
    assert saved["symbols"] == ["MSFT", "0700.HK", "^GSPC"]
    assert case.market.watchlist(case.owner.principal)["symbols"] == []
    with pytest.raises(MarketError) as error:
        case.market.update_watchlist(case.guest.principal, {"symbols": []}, expected_version=None)
    assert error.value.status_code == 409
    assert case.market.update_watchlist(case.guest.principal, {"symbols": []}, expected_version=saved["version"])["symbols"] == []


def test_data_key_accessor_does_not_require_model_settings_or_fallback(case, monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "environment-secret")
    assert case.settings.data_key(case.guest.principal, "alpha_vantage") is None
    case.settings.update(case.owner.principal, {"data_keys": {"alpha_vantage": "owner-secret"}}, expected_version=None)
    case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "guest-secret"}}, expected_version=None)
    assert case.settings.data_key(case.guest.principal, "alpha_vantage") == "guest-secret"
    assert case.settings.get(case.guest.principal)["provider"] is None


@pytest.mark.parametrize("changes", [
    {"symbols": ["MSFT"] * 13}, {"symbols": ["MSFT", "msft"]},
    {"symbols": ["https://evil.example"]}, {"selected": "AAPL"},
    {"range": "max"}, {"source": "https://evil.example"},
    {"symbols": "MSFT"}, {"extra": True},
])
def test_snapshot_rejects_invalid_contract_before_provider_calls(case, changes):
    from tradingagents.workbench.public_market import MarketError

    with pytest.raises(MarketError) as error:
        case.market.snapshot(case.guest.principal, payload(**changes))
    assert error.value.status_code == 400


def test_empty_snapshot_is_supported_without_keys_or_network(case):
    result = case.market.snapshot(case.guest.principal, payload([], selected=None, source="alpha_vantage"))
    assert result == {"quotes": [], "candles": [], "selected": None, "source": "alpha_vantage",
                      "fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
                      "interval": "1d", "errors": []}


def test_yahoo_nulls_timestamp_partial_failure_and_account_cache(case, monkeypatch):
    import yfinance

    calls = []
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: (calls.append(symbol), yahoo(symbol))[1])
    result = case.market.snapshot(case.guest.principal, payload(["msft", "BAD"]))
    quote = result["quotes"][0]
    assert quote == {"symbol": "MSFT", "name": "Microsoft", "price": 105,
                     "change": 5, "change_percent": 5, "currency": "USD", "exchange": "NMS",
                     "as_of": datetime.fromtimestamp(1791561600, timezone.utc).isoformat(),
                     "open": None, "high": None, "low": None, "previous_close": 100, "volume": None}
    assert result["candles"] == [{"time": "2026-10-08", "open": 101, "high": 106, "low": 100, "close": 105, "volume": 1000}]
    assert result["errors"] == [{"symbol": "BAD", "code": "provider_unavailable", "message": "Market data is unavailable."}]
    case.market.snapshot(case.guest.principal, payload())
    assert calls.count("MSFT") == 1
    case.market.snapshot(case.owner.principal, payload())
    assert calls.count("MSFT") == 2


def test_alpha_daily_quote_uses_only_user_key_and_fixed_endpoint(case):
    from tradingagents.workbench.public_market import MarketData

    case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "guest-secret"}}, expected_version=None)
    requests = []

    def wire(request):
        requests.append(request)
        assert str(request.url).startswith("https://www.alphavantage.co/query?")
        assert request.url.params["apikey"] == "guest-secret"
        if request.url.params["function"] == "GLOBAL_QUOTE":
            return httpx.Response(200, json={"Global Quote": {"01. symbol": "MSFT", "02. open": "101", "03. high": "106", "04. low": "100", "05. price": "105", "06. volume": "1000", "07. latest trading day": "2026-10-08", "08. previous close": "100", "09. change": "5", "10. change percent": "5%"}})
        return httpx.Response(200, json={"Time Series (Daily)": {"2026-10-08": {"1. open": "101", "2. high": "106", "3. low": "100", "4. close": "105", "5. volume": "1000"}}})

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, http_client=client)
        result = market.snapshot(case.guest.principal, payload(source="alpha_vantage"))
        assert result["quotes"][0]["as_of"] == "2026-10-08"
        assert result["quotes"][0]["currency"] is None
        assert result["candles"][0]["close"] == 105
        assert len(requests) == 2
        assert market.snapshot(case.owner.principal, payload(source="alpha_vantage"))["errors"][0]["code"] == "key_unconfigured"
        assert len(requests) == 2


def test_concurrent_requests_are_bounded_and_single_flight(case, monkeypatch):
    import yfinance

    from tradingagents.workbench.public_market import MarketError

    entered, release = Event(), Event()

    def blocked(symbol):
        entered.set()
        assert release.wait(5)
        return yahoo(symbol)

    monkeypatch.setattr(yfinance, "Ticker", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(case.market.snapshot, case.guest.principal, payload())
        assert entered.wait(5)
        try:
            with pytest.raises(MarketError) as error:
                case.market.snapshot(case.guest.principal, payload())
            assert error.value.status_code == 429
        finally:
            release.set()
        assert pending.result()["quotes"][0]["price"] == 105


@pytest.mark.parametrize("body,code", [
    ({"Note": "provider limit private detail"}, "rate_limited"),
    ({"Information": "Invalid API key: private detail"}, "invalid_key"),
    ({"Error Message": "Invalid symbol private detail"}, "no_data"),
    ({"Global Quote": {}}, "no_data"),
    ([], "provider_unavailable"),
])
def test_alpha_safe_errors_and_error_cache(case, body, code):
    from tradingagents.workbench.public_market import MarketData

    case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "private-secret"}}, expected_version=None)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: (calls.append(request), httpx.Response(200, json=body))[1])) as client:
        market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, http_client=client)
        first = market.snapshot(case.guest.principal, payload(source="alpha_vantage"))
        assert first["errors"][0]["code"] == code
        assert "private" not in str(first)
        market.snapshot(case.guest.principal, payload(source="alpha_vantage"))
        assert len(calls) == 2


def test_alpha_key_removal_cannot_reuse_cached_quotes(case):
    from tradingagents.workbench.public_market import MarketData

    saved = case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "first-key"}}, expected_version=None)
    body = {"Global Quote": {"01. symbol": "MSFT", "05. price": "105", "07. latest trading day": "2026-10-08"}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, http_client=client)
        assert market.snapshot(case.guest.principal, payload(source="alpha_vantage"))["quotes"][0]["price"] == 105
        case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": None}}, expected_version=saved["version"])
        result = market.snapshot(case.guest.principal, payload(source="alpha_vantage"))
        assert result["quotes"] == []
        assert result["errors"][0]["code"] == "key_unconfigured"


def test_cache_expires_and_range_switch_does_not_refetch_quote(case, monkeypatch):
    from tradingagents.workbench.public_market import MarketData

    calls, elapsed = [], [0]
    monkeypatch.setattr("yfinance.Ticker", lambda symbol: (calls.append(symbol), yahoo(symbol))[1])
    market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, monotonic=lambda: elapsed[0])
    market.snapshot(case.guest.principal, payload())
    market.snapshot(case.guest.principal, payload(range="3m"))
    assert len(calls) == 2
    elapsed[0] = 61
    market.snapshot(case.guest.principal, payload())
    assert len(calls) == 3


def test_cached_snapshot_preserves_actual_fetch_time(case, monkeypatch):
    from tradingagents.workbench.public_market import MarketData

    clock, elapsed = [NOW], [0]
    monkeypatch.setattr("yfinance.Ticker", yahoo)
    market = MarketData(store=case.store, settings=case.settings, clock=lambda: clock[0], monotonic=lambda: elapsed[0])
    first = market.snapshot(case.guest.principal, payload())
    clock[0] += 20
    elapsed[0] += 20
    cached = market.snapshot(case.guest.principal, payload())
    assert cached["fetched_at"] == first["fetched_at"]


def test_soft_request_limit_is_per_account_and_resets(case):
    from tradingagents.workbench.public_market import MarketData, MarketError

    elapsed = [0]
    market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, monotonic=lambda: elapsed[0])
    for _ in range(30):
        market.snapshot(case.guest.principal, payload([], selected=None))
    with pytest.raises(MarketError) as error:
        market.snapshot(case.guest.principal, payload([], selected=None))
    assert error.value.status_code == 429
    assert market.snapshot(case.owner.principal, payload([], selected=None))["quotes"] == []
    elapsed[0] = 61
    assert market.snapshot(case.guest.principal, payload([], selected=None))["quotes"] == []


def test_watchlist_encryption_tamper_and_uncertain_write_fail_closed(case, monkeypatch):
    from tradingagents.workbench.public_market import MarketError

    saved = case.market.update_watchlist(case.guest.principal, {"symbols": ["MSFT"]}, expected_version=None)
    key = f"{case.guest.principal.owner_key}/market-watchlist"
    assert b"MSFT" not in case.backend.records[key][0]

    def unavailable(*args, **kwargs):
        raise RuntimeError("storage secret")

    monkeypatch.setattr(case.store, "write", unavailable)
    with pytest.raises(MarketError) as error:
        case.market.update_watchlist(case.guest.principal, {"symbols": []}, expected_version=saved["version"])
    assert error.value.status_code == 503
    assert case.market.watchlist(case.guest.principal)["symbols"] == ["MSFT"]
    case.backend.records[key] = (b"corrupt", saved["version"])
    with pytest.raises(MarketError) as error:
        case.market.watchlist(case.guest.principal)
    assert error.value.status_code == 503


def test_unsafe_injected_http_client_is_rejected(case):
    from tradingagents.workbench.public_market import MarketData
    from tradingagents.workbench.public_transport import TransportPolicyError

    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)), follow_redirects=True) as client,
        pytest.raises(TransportPolicyError),
    ):
        MarketData(store=case.store, settings=case.settings, http_client=client)


def test_alpha_full_history_shared_across_ranges(case):
    from tradingagents.workbench.public_market import MarketData

    case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "guest-key"}}, expected_version=None)
    functions = []

    def wire(request):
        functions.append(request.url.params["function"])
        if request.url.params["function"] == "GLOBAL_QUOTE":
            return httpx.Response(200, json={"Global Quote": {"01. symbol": "MSFT", "05. price": "105"}})
        assert request.url.params["outputsize"] == "full"
        return httpx.Response(200, json={"Time Series (Daily)": {"2026-10-08": {"1. open": "101", "2. high": "106", "3. low": "100", "4. close": "105", "5. volume": "1000"}}})

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, http_client=client)
        for period in ("6m", "1y", "5y"):
            assert market.snapshot(case.guest.principal, payload(source="alpha_vantage", range=period))["candles"][0]["close"] == 105
        assert functions == ["GLOBAL_QUOTE", "TIME_SERIES_DAILY"]


def test_alpha_query_credentials_never_appear_in_http_logs(case, caplog):
    from tradingagents.workbench.public_market import MarketData

    case.settings.update(case.guest.principal, {"data_keys": {"alpha_vantage": "log-secret-marker"}}, expected_version=None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
        market = MarketData(store=case.store, settings=case.settings, clock=lambda: NOW, http_client=client)
        with caplog.at_level("DEBUG", logger="httpx"):
            market.snapshot(case.guest.principal, payload(source="alpha_vantage"))
        assert "log-secret-marker" not in caplog.text
