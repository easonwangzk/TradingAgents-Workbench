"""Alpha Vantage economic indicators for current-date macro enrichment."""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from . import alpha_vantage_common
from .errors import VendorNotConfiguredError, VendorRateLimitError

SERIES = {
    "unemployment": ("UNEMPLOYMENT", {}, "Unemployment Rate", "percent", "monthly"),
    "cpi": ("CPI", {"interval": "monthly"}, "Consumer Price Index", "index 1982-1984=100", "monthly"),
    "real_gdp": ("REAL_GDP", {"interval": "quarterly"}, "Real GDP", "billions of chained dollars", "quarterly"),
    "real_gdp_per_capita": ("REAL_GDP_PER_CAPITA", {}, "Real GDP per Capita", "chained dollars", "quarterly"),
    "fed_funds_rate": ("FEDERAL_FUNDS_RATE", {"interval": "monthly"}, "Federal Funds Effective Rate", "percent", "monthly"),
    "inflation": ("INFLATION", {}, "Annual Consumer Price Inflation", "percent", "annual"),
    "retail_sales": ("RETAIL_SALES", {}, "Advance Retail Sales: Retail Trade (RSXFSN, not seasonally adjusted)", "millions of dollars", "monthly"),
    "durables": ("DURABLES", {}, "Durable Goods New Orders (UMDMNO, not seasonally adjusted)", "millions of dollars", "monthly"),
    "nonfarm_payrolls": ("NONFARM_PAYROLL", {}, "Total Nonfarm Payrolls", "thousands of persons", "monthly"),
    "2y_treasury": ("TREASURY_YIELD", {"interval": "daily", "maturity": "2year"}, "2-Year Treasury Yield", "percent", "daily"),
    "10y_treasury": ("TREASURY_YIELD", {"interval": "daily", "maturity": "10year"}, "10-Year Treasury Yield", "percent", "daily"),
    "30y_treasury": ("TREASURY_YIELD", {"interval": "daily", "maturity": "30year"}, "30-Year Treasury Yield", "percent", "daily"),
}
ALIASES = {
    "unemployment_rate": "unemployment", "federal_funds_rate": "fed_funds_rate",
    "fed_funds": "fed_funds_rate", "payrolls": "nonfarm_payrolls",
    "nonfarm_payroll": "nonfarm_payrolls", "durable_goods": "durables",
    "10y_2y_spread": "yield_curve",
}


def _unavailable(reason):
    return f"DATA_UNAVAILABLE: Alpha Vantage macro data: {reason}. Proceed without it; do not fabricate values."


def _observations(series, start, end):
    function, params, _, _, _ = series
    payload = alpha_vantage_common._make_api_request(function, {**params, "datatype": "json"})
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError):
        raise ValueError("provider response was invalid") from None
    if not isinstance(data, dict):
        raise ValueError("provider response was invalid")
    if any(data.get(key) for key in ("Information", "Note", "Error Message")):
        raise ValueError("provider rejected the request; check API key, quota and plan")
    if not isinstance(data.get("data"), list):
        raise ValueError("provider returned no usable observations")
    observations = {}
    for row in data["data"]:
        if not isinstance(row, dict):
            continue
        try:
            observed = date.fromisoformat(row["date"])
            value = Decimal(str(row["value"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if start <= observed <= end and value.is_finite() and abs(value) < Decimal("1e20"):
            observations[observed] = value
    unit = data.get("unit")
    if not isinstance(unit, str) or len(unit) > 80 or any(char in unit for char in "\r\n|<>"):
        unit = series[3]
    return observations, unit


def get_macro_data(indicator: str, curr_date: str, look_back_days: int | None = None) -> str:
    """Return a bounded, source-labelled macro report without historical vintage claims."""
    if not isinstance(indicator, str):
        return _unavailable("unsupported indicator")
    alias = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    alias = ALIASES.get(alias, alias)
    if alias not in SERIES and alias != "yield_curve":
        return _unavailable("unsupported indicator; use cpi, real_gdp, unemployment, fed_funds_rate, 10y_treasury or FRED for additional series")
    try:
        end = date.fromisoformat(curr_date)
    except (TypeError, ValueError):
        return _unavailable("invalid analysis date")
    if end != date.today():
        return _unavailable("historical or future analysis requires FRED; no point-in-time vintage is available")
    days = 365 if look_back_days is None else look_back_days
    if type(days) is not int or not 1 <= days <= 36500:
        return _unavailable("invalid lookback window")
    start = end - timedelta(days=days)
    try:
        if alias == "yield_curve":
            long_yields, _ = _observations(SERIES["10y_treasury"], start, end)
            short_yields, _ = _observations(SERIES["2y_treasury"], start, end)
            observations = {observed: long_yields[observed] - short_yields[observed]
                            for observed in long_yields.keys() & short_yields.keys()}
            title, units, frequency = "10-Year minus 2-Year Treasury Yield (derived)", "percentage points", "daily"
        else:
            series = SERIES[alias]
            observations, units = _observations(series, start, end)
            title, frequency = series[2], series[4]
    except VendorRateLimitError:
        return _unavailable("quota or plan limit reached")
    except VendorNotConfiguredError:
        return _unavailable("API key missing or invalid")
    except ValueError as error:
        return _unavailable(str(error))
    if not observations:
        return _unavailable("no observations in the requested window")
    ordered = sorted(observations.items())
    latest_date, latest = ordered[-1]
    lines = [
        f"## Alpha Vantage: {title}",
        f"Units: {units} | Frequency: {frequency}",
        "Source: Alpha Vantage economic indicators, retrieved from FRED.",
        "Latest-revision data, not a point-in-time vintage. Observation dates are not release dates.",
        "Definitions and seasonal adjustment can differ from the workbench's FRED aliases; do not splice providers into one series.",
        f"Window: {start.isoformat()} to {end.isoformat()}",
        f"**Latest:** {latest} ({latest_date.isoformat()})",
        f"Change over window: {latest - ordered[0][1]:+.2f}",
        "Most recent 40 observations shown at most.",
        "| Date | Value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {observed.isoformat()} | {value} |" for observed, value in ordered[-40:])
    return "\n".join(lines)
