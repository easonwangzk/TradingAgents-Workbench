import json
from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.workbench.contracts import validate_request


@pytest.mark.unit
def test_workbench_accepts_etf_asset_type():
    request = validate_request({"asset_type": "etf", "ticker": "VOO"})
    assert request["asset_type"] == "etf"
    assert request["ticker"] == "VOO"


@pytest.fixture
def fund_vendor(monkeypatch):
    import yfinance as yf

    funds = SimpleNamespace(
        description="Tracks a broad equity index.",
        fund_overview={"categoryName": "Large Blend", "family": "Example"},
        fund_operations=pd.DataFrame({"VOO": [0.0003, float('nan'), 496754.28]},
                         index=["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"]),
        asset_classes={"stockPosition": 0.99, "bondPosition": None},
        top_holdings=pd.DataFrame({"Name": ["Example Company"], "Holding Percent": [0.07]},
                                  index=pd.Index(["EXAMPLE"], name="Symbol")),
        equity_holdings=pd.DataFrame({"VOO": [0.03974]}, index=["Price/Earnings"]), bond_holdings=pd.DataFrame(),
        bond_ratings={}, sector_weightings={"technology": 0.3},
    )

    def history(**kwargs):
        assert kwargs["end"] == "2026-09-09"
        return pd.DataFrame({"Dividends": [1.7, 9.0], "Capital Gains": [0, 0]},
                            index=pd.to_datetime(["2026-06-30", "2026-09-09"]))

    vendor = SimpleNamespace(info={"symbol": "VOO", "quoteType": "ETF",
                                  "longName": "Example Index ETF", "currency": "USD",
                                  "totalAssets": 1000000, "navPrice": float('inf')},
                             funds_data=funds, history=history)
    monkeypatch.setattr(yf, "Ticker", lambda symbol: vendor)
    return vendor


@pytest.mark.unit
def test_etf_profile_preserves_available_facts_and_snapshot_limits(fund_vendor):
    from tradingagents.dataflows.yfinance_etf import get_etf_profile

    result = json.loads(get_etf_profile("VOO", "2026-09-08"))
    assert result["status"] == "ok"
    assert result["identity"]["name"] == "Example Index ETF"
    assert result["metadata_as_of"] is None
    assert result["retrieved_at"]
    assert result["analysis_date"] == "2026-09-08"
    assert result["quote"]["navPrice"] is None
    assert result["fund_data"]["fund_operations"][0]["VOO"] == 0.0003
    assert result["fund_data"]["fund_operations"][1]["VOO"] is None
    assert len(result["fund_data"]["fund_operations"]) == 2
    assert "equity_holdings" not in result["fund_data"]
    assert result["snapshot_evidence_scope"] == "current_only_not_historical"
    assert result["fund_data"]["top_holdings"][0]["Symbol"] == "EXAMPLE"
    assert result["distributions"] == [{"date": "2026-06-30", "dividend": 1.7, "capital_gain": 0.0}]
    assert "not point-in-time" in " ".join(result["warnings"])


@pytest.mark.unit
@pytest.mark.parametrize("quote_type,symbol,status", [("EQUITY", "VOO", "identity_mismatch"),
                                                       ("ETF", "OTHER", "identity_mismatch"),
                                                       (None, "VOO", "unverified_identity")])
def test_etf_profile_does_not_accept_wrong_or_unverified_identity(fund_vendor, quote_type, symbol, status):
    from tradingagents.dataflows.yfinance_etf import get_etf_profile

    fund_vendor.info.update(quoteType=quote_type, symbol=symbol)
    result = json.loads(get_etf_profile("VOO", "2026-09-08"))
    assert result["status"] == status
    assert result["fund_data"] == {}


@pytest.mark.unit
def test_etf_profile_keeps_missing_provider_data_explicit(fund_vendor):
    from tradingagents.dataflows.yfinance_etf import get_etf_profile

    class UnavailableFunds:
        @property
        def fund_overview(self):
            raise RuntimeError("Provider unavailable")

    fund_vendor.funds_data = UnavailableFunds()
    result = json.loads(get_etf_profile("VOO", "2026-09-08"))
    assert result["status"] == "partial"
    assert result["fund_data"] == {}
    assert "fund_data unavailable" in result["warnings"]


@pytest.mark.unit
@pytest.mark.parametrize("asset_type,expected_tools", [
    ("etf", {"get_etf_profile"}),
    ("stock", {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"}),
])
def test_fundamentals_analyst_selects_instrument_tools(asset_type, expected_tools):
    from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst

    class Model:
        def bind_tools(self, tools):
            def answer(prompt):
                assert {tool.name for tool in tools} == expected_tools
                if asset_type == "etf":
                    assert "daily reset" in prompt.to_string()
                    assert "not point-in-time" in prompt.to_string()
                return AIMessage(content="Fund-specific research")
            return RunnableLambda(answer)

    result = create_fundamentals_analyst(Model())({
        "asset_type": asset_type, "company_of_interest": "VOO", "trade_date": "2026-09-08",
        "messages": [HumanMessage(content="Analyze the instrument")],
    })
    assert result["fundamentals_report"] == "Fund-specific research"


@pytest.mark.unit
def test_etf_profile_tool_is_executable_in_real_graph(fund_vendor):
    from langgraph.graph import END, START, MessagesState, StateGraph

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    node = graph._create_tool_nodes()["fundamentals"]
    message = AIMessage(content="", tool_calls=[{
        "name": "get_etf_profile", "args": {"symbol": "VOO", "curr_date": "2026-09-08"},
        "id": "etf-profile", "type": "tool_call",
    }])
    workflow = StateGraph(MessagesState)
    workflow.add_node("fundamentals_tools", node)
    workflow.add_edge(START, "fundamentals_tools")
    workflow.add_edge("fundamentals_tools", END)
    response = workflow.compile().invoke({"messages": [message]})["messages"][-1]
    assert response.status == "success"
    assert json.loads(response.content)["identity"]["quote_type"] == "ETF"


@pytest.mark.unit
@pytest.mark.parametrize("side", ["bull", "bear"])
def test_etf_debate_uses_fund_exposure_not_company_competition(side):
    from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
    from tradingagents.agents.researchers.bull_researcher import create_bull_researcher

    def answer(prompt):
        assert "ETF fundamentals report" in prompt
        assert "fund exposures" in prompt
        assert "Growth Potential: Highlight the company's" not in prompt
        assert "Competitive Weaknesses" not in prompt
        return AIMessage(content="Evidence-based fund assessment")

    factory = create_bull_researcher if side == "bull" else create_bear_researcher
    result = factory(RunnableLambda(answer))({
        "asset_type": "etf", "company_of_interest": "VOO", "trade_date": "2026-09-08",
        "investment_debate_state": {"count": 0}, "market_report": "Dated prices",
        "sentiment_report": "Limited sample", "news_report": "Macro evidence",
        "fundamentals_report": "Fund holdings and fees",
    })
    assert "Evidence-based fund assessment" in result["investment_debate_state"]["current_response"]


@pytest.mark.unit
def test_etf_profile_phase_is_visible_to_workbench():
    from tradingagents.workbench.worker import WorkbenchCallback

    events = []
    WorkbenchCallback(lambda kind, payload: events.append((kind, payload))).on_tool_start(
        {"name": "get_etf_profile"}, "{}")
    assert ("phase", {"stage": "tool:get_etf_profile"}) in events


@pytest.mark.unit
def test_etf_contract_rejects_misleading_fundamental_provider():
    with pytest.raises(ValueError, match="ETF fundamentals require yfinance"):
        validate_request({"asset_type": "etf", "data_vendors": {"fundamental_data": "alpha_vantage"}})


@pytest.mark.unit
def test_workbench_etf_preflight_rejects_wrong_identity_before_model(monkeypatch, fund_vendor):
    from tradingagents.workbench import worker

    fund_vendor.info["quoteType"] = "EQUITY"
    monkeypatch.setattr(worker, "run_connectivity", lambda *args, **kwargs: {"connectivity": []})
    events = []
    with pytest.raises(ValueError, match="ETF identity"):
        worker.run_analysis({"ticker": "VOO", "asset_type": "etf", "analysis_date": "2026-09-08"},
                            lambda kind, payload: events.append((kind, payload)))
    assert any(kind == "report" and payload["section"] == "etf_profile" for kind, payload in events)
