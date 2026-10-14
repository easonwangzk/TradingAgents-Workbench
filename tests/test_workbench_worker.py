import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import StructuredTool, tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.workbench.contracts import decode_events, default_request, validate_request

FIXTURE_RUN_CAPABILITY = "11111111-1111-4111-8111-111111111111." + ("a" * 43)


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    import httpx
    import requests
    from curl_cffi import requests as curl_requests

    def forbidden(*args, **kwargs):
        pytest.fail("Offline worker test attempted a network request")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(curl_requests.Session, "request", forbidden)


def test_analysis_preserves_graph_contract_and_streams_reports(monkeypatch, native_probes):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph
    from tradingagents.workbench.worker import run_analysis

    baseline = deepcopy(DEFAULT_CONFIG)
    state = {
        "market_report": "market text", "sentiment_report": "social text",
        "news_report": "news text", "fundamentals_report": "fundamentals text",
        "investment_debate_state": {"bull_history": "bull", "bear_history": "bear",
                                    "judge_decision": "research decision"},
        "trader_investment_plan": "trade plan",
        "risk_debate_state": {"aggressive_history": "aggressive", "conservative_history": "conservative",
                              "neutral_history": "neutral", "judge_decision": "portfolio decision"},
        "final_trade_decision": "final decision", "messages": ["must not be emitted"],
    }
    events = []
    captured = {}

    class Graph:
        def __init__(self, *, selected_analysts, debug, config, callbacks):
            captured.update(analysts=selected_analysts, debug=debug, config=config)
            self.callback = callbacks[0]

        def propagate(self, company_name, trade_date, asset_type="stock"):
            assert (company_name, trade_date, asset_type) == ("MSFT", "2026-09-01", "stock")
            workflow = StateGraph(dict)
            workflow.add_node("Market Analyst", lambda _: state)
            workflow.add_edge(START, "Market Analyst")
            workflow.add_edge("Market Analyst", END)
            result = workflow.compile().invoke({})
            assert any(kind == "report" for kind, _ in events)
            run_id = uuid4()
            self.callback.on_chat_model_start({}, [[]], run_id=run_id)
            self.callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
                content="Hold", usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
            ))]]), run_id=run_id)
            self.callback.on_tool_start({"name": "get_stock_data"}, "secret input", run_id=uuid4())
            return result, "Hold"

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5.6-sol")
    request = validate_request({
        "analysis_date": "2026-09-01", "analysts": ["news", "market"],
        "debate_rounds": 2, "risk_rounds": 3,
        "data_vendors": {"news_data": "alpha_vantage,yfinance"},
    })
    result = run_analysis(request, lambda kind, payload: events.append((kind, payload)))
    assert result == {
        "decision": "Hold", "connectivity": result["connectivity"],
        "stats": {"llm_calls": 2, "tool_calls": 1, "input_tokens": 16, "output_tokens": 8},
        "reports": {
            "market_report": "market text", "sentiment_report": "social text",
            "news_report": "news text", "fundamentals_report": "fundamentals text",
            "investment_debate_state.bull_history": "bull",
            "investment_debate_state.bear_history": "bear",
            "investment_debate_state.judge_decision": "research decision",
            "trader_investment_plan": "trade plan",
            "risk_debate_state.aggressive_history": "aggressive",
            "risk_debate_state.conservative_history": "conservative",
            "risk_debate_state.neutral_history": "neutral",
            "risk_debate_state.judge_decision": "portfolio decision",
            "final_trade_decision": "final decision",
        },
    }
    config = captured["config"]
    assert [row["status"] for row in result["connectivity"]] == ["ok"] * 5
    assert captured["analysts"] == ["market", "news"] and captured["debug"] is False
    assert config["llm_provider"] == "azure"
    assert config["deep_think_llm"] == config["quick_think_llm"] == "gpt-5.6-sol"
    assert config["output_language"] == "Chinese"
    assert config["max_debate_rounds"] == 2 and config["max_risk_discuss_rounds"] == 3
    assert config["data_vendors"] == request["data_vendors"]
    assert config["tool_vendors"] == {} and config["checkpoint_enabled"] is False
    assert baseline == DEFAULT_CONFIG
    assert ("phase", {"stage": "market"}) in events
    assert "secret input" not in json.dumps(events)
    assert sum(kind == "report" for kind, _ in events) == len(result["reports"])


@pytest.mark.parametrize("asynchronous", [False, True])
def test_analysis_scrubs_real_tool_messages_before_model_state(monkeypatch, asynchronous, native_probes):
    from tradingagents.dataflows import alpha_vantage_indicator
    from tradingagents.graph import trading_graph
    from tradingagents.workbench.worker import run_analysis

    secret = "offline-alpha-key"
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", secret)

    def fail_request(function_name, params):
        raise RuntimeError(f"Request failed https://example.test/query?apikey={secret}")

    monkeypatch.setattr(alpha_vantage_indicator, "_make_api_request", fail_request)
    indicator = StructuredTool.from_function(alpha_vantage_indicator.get_indicator)

    @tool(response_format="content_and_artifact")
    def successful_tool() -> tuple:
        """Return structured content without changing market data."""
        return ([{"type": "text", "text": "2026-09-01 close=405.25"},
                 {"type": "text", "text": secret}], {"raw": secret})

    original_nodes = {name: ToolNode([indicator, successful_tool])
                      for name in ("market", "social", "news", "fundamentals")}
    observed = []

    class Graph:
        def __init__(self, **kwargs):
            self.tool_nodes = self._create_tool_nodes()

        def _create_tool_nodes(self):
            return original_nodes

        def propagate(self, company_name, trade_date, asset_type):
            for node in self.tool_nodes.values():
                assert isinstance(node, ToolNode)
                workflow = StateGraph(MessagesState)
                workflow.add_node("tools", node)

                def model(state):
                    messages = [message for message in state["messages"] if isinstance(message, ToolMessage)]
                    observed.extend(messages)
                    assert secret not in json.dumps([message.model_dump() for message in messages])
                    return {}

                workflow.add_node("model", model)
                workflow.add_edge(START, "tools")
                workflow.add_edge("tools", "model")
                workflow.add_edge("model", END)
                inputs = {"messages": [AIMessage(content="", tool_calls=[
                    {"name": "get_indicator", "args": {"symbol": company_name, "indicator": "rsi",
                     "curr_date": trade_date, "look_back_days": 10}, "id": "indicator"},
                    {"name": "successful_tool", "args": {}, "id": "success"},
                ])]}
                compiled = workflow.compile()
                if asynchronous:
                    asyncio.run(compiled.ainvoke(inputs))
                else:
                    compiled.invoke(inputs)
            return {}, "Hold"

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    run_analysis({"analysis_date": "2026-09-01"}, lambda *args: None)
    assert len(observed) == 8
    assert "apikey=[REDACTED]" in observed[0].content
    assert observed[1].content == [{"type": "text", "text": "2026-09-01 close=405.25"},
                                   {"type": "text", "text": "[REDACTED]"}]
    assert observed[1].artifact == {"raw": "[REDACTED]"}
    assert secret in indicator.invoke({"symbol": "MSFT", "indicator": "rsi",
                                      "curr_date": "2026-09-01", "look_back_days": 10})
    assert original_nodes["market"].tools_by_name["get_indicator"] is indicator


def test_analysis_tool_exception_is_not_swallowed(monkeypatch, native_probes):
    from tradingagents.graph import trading_graph
    from tradingagents.workbench.worker import run_analysis

    failure = RuntimeError("Provider unavailable")

    @tool
    def failing_tool() -> str:
        """Raise the original provider failure."""
        raise failure

    class Graph:
        def __init__(self, **kwargs):
            self.tool_nodes = self._create_tool_nodes()

        def _create_tool_nodes(self):
            return {"market": ToolNode([failing_tool])}

        def propagate(self, *args, **kwargs):
            workflow = StateGraph(MessagesState)
            workflow.add_node("tools", self.tool_nodes["market"])
            workflow.add_edge(START, "tools")
            workflow.add_edge("tools", END)
            workflow.compile().invoke({"messages": [AIMessage(content="", tool_calls=[
                {"name": "failing_tool", "args": {}, "id": "failure"},
            ])]})

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    with pytest.raises(RuntimeError) as caught:
        run_analysis({}, lambda *args: None)
    assert caught.value is failure


def test_callback_deduplicates_usage_and_uses_legacy_token_metadata():
    from tradingagents.workbench.worker import WorkbenchCallback

    events = []
    callback = WorkbenchCallback(lambda kind, payload: events.append((kind, payload)))
    run_id = uuid4()
    callback.on_chat_model_start({}, [[]], run_id=run_id)
    callback.on_chat_model_start({}, [[]], run_id=run_id)
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="OK"))]],
                         llm_output={"token_usage": {"prompt_tokens": 8, "completion_tokens": 3}})
    callback.on_llm_end(response, run_id=run_id)
    callback.on_llm_end(response, run_id=run_id)
    assert callback.stats() == {"llm_calls": 1, "tool_calls": 0, "input_tokens": 8, "output_tokens": 3}
    assert events[-1] == ("usage", callback.stats())
    callback.on_chain_start(None, {"secret": "prompt"}, name="untrusted prompt", run_id=uuid4())
    callback.on_tool_start({"name": "secret tool"}, "secret args", run_id=uuid4())
    callback.on_llm_error(RuntimeError("SDK raw secret"), run_id=uuid4())
    assert "secret" not in json.dumps(events)


def _set_request(monkeypatch, payload):
    run_id = str(uuid4())
    monkeypatch.setenv("TRADINGAGENTS_RUN_ID", run_id)
    monkeypatch.setenv("TRADINGAGENTS_RUN_REQUEST", base64.b64encode(json.dumps(payload).encode()).decode())
    return run_id


def test_main_outputs_only_protocol_events_and_redacts_reports(monkeypatch, capsys):
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {})
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-alpha-secret")
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra_id")

    def analysis(request, emit):
        import os

        assert os.getenv("AZURE_OPENAI_AUTH_MODE") == "managed_identity"
        print("raw SDK secret")
        print("raw stderr", file=sys.stderr)
        logging.getLogger("vendor").critical("raw logging secret")
        emit("report", {"section": "market_report", "content": "test-alpha-secret"})
        return {"decision": "Hold", "reports": {"market_report": "test-alpha-secret"},
                "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "connectivity": []}

    monkeypatch.setattr(worker, "run_analysis", analysis)
    assert worker.main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert all(line.startswith("TAWB1 ") for line in output.out.splitlines())
    events = decode_events(output.out.splitlines(), run_id)
    assert events[-1]["kind"] == "result"
    assert events[-1]["payload"]["reports"] == {"market_report": "[REDACTED]"}
    assert "raw" not in json.dumps(events)


@pytest.mark.parametrize("raw_request", ["%%%", "W10=", "e30=garbage", "eA==", "x" * 8193],
                         ids=["invalid-base64", "not-object", "trailing-data", "not-json", "oversized"])
def test_main_rejects_invalid_environment_request_without_analysis(monkeypatch, capsys, raw_request):
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {})
    monkeypatch.setenv("TRADINGAGENTS_RUN_REQUEST", raw_request)
    monkeypatch.setattr(worker, "run_analysis", lambda *args: pytest.fail("Invalid request ran"))
    assert worker.main() == 1
    output = capsys.readouterr()
    events = decode_events(output.out.splitlines(), run_id)
    assert events[-1]["kind"] == "error"
    assert output.err == ""


def test_main_never_emits_raw_exception(monkeypatch, capsys):
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {})

    def fail(*args):
        raise RuntimeError("Authorization: bearer raw-unknown-secret")

    monkeypatch.setattr(worker, "run_analysis", fail)
    assert worker.main() == 1
    output = capsys.readouterr()
    events = decode_events(output.out.splitlines(), run_id)
    assert events[-1] == {"kind": "error", "payload": {"message": "Worker failed; check configuration and provider access."},
                          "timestamp": events[-1]["timestamp"]}
    assert "raw-unknown-secret" not in output.out and output.err == ""


@pytest.mark.parametrize("failure", [None, "generic", "budget_exhausted"])
@pytest.mark.parametrize("previous_auth", [None, "entra_id"])
def test_main_hosted_close_failure_preserves_protocol(monkeypatch, capsys, failure, previous_auth):
    import httpx

    from tradingagents.workbench import public_worker, worker

    run_id = _set_request(monkeypatch, {})
    capability = run_id + "." + "a" * 43
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", capability)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps({
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
        "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
    }))
    if previous_auth is None:
        monkeypatch.delenv("AZURE_OPENAI_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", previous_auth)
    closed = []

    def fail_close(name):
        closed.append(name)
        print(capability)
        print("cleanup-secret", file=sys.stderr)
        logging.getLogger("vendor").critical("cleanup-secret")
        raise RuntimeError("cleanup-secret " + capability)

    backend_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"accepted": True}))
    backend = httpx.Client(transport=backend_transport)
    monkeypatch.setattr(backend_transport, "close", lambda: fail_close("backend"))
    telemetry_backend = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"accepted": True})))
    clients = iter((backend, telemetry_backend))
    monkeypatch.setattr(public_worker, "create_pinned_http_client", lambda: next(clients))
    monkeypatch.setattr(public_worker.GatewayDispatchTransport, "close", lambda self: fail_close("transport"))

    def analysis(request, emit):
        assert public_worker.current_hosted_worker_context() is not None
        assert os.environ.get("AZURE_OPENAI_AUTH_MODE") == previous_auth
        os.environ["AZURE_OPENAI_AUTH_MODE"] = "changed-during-run"
        emit("report", {"section": "market_report", "content": "Partial market report"})
        if failure == "generic":
            raise RuntimeError("original-secret " + capability)
        if failure is not None:
            raise public_worker.HostedProviderError(failure)
        return {"decision": "Hold", "reports": {"market_report": "Partial market report"},
                "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "connectivity": []}

    monkeypatch.setattr(worker, "run_analysis", analysis)
    previous_logging = logging.root.manager.disable
    try:
        assert worker.main() == (0 if failure is None else 1)
        assert logging.root.manager.disable == previous_logging
        assert os.environ.get("AZURE_OPENAI_AUTH_MODE") == previous_auth
    finally:
        logging.disable(previous_logging)
    assert closed == ["transport", "backend"]
    assert backend.is_closed
    assert telemetry_backend.is_closed
    assert public_worker.current_hosted_worker_context() is None
    output = capsys.readouterr()
    assert output.err == ""
    assert all(line.startswith("TAWB1 ") for line in output.out.splitlines())
    assert capability not in output.out and "cleanup-secret" not in output.out and "original-secret" not in output.out
    events = decode_events(output.out.splitlines(), run_id)
    assert [event["kind"] for event in events] == ["report", "result" if failure is None else "error"]
    assert events[0]["payload"] == {"section": "market_report", "content": "Partial market report"}
    if failure is None:
        assert events[-1]["payload"]["decision"] == "Hold"
        assert events[-1]["payload"]["reports"] == {"market_report": "Partial market report"}
    elif failure == "generic":
        assert events[-1]["payload"] == {"message": "Worker failed; check configuration and provider access."}
    else:
        assert events[-1]["payload"] == {"code": "budget_exhausted", "message": "Hosted worker provider budget is exhausted."}


def test_main_hosted_telemetry_relay_failure_preserves_console_events(monkeypatch, capsys):
    import httpx

    from tradingagents.workbench import public_worker, worker

    run_id = _set_request(monkeypatch, {})
    capability = run_id + "." + "a" * 43
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", capability)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps({
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
        "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
    }))
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            raise httpx.ReadTimeout("telemetry-timeout")
        return httpx.Response(200, json={"status_code": 200, "body": {"ok": True}})

    monkeypatch.setattr(public_worker, "create_pinned_http_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))

    def analysis(request, emit):
        emit("report", {"section": "market_report", "content": "Partial market report"})
        return {"decision": "Hold", "reports": {"market_report": "Partial market report"},
                "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "connectivity": []}

    monkeypatch.setattr(worker, "run_analysis", analysis)
    assert worker.main() == 0
    output = capsys.readouterr()
    events = decode_events(output.out.splitlines(), run_id)
    assert [event["kind"] for event in events] == ["report", "result"]
    assert events[-1]["payload"]["decision"] == "Hold"
    assert output.err == ""
    assert any(request.url.path.endswith("/events") for request in requests)


def test_main_hosted_telemetry_client_creation_failure_preserves_research(monkeypatch, capsys):
    import httpx

    from tradingagents.workbench import public_worker, worker

    run_id = _set_request(monkeypatch, {})
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", run_id + "." + "a" * 43)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps({
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
        "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
    }))
    calls = [httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))]

    def pinned():
        if calls:
            return calls.pop()
        raise RuntimeError("telemetry-unavailable")

    monkeypatch.setattr(public_worker, "create_pinned_http_client", pinned)
    monkeypatch.setattr(worker, "run_analysis", lambda request, emit: {
        "decision": "Hold", "reports": {"market_report": "Complete"}, "connectivity": [],
        "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
    })
    assert worker.main() == 0
    assert decode_events(capsys.readouterr().out.splitlines(), run_id)[-1]["kind"] == "result"


def test_main_hosted_telemetry_relay_posts_protocol_lines(monkeypatch, capsys):
    import httpx

    from tradingagents.workbench import public_worker, worker

    run_id = _set_request(monkeypatch, {})
    capability = run_id + "." + "a" * 43
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", capability)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps({
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
        "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
    }))
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"status_code": 200, "body": {"ok": True}})

    monkeypatch.setattr(public_worker, "create_pinned_http_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))

    def analysis(request, emit):
        emit("report", {"section": "market_report", "content": "Partial market report"})
        return {"decision": "Hold", "reports": {"market_report": "Partial market report"},
                "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "connectivity": []}

    monkeypatch.setattr(worker, "run_analysis", analysis)
    assert worker.main() == 0
    output = capsys.readouterr()
    console_events = decode_events(output.out.splitlines(), run_id)
    event_posts = [json.loads(request.content.decode("utf-8")) for request in requests if request.url.path.endswith("/events")]
    assert [event["kind"] for event in console_events] == ["report", "result"]
    assert [decode_events(post["lines"], run_id)[0]["kind"] for post in event_posts] == ["report", "result"]
    assert output.err == ""


def test_main_hosted_telemetry_relay_posts_multipart_protocol_lines(monkeypatch, capsys):
    import httpx

    from tradingagents.workbench import public_worker, worker

    run_id = _set_request(monkeypatch, {})
    capability = run_id + "." + "a" * 43
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", capability)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", json.dumps({
        "mode": "api_key", "provider": "openai", "quick_model": "gpt-5", "deep_model": "gpt-5",
        "asset_type": "stock", "data_vendors": default_request()["data_vendors"],
    }))
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"status_code": 200, "body": {"ok": True}})

    monkeypatch.setattr(public_worker, "create_pinned_http_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))

    large_report = "".join(f"{index:08x}" for index in range(160000))

    def analysis(request, emit):
        emit("report", {"section": "market_report", "content": large_report})
        return {"decision": "Hold", "reports": {"market_report": large_report},
                "stats": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "connectivity": []}

    monkeypatch.setattr(worker, "run_analysis", analysis)
    assert worker.main() == 0
    output = capsys.readouterr()
    console_events = decode_events(output.out.splitlines(), run_id)
    event_posts = [json.loads(request.content.decode("utf-8")) for request in requests if request.url.path.endswith("/events")]
    assert [event["kind"] for event in console_events] == ["report", "result"]
    assert len(event_posts) == 2
    assert all(len(post["lines"]) > 1 for post in event_posts)
    assert [decode_events(post["lines"], run_id)[0]["kind"] for post in event_posts] == ["report", "result"]
    assert output.err == ""


def test_hosted_telemetry_relay_discards_queued_events_after_shutdown_deadline():
    from tradingagents.workbench.worker import _HostedTelemetryRelay

    started = threading.Event()
    release = threading.Event()
    extra_call = threading.Event()
    calls = []

    class HostedContext:
        def post_events(self, lines):
            calls.append(lines)
            if len(calls) == 1:
                started.set()
                release.wait(timeout=1)
            else:
                extra_call.set()

    relay = _HostedTelemetryRelay(HostedContext(), max_pending_bytes=32, shutdown_seconds=0.01)
    relay.submit(["one"])
    relay.submit(["two"])
    relay.submit(["three"])
    assert started.wait(timeout=1)

    relay.close()
    release.set()
    time.sleep(0.05)

    assert calls == [["one"]]
    assert not extra_call.is_set()


def test_hosted_telemetry_relay_close_does_not_close_inflight_client(monkeypatch):
    import httpx

    from tradingagents.workbench import public_worker
    from tradingagents.workbench.worker import _HostedTelemetryRelay

    started = threading.Event()
    release = threading.Event()
    clients = []

    def pinned():
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"accepted": True})))
        clients.append(client)
        return client

    monkeypatch.setattr(public_worker, "create_pinned_http_client", pinned)

    class HostedContext:
        def telemetry_context(self):
            client = pinned()

            class TelemetryContext:
                def post_events(self, lines):
                    started.set()
                    release.wait(timeout=1)
                    assert not client.is_closed

                def close(self):
                    client.close()

            return TelemetryContext()

    relay = _HostedTelemetryRelay(HostedContext(), shutdown_seconds=0.01)
    relay.submit(["one"])
    assert started.wait(timeout=1)
    relay.close()
    assert not clients[0].is_closed
    release.set()
    relay._thread.join(timeout=1)
    assert clients[0].is_closed


def test_hosted_telemetry_relay_drops_nonimportant_events_when_byte_budget_is_full():
    from tradingagents.workbench.worker import _HostedTelemetryRelay

    started = threading.Event()
    release = threading.Event()
    calls = []

    class HostedContext:
        def post_events(self, lines):
            calls.append(lines)
            if len(calls) == 1:
                started.set()
                release.wait(timeout=1)

    relay = _HostedTelemetryRelay(HostedContext(), max_pending_bytes=8, shutdown_seconds=0.2)
    relay.submit(["1111"])
    assert started.wait(timeout=1)
    relay.submit(["2222"])
    relay.submit(["3333"])
    release.set()
    relay.close()

    assert calls == [["1111"], ["2222"]]


def test_hosted_telemetry_relay_keeps_important_event_when_byte_budget_is_full():
    from tradingagents.workbench.worker import _HostedTelemetryRelay

    started = threading.Event()
    release = threading.Event()
    calls = []

    class HostedContext:
        def post_events(self, lines):
            calls.append(lines)
            if len(calls) == 1:
                started.set()
                release.wait(timeout=1)

    relay = _HostedTelemetryRelay(HostedContext(), max_pending_bytes=8, shutdown_seconds=0.2)
    relay.submit(["1111"])
    assert started.wait(timeout=1)
    relay.submit(["2222"])
    relay.submit(["3333"], important=True)
    release.set()
    relay.close()

    assert calls == [["1111"], ["3333"]]


def test_oversize_important_telemetry_is_dropped_as_a_whole_message():
    from tradingagents.workbench.worker import _HostedTelemetryRelay

    calls = []

    class HostedContext:
        def post_events(self, lines):
            calls.append(lines)

    relay = _HostedTelemetryRelay(HostedContext(), max_pending_bytes=8)
    relay.submit(["11111", "22222"], important=True)
    relay.close()
    assert calls == []


def test_main_hosted_mode_fails_closed_before_legacy_env_path(monkeypatch, capsys):
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {})
    monkeypatch.setenv("TAWB_WORKER_MODE", "hosted")
    monkeypatch.setenv("TAWB_BACKEND_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("TAWB_RUN_CAPABILITY", FIXTURE_RUN_CAPABILITY)
    monkeypatch.setenv("TAWB_PUBLIC_CONFIG", "{")
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra_id")
    monkeypatch.setattr(worker, "run_analysis", lambda *args: pytest.fail("Hosted config error must fail before analysis"))

    assert worker.main() == 1
    output = capsys.readouterr()
    events = decode_events(output.out.splitlines(), run_id)
    assert events[-1]["kind"] == "error"
    assert "fixture-run-capability" not in output.out
    assert output.err == ""


def test_run_analysis_uses_hosted_provider_config_and_explicit_client_factory(monkeypatch):
    from tradingagents.graph import trading_graph
    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext
    from tradingagents.workbench.worker import run_analysis

    captured = {}

    class Graph:
        def __init__(self, *, selected_analysts, debug, config, callbacks, client_factory=None):
            captured["analysts"] = selected_analysts
            captured["debug"] = debug
            captured["config"] = config
            captured["client_factory"] = client_factory

        def propagate(self, company_name, trade_date, asset_type="stock"):
            return {}, "Hold"

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    monkeypatch.setattr("tradingagents.workbench.worker.run_connectivity", lambda *args, **kwargs: {"connectivity": []})
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "hostile-azure-model")
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": FIXTURE_RUN_CAPABILITY,
            "TRADINGAGENTS_RUN_ID": FIXTURE_RUN_CAPABILITY.split(".")[0],
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "anthropic",
                    "quick_model": "claude-sonnet-5",
                    "deep_model": "claude-fable-5",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )

    with HostedWorkerContext(config).activate():
        result = run_analysis({"analysis_date": "2026-09-01"}, lambda *args: None)

    assert result["decision"] == "Hold"
    assert captured["config"]["llm_provider"] == "anthropic"
    assert captured["config"]["quick_think_llm"] == "claude-sonnet-5"
    assert captured["config"]["deep_think_llm"] == "claude-fable-5"
    assert callable(captured["client_factory"])


@pytest.fixture
def native_probes(monkeypatch):
    from tradingagents.dataflows import alpha_vantage_common, fred, polymarket, y_finance
    from tradingagents.llm_clients.azure_client import AzureOpenAIClient

    today = date.today().isoformat()
    values = {
        "yfinance": f"# Stock data for MSFT\n\nDate,Open,High,Low,Close,Volume\n{today},400,410,399,405,1000\n",
        "alpha_vantage": json.dumps({"Global Quote": {"01. symbol": "MSFT", "05. price": "405.0",
                                                       "07. latest trading day": today}}),
        "fred": f"## FRED: Treasury (DGS10)\n| Date | Value |\n|---|---|\n| {today} | 4.3 |\n",
        "polymarket": f"## Polymarket prediction markets\n- **Fed rate cut?** \u2014 Yes 54% ($1,000 volume, resolves {today})\n",
        "foundry": "OK",
    }
    calls = []

    def value(provider):
        calls.append(provider)
        result = values[provider]
        if isinstance(result, Exception):
            raise result
        return result

    def yahoo(symbol, start_date, end_date):
        assert symbol == "MSFT" and end_date == today
        assert 7 <= (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days <= 30
        return value("yfinance")

    def alpha(function_name, params):
        assert function_name == "GLOBAL_QUOTE" and params == {"symbol": "MSFT"}
        return value("alpha_vantage")

    def macro(indicator, curr_date, look_back_days=None):
        assert indicator == "DGS10" and curr_date == today and look_back_days == 30
        return value("fred")

    def prediction(topic, limit=None):
        assert topic == "Fed" and limit == 1
        return value("polymarket")

    def get_llm(client):
        assert client.kwargs["max_tokens"] == 16
        assert client.kwargs["max_retries"] == 0
        assert client.kwargs["timeout"] == 30

        class Model:
            def invoke(self, prompt, config=None):
                assert prompt == "Reply only OK."
                callback = config["callbacks"][0]
                run_id = uuid4()
                callback.on_chat_model_start({}, [[]], run_id=run_id)
                response = AIMessage(content=value("foundry"), usage_metadata={
                    "input_tokens": 5, "output_tokens": 1, "total_tokens": 6,
                })
                callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=response)]]), run_id=run_id)
                return response

        return Model()

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "alpha-offline-key")
    monkeypatch.setenv("FRED_API_KEY", "fred-offline-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test.services.ai.azure.com/openai/v1/")
    monkeypatch.setattr(y_finance, "get_YFin_data_online", yahoo)
    monkeypatch.setattr(alpha_vantage_common, "_make_api_request", alpha)
    monkeypatch.setattr(fred, "get_macro_data", macro)
    monkeypatch.setattr(polymarket, "get_prediction_markets", prediction)
    monkeypatch.setattr(AzureOpenAIClient, "get_llm", get_llm)
    return values, calls


def test_connectivity_uses_native_probes_and_one_model_call(native_probes):
    from tradingagents.workbench.worker import run_connectivity

    _, calls = native_probes
    events = []
    result = run_connectivity(validate_request({"mode": "connectivity", "analysis_date": "2020-01-01"}),
                              lambda kind, payload: events.append((kind, payload)))
    assert calls == ["yfinance", "alpha_vantage", "fred", "polymarket", "foundry"]
    assert result["decision"] is None and result["reports"] == {}
    assert result["stats"] == {"llm_calls": 1, "tool_calls": 0, "input_tokens": 5, "output_tokens": 1}
    assert [row["status"] for row in result["connectivity"]] == ["ok"] * 5
    for row in result["connectivity"]:
        assert set(row) == {"provider", "status", "detail", "elapsed_seconds"}
        assert row["elapsed_seconds"] >= 0


def test_analysis_preflight_runs_before_graph_and_keeps_failed_provider(monkeypatch, native_probes):
    from tradingagents.graph import trading_graph
    from tradingagents.workbench.worker import run_analysis

    values, calls = native_probes
    values["alpha_vantage"] = RuntimeError("Provider unavailable")
    events = []

    class Graph:
        def __init__(self, *, selected_analysts, config, callbacks, **kwargs):
            assert calls == ["yfinance", "alpha_vantage", "fred", "polymarket", "foundry"]
            snapshots = [payload["connectivity"] for kind, payload in events if kind == "connectivity"]
            assert [len(rows) for rows in snapshots] == [1, 2, 3, 4, 5]
            assert snapshots[-1][1]["status"] == "error"
            assert config["data_vendors"]["core_stock_apis"] == "yfinance"
            self.callback = callbacks[0]

        def propagate(self, *args, **kwargs):
            run_id = uuid4()
            self.callback.on_chat_model_start({}, [[]], run_id=run_id)
            self.callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
                content="Hold", usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
            ))]]), run_id=run_id)
            return {"market_report": "Research after preflight"}, "Hold"

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    result = run_analysis({"analysts": ["market"]}, lambda kind, payload: events.append((kind, payload)))
    assert [row["status"] for row in result["connectivity"]] == ["ok", "error", "ok", "ok", "ok"]
    assert result["reports"]["market_report"] == "Research after preflight"
    assert result["stats"] == {"llm_calls": 2, "tool_calls": 0, "input_tokens": 16, "output_tokens": 8}
    assert not any(kind == "result" for kind, _ in events)
    for field in ("llm_calls", "input_tokens", "output_tokens"):
        counts = [payload[field] for kind, payload in events if kind == "usage"]
        assert counts == sorted(counts)


def test_main_preserves_connectivity_events_when_analysis_fails(monkeypatch, native_probes, capsys):
    from tradingagents.graph import trading_graph
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {"mode": "analysis"})

    class Graph:
        def __init__(self, **kwargs):
            raise RuntimeError("Graph initialization failed with fred-offline-key")

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Graph)
    assert worker.main() == 1
    output = capsys.readouterr()
    events = decode_events(output.out.splitlines(), run_id)
    snapshots = [event["payload"]["connectivity"] for event in events if event["kind"] == "connectivity"]
    assert [len(rows) for rows in snapshots] == [1, 2, 3, 4, 5]
    assert [row["status"] for row in snapshots[-1]] == ["ok"] * 5
    assert events[-1]["kind"] == "error"
    assert not any(event["kind"] == "result" for event in events)
    assert "fred-offline-key" not in output.out and output.err == ""


def test_connectivity_skips_unconfigured_providers_without_demo_keys(monkeypatch, native_probes):
    from tradingagents.workbench.worker import run_connectivity

    _, calls = native_probes
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY")
    monkeypatch.delenv("FRED_API_KEY")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    result = run_connectivity(validate_request({"mode": "connectivity"}), lambda *args: None)
    statuses = {row["provider"]: row["status"] for row in result["connectivity"]}
    assert statuses == {"yfinance": "ok", "alpha_vantage": "unconfigured", "fred": "unconfigured",
                        "polymarket": "ok", "foundry": "unconfigured"}
    assert calls == ["yfinance", "polymarket"]


@pytest.mark.parametrize("provider,value,status", [
    ("yfinance", "No data", "no_data"),
    ("yfinance", "Date,Close\n2020-01-01,400\n", "no_data"),
    ("alpha_vantage", '{"Global Quote":{}}', "no_data"),
    ("alpha_vantage", '{"Error Message":"secret invalid request"}', "error"),
    ("fred", "## FRED\nNo observations", "no_data"),
    ("polymarket", "## Polymarket\nNo open prediction markets matched", "no_data"),
    ("polymarket", "Polymarket data is currently unavailable (network error: raw secret)", "error"),
    ("polymarket", "## Polymarket\n- **Fed?** Yes 150% ($1,000 volume, resolves 2099-01-01)", "no_data"),
    ("foundry", "", "no_data"),
    ("yfinance", RuntimeError("SDK secret"), "error"),
], ids=["yahoo-empty", "yahoo-stale", "alpha-empty", "alpha-error", "fred-empty",
        "poly-empty", "poly-network", "poly-invalid-odds", "model-empty", "vendor-exception"])
def test_connectivity_requires_usable_data_and_safe_diagnostics(native_probes, provider, value, status):
    from tradingagents.workbench.worker import run_connectivity

    values, _ = native_probes
    values[provider] = value
    result = run_connectivity(validate_request({"mode": "connectivity"}), lambda *args: None)
    row = next(row for row in result["connectivity"] if row["provider"] == provider)
    assert row["status"] == status
    assert "secret" not in json.dumps(result)


def test_connectivity_classifies_typed_errors_and_continues(native_probes):
    from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorRateLimitError
    from tradingagents.workbench.worker import run_connectivity

    values, calls = native_probes
    values["alpha_vantage"] = VendorRateLimitError("raw key quota")
    values["fred"] = VendorNotConfiguredError("raw key rejected")
    result = run_connectivity(validate_request({"mode": "connectivity"}), lambda *args: None)
    statuses = {row["provider"]: row["status"] for row in result["connectivity"]}
    assert statuses["alpha_vantage"] == "rate_limited"
    assert statuses["fred"] == "auth_error"
    assert calls[-1] == "foundry"


def test_main_connectivity_does_not_run_analysis(monkeypatch, capsys, native_probes):
    from tradingagents.workbench import worker

    run_id = _set_request(monkeypatch, {"mode": "connectivity"})
    monkeypatch.setattr(worker, "run_analysis", lambda *args: pytest.fail("Analysis must not run"))
    assert worker.main() == 0
    events = decode_events(capsys.readouterr().out.splitlines(), run_id)
    assert len(events[-1]["payload"]["connectivity"]) == 5


def test_alpha_probe_uses_actual_native_request_with_environment_key(monkeypatch):
    import requests

    from tradingagents.workbench.worker import probe_alpha_vantage

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "alpha-offline-key")
    captured = {}

    def get(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"Global Quote": {
            "01. symbol": "MSFT", "05. price": "405.00",
            "07. latest trading day": date.today().isoformat(),
        }}).encode()
        return response

    monkeypatch.setattr(requests, "get", get)
    assert probe_alpha_vantage() is True
    assert captured["url"] == "https://www.alphavantage.co/query"
    assert captured["params"]["function"] == "GLOBAL_QUOTE"
    assert captured["params"]["symbol"] == "MSFT"
    assert captured["params"]["apikey"] == "alpha-offline-key"
    assert captured["timeout"] == 30


@pytest.mark.parametrize("failure,status", [
    ("unauthorized", "auth_error"), ("throttled", "rate_limited"),
    ("model-timeout", "timeout"), ("http-timeout", "timeout"),
])
def test_connectivity_sdk_failures_have_safe_classifications(native_probes, failure, status):
    import httpx
    from openai import APITimeoutError, AuthenticationError, RateLimitError

    from tradingagents.workbench.worker import run_connectivity

    values, _ = native_probes
    http_request = httpx.Request("POST", "https://test.openai.azure.com/openai/v1/responses")
    errors = {
        "unauthorized": AuthenticationError("raw bearer secret", response=httpx.Response(401, request=http_request), body=None),
        "throttled": RateLimitError("raw bearer secret", response=httpx.Response(429, request=http_request), body=None),
        "model-timeout": APITimeoutError(request=http_request),
        "http-timeout": httpx.ReadTimeout("raw bearer secret"),
    }
    values["foundry"] = errors[failure]
    result = run_connectivity(validate_request({"mode": "connectivity"}), lambda *args: None)
    assert result["connectivity"][-1]["status"] == status
    assert "raw bearer secret" not in json.dumps(result)


def test_fred_probe_uses_native_dated_data(monkeypatch):
    from tradingagents.dataflows import fred
    from tradingagents.workbench.worker import probe_fred

    calls = []
    today = date.today().isoformat()

    def request(path, params):
        calls.append((path, params))
        assert params["series_id"] == "DGS10"
        assert params["realtime_start"] == params["realtime_end"] == min(today, fred._fred_today())
        if path == "series":
            return {"seriess": [{"title": "Treasury yield", "units": "Percent", "frequency": "Daily"}]}
        return {"observations": [{"date": today, "value": "4.3"}]}

    monkeypatch.setattr(fred, "_request", request)
    assert probe_fred() is True
    assert [path for path, _ in calls] == ["series", "series/observations"]


@pytest.mark.parametrize("prices,expected", [(["0.54", "0.46"], True), ([], False), (["invalid"], False)])
def test_polymarket_probe_requires_native_market_probability(monkeypatch, prices, expected):
    from tradingagents.dataflows import polymarket
    from tradingagents.workbench.worker import probe_polymarket

    def request(path, params):
        assert path == "public-search" and params == {"q": "Fed", "limit_per_type": 20}
        return {"events": [{"markets": [{
            "question": "Fed rate cut?", "closed": False, "outcomePrices": json.dumps(prices),
            "outcomes": '["Yes", "No"]', "volumeNum": 1000,
            "endDate": (date.today() + timedelta(days=30)).isoformat() + "T00:00:00Z",
        }]}]}

    monkeypatch.setattr(polymarket, "_request", request)
    assert probe_polymarket() is expected


def test_module_entrypoint_rejects_missing_request_offline():
    environment = {
        "TRADINGAGENTS_RUN_ID": str(uuid4()),
        "PYTHONIOENCODING": "utf-8",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if name in os.environ:
            environment[name] = os.environ[name]
    completed = subprocess.run(
        [sys.executable, "-m", "tradingagents.workbench.worker"],
        env=environment, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 1
    assert completed.stderr == ""
    events = decode_events(completed.stdout.splitlines(), environment["TRADINGAGENTS_RUN_ID"])
    assert len(events) == 1 and events[0]["kind"] == "error"

