"""The latest trading day's bar must not silently vanish (#1201).

yfinance can return the newest in-range bar with a NaN close (an unsettled or
glitched session). The old path parsed dates without normalizing timezone and
dropped every NaN-close row before applying the curr_date cutoff, so the latest
bar disappeared and the previous trading day looked like the latest. Now dates
are normalized, and a latest in-range bar with no close raises rather than
silently falling back.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows import stockstats_utils as su
from tradingagents.dataflows.symbol_utils import NoMarketDataError

# --- date normalization -----------------------------------------------------

@pytest.mark.unit
def test_normalize_dates_strips_tz_and_normalizes_to_midnight():
    aware = pd.Series(pd.to_datetime(
        ["2026-05-08 09:30:00-04:00", "2026-05-09 16:00:00-04:00"]
    ))
    out = su._normalize_dates(aware)
    assert out.dt.tz is None
    assert list(out) == [pd.Timestamp("2026-05-08"), pd.Timestamp("2026-05-09")]


@pytest.mark.unit
def test_normalize_dates_leaves_naive_dates_at_midnight():
    naive = pd.Series(pd.to_datetime(["2026-05-08 14:30:00", "2026-05-09 00:00:00"]))
    out = su._normalize_dates(naive)
    assert out.dt.tz is None
    assert list(out) == [pd.Timestamp("2026-05-08"), pd.Timestamp("2026-05-09")]


@pytest.mark.unit
def test_normalize_dates_handles_mixed_dst_offsets():
    # 5y of US bars span DST; via a cache CSV they arrive as mixed-offset
    # strings, which pd.to_datetime can't unify. Each keeps its own local date.
    mixed = pd.Series([
        "2026-01-08 00:00:00-05:00",  # EST
        "2026-06-08 00:00:00-04:00",  # EDT
        "not-a-date",                 # -> NaT
    ])
    out = su._normalize_dates(mixed)
    assert out.iloc[0] == pd.Timestamp("2026-01-08")
    assert out.iloc[1] == pd.Timestamp("2026-06-08")
    assert pd.isna(out.iloc[2])


@pytest.mark.unit
def test_normalize_dates_keeps_positive_offset_local_date():
    # A Tokyo bar at local midnight (+09:00) must stay on its own calendar day,
    # not shift to the previous UTC day (which utc=True parsing would cause).
    jst = pd.Series(["2026-05-08 00:00:00+09:00"])
    assert su._normalize_dates(jst).iloc[0] == pd.Timestamp("2026-05-08")


# --- fill vs guard responsibilities ----------------------------------------

@pytest.mark.unit
def test_clean_dataframe_keeps_nan_close_for_the_caller_to_inspect():
    # _clean_dataframe normalizes but no longer drops the NaN close itself.
    df = pd.DataFrame({"Date": ["2026-05-08", "2026-05-09"], "Close": [100.0, float("nan")]})
    cleaned = su._clean_dataframe(df)
    assert len(cleaned) == 2
    assert pd.isna(cleaned["Close"].iloc[-1])


@pytest.mark.unit
def test_fill_price_gaps_drops_nan_close_rows():
    df = pd.DataFrame({"Date": pd.to_datetime(["2026-05-07", "2026-05-08"]),
                       "Close": [float("nan"), 100.0]})
    filled = su._fill_price_gaps(df)
    assert len(filled) == 1
    assert filled["Close"].iloc[0] == 100.0


# --- load_ohlcv end-to-end (with a mocked cache read) -----------------------

def _run_load(monkeypatch, tmp_path, frame, curr_date):
    """Drive load_ohlcv against a pre-seeded cache frame (no network)."""
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    today = pd.Timestamp(curr_date)
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: today))
    start = (today - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"AAPL-YFin-data-{start}-{end}.csv").write_text(frame.to_csv(index=False))

    def _fail_download(*a, **k):
        raise AssertionError("should use the seeded cache, not download")
    monkeypatch.setattr(su.yf, "download", _fail_download)
    monkeypatch.setattr(su, "_assert_ohlcv_not_stale", lambda *a, **k: None)
    return su.load_ohlcv("AAPL", curr_date)


@pytest.mark.unit
def test_latest_in_range_nan_close_raises_not_silent_fallback(monkeypatch, tmp_path):
    # Newest bar (the curr_date) has no close -> raise, don't return Thursday.
    frame = pd.DataFrame({
        "Date": ["2026-05-07", "2026-05-08"],
        "Open": [100.0, 101.0], "High": [101.0, 102.0], "Low": [99.0, 100.0],
        "Close": [100.5, float("nan")], "Volume": [1_000_000, 1_000_000],
    })
    monkeypatch.setattr(su.yf, "Ticker", lambda symbol: SimpleNamespace(history=lambda **kwargs: pd.DataFrame()))
    with pytest.raises(NoMarketDataError, match="no closing price"):
        _run_load(monkeypatch, tmp_path, frame, "2026-05-08")


@pytest.mark.unit
def test_latest_missing_bar_is_recovered_from_same_day_native_history_and_cached(monkeypatch, tmp_path):
    frame = pd.DataFrame({
        "Date": ["2026-09-04", "2026-09-08"],
        "Open": [53.5, float("nan")], "High": [55.21, float("nan")],
        "Low": [53.26, float("nan")], "Close": [55.0, float("nan")],
        "Volume": [1527400, 4992002],
    })
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        assert len(calls) == 1
        assert kwargs == {"start": "2026-09-08", "end": "2026-09-09", "auto_adjust": True}
        return pd.DataFrame({
            "Date": pd.to_datetime(["2026-09-08 00:00:00-04:00"]),
            "Open": [61.85], "High": [65.26], "Low": [60.45],
            "Close": [63.96], "Volume": [4992002],
        }).set_index("Date")

    def ticker(symbol):
        assert symbol == "AAPL"
        return SimpleNamespace(history=history)

    monkeypatch.setattr(su.yf, "Ticker", ticker)
    result = _run_load(monkeypatch, tmp_path, frame, "2026-09-08")
    assert list(result["Close"]) == [55.0, 63.96]
    assert result["Date"].iloc[-1] == pd.Timestamp("2026-09-08")
    assert su.load_ohlcv("AAPL", "2026-09-08")["Close"].iloc[-1] == 63.96


@pytest.mark.unit
@pytest.mark.parametrize('day, close', [
    ('2026-09-04', 63.96), ('2026-09-09', 63.96),
    ('2026-09-08', float('nan')), ('2026-09-08', 99.0),
])
def test_recovery_rejects_wrong_dates_missing_or_inconsistent_prices(monkeypatch, tmp_path, day, close):
    frame = pd.DataFrame({'Date': ['2026-09-08'], 'Close': [float('nan')]})
    recovered = pd.DataFrame({
        'Date': pd.to_datetime([day]), 'Open': [61.85], 'High': [65.26],
        'Low': [60.45], 'Close': [close], 'Volume': [4992002],
    }).set_index('Date')
    monkeypatch.setattr(su.yf, 'Ticker', lambda symbol: SimpleNamespace(history=lambda **kwargs: recovered))
    with pytest.raises(NoMarketDataError, match='no closing price'):
        _run_load(monkeypatch, tmp_path, frame, '2026-09-08')


@pytest.mark.unit
def test_recovery_failure_preserves_typed_missing_close_error(monkeypatch, tmp_path):
    frame = pd.DataFrame({'Date': ['2026-09-08'], 'Close': [float('nan')]})

    def history(**kwargs):
        raise RuntimeError('Native history unavailable')

    monkeypatch.setattr(su.yf, 'Ticker', lambda symbol: SimpleNamespace(history=history))
    with pytest.raises(NoMarketDataError, match='no closing price'):
        _run_load(monkeypatch, tmp_path, frame, '2026-09-08')


@pytest.mark.unit
def test_older_nan_close_row_is_still_dropped(monkeypatch, tmp_path):
    # A stale gap mid-series is dropped; the valid latest bar is served.
    frame = pd.DataFrame({
        "Date": ["2026-05-06", "2026-05-07", "2026-05-08"],
        "Open": [100.0, 101.0, 102.0], "High": [101.0, 102.0, 103.0],
        "Low": [99.0, 100.0, 101.0],
        "Close": [100.5, float("nan"), 102.5], "Volume": [1_000_000, 1_000_000, 1_000_000],
    })
    out = _run_load(monkeypatch, tmp_path, frame, "2026-05-08")
    assert out["Close"].iloc[-1] == 102.5
    assert (out["Date"] == pd.Timestamp("2026-05-07")).sum() == 0  # the NaN row is gone


@pytest.mark.unit
def test_tz_aware_latest_bar_is_kept_at_the_cutoff(monkeypatch, tmp_path):
    # A tz-aware/intraday latest bar on the cutoff day must not be filtered out
    # by a naive-vs-aware comparison.
    frame = pd.DataFrame({
        "Date": ["2026-05-07 09:30:00-04:00", "2026-05-08 09:30:00-04:00"],
        "Open": [100.0, 101.0], "High": [101.0, 102.0], "Low": [99.0, 100.0],
        "Close": [100.5, 101.5], "Volume": [1_000_000, 1_000_000],
    })
    out = _run_load(monkeypatch, tmp_path, frame, "2026-05-08")
    assert out["Close"].iloc[-1] == 101.5
    assert out["Date"].iloc[-1] == pd.Timestamp("2026-05-08")
