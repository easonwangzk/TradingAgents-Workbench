from langchain_core.tools import tool

from tradingagents.dataflows.yfinance_etf import get_etf_profile as fetch_etf_profile


@tool
def get_etf_profile(symbol: str, curr_date: str) -> str:
    """Get verified ETF identity, native Yahoo fund data and dated distributions.

    Fund metadata is a current snapshot, not proven historical data. Use the exact
    ETF symbol and research date (YYYY-MM-DD); respect all returned data warnings.
    """
    return fetch_etf_profile(symbol, curr_date)
