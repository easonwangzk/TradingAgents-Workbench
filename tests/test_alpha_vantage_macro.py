import json
from datetime import date, timedelta

import httpx
import pytest

from tradingagents.dataflows import alpha_vantage_common, interface


def test_macro_route_uses_alpha_vantage_without_fred(monkeypatch):
    today = date.today().isoformat()
    calls = []

    def request(function_name, params):
        calls.append((function_name, params))
        return json.dumps({
            "name": "Unemployment Rate", "interval": "monthly", "unit": "percent",
            "data": [{"date": today, "value": "4.3"}],
        })

    monkeypatch.setattr(interface, "get_vendor", lambda *args: "alpha_vantage")
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    report = interface.route_to_vendor("get_macro_indicators", "unemployment", today, 365)

    assert "Alpha Vantage" in report
    assert f"| {today} | 4.3 |" in report
    assert calls == [("UNEMPLOYMENT", {"datatype": "json"})]


@pytest.mark.parametrize("alias,function,params", [
    ("cpi", "CPI", {"interval": "monthly"}),
    ("real_gdp", "REAL_GDP", {"interval": "quarterly"}),
    ("real_gdp_per_capita", "REAL_GDP_PER_CAPITA", {}),
    ("Fed Funds Rate", "FEDERAL_FUNDS_RATE", {"interval": "monthly"}),
    ("10y-treasury", "TREASURY_YIELD", {"interval": "daily", "maturity": "10year"}),
    ("2y_treasury", "TREASURY_YIELD", {"interval": "daily", "maturity": "2year"}),
    ("30y_treasury", "TREASURY_YIELD", {"interval": "daily", "maturity": "30year"}),
    ("inflation", "INFLATION", {}),
    ("retail_sales", "RETAIL_SALES", {}),
    ("durables", "DURABLES", {}),
    ("nonfarm_payrolls", "NONFARM_PAYROLL", {}),
])
def test_economic_alias_requests_documented_endpoint(monkeypatch, alias, function, params):
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data

    calls = []
    today = date.today().isoformat()

    def request(name, parameters):
        calls.append((name, parameters))
        return {"name": "Fixture", "interval": "monthly", "unit": "percent",
                "data": [{"date": today, "value": "4.3"}]}

    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    report = get_macro_data(alias, today)
    assert f"| {today} | 4.3 |" in report
    assert calls == [(function, {**params, "datatype": "json"})]
    assert "Latest-revision" in report


def test_observations_filtered_sorted_bounded_and_finite(monkeypatch):
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data

    today = date.today()
    rows = [{"date": (today - timedelta(days=offset)).isoformat(), "value": str(50 - offset)}
            for offset in range(50)]
    rows += [{"date": (today + timedelta(days=1)).isoformat(), "value": "999"},
             {"date": (today - timedelta(days=2)).isoformat(), "value": "NaN"},
             {"date": today.isoformat(), "value": "."}, None]
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", lambda *args: {"data": rows})
    report = get_macro_data("unemployment", today.isoformat(), 45)
    assert "999" not in report and "NaN" not in report
    assert "Change over window: +45.00" in report
    assert len([line for line in report.splitlines() if line.startswith("| 20")]) == 40
    assert report.endswith(f"| {today} | 50 |")


@pytest.mark.parametrize("indicator,offset,days", [
    ("core_pce", 0, 365), ("core_cpi", 0, 365), ("m2", 0, 365),
    ("gdp", 0, 365), ("CPIAUCSL", 0, 365),
    ("cpi", -1, 365), ("cpi", 1, 365), ("cpi", 0, 0), ("cpi", 0, True),
])
def test_unsupported_or_historical_requests_do_not_call_provider(monkeypatch, indicator, offset, days):
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data

    def unexpected(*args):
        pytest.fail("unsupported request must not consume quota")

    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", unexpected)
    report = get_macro_data(indicator, (date.today() + timedelta(days=offset)).isoformat(), days)
    assert report.startswith("DATA_UNAVAILABLE:")


def test_yield_curve_joins_dates_before_subtracting(monkeypatch):
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data

    today = date.today().isoformat()
    earlier = (date.today() - timedelta(days=1)).isoformat()
    calls = []

    def request(function, params):
        calls.append(params["maturity"])
        rows = ([{"date": today, "value": "4.5"}, {"date": earlier, "value": "4.4"}]
                if params["maturity"] == "10year" else [{"date": today, "value": "3.9"}])
        return {"data": rows}

    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    report = get_macro_data("yield_curve", today)
    assert f"| {today} | 0.6 |" in report
    assert f"| {earlier} |" not in report
    assert "percentage points" in report
    assert calls == ["10year", "2year"]


@pytest.mark.parametrize("payload", [
    {"Information": "Your API key has reached 25 requests per day"},
    {"Error Message": "invalid request"}, {"data": []}, "not JSON", [],
])
def test_provider_notices_and_invalid_data_are_unavailable(monkeypatch, payload):
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data

    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", lambda *args: payload)
    assert get_macro_data("unemployment", date.today().isoformat()).startswith("DATA_UNAVAILABLE:")


@pytest.mark.parametrize("error", [
    alpha_vantage_common.AlphaVantageRateLimitError("fixture quota"),
    alpha_vantage_common.AlphaVantageNotConfiguredError("fixture key"),
])
def test_native_key_and_quota_errors_are_optional(monkeypatch, error):
    def request(*args):
        raise error

    monkeypatch.setattr(interface, "get_vendor", lambda *args: "alpha_vantage")
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    assert interface.route_to_vendor("get_macro_indicators", "unemployment", date.today().isoformat()).startswith("DATA_UNAVAILABLE:")


def test_workbench_accepts_alpha_macro_and_preserves_fred_default():
    from tradingagents.workbench.contracts import default_request, validate_request

    assert default_request()["data_vendors"]["macro_data"] == "fred"
    assert validate_request({"data_vendors": {"macro_data": "alpha_vantage"}})["data_vendors"]["macro_data"] == "alpha_vantage"


def test_hosted_macro_bridge_uses_selected_key_without_fred(monkeypatch):
    from tests.test_public_gateway import _make_data_gateway
    from tradingagents.dataflows.alpha_vantage_macro import get_macro_data
    from tradingagents.workbench import public_worker

    today = date.today().isoformat()
    fixture = _make_data_gateway(
        data_keys={"alpha_vantage": "fixture-alpha"}, data_vendors={"macro_data": "alpha_vantage"},
        upstream=lambda request: httpx.Response(200, json={
            "name": "Consumer Price Index", "interval": "monthly", "unit": "index 1982-1984=100",
            "data": [{"date": today, "value": "320.5"}],
        }),
    )

    class Hosted:
        def dispatch(self, method, path, body):
            return fixture["gateway"].dispatch(fixture["capability"], method=method, path=path,
                                               headers={}, body=body)["body"]

    monkeypatch.setattr(public_worker, "current_hosted_worker_context", lambda: Hosted())
    report = get_macro_data("cpi", today)
    assert "320.5" in report
    assert "fixture-alpha" not in report
    assert len(fixture["requests"]) == 1
    params = dict(fixture["requests"][0].url.params)
    assert params == {"function": "CPI", "interval": "monthly", "datatype": "json",
                      "apikey": "fixture-alpha", "source": "trading_agents"}


@pytest.mark.parametrize("function,params", [
    ("CPI", {"interval": "daily"}), ("CPI", {"apikey": "injected"}),
    ("TREASURY_YIELD", {"maturity": "100year"}),
    ("UNEMPLOYMENT", {"symbol": "MSFT"}), ("REAL_GDP", {"interval": "monthly"}),
    ("CPI", {"datatype": "xml"}),
])
def test_gateway_rejects_invalid_economic_parameters(function, params):
    from tradingagents.workbench.public_gateway import GatewayError, _validate_alpha_vantage_request

    with pytest.raises(GatewayError):
        _validate_alpha_vantage_request({"function": function, "params": params})


def test_other_alpha_category_does_not_authorize_macro():
    from tests.test_public_gateway import _make_data_gateway
    from tradingagents.workbench.public_gateway import GatewayError

    fixture = _make_data_gateway(data_keys={"alpha_vantage": "fixture-alpha"},
                                 data_vendors={"news_data": "alpha_vantage"})
    with pytest.raises(GatewayError):
        fixture["gateway"].dispatch(fixture["capability"], method="POST", path="/data/alpha_vantage",
                                    headers={}, body={"function": "CPI", "params": {"interval": "monthly"}})
    assert fixture["requests"] == []


def test_macro_only_hosted_connectivity_uses_economic_endpoint(monkeypatch):
    from types import SimpleNamespace

    from tradingagents.workbench import worker

    hosted = SimpleNamespace(config=SimpleNamespace(
        data_vendors={"core_stock_apis": "yfinance", "macro_data": "alpha_vantage"},
        data_key_configured={"alpha_vantage": True},
    ))
    calls = []

    def request(function, params):
        calls.append((function, params))
        return {"data": [{"date": date.today().isoformat(), "value": "4.3"}]}

    monkeypatch.setattr(worker, "current_hosted_worker_context", lambda: hosted)
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    assert worker._hosted_probe_enabled(hosted, {}, "alpha_vantage")
    assert worker.probe_alpha_vantage()
    assert calls == [("UNEMPLOYMENT", {"datatype": "json"})]
    hosted.config.data_key_configured["alpha_vantage"] = False
    assert not worker._hosted_probe_enabled(hosted, {}, "alpha_vantage")


def test_mixed_alpha_connectivity_checks_macro_as_well_as_quotes(monkeypatch):
    from types import SimpleNamespace

    from tradingagents.workbench import worker

    hosted = SimpleNamespace(config=SimpleNamespace(
        data_vendors={"core_stock_apis": "alpha_vantage", "macro_data": "alpha_vantage"},
    ))
    calls = []

    def request(function, params):
        calls.append(function)
        if function == "UNEMPLOYMENT":
            return {"data": []}
        return {"Global Quote": {"01. symbol": "MSFT", "05. price": "400",
                                 "07. latest trading day": date.today().isoformat()}}

    monkeypatch.setattr(worker, "current_hosted_worker_context", lambda: hosted)
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", request)
    assert not worker.probe_alpha_vantage()
    assert "UNEMPLOYMENT" in calls
