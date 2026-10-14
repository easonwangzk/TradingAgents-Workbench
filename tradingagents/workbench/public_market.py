"""Account-owned watchlists and bounded, daily public market data reads."""

import hashlib
import logging
import math
import time
from collections import OrderedDict, deque
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from threading import BoundedSemaphore, Lock

import httpx
import yfinance

from tradingagents.workbench.contracts import validate_request
from tradingagents.workbench.public_settings import _owner
from tradingagents.workbench.public_store import StoreConflict
from tradingagents.workbench.public_transport import (
    create_pinned_http_client,
    validate_gateway_http_client,
)

RANGES = {"1m": 31, "3m": 93, "6m": 186, "1y": 366, "5y": 1830}
SOURCES = {"yfinance", "alpha_vantage"}
QUOTE_FIELDS = ("symbol", "name", "price", "change", "change_percent", "currency",
                "exchange", "as_of", "open", "high", "low", "previous_close", "volume")


class _AlphaLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                value.copy_set_param("apikey", "REDACTED")
                if isinstance(value, httpx.URL) and value.host == "www.alphavantage.co" and "apikey" in value.params
                else value for value in record.args
            )
        return True


_ALPHA_LOG_FILTER = _AlphaLogFilter()


class MarketError(RuntimeError):
    def __init__(self, status_code=400):
        super().__init__({400: "Invalid market input.", 403: "Market operation is not permitted.", 409: "Watchlist changed.",
                          429: "Market request limit reached.",
                          503: "Market storage is unavailable."}[status_code])
        self.status_code = status_code


class _ProviderError(Exception):
    def __init__(self, code="provider_unavailable"):
        self.code = code


def _symbol(value):
    try:
        return validate_request({"ticker": value})["ticker"]
    except (ValueError, TypeError):
        raise MarketError() from None


def _symbols(values):
    if type(values) is not list or len(values) > 12:
        raise MarketError()
    result = [_symbol(value) for value in values]
    if len(set(result)) != len(result):
        raise MarketError()
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (Real, str)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _text(value):
    return value[:160] if type(value) is str and value else None


def _day(value):
    if type(value) is not str or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _timestamp(value):
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _candle(day, values):
    numbers = [_number(value) for value in values]
    opening, high, low, close, volume = numbers
    if (day is None or any(value is None or value < 0 for value in numbers[:4])
            or high < max(opening, low, close) or low > min(opening, high, close)):
        return None
    return {"time": day, "open": opening, "high": high, "low": low,
            "close": close, "volume": volume if volume is None or volume >= 0 else None}


class MarketData:
    def __init__(self, *, store, settings, clock=time.time, monotonic=time.monotonic, http_client=None):
        logging.getLogger("httpx").addFilter(_ALPHA_LOG_FILTER)
        self._store = store
        self._settings = settings
        self._clock = clock
        self._monotonic = monotonic
        self._http = validate_gateway_http_client(http_client) if http_client is not None else None
        self._lock = Lock()
        self._slots = BoundedSemaphore(4)
        self._active = set()
        self._accounts = OrderedDict()
        self._cache = OrderedDict()

    def _authorize(self, principal):
        from tradingagents.workbench.public_accounts import REGISTRY_KEY, permissions

        try:
            stored = self._store.read(REGISTRY_KEY)
            if stored is None:
                raise MarketError(403)
            user = stored[0]["users"].get(principal.object_id)
            if (user is None or user["disabled"] or user["email"] != principal.email
                    or (principal.object_id == stored[0]["owner_id"]) != principal.is_admin
                    or not permissions(user)["market"]):
                raise MarketError(403)
        except MarketError:
            raise
        except Exception:
            raise MarketError(503) from None

    def watchlist(self, principal):
        self._authorize(principal)
        owner = _owner(principal)
        try:
            stored = self._store.read(f"{owner}/market-watchlist")
            if stored is None:
                return {"symbols": [], "version": None}
            record, version = stored
            if (type(record) is not dict or set(record) != {"symbols"}
                    or type(version) is not str or not 1 <= len(version) <= 256
                    or _symbols(record["symbols"]) != record["symbols"]):
                raise ValueError
            return {"symbols": list(record["symbols"]), "version": version}
        except Exception:
            raise MarketError(503) from None

    def update_watchlist(self, principal, payload, *, expected_version):
        self._authorize(principal)
        owner = _owner(principal)
        if type(payload) is not dict or set(payload) != {"symbols"}:
            raise MarketError()
        symbols = _symbols(payload["symbols"])
        if expected_version is not None and (type(expected_version) is not str or not 1 <= len(expected_version) <= 256):
            raise MarketError()
        current = self.watchlist(principal)
        if expected_version != current["version"]:
            raise MarketError(409)
        try:
            version = self._store.write(f"{owner}/market-watchlist", {"symbols": symbols}, expected_version)
            if type(version) is not str or not 1 <= len(version) <= 256:
                raise ValueError
        except StoreConflict:
            raise MarketError(409) from None
        except Exception:
            raise MarketError(503) from None
        return {"symbols": symbols, "version": version}

    def _admit(self, owner):
        now = self._monotonic()
        with self._lock:
            for key in list(self._accounts):
                requests, calls = self._accounts[key]
                for queue in (requests, calls):
                    while queue and queue[0] <= now - 60:
                        queue.popleft()
                if not requests and not calls and key not in self._active:
                    del self._accounts[key]
            if owner in self._active or (owner not in self._accounts and len(self._accounts) >= 256):
                raise MarketError(429)
            requests, _ = self._accounts.setdefault(owner, (deque(), deque()))
            if len(requests) >= 30 or not self._slots.acquire(blocking=False):
                raise MarketError(429)
            requests.append(now)
            self._active.add(owner)

    def _provider_call(self, owner):
        now = self._monotonic()
        with self._lock:
            calls = self._accounts[owner][1]
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= 20:
                raise _ProviderError("rate_limited")
            calls.append(now)

    def _cached(self, key, loader, fetched_times):
        now = self._monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(key)
                fetched_times.append(cached[2])
                if isinstance(cached[1], _ProviderError):
                    raise _ProviderError(cached[1].code)
                return deepcopy(cached[1])
        try:
            result = loader()
        except MarketError:
            raise
        except _ProviderError as error:
            result = _ProviderError(error.code)
        except Exception:
            result = _ProviderError()
        fetched_at = self._clock()
        fetched_times.append(fetched_at)
        with self._lock:
            self._cache[key] = (self._monotonic() + (15 if isinstance(result, _ProviderError) else 60), result, fetched_at)
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
        if isinstance(result, _ProviderError):
            raise result
        return deepcopy(result)

    def snapshot(self, principal, payload):
        self._authorize(principal)
        owner = _owner(principal)
        if type(payload) is not dict or set(payload) != {"symbols", "selected", "range", "source"}:
            raise MarketError()
        symbols = _symbols(payload["symbols"])
        selected = _symbol(payload["selected"]) if payload["selected"] is not None else None
        source, period = payload["source"], payload["range"]
        if (type(source) is not str or source not in SOURCES
                or type(period) is not str or period not in RANGES
                or (symbols and selected not in symbols) or (not symbols and selected is not None)):
            raise MarketError()
        self._admit(owner)
        try:
            result = {"quotes": [], "candles": [], "selected": selected, "source": source,
                      "fetched_at": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
                      "interval": "1d", "errors": []}
            if not symbols:
                return result
            key = self._settings.data_key(principal, "alpha_vantage") if source == "alpha_vantage" else None
            fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else None
            tickers = {}
            fetched_times = []

            def ticker(symbol):
                if symbol not in tickers:
                    tickers[symbol] = yfinance.Ticker(symbol)
                return tickers[symbol]

            def quote(symbol):
                self._authorize(principal)
                if source == "alpha_vantage" and not key:
                    raise _ProviderError("key_unconfigured")
                self._provider_call(owner)
                return (self._yahoo_quote(symbol, ticker(symbol)) if source == "yfinance"
                        else self._alpha_quote(symbol, self._alpha("GLOBAL_QUOTE", symbol, key)))

            for symbol in symbols:
                try:
                    result["quotes"].append(self._cached((owner, source, symbol, "quote", fingerprint), lambda symbol=symbol: quote(symbol), fetched_times))
                except _ProviderError as error:
                    self._error(result, symbol, error.code)
            if selected is not None:
                try:
                    history_period = period if source == "yfinance" else "3m" if period in {"1m", "3m"} else "5y"

                    def history():
                        self._authorize(principal)
                        if source == "alpha_vantage" and not key:
                            raise _ProviderError("key_unconfigured")
                        self._provider_call(owner)
                        return (self._yahoo_candles(ticker(selected), period) if source == "yfinance"
                                else self._alpha_candles(self._alpha("TIME_SERIES_DAILY", selected, key, period=history_period), history_period))
                    candles = self._cached((owner, source, selected, history_period, fingerprint), history, fetched_times)
                    result["candles"] = self._filter_candles(candles, period)
                except _ProviderError as error:
                    self._error(result, selected, error.code)
            if fetched_times:
                result["fetched_at"] = datetime.fromtimestamp(min(fetched_times), timezone.utc).isoformat()
            return result
        finally:
            with self._lock:
                self._active.discard(owner)
                self._slots.release()

    @staticmethod
    def _error(result, symbol, code):
        messages = {"provider_unavailable": "Market data is unavailable.",
                    "key_unconfigured": "Configure your Alpha Vantage data key.",
                    "rate_limited": "Market provider request limit reached.",
                    "no_data": "No market data is available.",
                    "invalid_key": "Alpha Vantage credentials are unavailable."}
        if not any(error["symbol"] == symbol for error in result["errors"]):
            result["errors"].append({"symbol": symbol, "code": code, "message": messages[code]})

    @staticmethod
    def _yahoo_quote(symbol, ticker):
        info = ticker.get_info()
        if type(info) is not dict or not info:
            raise _ProviderError("no_data")
        if info.get("symbol") is not None and info["symbol"] != symbol:
            raise _ProviderError()
        quote = dict.fromkeys(QUOTE_FIELDS)
        quote.update(symbol=symbol, name=_text(info.get("shortName") or info.get("longName")),
                     currency=_text(info.get("currency")), exchange=_text(info.get("exchange")),
                     as_of=_timestamp(info.get("regularMarketTime")))
        for field, native in (("price", "regularMarketPrice"), ("previous_close", "regularMarketPreviousClose"),
                              ("open", "regularMarketOpen"), ("high", "regularMarketDayHigh"),
                              ("low", "regularMarketDayLow"), ("volume", "regularMarketVolume")):
            quote[field] = _number(info.get(native))
        if quote["price"] is None:
            raise _ProviderError("no_data")
        if quote["previous_close"] is not None:
            quote["change"] = quote["price"] - quote["previous_close"]
            if quote["previous_close"] != 0:
                quote["change_percent"] = quote["change"] / quote["previous_close"] * 100
        return quote

    def _filter_candles(self, candles, period):
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        start = (today - timedelta(days=RANGES[period])).isoformat()
        result = sorted({item["time"]: item for item in candles if item is not None
                         and start <= item["time"] <= today.isoformat()}.values(), key=lambda item: item["time"])[-1260:]
        if not result:
            raise _ProviderError("no_data")
        return result

    def _yahoo_candles(self, ticker, period):
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        frame = ticker.history(start=(today - timedelta(days=RANGES[period])).isoformat(),
                               end=(today + timedelta(days=1)).isoformat(), interval="1d",
                               auto_adjust=False, actions=False, timeout=10, raise_errors=True)
        if frame is None or frame.empty or len(frame) > 2000:
            raise _ProviderError("no_data")
        return self._filter_candles([
            _candle(_day(index.date().isoformat()), [row.get(name) for name in ("Open", "High", "Low", "Close", "Volume")])
            for index, row in frame.iterrows()
        ], period)

    def _alpha(self, function, symbol, key, *, period=None):
        params = {"function": function, "symbol": symbol, "apikey": key}
        if period is not None:
            params["outputsize"] = "compact" if period in {"1m", "3m"} else "full"
        context = nullcontext(self._http) if self._http is not None else create_pinned_http_client(timeout=httpx.Timeout(10, connect=5))
        with context as client:
            validate_gateway_http_client(client)
            with client.stream("GET", "https://www.alphavantage.co/query", params=params) as response:
                if response.status_code == 429:
                    raise _ProviderError("rate_limited")
                if response.status_code in {401, 403}:
                    raise _ProviderError("invalid_key")
                if response.status_code != 200:
                    raise _ProviderError()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 3 * 1024 * 1024:
                        raise _ProviderError()
                data = httpx.Response(200, content=bytes(content)).json()
        if type(data) is not dict:
            raise _ProviderError()
        if data.get("Note") or data.get("Information"):
            message = str(data.get("Note") or data.get("Information")).lower()
            raise _ProviderError("invalid_key" if "invalid" in message and "key" in message else "rate_limited")
        if data.get("Error Message"):
            raise _ProviderError("no_data")
        return data

    @staticmethod
    def _alpha_quote(symbol, data):
        native = data.get("Global Quote")
        if type(native) is not dict or native.get("01. symbol") != symbol:
            raise _ProviderError("no_data")
        quote = dict.fromkeys(QUOTE_FIELDS)
        quote.update(symbol=symbol, as_of=_day(native.get("07. latest trading day")))
        for field, key in (("open", "02. open"), ("high", "03. high"), ("low", "04. low"),
                           ("price", "05. price"), ("volume", "06. volume"),
                           ("previous_close", "08. previous close"), ("change", "09. change")):
            quote[field] = _number(native.get(key))
        percent = native.get("10. change percent")
        quote["change_percent"] = _number(percent[:-1]) if type(percent) is str and percent.endswith("%") else None
        if quote["price"] is None:
            raise _ProviderError("no_data")
        return quote

    def _alpha_candles(self, data, period):
        series = data.get("Time Series (Daily)")
        if type(series) is not dict or not series or len(series) > 10000:
            raise _ProviderError("no_data")
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        start = (today - timedelta(days=RANGES[period])).isoformat()
        return self._filter_candles([
            _candle(_day(day), [row.get(name) for name in ("1. open", "2. high", "3. low", "4. close", "5. volume")])
            for day, row in series.items() if type(day) is str and start <= day <= today.isoformat() and type(row) is dict
        ], period)
