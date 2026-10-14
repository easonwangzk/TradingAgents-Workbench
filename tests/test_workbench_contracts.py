import base64
import gzip
import hashlib
import json
import random
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def frozen_utc_clock(monkeypatch):
    from tradingagents.workbench import contracts

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
            if tz is not None:
                return instant.astimezone(tz)
            return instant.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)

    class LocalDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 9)

    monkeypatch.setattr(contracts, "datetime", FrozenDateTime)
    monkeypatch.setattr(contracts, "date", LocalDate)


@pytest.mark.parametrize("local_day", [7, 9])
def test_request_dates_use_utc_independent_of_local_day(monkeypatch, local_day):
    from tradingagents.workbench import contracts

    monkeypatch.setattr(contracts.date, "today", staticmethod(lambda: date(2026, 9, local_day)))
    assert contracts.default_request()["analysis_date"] == "2026-09-08"
    assert contracts.validate_request({"analysis_date": "2026-09-08"})["analysis_date"] == "2026-09-08"
    with pytest.raises(ValueError, match="future"):
        contracts.validate_request({"analysis_date": "2026-09-09"})


def test_rejects_unbounded_rounds():
    from tradingagents.workbench.contracts import validate_request

    with pytest.raises(ValueError):
        validate_request({"debate_rounds": 100})


def test_rejects_request_environment_injection():
    from tradingagents.workbench.contracts import validate_request

    with pytest.raises(ValueError):
        validate_request({"env": {"AZURE_OPENAI_ENDPOINT": "https://example.com"}})


def test_rejects_unselected_vendor():
    from tradingagents.workbench.contracts import validate_request

    with pytest.raises(ValueError):
        validate_request({"data_vendors": {"news_data": "unknown_vendor"}})


def test_normalizes_analysts_without_changing_vendor_chain():
    from tradingagents.workbench.contracts import validate_request

    result = validate_request({
        "ticker": "msft", "analysts": ["news", "market"],
        "data_vendors": {"news_data": "yfinance,alpha_vantage"},
    })
    assert result["ticker"] == "MSFT"
    assert result["analysts"] == ["market", "news"]
    assert result["data_vendors"]["news_data"] == "yfinance,alpha_vantage"


def test_defaults_are_complete_and_independent():
    from tradingagents.workbench.contracts import default_request, validate_request

    expected = {
        "mode": "analysis", "ticker": "MSFT", "analysis_date": "2026-09-08",
        "asset_type": "stock", "analysts": ["market", "social", "news", "fundamentals"],
        "debate_rounds": 1, "risk_rounds": 1, "language": "Chinese",
        "data_vendors": {
            "core_stock_apis": "yfinance", "technical_indicators": "yfinance",
            "fundamental_data": "yfinance", "news_data": "yfinance",
            "macro_data": "fred", "prediction_markets": "polymarket",
        },
    }
    assert validate_request({}) == expected
    defaults = default_request()
    defaults["analysts"].clear()
    defaults["data_vendors"]["news_data"] = "alpha_vantage"
    assert default_request() == expected


@pytest.mark.parametrize("payload", [
    None, [], {"mode": "shell"}, {"mode": []}, {"ticker": 42},
    {"ticker": "../../tmp"}, {"ticker": "MSFT;whoami"}, {"ticker": "'MSFT'"},
    {"ticker": "A" * 25}, {"ticker": ""}, {"ticker": "MSFT\\file"},
    {"ticker": ".."}, {"ticker": "MSFT\n"}, {"ticker": " MSFT"},
    {"analysis_date": "20260901"}, {"analysis_date": "2026-02-30"},
    {"analysis_date": "2026-09-09"},
    {"analysis_date": 20260901}, {"asset_type": "crypto"},
    {"analysts": []}, {"analysts": "market"}, {"analysts": ["unknown"]},
    {"analysts": ["market", "market"]}, {"analysts": [True]},
    {"debate_rounds": True}, {"debate_rounds": 1.0}, {"risk_rounds": "1"},
    {"risk_rounds": 0}, {"risk_rounds": 4}, {"language": "French"},
    {"data_vendors": []}, {"data_vendors": {"unknown": "fred"}},
    {"data_vendors": {"news_data": "default"}},
    {"data_vendors": {"news_data": ["yfinance"]}},
    {"data_vendors": {"news_data": "yfinance,"}},
    {"data_vendors": {"news_data": "yfinance,yfinance"}},
    {"data_vendors": {"macro_data": "yfinance"}},
])
def test_rejects_invalid_request(payload):
    from tradingagents.workbench.contracts import validate_request

    with pytest.raises(ValueError):
        validate_request(payload)


@pytest.mark.parametrize("ticker", ["BRK.B", "0700.HK", "BTC-USD", "^GSPC", "ES=F"])
def test_accepts_safe_upstream_symbols(ticker):
    from tradingagents.workbench.contracts import validate_request

    assert validate_request({"ticker": ticker})["ticker"] == ticker


def test_merges_vendor_overrides_without_mutating_request():
    from tradingagents.workbench.contracts import validate_request

    payload = {"mode": "connectivity", "risk_rounds": 3, "debate_rounds": 3,
               "data_vendors": {"news_data": "alpha_vantage, yfinance"}}
    result = validate_request(payload)
    assert result["data_vendors"]["news_data"] == "alpha_vantage,yfinance"
    assert result["data_vendors"]["macro_data"] == "fred"
    assert payload["data_vendors"] == {"news_data": "alpha_vantage, yfinance"}


def test_event_roundtrip_reordered_parts_duplicates_and_cross_run():
    from tradingagents.workbench.contracts import decode_events, encode_event

    run_id = str(uuid4())
    content = random.Random(7).randbytes(20000).hex() + "\u4e2d\u6587"
    first = encode_event(run_id, "report", {"section": "market_report", "content": content})
    second = encode_event(run_id, "result", {"decision": "HOLD"})
    other = encode_event(str(uuid4()), "error", {"message": "other run"})
    assert len(first) > 1
    assert all(line.startswith("TAWB1 ") and len((line + "\n").encode()) <= 3500
               for line in first + second)
    envelope = json.loads(first[0][6:])
    assert set(envelope) == {"run_id", "message_id", "part", "total", "sha256", "data"}
    assert envelope["part"] == 1 and envelope["total"] == len(first)
    events = decode_events(other + second + list(reversed(first)) + first + second, run_id)
    assert [event["kind"] for event in events] == ["report", "result"]
    assert events[0]["payload"] == {"section": "market_report", "content": content}
    assert set(events[0]) == {"kind", "payload", "timestamp"}


def test_event_order_survives_equal_clock_ticks(monkeypatch):
    from datetime import datetime, timezone

    from tradingagents.workbench import contracts

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(contracts, "datetime", FrozenClock)
    run_id = str(uuid4())
    first = contracts.encode_event(run_id, "report", {"content": "report first"})
    second = contracts.encode_event(run_id, "result", {"decision": "HOLD"})
    events = contracts.decode_events(second + first, run_id)
    assert [event["kind"] for event in events] == ["report", "result"]


def _forged_lines(run_id, raw):
    encoded = base64.b64encode(gzip.compress(raw)).decode("ascii")
    parts = [encoded[index:index + 3000] for index in range(0, len(encoded), 3000)]
    message_id = str(uuid4())
    return ["TAWB1 " + json.dumps({
        "run_id": run_id, "message_id": message_id, "part": index + 1,
        "total": len(parts), "sha256": hashlib.sha256(raw).hexdigest(), "data": part,
    }) for index, part in enumerate(parts)]


@pytest.mark.parametrize("change", [
    {"part": 0}, {"part": True}, {"total": 1001}, {"total": "1"},
    {"sha256": "0" * 64}, {"data": "%%%"}, {"message_id": "not-a-uuid"},
    {"data": []}, {"extra": "unexpected"},
])
def test_ignores_invalid_event_envelopes(change):
    from tradingagents.workbench.contracts import decode_events, encode_event

    run_id = str(uuid4())
    envelope = json.loads(encode_event(run_id, "phase", {"stage": "start"})[0][6:])
    envelope.update(change)
    assert decode_events(["TAWB1 " + json.dumps(envelope)], run_id) == []


def test_ignores_incomplete_corrupt_and_conflicting_messages():
    from tradingagents.workbench.contracts import decode_events, encode_event

    run_id = str(uuid4())
    lines = encode_event(run_id, "report", {"content": random.Random(1).randbytes(8000).hex()})
    assert decode_events(lines[:-1], run_id) == []
    conflicting = json.loads(lines[0][6:])
    conflicting["data"] = "AAAA"
    assert decode_events(lines + ["TAWB1 " + json.dumps(conflicting)], run_id) == []
    assert decode_events([None, 42, "log noise", "TAWB1 {}", "TAWB1 []", "TAWB1 {",
                          "TAWB1 " + "x" * 3500], run_id) == []


@pytest.mark.parametrize("raw", [
    b"not JSON", b"[]", b'{"kind":"shell","payload":{},"timestamp":"2026-09-01T00:00:00Z"}',
    b'{"kind":"phase","payload":{},"timestamp":"invalid"}',
    b'{"kind":"phase","payload":[],"timestamp":"2026-09-01T00:00:00Z"}',
    b"x" * (2 * 1024 * 1024 + 1),
], ids=["not-json", "not-object", "unknown-kind", "invalid-time", "invalid-payload", "zip-bomb"])
def test_decoding_valid_checksum_still_enforces_event_and_expansion_limits(raw):
    from tradingagents.workbench.contracts import decode_events

    run_id = str(uuid4())
    assert decode_events(_forged_lines(run_id, raw), run_id) == []


def test_encoding_rejects_invalid_ids_kinds_and_oversize_payloads():
    from tradingagents.workbench.contracts import encode_event

    for run_id, kind, payload in [
        ("invalid", "phase", {}), (str(uuid4()), "shell", {}),
        (str(uuid4()), "phase", []),
        (str(uuid4()), "report", {"content": "x" * (2 * 1024 * 1024)}),
        (str(uuid4()), "usage", {"input_tokens": float("nan")}),
    ]:
        with pytest.raises(ValueError):
            encode_event(run_id, kind, payload)


def test_scrubs_configured_secrets_recursively_before_encoding(monkeypatch):
    from tradingagents.workbench.contracts import decode_events, encode_event, scrub_secrets

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "alpha-test-secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-test-secret")
    payload = {"content": ["alpha-test-secret", {"detail": "key=fred-test-secret"}]}
    assert scrub_secrets(payload) == {"content": ["[REDACTED]", {"detail": "key=[REDACTED]"}]}
    assert payload["content"][0] == "alpha-test-secret"
    run_id = str(uuid4())
    events = decode_events(encode_event(run_id, "report", payload), run_id)
    assert "alpha-test-secret" not in json.dumps(events)
    assert "fred-test-secret" not in json.dumps(events)
