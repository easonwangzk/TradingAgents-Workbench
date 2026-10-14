"""Native Yahoo fund snapshots with explicit availability and temporal limits."""

import json
import math
from datetime import date, datetime, timedelta, timezone
from numbers import Real

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.symbol_utils import normalize_symbol
from tradingagents.dataflows.utils import safe_ticker_component


def _json_value(value):
    if isinstance(value, pd.DataFrame):
        return _json_value(value.head(100).reset_index().to_dict(orient="records"))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:6000]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Real):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None


def get_etf_profile(symbol: str, curr_date: str) -> str:
    """Return an ETF identity and current fund snapshot, not historical metadata."""
    analysis_date = date.fromisoformat(curr_date)
    if analysis_date.isoformat() != curr_date or analysis_date > datetime.now(timezone.utc).date():
        raise ValueError("Invalid analysis date")
    canonical = normalize_symbol(symbol)
    safe_ticker_component(canonical)
    result = {
        "source": "Yahoo Finance via native yfinance Ticker.info, funds_data and history",
        "source_url": f"https://finance.yahoo.com/quote/{canonical}/holdings/",
        "ticker": canonical, "analysis_date": curr_date,
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "metadata_as_of": None,
        "snapshot_evidence_scope": "current_only_not_historical",
        "status": "unverified_identity", "identity": {}, "quote": {},
        "fund_data": {}, "distributions": [],
        "warnings": [
            "Fund metadata and quotes are current retrieval-time snapshots, not point-in-time data. "
            "Do not claim they were known on the analysis date or use them as historical evidence.",
            "Unavailable values are null or omitted, never zero. Holdings may be partial and their "
            "effective dates are unverified. Portfolio valuation ratios describe holdings, not issuer earnings.",
            "Native fund expense ratios, weights and yields are raw proportions, not percentage points. "
            "Do not infer benchmark tracking error, NAV premium, leverage multiple or SEC yield from missing data.",
            "Holdings valuation ratios and fund-operations Total Net Assets are excluded because their "
            "native units or conventions are ambiguous. quote.totalAssets is Yahoo's separately labelled currency value.",
        ],
    }
    try:
        vendor = yf.Ticker(canonical)
        info = vendor.info or {}
    except Exception:
        result["warnings"].append("Fund identity unavailable; do not issue a recommendation.")
        return json.dumps(result, allow_nan=False)

    quote_type = info.get("quoteType")
    reported_symbol = info.get("symbol")
    result["identity"] = {"name": info.get("longName") or info.get("shortName"),
                          "symbol": reported_symbol, "quote_type": quote_type,
                          "exchange": info.get("exchange"), "currency": info.get("currency")}
    if (quote_type and quote_type != "ETF") or (reported_symbol and reported_symbol.upper() != canonical.upper()):
        result["status"] = "identity_mismatch"
    elif quote_type == "ETF" and reported_symbol and result["identity"]["name"]:
        result["status"] = "ok"
    if result["status"] != "ok":
        result["warnings"].append("ETF identity is not verified; do not substitute another instrument or recommend a trade.")
        return json.dumps(_json_value(result), allow_nan=False)

    quote_fields = (
        "category", "fundFamily", "legalType", "longBusinessSummary", "totalAssets", "navPrice",
        "annualReportExpenseRatio", "yield", "trailingAnnualDividendYield", "trailingAnnualDividendRate",
        "fundInceptionDate", "bid", "ask", "bidSize", "askSize", "volume", "averageVolume",
        "regularMarketPrice", "regularMarketTime", "currency",
    )
    result["quote"] = {field: info.get(field) for field in quote_fields}
    try:
        funds = vendor.funds_data
        result["fund_data"]["fund_overview"] = _json_value(funds.fund_overview)
    except Exception:
        result["status"] = "partial"
        result["warnings"].append("fund_data unavailable")
    else:
        for field in ("description", "fund_operations", "asset_classes", "top_holdings",
                      "bond_holdings", "bond_ratings", "sector_weightings"):
            try:
                value = getattr(funds, field)
                if field == "fund_operations":
                    value = value.loc[value.index.isin(["Annual Report Expense Ratio", "Annual Holdings Turnover"])]
                result["fund_data"][field] = _json_value(value)
            except Exception:
                result["status"] = "partial"
                result["warnings"].append(f"{field} unavailable")

    try:
        history = vendor.history(start=(analysis_date - timedelta(days=366)).isoformat(),
                                 end=(analysis_date + timedelta(days=1)).isoformat(),
                                 auto_adjust=False, actions=True)
        for timestamp, row in history.iterrows():
            trading_day = pd.Timestamp(timestamp).date()
            if not analysis_date - timedelta(days=366) <= trading_day <= analysis_date:
                continue
            dividend = _json_value(row.get("Dividends"))
            capital_gain = _json_value(row.get("Capital Gains"))
            if (dividend is not None and dividend > 0) or (capital_gain is not None and capital_gain > 0):
                result["distributions"].append({"date": trading_day.isoformat(), "dividend": dividend,
                                                 "capital_gain": capital_gain})
        result["distribution_status"] = "available" if not history.empty and "Dividends" in history else "unavailable"
    except Exception:
        result["distribution_status"] = "unavailable"
    if result["distribution_status"] == "unavailable":
        result["status"] = "partial"
        result["warnings"].append("Historical distributions unavailable; do not assume zero distributions.")
    return json.dumps(_json_value(result), ensure_ascii=False, allow_nan=False)
