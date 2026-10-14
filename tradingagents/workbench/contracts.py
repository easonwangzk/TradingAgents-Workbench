"""Validated requests and bounded console events shared by worker and controller."""

import base64
import gzip
import hashlib
import io
import json
import os
import re
import threading
import zlib
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

ANALYSTS = ("market", "social", "news", "fundamentals")
VENDOR_OPTIONS = {
    "core_stock_apis": ("yfinance", "alpha_vantage"),
    "technical_indicators": ("yfinance", "alpha_vantage"),
    "fundamental_data": ("yfinance", "alpha_vantage"),
    "news_data": ("yfinance", "alpha_vantage"),
    "macro_data": ("fred", "alpha_vantage"),
    "prediction_markets": ("polymarket",),
}
EVENT_KINDS = ("phase", "report", "usage", "connectivity", "result", "error")
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_PARTS = 1000
MAX_LINE_BYTES = 3500
_PART_SIZE = 3000
_SECRET_ENV_NAMES = (
    "ALPHA_VANTAGE_API_KEY", "FRED_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY",
    "TAWB_RUN_CAPABILITY",
)
_TIMESTAMP_LOCK = threading.Lock()
_last_timestamp = datetime.min.replace(tzinfo=timezone.utc)


def _event_timestamp():
    global _last_timestamp
    with _TIMESTAMP_LOCK:
        _last_timestamp = max(datetime.now(timezone.utc), _last_timestamp + timedelta(microseconds=1))
        return _last_timestamp.isoformat()


def default_request() -> dict:
    """Return an independent complete request; data_vendors maps category to chain."""
    return {
        "mode": "analysis",
        "ticker": "MSFT",
        "analysis_date": datetime.now(timezone.utc).date().isoformat(),
        "asset_type": "stock",
        "analysts": list(ANALYSTS),
        "debate_rounds": 1,
        "risk_rounds": 1,
        "language": "Chinese",
        "data_vendors": {category: options[0] for category, options in VENDOR_OPTIONS.items()},
    }


def validate_request(payload: dict) -> dict:
    """Return a complete normalized request or raise ValueError with no input echoed."""
    result = default_request()
    if type(payload) is not dict or payload.keys() - result.keys():
        raise ValueError("Invalid request fields")
    result.update(payload)
    for field, choices in (
        ("mode", ("analysis", "connectivity")),
        ("asset_type", ("stock", "etf")),
        ("language", ("Chinese", "English")),
    ):
        if type(result[field]) is not str or result[field] not in choices:
            raise ValueError(f"Invalid {field}")
    ticker = result["ticker"]
    if (type(ticker) is not str or len(ticker) > 24
            or not re.fullmatch(r"\^?[A-Za-z0-9]+(?:[.=-][A-Za-z0-9]+)*", ticker)):
        raise ValueError("Invalid ticker")
    result["ticker"] = ticker.upper()
    raw_date = result["analysis_date"]
    if type(raw_date) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date):
        raise ValueError("Invalid analysis_date")
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError:
        raise ValueError("Invalid analysis_date") from None
    if parsed_date > datetime.now(timezone.utc).date():
        raise ValueError("analysis_date must not be in the future")
    for field in ("debate_rounds", "risk_rounds"):
        if type(result[field]) is not int or not 1 <= result[field] <= 3:
            raise ValueError(f"Invalid {field}")
    analysts = result["analysts"]
    if (type(analysts) is not list or not analysts
            or any(type(analyst) is not str or analyst not in ANALYSTS for analyst in analysts)
            or len(set(analysts)) != len(analysts)):
        raise ValueError("Invalid analysts")
    result["analysts"] = [analyst for analyst in ANALYSTS if analyst in analysts]
    vendors = result["data_vendors"]
    if type(vendors) is not dict or vendors.keys() - VENDOR_OPTIONS.keys():
        raise ValueError("Invalid data_vendors")
    normalized = default_request()["data_vendors"]
    for category, chain in vendors.items():
        if type(chain) is not str:
            raise ValueError("Invalid vendor chain")
        selected = [vendor.strip() for vendor in chain.split(",")]
        if (any(vendor not in VENDOR_OPTIONS[category] for vendor in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("Invalid vendor chain")
        normalized[category] = ",".join(selected)
    if result["asset_type"] == "etf" and normalized["fundamental_data"] != "yfinance":
        raise ValueError("ETF fundamentals require yfinance")
    result["data_vendors"] = normalized
    return result


def scrub_secrets(value):
    """Copy JSON-compatible values, redacting configured vendor and model API keys."""
    secrets = sorted({os.getenv(name) for name in _SECRET_ENV_NAMES} - {None, ""},
                     key=len, reverse=True)

    def scrub(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, dict):
            return {scrub(key): scrub(content) for key, content in item.items()}
        if isinstance(item, list):
            return [scrub(content) for content in item]
        return item

    return scrub(value)


def _valid_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def encode_event(run_id: str, kind: str, payload: dict) -> list[str]:
    """Encode an event as newline-free TAWB1 lines (1-based parts, SHA-256 of JSON)."""
    if not _valid_uuid(run_id) or kind not in EVENT_KINDS or type(payload) is not dict:
        raise ValueError("Invalid event")
    try:
        raw = json.dumps({
            "kind": kind, "payload": scrub_secrets(payload),
            "timestamp": _event_timestamp(),
        }, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Invalid event payload") from None
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("Event exceeds decoded size limit")
    data = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    parts = [data[index:index + _PART_SIZE] for index in range(0, len(data), _PART_SIZE)]
    if len(parts) > MAX_PARTS:
        raise ValueError("Event exceeds part limit")
    message_id = str(uuid4())
    digest = hashlib.sha256(raw).hexdigest()
    return ["TAWB1 " + json.dumps({
        "run_id": run_id, "message_id": message_id, "part": index + 1,
        "total": len(parts), "sha256": digest, "data": part,
    }, separators=(",", ":")) for index, part in enumerate(parts)]


def _reject_json_constant(value):
    raise ValueError("Invalid JSON constant")


def decode_events(lines, run_id: str) -> list[dict]:
    """Return complete verified events, deduplicated by message ID and timestamp ordered.

    Parts are 1-based; conflicting duplicates invalidate the entire message.
    Limits apply per message, including at most 2 MiB of decompressed JSON.
    """
    if not _valid_uuid(run_id):
        return []
    messages = {}
    invalid = set()
    fields = {"run_id", "message_id", "part", "total", "sha256", "data"}
    for line in lines:
        if (not isinstance(line, str) or not line.startswith("TAWB1 ")
                or len(line) > MAX_LINE_BYTES):
            continue
        try:
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                continue
            part = json.loads(line[6:], parse_constant=_reject_json_constant)
            if (type(part) is not dict or set(part) != fields or part["run_id"] != run_id
                    or not _valid_uuid(part["message_id"])
                    or type(part["total"]) is not int or not 1 <= part["total"] <= MAX_PARTS
                    or type(part["part"]) is not int or not 1 <= part["part"] <= part["total"]
                    or not isinstance(part["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", part["sha256"])
                    or not isinstance(part["data"], str)
                    or not 1 <= len(part["data"]) <= _PART_SIZE
                    or not re.fullmatch(r"[A-Za-z0-9+/=]+", part["data"])):
                continue
        except (ValueError, UnicodeError, RecursionError):
            continue
        message_id = part["message_id"]
        if message_id in invalid:
            continue
        message = messages.setdefault(message_id, {
            "total": part["total"], "sha256": part["sha256"], "parts": {},
        })
        existing = message["parts"].get(part["part"])
        if (message["total"] != part["total"] or message["sha256"] != part["sha256"]
                or (existing is not None and existing != part["data"])):
            invalid.add(message_id)
            del messages[message_id]
            continue
        message["parts"][part["part"]] = part["data"]
    events = []
    for message in messages.values():
        if len(message["parts"]) != message["total"]:
            continue
        try:
            encoded = "".join(message["parts"][index] for index in range(1, message["total"] + 1))
            compressed = base64.b64decode(encoded, validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(MAX_EVENT_BYTES + 1)
            if len(raw) > MAX_EVENT_BYTES or hashlib.sha256(raw).hexdigest() != message["sha256"]:
                continue
            event = json.loads(raw, parse_constant=_reject_json_constant)
            if (type(event) is not dict or set(event) != {"kind", "payload", "timestamp"}
                    or event["kind"] not in EVENT_KINDS or type(event["payload"]) is not dict
                    or not isinstance(event["timestamp"], str)):
                continue
            timestamp = datetime.fromisoformat(event["timestamp"])
            if timestamp.tzinfo is None:
                continue
            events.append((timestamp, event))
        except (ValueError, TypeError, OSError, EOFError, zlib.error, RecursionError):
            continue
    return [event for _, event in sorted(events, key=lambda item: item[0])]
