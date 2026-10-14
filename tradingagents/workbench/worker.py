"""Cloud entry point: python -m tradingagents.workbench.worker."""

import base64
import collections
import csv
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from copy import deepcopy
from datetime import date, timedelta
from uuid import UUID

from langchain_core.runnables.config import set_config_context

from cli.stats_handler import StatsCallbackHandler

from .contracts import encode_event, scrub_secrets, validate_request
from .public_worker import (
    HostedProviderError,
    current_hosted_worker_context,
    hosted_worker_context_from_environment,
)

_REPORT_FIELDS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
    "trader_investment_plan", "final_trade_decision",
)
_DEBATE_FIELDS = {
    "investment_debate_state": ("bull_history", "bear_history", "judge_decision"),
    "risk_debate_state": ("aggressive_history", "conservative_history", "neutral_history", "judge_decision"),
}
_STAGES = {
    "Market Analyst": "market", "Social Analyst": "social", "Sentiment Analyst": "social",
    "News Analyst": "news", "Fundamentals Analyst": "fundamentals",
    "Bull Researcher": "bull_researcher", "Bear Researcher": "bear_researcher",
    "Research Manager": "research_manager", "Trader": "trader",
    "Aggressive Analyst": "aggressive_risk", "Conservative Analyst": "conservative_risk",
    "Neutral Analyst": "neutral_risk", "Portfolio Manager": "portfolio_manager",
}
_TOOLS = (
    "get_etf_profile", "get_verified_market_snapshot",
    "get_stock_data", "get_indicators", "get_fundamentals", "get_balance_sheet",
    "get_cashflow", "get_income_statement", "get_news", "get_global_news",
    "get_insider_transactions", "get_macro_indicators", "get_prediction_markets",
)
CONNECTIVITY_STATUSES = ("ok", "unconfigured", "no_data", "rate_limited", "auth_error", "timeout", "error")
_DETAILS = {
    "ok": "Native provider returned usable data.",
    "unconfigured": "Required environment configuration is missing.",
    "no_data": "Provider returned no usable data or signal.",
    "rate_limited": "Provider rate limit or quota reached.",
    "auth_error": "Provider credentials or access were rejected.",
    "timeout": "Provider request timed out.",
    "error": "Provider request failed.",
}


class _HostedTelemetryRelay:
    def __init__(self, hosted_context, *, max_pending_bytes=2 * 1024 * 1024, shutdown_seconds=2.0):
        self._hosted_context = (hosted_context.telemetry_context()
                                if hasattr(hosted_context, "telemetry_context") else hosted_context)
        self._owns_context = self._hosted_context is not hosted_context
        self._shutdown_seconds = shutdown_seconds
        self._max_pending_bytes = max_pending_bytes
        self._pending = collections.deque()
        self._pending_bytes = 0
        self._inflight_bytes = 0
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._inflight = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tawb-telemetry", daemon=True)
        self._thread.start()

    def submit(self, lines, *, important=False):
        size = sum(len(line.encode("utf-8")) for line in lines)
        if size > self._max_pending_bytes:
            return
        with self._ready:
            while self._pending_bytes + self._inflight_bytes + size > self._max_pending_bytes:
                if not important or not self._pending:
                    return
                dropped_lines, dropped_size = self._pending.popleft()
                self._pending_bytes -= dropped_size
            self._pending.append((lines, size))
            self._pending_bytes += size
            self._ready.notify()

    def _run(self):
        try:
            while True:
                with self._ready:
                    while not self._pending and not self._stop.is_set():
                        self._ready.wait(timeout=0.05)
                    if not self._pending:
                        return
                    lines, size = self._pending.popleft()
                    self._pending_bytes -= size
                    self._inflight += 1
                    self._inflight_bytes += size
                try:
                    self._hosted_context.post_events(lines)
                except Exception:
                    pass
                finally:
                    with self._ready:
                        self._inflight -= 1
                        self._inflight_bytes -= size
                        self._ready.notify_all()
        finally:
            if self._owns_context:
                with suppress(Exception):
                    self._hosted_context.close()

    def close(self):
        deadline = time.monotonic() + self._shutdown_seconds
        with self._ready:
            while (self._pending or self._inflight) and time.monotonic() < deadline:
                self._ready.wait(timeout=0.01)
            self._stop.set()
            self._pending.clear()
            self._pending_bytes = 0
            self._ready.notify_all()
        self._thread.join(max(0.0, deadline - time.monotonic()))


class WorkbenchCallback(StatsCallbackHandler):
    """Emit fixed stages, report deltas and deduplicated cumulative usage."""

    raise_error = True

    def __init__(self, emit):
        super().__init__()
        self.emit = emit
        self.reports = {}
        self._seen = set()
        self._callback_lock = threading.RLock()

    def _first(self, kind, run_id):
        if run_id is None:
            return True
        key = (kind, run_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def stats(self) -> dict:
        stats = super().get_stats()
        return {"llm_calls": stats["llm_calls"], "tool_calls": stats["tool_calls"],
                "input_tokens": stats["tokens_in"], "output_tokens": stats["tokens_out"]}

    def on_chain_start(self, serialized, inputs, **kwargs):
        metadata = kwargs.get("metadata") or {}
        name = metadata.get("langgraph_node") or kwargs.get("name") or (serialized or {}).get("name")
        if name in _STAGES:
            self.emit("phase", {"stage": _STAGES[name]})

    def on_chain_end(self, outputs, **kwargs):
        self.report_state(outputs)

    def report_state(self, state):
        if not isinstance(state, dict):
            return
        reports = {field: state.get(field) for field in _REPORT_FIELDS}
        for section, fields in _DEBATE_FIELDS.items():
            debate = state.get(section)
            if isinstance(debate, dict):
                reports.update({f"{section}.{field}": debate.get(field) for field in fields})
        with self._callback_lock:
            for section, content in reports.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                content = scrub_secrets(content)
                if self.reports.get(section) != content:
                    self.reports[section] = content
                    self.emit("report", {"section": section, "content": content})

    def _llm_started(self, run_id):
        with self._callback_lock:
            if self._first("llm_start", run_id):
                super().on_llm_start({}, [])
                self.emit("usage", self.stats())

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._llm_started(kwargs.get("run_id"))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self._llm_started(kwargs.get("run_id"))

    def on_llm_end(self, response, **kwargs):
        with self._callback_lock:
            if not self._first("llm_end", kwargs.get("run_id")):
                return
            usage = None
            with suppress(AttributeError, IndexError, TypeError):
                usage = getattr(response.generations[0][0].message, "usage_metadata", None)
            if not usage:
                output = response.llm_output or {}
                usage = output.get("token_usage") or output.get("usage") or {}
            tokens_in = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            tokens_out = usage.get("output_tokens", usage.get("completion_tokens", 0))
            with self._lock:
                self.tokens_in += tokens_in if type(tokens_in) is int and tokens_in >= 0 else 0
                self.tokens_out += tokens_out if type(tokens_out) is int and tokens_out >= 0 else 0
            self.emit("usage", self.stats())

    def on_tool_start(self, serialized, input_str, **kwargs):
        with self._callback_lock:
            if not self._first("tool_start", kwargs.get("run_id")):
                return
            super().on_tool_start(serialized, input_str)
            name = (serialized or {}).get("name")
            self.emit("phase", {"stage": f"tool:{name}" if name in _TOOLS else "tool"})
            self.emit("usage", self.stats())


def run_analysis(request: dict, emit) -> dict:
    """Check connectivity, then run the upstream graph with cumulative usage."""
    from langgraph.prebuilt import ToolNode

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    def scrub_tool_call(tool_request, execute):
        message = execute(tool_request)
        return message.model_copy(update=scrub_secrets(message.model_dump()))

    async def scrub_async_tool_call(tool_request, execute):
        message = await execute(tool_request)
        return message.model_copy(update=scrub_secrets(message.model_dump()))

    class WorkbenchGraph(TradingAgentsGraph):
        def _create_tool_nodes(self):
            return {
                category: ToolNode(list(node.tools_by_name.values()),
                                   wrap_tool_call=scrub_tool_call,
                                   awrap_tool_call=scrub_async_tool_call)
                for category, node in super()._create_tool_nodes().items()
            }

    request = validate_request(request)
    config = deepcopy(DEFAULT_CONFIG)
    hosted = current_hosted_worker_context()
    if hosted is None:
        model = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-5.6-sol"
        config.update({
            "llm_provider": "azure", "deep_think_llm": model, "quick_think_llm": model,
            "backend_url": os.getenv("AZURE_OPENAI_ENDPOINT"),
            "data_vendors": dict(request["data_vendors"]),
        })
        client_factory = None
    else:
        config.update({
            "llm_provider": hosted.config.provider,
            "deep_think_llm": hosted.config.deep_model or hosted.config.quick_model,
            "quick_think_llm": hosted.config.quick_model or hosted.config.deep_model,
            "backend_url": hosted.config.provider_config.get("base_url"),
            "data_vendors": dict(hosted.config.data_vendors),
        })
        client_factory = hosted.create_llm_client
    config.update({
        "output_language": request["language"],
        "max_debate_rounds": request["debate_rounds"],
        "max_risk_discuss_rounds": request["risk_rounds"],
        "tool_vendors": {}, "checkpoint_enabled": False,
    })
    callback = WorkbenchCallback(emit)
    connectivity = run_connectivity(request, emit, callback=callback)["connectivity"]
    if request["asset_type"] == "etf":
        from tradingagents.dataflows.yfinance_etf import get_etf_profile

        callback.on_tool_start({"name": "get_etf_profile"}, "")
        profile = get_etf_profile(request["ticker"], request["analysis_date"])
        callback.reports["etf_profile"] = scrub_secrets(profile)
        emit("report", {"section": "etf_profile", "content": callback.reports["etf_profile"]})
        if json.loads(profile)["status"] not in ("ok", "partial"):
            raise ValueError("ETF identity must be verified before analysis")
    emit("phase", {"stage": "initializing"})
    graph_kwargs = {
        "selected_analysts": request["analysts"],
        "debug": False,
        "config": config,
        "callbacks": [callback],
    }
    if client_factory is not None:
        graph_kwargs["client_factory"] = client_factory
    graph = WorkbenchGraph(**graph_kwargs)
    with set_config_context({"callbacks": [callback]}) as context:
        state, decision = context.run(graph.propagate, request["ticker"], request["analysis_date"],
                                      asset_type=request["asset_type"])
    callback.report_state(state)
    emit("usage", callback.stats())
    return {"decision": scrub_secrets(decision), "reports": dict(callback.reports),
            "stats": callback.stats(), "connectivity": connectivity}


def _finite_number(value, *, positive=False):
    try:
        number = float(value)
        return math.isfinite(number) and (not positive or number > 0)
    except (TypeError, ValueError):
        return False


def _recent_date(value, days=14):
    try:
        return date.today() - timedelta(days=days) <= date.fromisoformat(value[:10]) <= date.today()
    except (TypeError, ValueError):
        return False


def probe_yfinance() -> bool:
    """Check recent MSFT prices through the upstream Yahoo function."""
    from tradingagents.dataflows.y_finance import get_YFin_data_online

    today = date.today()
    report = get_YFin_data_online("MSFT", (today - timedelta(days=14)).isoformat(), today.isoformat())
    if not isinstance(report, str):
        return False
    text = "\n".join(line for line in report.splitlines() if line and not line.startswith("#"))
    rows = csv.DictReader(io.StringIO(text))
    return any(_recent_date(row.get("Date", "")) and _finite_number(row.get("Close"), positive=True)
               for row in rows)


def probe_alpha_vantage() -> bool:
    """Check a configured Alpha Vantage category through the shared helper."""
    from tradingagents.dataflows.alpha_vantage_common import _make_api_request

    hosted = current_hosted_worker_context()
    if (hosted is not None
            and "alpha_vantage" in hosted.config.data_vendors.get("macro_data", "").split(",")):
        data = _make_api_request("UNEMPLOYMENT", {"datatype": "json"})
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return False
        if any(key in data for key in ("Error Message", "Information", "Note")):
            raise ValueError("Provider returned an error response")
        rows = data.get("data")
        macro_ok = isinstance(rows, list) and any(
            isinstance(row, dict) and _recent_date(row.get("date", ""), days=120)
            and _finite_number(row.get("value")) for row in rows
        )
        if not macro_ok:
            return False
        if "alpha_vantage" not in hosted.config.data_vendors["core_stock_apis"].split(","):
            return True
    data = _make_api_request("GLOBAL_QUOTE", {"symbol": "MSFT"})
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        return False
    if any(key in data for key in ("Error Message", "Information", "Note")):
        raise ValueError("Provider returned an error response")
    quote = data.get("Global Quote") or {}
    return (isinstance(quote, dict) and quote.get("01. symbol") == "MSFT"
            and _finite_number(quote.get("05. price"), positive=True)
            and _recent_date(quote.get("07. latest trading day", "")))


def probe_fred() -> bool:
    """Check DGS10 using the upstream dated/vintage-aware macro function."""
    from tradingagents.dataflows.fred import get_macro_data

    report = get_macro_data("DGS10", date.today().isoformat(), look_back_days=30)
    if not isinstance(report, str) or not report.startswith("## FRED:"):
        return False
    for line in report.splitlines():
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        if len(columns) == 2 and _recent_date(columns[0], days=30) and _finite_number(columns[1]):
            return True
    return False


def probe_polymarket() -> bool:
    """Require an actual bounded probability from a native live market report."""
    from tradingagents.dataflows.polymarket import get_prediction_markets

    report = get_prediction_markets("Fed", limit=1)
    if not isinstance(report, str):
        return False
    if report.startswith("Polymarket data is currently unavailable"):
        raise ValueError("Provider unavailable")
    if not report.startswith("## Polymarket prediction markets"):
        return False
    for match in re.finditer(
        r"^- \*\*[^\n]+\*\*[^\n]*?\s(-?\d+(?:\.\d+)?)% "
        r"\(\$[\d,.]+ volume, resolves (\d{4}-\d{2}-\d{2})", report, re.MULTILINE,
    ):
        try:
            if 0 <= float(match[1]) <= 100 and date.fromisoformat(match[2]) >= date.today():
                return True
        except ValueError:
            continue
    return False


def probe_foundry(callback: WorkbenchCallback) -> bool:
    """Make one small Responses call; invoked only by connectivity mode."""
    from tradingagents.llm_clients.azure_client import AzureOpenAIClient

    hosted = current_hosted_worker_context()
    if hosted is None:
        model = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-5.6-sol"
        client = AzureOpenAIClient(model, max_tokens=16, max_retries=0, timeout=30)
    else:
        model = hosted.config.quick_model
        options = {"max_output_tokens": 128} if hosted.config.provider == "google" else {"max_tokens": 128}
        client = hosted.create_llm_client(hosted.config.provider, model, timeout=30, **options)
    response = client.get_llm().invoke("Reply only OK.", config={"callbacks": [callback]})
    if hosted is not None:
        return isinstance(response.content, str) and response.content.strip() == "OK"
    return isinstance(response.content, str) and bool(response.content.strip())


def _request_uses_vendor(request: dict, vendor: str) -> bool:
    return any(vendor in chain.split(",") for chain in request["data_vendors"].values())


def _hosted_probe_enabled(hosted, request: dict, provider: str) -> bool:
    request = {"data_vendors": hosted.config.data_vendors}
    if provider == "foundry":
        return True
    if provider == "alpha_vantage":
        return (hosted.config.data_key_configured["alpha_vantage"]
            and any("alpha_vantage" in hosted.config.data_vendors.get(category, "").split(",")
                for category in ("core_stock_apis", "macro_data")))
    if provider == "fred":
        return hosted.config.data_key_configured["fred"] and _request_uses_vendor(request, "fred")
    if provider == "yfinance":
        return _request_uses_vendor(request, "yfinance")
    if provider == "polymarket":
        return _request_uses_vendor(request, "polymarket")
    return False


def _hosted_provider_error(error: Exception) -> HostedProviderError | None:
    seen = set()
    for _ in range(16):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, HostedProviderError):
            return HostedProviderError(error.code)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return None


def _failure_status(error: Exception) -> str:
    import httpx
    import requests
    from openai import APITimeoutError

    from tradingagents.dataflows.errors import (
        NoMarketDataError,
        VendorNotConfiguredError,
        VendorRateLimitError,
    )

    error = _hosted_provider_error(error) or error
    if isinstance(error, NoMarketDataError):
        return "no_data"
    if isinstance(error, VendorRateLimitError):
        return "rate_limited"
    if isinstance(error, VendorNotConfiguredError):
        return "auth_error"
    if isinstance(error, (TimeoutError, requests.Timeout, httpx.TimeoutException, APITimeoutError)):
        return "timeout"
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth_error"
    return "error"


def run_connectivity(request: dict, emit, *, callback=None) -> dict:
    """Return all five provider rows; failed probes do not abort the diagnostic run."""
    validate_request(request)
    if callback is None:
        callback = WorkbenchCallback(emit)
    hosted = current_hosted_worker_context()
    rows = []
    probes = (
        ("yfinance", None, probe_yfinance),
        ("alpha_vantage", "ALPHA_VANTAGE_API_KEY", probe_alpha_vantage),
        ("fred", "FRED_API_KEY", probe_fred),
        ("polymarket", None, probe_polymarket),
        ("foundry", "AZURE_OPENAI_ENDPOINT", lambda: probe_foundry(callback)),
    )
    for provider, required_env, probe in probes:
        emit("phase", {"stage": f"connectivity:{provider}"})
        started = time.monotonic()
        terminal_error = None
        if (
            (hosted is not None and not _hosted_probe_enabled(hosted, request, provider))
            or (hosted is None and required_env and not os.getenv(required_env, "").strip())
        ):
            status = "unconfigured"
        else:
            try:
                status = "ok" if probe() else "no_data"
            except Exception as error:
                status = _failure_status(error)
                if hosted is not None:
                    candidate = _hosted_provider_error(error)
                    if candidate is not None and candidate.code in {"budget_exhausted", "revoked"}:
                        terminal_error = candidate
        rows.append({"provider": provider, "status": status, "detail": _DETAILS[status],
                     "elapsed_seconds": round(max(0, time.monotonic() - started), 3)})
        emit("connectivity", {"connectivity": list(rows)})
        if terminal_error is not None:
            emit("usage", callback.stats())
            raise terminal_error
    emit("usage", callback.stats())
    return {"decision": None, "reports": {}, "stats": callback.stats(), "connectivity": rows}


def main() -> int:
    """Read bounded base64 JSON env input; print only safe events; return 0 or 1.

    An invalid/missing run ID emits an error under the nil UUID, never a result.
    """
    output = sys.stdout
    output_lock = threading.Lock()
    run_id = str(UUID(int=0))

    def emit(kind, payload):
        lines = encode_event(run_id, kind, payload)
        with output_lock:
            output.write("\n".join(lines) + "\n")
            output.flush()

    previous_logging = logging.root.manager.disable
    previous_auth = os.environ.get("AZURE_OPENAI_AUTH_MODE")
    hosted_context = None
    telemetry = None
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            logging.disable(sys.maxsize)
            try:
                raw_id = os.environ["TRADINGAGENTS_RUN_ID"]
                if str(UUID(raw_id)) != raw_id:
                    raise ValueError("Invalid run ID")
                run_id = raw_id
                raw_request = os.environ["TRADINGAGENTS_RUN_REQUEST"]
                if len(raw_request) > 8192:
                    raise ValueError("Request too large")
                payload = json.loads(base64.b64decode(raw_request, validate=True))
                request = validate_request(payload)
            except (KeyError, ValueError, TypeError, RecursionError):
                emit("error", {"message": "Invalid run configuration."})
                return 1
            try:
                hosted_context = hosted_worker_context_from_environment(os.environ)
            except RuntimeError:
                emit("error", {"message": "Invalid run configuration."})
                return 1
            if hosted_context is not None:
                with suppress(Exception):
                    telemetry = _HostedTelemetryRelay(hosted_context)
            if hosted_context is None:
                os.environ["AZURE_OPENAI_AUTH_MODE"] = "managed_identity"

            def emit(kind, payload):
                lines = encode_event(run_id, kind, payload)
                with output_lock:
                    output.write("\n".join(lines) + "\n")
                    output.flush()
                if telemetry is not None:
                    with suppress(Exception):
                        telemetry.submit(lines, important=kind in {"result", "error"})

            try:
                runner = run_analysis if request["mode"] == "analysis" else run_connectivity
                if hosted_context is None:
                    result = runner(request, emit)
                else:
                    with hosted_context.activate():
                        result = runner(request, emit)
                emit("result", result)
                return 0
            except Exception as error:
                hosted_error = _hosted_provider_error(error) if hosted_context is not None else None
                if hosted_error is None:
                    emit("error", {"message": "Worker failed; check configuration and provider access."})
                else:
                    emit("error", {"code": hosted_error.code, "message": str(hosted_error)})
                return 1
            finally:
                if telemetry is not None:
                    with suppress(Exception):
                        telemetry.close()
                if hosted_context is not None:
                    with suppress(Exception):
                        hosted_context.close()
    finally:
        logging.disable(previous_logging)
        if previous_auth is None:
            os.environ.pop("AZURE_OPENAI_AUTH_MODE", None)
        else:
            os.environ["AZURE_OPENAI_AUTH_MODE"] = previous_auth


if __name__ == "__main__":
    raise SystemExit(main())
