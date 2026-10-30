"""Replaceable market and research I/O adapters.

These adapters preserve source metadata; they do not rank or admit evidence.
Research URLs are untrusted input and cannot access the local machine/network.
"""
from __future__ import annotations

import asyncio
import io
import hashlib
import json
import time
from collections import OrderedDict
import ipaddress
import os
import re
import socket
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .calendar import market_state

SHANGHAI = ZoneInfo("Asia/Shanghai")
EASTMONEY_ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_FIELDS = "f43,f57,f58,f59,f60,f86,f170,f127"


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat()


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"(?:SH|SZ|BJ):\d{6}", symbol):
        raise ValueError("证券标识须为 SH:600519、SZ:000001 或 BJ:920xxx 格式")
    return symbol


def _kind(symbol: str, name: str = "") -> str | None:
    """Only supported A-share/ETF bands; unknown identity stays unknown."""
    exchange, code = symbol.split(":")
    if (exchange == "SH" and code.startswith(("51", "52", "56", "58"))) or (exchange == "SZ" and code.startswith("15")):
        return "etf"
    if (exchange == "SH" and code.startswith(("600", "601", "603", "605", "688", "689"))) or (exchange == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301"))) or (exchange == "BJ" and code.startswith(("4", "8", "92"))):
        return "stock"
    return None


def _decimal(value: Any, places: int) -> str | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        number = Decimal(str(value)) / (Decimal(10) ** places)
        if not number.is_finite():
            return None
        return format(number, "f")
    except (InvalidOperation, ValueError):
        return None


def _error(exc: Exception) -> str:
    # Do not echo HTTP response bodies, request headers, or secret-bearing URLs.
    if isinstance(exc, DeferredRequestError):
        return '本轮已确认同一路径取数失败，暂缓重复请求；可使用已存原始材料或其他公开来源，新任务可重新验证'
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "网络请求超时"
    if isinstance(exc, (httpx.RequestError, OSError)):
        return f"网络连接失败 ({type(exc).__name__})"
    return str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__


def _quote_failure_kind(exc: Exception) -> str:
    """Only report a network cause when the exception chain supports it."""
    if isinstance(exc, DeferredRequestError):
        return exc.failure_kind
    causes, current = [], exc
    while current is not None and len(causes) < 8:
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, socket.gaierror) for item in causes):
        return "dns"
    if isinstance(exc, httpx.ProxyError):
        return "proxy"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol"
    if isinstance(exc, httpx.HTTPStatusError):
        return "rate_limited" if exc.response.status_code == 429 else "http_status"
    if isinstance(exc, httpx.RequestError):
        return "connection"
    return "invalid_response"


class _QuoteFailure(ValueError):
    def __init__(self, exc: Exception, diagnostics: list[dict]):
        super().__init__(_error(exc))
        self.diagnostics = diagnostics


class DeferredRequestError(httpx.RequestError):
    def __init__(self, failure_kind):
        super().__init__('equivalent request deferred within this research task')
        self.failure_kind = failure_kind


def _deferred_failure(config, key, refresh=False):
    memory = config.get('_research_failure_memory')
    if memory is None:
        return None
    if refresh:
        memory.pop(key, None)
        return None
    previous = memory.get(key)
    return previous if previous and (previous['attempts'] >= 2 or time.monotonic() - previous['at'] < 30) else None


def _remember_failure(config, key, kind, stage=None, action=None):
    memory = config.get('_research_failure_memory')
    if memory is not None:
        previous = memory.get(key, {})
        memory[key] = {'at': time.monotonic(), 'attempts': previous.get('attempts', 0) + 1,
                       'kind': kind, 'stage': stage, 'action': action}
        while len(memory) > 128:
            del memory[next(iter(memory))]


def _last_completed_session(stamp: datetime, calendar_path=None) -> datetime | None:
    for days in range(15):
        day = (stamp - timedelta(days=days)).replace(hour=15, minute=0, second=0, microsecond=0)
        if day > stamp:
            continue
        trading = market_state(day, calendar_path)["trading_day"]
        if trading is None:
            return None
        if trading:
            return day
    return None


def quote_time_state(as_of: str | None, config: dict, *, stamp: datetime | None = None,
                     source_precision: str = "timestamp") -> dict:
    """Re-evaluate cached timestamps without presenting a close as a live tick."""
    stamp = stamp or datetime.now(SHANGHAI)
    calendar = market_state(stamp, config.get("calendar_path"))
    result = {"status": "unknown_time", "delay_seconds": None,
              "market_phase": calendar["phase"], "market_session": calendar["session"],
              "has_closed_today": calendar["has_closed_today"], "quote_kind": "unknown",
              "fresh_for_triggers": False}
    if source_precision == "date":
        try:
            if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
                return result
            source_date = datetime.strptime(as_of, "%Y-%m-%d").date()
        except ValueError:
            return result
        latest = _last_completed_session(stamp, config.get("calendar_path")) if calendar["session"] in ("after_close", "non_trading_day", "pre_open", "lunch_break") else None
        result.update(status="closed" if latest and source_date == latest.date() else
                      "future_timestamp" if source_date > stamp.date() else "stale", quote_kind="last_close")
        return result
    try:
        observed = datetime.fromisoformat(as_of) if as_of else None
        if observed is None or observed.tzinfo is None:
            return result
        observed = observed.astimezone(SHANGHAI)
        age = int((stamp - observed).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return result
    result.update(delay_seconds=max(0, age), status="future_timestamp" if age < -60 else "stale")
    if age < -60:
        return result
    # Calendar coverage is required: an unknown holiday cannot certify a close.
    if calendar["session"] in ("after_close", "non_trading_day", "pre_open", "lunch_break"):
        latest = _last_completed_session(stamp, config.get("calendar_path"))
        if latest and observed.date() == latest.date() and observed.hour >= 15:
            result.update(status="closed", quote_kind="last_close",
                          valuation_note="休市估值使用最近交易日收盘后数据源报价；源时间为供应商更新时间，不是逐笔成交时间。")
        if result["status"] == "closed":
            return result
    if age <= int(config.get("quote_stale_seconds", 180)):
        result.update(status="ok", quote_kind="session_snapshot",
                      fresh_for_triggers=age >= 0 and calendar["quote_trigger_eligible"])
    return result


class MarketProvider:
    """A public website adapter, not an exchange-authorized real-time feed."""

    def __init__(self, config: dict):
        self.config = config
        self.provider = config.get("quote_provider", "eastmoney")
        self._snapshots: dict = {}
        self._close_snapshots: dict = {}

    async def _data(self, symbol: str) -> dict:
        if self.provider != "eastmoney":
            raise ValueError("行情数据源未启用；manual 模式只使用显式录入的行情快照")
        exchange, code = normalize_symbol(symbol).split(":")
        if _kind(symbol) is None:
            raise ValueError("首版仅支持可识别的 A 股与场内 ETF，请核对交易所和代码")
        ttl = _bounded(self.config.get("quote_cache_seconds"), 30, 0, 300)
        cached = self._snapshots.get(symbol)
        if ttl and cached and time.monotonic() - cached[0] < ttl:
            return {**cached[1], "_quote_meta": {**cached[1]["_quote_meta"], "cache_hit": True,
                    "cache_age_seconds": int(time.monotonic() - cached[0])}}
        secid = f"{'1' if exchange == 'SH' else '0'}.{code}"
        diagnostics = []
        attempts = _bounded(self.config.get("quote_max_attempts"), 2, 1, 3)
        async with httpx.AsyncClient(timeout=float(self.config.get("network_timeout_seconds", 12))) as client:
            for attempt in range(1, attempts + 1):
                started = time.monotonic()
                diagnostic = {"stage": "quote", "attempt": attempt, "host": "push2.eastmoney.com"}
                try:
                    response = await client.get(
                        EASTMONEY_ENDPOINT,
                        params={"secid": secid, "fields": EASTMONEY_FIELDS},
                        headers={"User-Agent": "Mozilla/5.0 LocalStockResearch/1.0", "Referer": "https://quote.eastmoney.com/"},
                    )
                    diagnostic["http_status"] = response.status_code
                    response.raise_for_status()
                    if len(response.content) > 100_000:
                        raise ValueError("单证券行情响应超过 100 KB 上限")
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("行情响应格式异常，预期证券数据对象")
                    data = payload.get("data")
                    if payload.get("rc") != 0 or not isinstance(data, dict):
                        raise ValueError("行情服务未返回证券数据；证券代码可能无效或服务暂不可用")
                    if str(data.get("f57", "")) != code or not data.get("f58"):
                        raise ValueError("行情响应中的证券身份与请求不匹配")
                    diagnostic.update(status="received", elapsed_seconds=round(time.monotonic() - started, 3))
                    diagnostics.append(diagnostic)
                    break
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    diagnostic.update(status="failed", failure_kind=_quote_failure_kind(exc),
                                      exception=type(exc).__name__, elapsed_seconds=round(time.monotonic() - started, 3))
                    diagnostics.append(diagnostic)
                    # A 429 is an explicit throttle, not a transient disconnect:
                    # let the normal refresh interval retry instead of adding load.
                    retryable = isinstance(exc, httpx.RequestError) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500)
                    if not retryable or attempt == attempts:
                        raise _QuoteFailure(exc, diagnostics) from exc
                    await asyncio.sleep(min(0.15 * 2 ** (attempt - 1), 0.6))
        data = {**data, "_quote_meta": {"fetched_at": now_iso(), "cache_hit": False,
                "cache_age_seconds": 0, "request_diagnostics": diagnostics}}
        if ttl:
            self._snapshots[symbol] = (time.monotonic(), data)
        return data

    async def _closed_quote(self, symbol: str) -> dict | None:
        """Same public provider, unadjusted daily close; never an intraday feed."""
        stamp = datetime.now(SHANGHAI)
        calendar = market_state(stamp, self.config.get("calendar_path"))
        if self.provider != "eastmoney" or calendar["session"] not in ("after_close", "non_trading_day", "pre_open", "lunch_break"):
            return None
        latest = _last_completed_session(stamp, self.config.get("calendar_path"))
        if latest is None:
            return None
        exchange, code = symbol.split(":")
        started = time.monotonic()
        async with asyncio.timeout(float(self.config.get("network_timeout_seconds", 12)) + 1):
            rows, meta = await self._eastmoney_history(f"{'1' if exchange == 'SH' else '0'}.{code}",
                latest.strftime("%Y%m%d"), latest.strftime("%Y%m%d"), "daily", "none")
        rows = _validated_bars(rows, latest.strftime("%Y%m%d"), latest.strftime("%Y%m%d"))
        if not rows or rows[-1]["time"] != latest.date().isoformat() or not meta.get("name"):
            return None
        row = rows[-1]
        if Decimal(row["close"]) <= 0:
            return None
        result = {"symbol": symbol, "name": meta["name"], "price": row["close"],
                  "change_pct": row.get("change_pct"), "as_of": row["time"], "as_of_precision": "date",
                  "source_time_precision": "date", "fetched_at": meta["fetched_at"],
                  "source": "eastmoney:daily-unadjusted-close", "source_url": f"https://quote.eastmoney.com/{symbol.replace(':', '').lower()}.html",
                  "status": "closed", "quote_kind": "last_close", "fresh_for_triggers": False,
                  "delay_seconds": None, "market_phase": "closed", "market_session": calendar["session"],
                  "has_closed_today": calendar["has_closed_today"], "security_status": "unknown",
                  "cache_hit": meta.get("cache_hit", False), "cache_age_seconds": meta.get("cache_age_seconds", 0),
                  "session_close_at": latest.isoformat(), "session_close_time_basis": "exchange_calendar_not_supplier_timestamp",
                  "valuation_note": "即时接口失败，休市估值改用同供应商最近已完成交易日的未复权日线收盘价。源时间仅精确到日期；不构造秒级行情时间，不触发新鲜行情条件。",
                  "fallback_diagnostic": {"stage": "closed_quote", "host": "push2his.eastmoney.com", "status": "received",
                                          "elapsed_seconds": round(time.monotonic() - started, 3)}}
        self._close_snapshots[symbol] = result
        return result

    async def quote(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        budget = max(0.1, min(45.0, float(self.config.get("quote_refresh_timeout_seconds", 45))))
        try:
            async with asyncio.timeout(budget):
                return await self._quote(symbol)
        except TimeoutError:
            return {"symbol": symbol, "name": None, "price": None, "change_pct": None,
                    "as_of": None, "fetched_at": now_iso(), "source": self.provider,
                    "status": "unavailable", "security_status": "unknown", "delay_seconds": None,
                    "fresh_for_triggers": False, "quote_kind": "unknown",
                    "error": "行情读取达到整条请求路径的总时限", "request_diagnostics": [
                        {"stage": "quote_total", "status": "failed", "failure_kind": "timeout", "budget_seconds": budget}]}

    async def _sina_quote(self, symbol: str) -> dict | None:
        """Read the publicly displayed Sina quote resource; no API/SLA claim.

        The official stock page embeds hq.sinajs.cn/list=<symbol>; its hqParser
        maps last/previous close/date/time to fields 3/2/30/31. Do not execute JS.
        """
        if self.config.get("quote_fallback_provider", "none") != "sina" or symbol.split(":")[0] not in ("SH", "SZ"):
            return None
        security = symbol.replace(":", "").lower()
        source_url = f"https://finance.sina.com.cn/realstock/company/{security}/nc.shtml"
        timeout = min(8.0, float(self.config.get("network_timeout_seconds", 12)))
        started = time.monotonic()
        diagnostic = {"stage": "quote_fallback", "host": "hq.sinajs.cn", "attempt": 1}
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    async with client.stream("GET", "https://hq.sinajs.cn/list=" + security,
                        headers={"User-Agent": "Mozilla/5.0 LocalStockResearch/4.0", "Referer": "https://finance.sina.com.cn/"}) as response:
                        diagnostic["http_status"] = response.status_code
                        response.raise_for_status()
                        parts, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 100_000:
                                raise ValueError("备用单证券行情响应超过 100 KB 上限")
                            parts.append(chunk)
            text = b"".join(parts).decode("gb18030")
            matched = re.fullmatch(r'\s*var\s+hq_str_' + re.escape(security) + r'\s*=\s*"([^"\r\n]*)";?\s*', text)
            if not matched:
                raise ValueError("备用行情响应中的证券身份或文本格式不匹配")
            fields = matched[1].split(",")
            if len(fields) < 33 or not fields[0].strip():
                raise ValueError("备用行情响应缺少证券名称、价格或源时间字段")
            price, previous = _decimal(fields[3], 0), _decimal(fields[2], 0)
            if price is None or Decimal(price) <= 0:
                raise ValueError("备用数据源未返回有效价格")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[30]) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", fields[31]):
                raise ValueError("备用行情源时间缺失或格式异常")
            observed = datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
            change = format(((Decimal(price) / Decimal(previous) - 1) * 100).quantize(Decimal("0.01")), "f") if previous and Decimal(previous) > 0 else None
            diagnostic.update(status="received", elapsed_seconds=round(time.monotonic() - started, 3))
            result = {"symbol": symbol, "name": fields[0].strip(), "price": price, "change_pct": change,
                      "change_pct_basis": "calculated_from_source_last_and_previous_close",
                      "as_of": observed.isoformat(), "as_of_precision": "timestamp", "source_time_precision": "second",
                      "fetched_at": now_iso(), "source": "sina:public-website", "source_url": source_url,
                      "security_status": "unknown", "provider_status_raw": fields[32], "cache_hit": False,
                      "fallback_diagnostic": diagnostic,
                      "source_note": "新浪公开网页展示行情，来自其合作方；非签约数据API，不保证无延迟或持续可用。",
                      **quote_time_state(observed.isoformat(), self.config)}
            self._close_snapshots[symbol] = result  # Also supplies observed identity without another network call.
            return result
        except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
            diagnostic.update(status="failed", failure_kind=_quote_failure_kind(exc), exception=type(exc).__name__,
                              elapsed_seconds=round(time.monotonic() - started, 3))
            raise _QuoteFailure(exc, [diagnostic]) from exc

    async def _quote(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        result = {"symbol": symbol, "name": None, "price": None, "change_pct": None,
                  "as_of": None, "fetched_at": now_iso(), "source": self.provider,
                  "status": "unavailable", "delay_seconds": None, "security_status": "unknown",
                  "fresh_for_triggers": False, "quote_kind": "unknown"}
        try:
            data = await self._data(symbol)
            result.update(data.get("_quote_meta", {}))
            precision = data.get("f59")
            if type(precision) is not int or not 0 <= precision <= 6:
                raise ValueError("行情价格精度字段缺失或异常，不能推测 ETF/股票小数位")
            result.update(name=data["f58"], price=_decimal(data.get("f43"), precision),
                          change_pct=_decimal(data.get("f170"), 2), source="eastmoney:public-website",
                          source_url=f"https://quote.eastmoney.com/{symbol.replace(':', '').lower()}.html",
                          precision=precision)
            stamp = data.get("f86")
            if isinstance(stamp, (int, float)) and stamp > 0:
                try:
                    observed = datetime.fromtimestamp(stamp, SHANGHAI)
                    result.update(as_of=observed.isoformat())
                    result.update(quote_time_state(result["as_of"], self.config))
                except (OverflowError, OSError, ValueError):
                    result["status"] = "unknown_time"
            else:
                result["status"] = "unknown_time"
            if result["price"] is None or Decimal(result["price"]) <= 0:
                result.update(price=None, status="unavailable", fresh_for_triggers=False,
                              error="数据源未返回有效价格；不能据此推断停牌状态")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            result["error"] = _error(exc)
            if isinstance(exc, _QuoteFailure):
                result["request_diagnostics"] = exc.diagnostics
            result["fetched_at"] = now_iso()
            cause = exc.__cause__ if isinstance(exc, _QuoteFailure) else exc
            transient = isinstance(cause, httpx.RequestError) or (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code >= 500)
            if transient:
                fallback = None
                try:
                    fallback = await self._sina_quote(symbol)
                except (httpx.HTTPError, ValueError, OSError, TimeoutError) as fallback_exc:
                    result["fallback_error"] = _error(fallback_exc)
                    if isinstance(fallback_exc, _QuoteFailure):
                        result["fallback_diagnostics"] = fallback_exc.diagnostics
                if not fallback:
                    try:
                        fallback = await self._closed_quote(symbol)
                    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as fallback_exc:
                        result["close_fallback_error"] = _error(fallback_exc)
                if fallback:
                    result.update(fallback, live_quote_error=result.pop("error"))
        return result

    async def security(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        exchange, code = symbol.split(":")
        result = {"symbol": symbol, "code": code, "exchange": exchange, "name": None,
                  "kind": _kind(symbol), "sector": None, "source": self.provider,
                  "fetched_at": now_iso(), "status": "unavailable"}
        fallback = self._close_snapshots.get(symbol)
        if fallback:
            return {**result, "name": fallback["name"], "source": fallback["source"],
                    "fetched_at": fallback["fetched_at"], "cache_hit": True, "status": "ok"}
        try:
            data = await self._data(symbol)
            result.update(data.get("_quote_meta", {}))
            result.update(name=data["f58"], kind=_kind(symbol, data["f58"]), sector=data.get("f127") or None,
                          source="eastmoney:public-website", status="ok")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            result["error"] = _error(exc)
            if isinstance(exc, _QuoteFailure):
                result["request_diagnostics"] = exc.diagnostics
        return result


    @property
    def research_provider(self) -> str:
        return self.config.get("research_provider", self.provider)

    def _research_result(self, **identity) -> dict:
        return {**identity, "source": self.research_provider, "fetched_at": now_iso(),
                "status": "unavailable", "rows": [], "as_of": None, "error": None}

    async def _tushare(self, api: str, params: dict, fields: str) -> tuple[list, dict]:
        env_name = self.config.get("tushare_token_env", "TUSHARE_TOKEN")
        token = os.environ.get(env_name)
        if not token:
            raise ValueError(f"缺少 Tushare 环境变量 {env_name}；接口另需对应积分或单独授权")
        payload, meta = await _market_json("https://api.tushare.pro", self.config,
            body={"api_name": api, "token": token, "params": params, "fields": fields})
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            code = code if isinstance(code, (int, float)) else "unknown"
            raise ValueError(f"Tushare 请求未成功（代码 {code}）；请检查凭据、接口权限与频率限制")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise ValueError("Tushare 响应结构异常")
        names = data["fields"]
        if not names or not all(isinstance(n, str) for n in names):
            raise ValueError("Tushare 缺少数据字段")
        if any(not isinstance(row, list) or len(row) != len(names) for row in data["items"]):
            raise ValueError("Tushare 字段与数据长度不一致")
        return [dict(zip(names, row)) for row in data["items"]], {**meta, "source": f"tushare:{api}", "source_url": "https://tushare.pro/document/2"}

    async def _eastmoney_history(self, secid: str, start: str, end: str, interval: str, adjust: str) -> tuple[list, dict]:
        periods = {"daily": 101, "weekly": 102, "monthly": 103, "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
        if interval not in periods or adjust not in ("none", "qfq", "hfq"):
            raise ValueError("不支持的 K 线周期或复权方式")
        if interval == "1m" and adjust != "none":
            raise ValueError("1 分钟行情不支持本适配器复权")
        payload, meta = await _market_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", self.config,
            params={"secid": secid, "klt": periods[interval], "fqt": {"none": 0, "qfq": 1, "hfq": 2}[adjust],
                    "beg": start, "end": end, "lmt": 1000, "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("rc") != 0 or str(data.get("code")) != secid.split(".")[1]:
            raise ValueError("历史行情未返回匹配的证券或板块")
        raw = data.get("klines")
        if not isinstance(raw, list):
            raise ValueError("历史行情列表缺失")
        rows = []
        for raw_row in raw:
            values = raw_row.split(",") if isinstance(raw_row, str) else []
            if len(values) < 7:
                raise ValueError("历史行情行格式异常")
            stamp = datetime.fromisoformat(values[0])
            if not start <= stamp.strftime("%Y%m%d") <= end:
                continue
            row = dict(zip(("time", "open", "close", "high", "low", "volume", "amount", "amplitude_pct", "change_pct", "change", "turnover_pct"), values))
            row = {k: v if k == "time" else _decimal(v, 0) for k, v in row.items()}
            if any(row.get(k) is None for k in ("open", "high", "low", "close")):
                raise ValueError("历史行情缺少有效 OHLC 数值")
            rows.append(row)
        return sorted(rows, key=lambda r: r["time"]), {**meta, "name": data.get("name"),
            "source": "eastmoney:public-website", "source_url": "https://quote.eastmoney.com/",
            "volume_unit": "手（供应商原始口径）", "amount_unit": "人民币元", "provider_cap_reached": len(raw) >= 1000}

    async def _tencent_daily(self, symbol: str, start: str, end: str) -> tuple[list, dict]:
        """One distinct, unadjusted daily snapshot; never splice adjusted feeds."""
        identity = symbol.replace(':', '').lower()
        if not re.fullmatch(r'(sh|sz)\d{6}', identity):
            raise ValueError('备用日线仅支持沪深证券或明确的沪深指数身份')
        first = datetime.strptime(start, '%Y%m%d').date().isoformat()
        last = datetime.strptime(end, '%Y%m%d').date().isoformat()
        payload, meta = await _market_json('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get', self.config,
            params={'param': f'{identity},day,{first},{last},1000,'})
        data = payload.get('data', {}).get(identity) if isinstance(payload, dict) else None
        if payload.get('code') != 0 or not isinstance(data, dict) or not isinstance(data.get('day'), list):
            raise ValueError('备用日线未返回匹配证券的未复权 day 数据；不以 qfqday/hfqday 替代')
        rows = []
        for raw in data['day']:
            if not isinstance(raw, list) or len(raw) < 6:
                raise ValueError('备用日线格式异常')
            row = {'time': raw[0], **{k: _decimal(v, 0) for k, v in zip(('open','close','high','low','volume'), raw[1:6])}, 'amount': None}
            if any(row[k] is None or Decimal(row[k]) <= 0 for k in ('open','close','high','low')):
                raise ValueError('备用日线缺有效 OHLC')
            rows.append(row)
        return _validated_bars(rows, start, end), {**meta, 'source': 'tencent:unadjusted-daily',
            'source_url': 'https://gu.qq.com/' + identity, 'volume_unit': '供应商原始值（未核实单位）',
            'amount_unit': None, 'provider_cap_reached': len(data['day']) >= 1000,
            'coverage_note': '腾讯公开未复权日线；成交额未提供，成交量单位未独立确认，不作金额或跨源成交量比较。'}

    async def _public_daily_with_fallback(self, symbol, secid, first, last, interval, adjust):
        try:
            return await self._eastmoney_history(secid, first, last, interval, adjust)
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            if interval != 'daily' or adjust != 'none' or not symbol.startswith(('SH:', 'SZ:')):
                raise
            rows, meta = await self._tencent_daily(symbol, first, last)
            return rows, {**meta, 'fallback_from': 'eastmoney:public-website',
                'primary_failure': {'failure_kind': _quote_failure_kind(exc), 'error': _error(exc)},
                'fallback_note': '整段改用独立公开来源的未复权日线；未拼接或改写原来源缓存。'}

    async def history(self, symbol: str, start: str | None = None, end: str | None = None,
                      interval: str = "daily", adjust: str = "none", limit: int = 250) -> dict:
        symbol = normalize_symbol(symbol)
        result = self._research_result(symbol=symbol, interval=interval, adjustment=adjust, timezone="Asia/Shanghai")
        try:
            first, last = _date_range(start, end)
            count = _bounded(limit, 250, 1, 1000)
            if _kind(symbol) is None:
                raise ValueError("证券代码不在本适配器支持范围")
            exchange, code = symbol.split(":")
            if self.research_provider == "eastmoney":
                rows, meta = await self._public_daily_with_fallback(symbol, f"{'1' if exchange == 'SH' else '0'}.{code}", first, last, interval, adjust)
            elif self.research_provider == "tushare":
                if interval != "daily" or adjust != "none" or _kind(symbol) != "stock":
                    raise ValueError("当前 Tushare 适配器支持 A 股未复权日线；其他口径请切换供应商或扩展适配器")
                rows, meta = await self._tushare("daily", {"ts_code": f"{code}.{exchange}", "start_date": first, "end_date": last}, "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg")
                if any(row.get("ts_code") != f"{code}.{exchange}" for row in rows):
                    raise ValueError("Tushare 行情证券身份不匹配")
                rows = [{"time": _date_iso(row.get("trade_date")), **{key: _decimal(row.get(field), 0) for key, field in
                         {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "vol", "amount": "amount", "change_pct": "pct_chg"}.items()}} for row in rows]
                rows.sort(key=lambda r: r["time"])
                meta.update(volume_unit="手", amount_unit="人民币千元", provider_cap_reached=len(rows) >= 6000)
            else:
                raise ValueError("历史行情数据源未启用")
            rows = _validated_bars(rows, first, last)
            result.update(meta, rows=rows[-count:], requested_range={"start": first, "end": last},
                          returned_range={"start": rows[-count]["time"] if len(rows) >= count else rows[0]["time"], "end": rows[-1]["time"]} if rows else None,
                          as_of=rows[-1]["time"] if rows else None, truncated=len(rows) > count or meta.get("provider_cap_reached", False),
                          status="ok" if rows else "empty", coverage_note="仅返回供应商提供的记录；不填补停牌、缺失或未开放历史。复权口径由供应商计算；成交量与金额保留明确单位。")
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
            result.update(error_kind=_quote_failure_kind(exc), failure_stage='market_data',
                next_action='可读取已保存的同证券、区间与复权口径原始行情；未取得数据保持缺口。显式重试新任务可重新验证数据源。')
        return result

    async def _eastmoney_list(self, scope: str, limit: int, offset: int = 0) -> tuple[list, dict]:
        # Exact pages are determined locally; no unbounded full-market download.
        page_size = min(100, limit)
        payload, meta = await _market_json("https://push2.eastmoney.com/api/qt/clist/get", self.config,
            params={"pn": offset // page_size + 1, "pz": page_size, "po": 0, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f12", "fs": scope, "fields": "f2,f3,f12,f13,f14,f20,f104,f105,f124"})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("rc") != 0:
            raise ValueError("板块数据不可用")
        rows = data.get("diff")
        rows = list(rows.values()) if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("板块数据列表格式异常")
        total = data.get("total")
        return rows[:page_size], {**meta, "source": "eastmoney:public-website", "source_url": "https://quote.eastmoney.com/center/boardlist.html",
                      "response_truncated": len(rows) > page_size,
                      "total": total if isinstance(total, int) else None, "page_size": page_size, "offset": offset}

    async def sectors(self, kind: str = "industry", limit: int = 100, offset: int = 0) -> dict:
        result = self._research_result(kind=kind)
        try:
            count = _bounded(limit, 100, 1, 100)
            if int(offset) < 0 or int(offset) % count:
                raise ValueError("板块 offset 须为非负且为 limit 的整数倍")
            if kind not in ("industry", "concept"):
                raise ValueError("板块种类须为 industry 或 concept")
            if self.research_provider == "eastmoney":
                raw, meta = await self._eastmoney_list(f"m:90+t:{2 if kind == 'industry' else 3}", count, int(offset))
                rows = [{"sector_id": row.get("f12"), "name": row.get("f14"), "change_pct": _decimal(row.get("f3"), 0),
                         "advancing": row.get("f104"), "declining": row.get("f105"), "as_of": _epoch_iso(row.get("f124"))} for row in raw]
            elif self.research_provider == "tushare":
                if kind != "industry":
                    raise ValueError("当前 Tushare 适配器仅支持申万 2021 一级行业；概念分类请切换数据源")
                raw, meta = await self._tushare("index_classify", {"level": "L1", "src": "SW2021"}, "index_code,industry_name,level,industry_code,is_pub,parent_code")
                meta.update(total=len(raw))
                rows = [{"sector_id": row.get("index_code"), "name": row.get("industry_name"), "classification": "SW2021", **row} for row in raw[int(offset):int(offset) + count]]
            else:
                raise ValueError("板块数据源未启用")
            more = meta.get("response_truncated", False) or meta.get("total") is None or int(offset) + len(rows) < meta["total"]
            result.update(meta, rows=rows, status="ok" if rows else "empty", truncated=more,
                          next_offset=int(offset) + count if more and rows else None, coverage_note="供应商板块分类与可返回范围，不代表全市场或交易所官方行业标准")
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
        return result

    async def sector_members(self, sector_id: str, limit: int = 100, offset: int = 0) -> dict:
        result = self._research_result(sector_id=sector_id)
        try:
            count = _bounded(limit, 100, 1, 100)
            if int(offset) < 0 or int(offset) % count:
                raise ValueError("成分 offset 须为非负且为 limit 的整数倍")
            if self.research_provider == "eastmoney":
                if not re.fullmatch(r"BK\d{4,6}", sector_id):
                    raise ValueError("东方财富板块标识须为 BK 加数字")
                raw, meta = await self._eastmoney_list(f"b:{sector_id}", count, int(offset))
                rows = []
                for row in raw:
                    code = str(row.get("f12", ""))
                    market = row.get("f13")
                    exchange = "SH" if market == 1 else "BJ" if market == 0 and code.startswith(("4", "8", "92")) else "SZ" if market == 0 else None
                    symbol = f"{exchange}:{code}" if exchange and re.fullmatch(r"\d{6}", code) else None
                    rows.append({"symbol": symbol, "provider_code": code, "name": row.get("f14"), "price": _decimal(row.get("f2"), 0),
                                 "change_pct": _decimal(row.get("f3"), 0), "as_of": _epoch_iso(row.get("f124"))})
            elif self.research_provider == "tushare":
                if not re.fullmatch(r"\d{6}\.SI", sector_id):
                    raise ValueError("申万一级行业标识须来自 sectors，例如 801010.SI")
                raw, meta = await self._tushare("index_member_all", {"l1_code": sector_id, "is_new": "Y"}, "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new")
                meta.update(total=len(raw), provider_cap_reached=len(raw) >= 2000)
                rows = [{**row, "symbol": _from_ts_code(row.get("ts_code"))} for row in raw[int(offset):int(offset) + count]]
            else:
                raise ValueError("板块成分数据源未启用")
            more = meta.get("response_truncated", False) or meta.get("provider_cap_reached", False) or meta.get("total") is None or int(offset) + len(rows) < meta["total"]
            changes = [Decimal(row["change_pct"]) for row in rows if row.get("change_pct") is not None]
            result.update(meta, rows=rows, status="ok" if rows else "empty", truncated=more,
                          next_offset=int(offset) + count if more and rows else None,
                          breadth={"advancing": sum(v > 0 for v in changes), "declining": sum(v < 0 for v in changes),
                                   "unchanged": sum(v == 0 for v in changes), "priced_members": len(changes), "scope": "returned_members_only"},
                          coverage_note="成分为当前供应商分类；广度仅统计本页有涨跌幅的成分，不能冒充完整板块历史广度")
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
        return result

    async def sector_history(self, sector_id: str, start: str | None = None, end: str | None = None, limit: int = 250) -> dict:
        result = self._research_result(sector_id=sector_id, interval="daily", adjustment="index", timezone="Asia/Shanghai")
        try:
            first, last = _date_range(start, end)
            count = _bounded(limit, 250, 1, 1000)
            if self.research_provider == "eastmoney":
                if not re.fullmatch(r"BK\d{4,6}", sector_id):
                    raise ValueError("东方财富板块标识须为 BK 加数字")
                rows, meta = await self._eastmoney_history(f"90.{sector_id}", first, last, "daily", "none")
            elif self.research_provider == "tushare":
                if not re.fullmatch(r"\d{6}\.SI", sector_id):
                    raise ValueError("申万行业标识须来自 sectors")
                raw, meta = await self._tushare("sw_daily", {"ts_code": sector_id, "start_date": first, "end_date": last}, "ts_code,trade_date,name,open,high,low,close,vol,amount,pct_change")
                if any(row.get("ts_code") != sector_id for row in raw):
                    raise ValueError("行业历史数据身份不匹配")
                rows = sorted([{"time": _date_iso(row.get("trade_date")), **{key: _decimal(row.get(field), 0) for key, field in
                               {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "vol", "amount": "amount", "change_pct": "pct_change"}.items()}} for row in raw], key=lambda r: r["time"])
                meta.update(volume_unit="万股", amount_unit="人民币万元", provider_cap_reached=len(raw) >= 4000)
            else:
                raise ValueError("板块历史数据源未启用")
            rows = _validated_bars(rows, first, last)
            result.update(meta, rows=rows[-count:], as_of=rows[-1]["time"] if rows else None,
                          requested_range={"start": first, "end": last}, status="ok" if rows else "empty",
                          truncated=len(rows) > count or meta.get("provider_cap_reached", False))
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
        return result

    async def fundamentals(self, symbol: str, periods: int = 8) -> dict:
        symbol = normalize_symbol(symbol)
        result = self._research_result(symbol=symbol, amount_unit="人民币元")
        try:
            if _kind(symbol) != "stock":
                raise ValueError("公司利润表适用于股票，ETF 请读取基金报告和持仓披露")
            count = _bounded(periods, 8, 1, 20)
            exchange, code = symbol.split(":")
            if self.research_provider == "eastmoney":
                payload, meta = await _market_json("https://datacenter-web.eastmoney.com/api/data/v1/get", self.config,
                    params={"reportName": "RPT_DMSK_FN_INCOME", "columns": "ALL", "filter": f'(SECURITY_CODE="{code}")',
                            "sortColumns": "REPORT_DATE,NOTICE_DATE", "sortTypes": "-1,-1", "pageSize": count, "pageNumber": 1})
                data = payload.get("result") if isinstance(payload, dict) else None
                raw = data.get("data") if isinstance(data, dict) else None
                if not isinstance(raw, list):
                    raise ValueError("财务数据未返回有效利润表")
                if any(not isinstance(row, dict) or row.get("SECURITY_CODE") != code or row.get("SECUCODE") not in (None, f"{code}.{exchange}") for row in raw):
                    raise ValueError("财务数据证券身份不匹配")
                rows = [{"report_period": row.get("REPORT_DATE"), "published_at": row.get("NOTICE_DATE"),
                         "report_type": row.get("REPORT_TYPE_CODE"), "revenue": _decimal(row.get("TOTAL_OPERATE_INCOME"), 0),
                         "parent_net_profit": _decimal(row.get("PARENT_NETPROFIT"), 0), "operating_profit": _decimal(row.get("OPERATE_PROFIT"), 0),
                         "total_profit": _decimal(row.get("TOTAL_PROFIT"), 0), "raw": row} for row in raw[:count]]
                meta.update(source="eastmoney:datacenter-public-website", source_url=f"https://data.eastmoney.com/bbsj/{code}.html",
                            truncated=bool(data.get("pages", 1) > 1 or len(raw) > count))
            elif self.research_provider == "tushare":
                raw, meta = await self._tushare("income", {"ts_code": f"{code}.{exchange}", "report_type": "1"},
                    "ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,n_income_attr_p,operate_profit,total_profit,basic_eps,update_flag")
                if any(row.get("ts_code") != f"{code}.{exchange}" for row in raw):
                    raise ValueError("Tushare 财务数据证券身份不匹配")
                raw.sort(key=lambda r: (str(r.get("end_date", "")), str(r.get("f_ann_date", ""))), reverse=True)
                rows = [{"report_period": _date_iso(row.get("end_date")), "published_at": _date_iso(row.get("f_ann_date") or row.get("ann_date")),
                         "report_type": row.get("report_type"), "revenue": _decimal(row.get("total_revenue"), 0),
                         "parent_net_profit": _decimal(row.get("n_income_attr_p"), 0), "operating_profit": _decimal(row.get("operate_profit"), 0),
                         "total_profit": _decimal(row.get("total_profit"), 0), "raw": row} for row in raw[:count]]
                meta.update(truncated=len(raw) > count)
            else:
                raise ValueError("财务数据源未启用")
            result.update(meta, rows=rows, status="ok" if rows else "empty", as_of=rows[0]["published_at"] if rows else None,
                          coverage_note="结构化供应商利润表，非原始财报全文；报告期与披露时间分开保存，数值一般为年初至报告期累计值。不同报告类型或更正版本不可直接相减当作环比。")
            result["comparisons"] = _financial_comparisons(rows)
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
        return result

    async def benchmark_history(self, benchmark: str = "CSI300", start: str | None = None, end: str | None = None, limit: int = 250) -> dict:
        codes = {"CSI300": ("1.000300", "000300.SH"), "SSE50": ("1.000016", "000016.SH"), "CSI500": ("1.000905", "000905.SH")}
        result = self._research_result(benchmark=benchmark, interval="daily", adjustment="index")
        try:
            if benchmark not in codes:
                raise ValueError("可用基准为 CSI300、SSE50、CSI500")
            first, last = _date_range(start, end)
            count = _bounded(limit, 250, 1, 1000)
            if self.research_provider == "eastmoney":
                rows, meta = await self._public_daily_with_fallback('SH:' + codes[benchmark][0].split('.')[1], codes[benchmark][0], first, last, "daily", "none")
            elif self.research_provider == "tushare":
                raw, meta = await self._tushare("index_daily", {"ts_code": codes[benchmark][1], "start_date": first, "end_date": last}, "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg")
                if any(row.get("ts_code") != codes[benchmark][1] for row in raw):
                    raise ValueError("基准历史数据身份不匹配")
                rows = sorted([{"time": _date_iso(row.get("trade_date")), **{key: _decimal(row.get(field), 0) for key, field in
                              {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "vol", "amount": "amount", "change_pct": "pct_chg"}.items()}} for row in raw], key=lambda r: r["time"])
                meta.update(volume_unit="手", amount_unit="人民币千元", provider_cap_reached=len(raw) >= 8000)
            else:
                raise ValueError("基准数据源未启用")
            rows = _validated_bars(rows, first, last)
            result.update(meta, rows=rows[-count:], as_of=rows[-1]["time"] if rows else None,
                          requested_range={"start": first, "end": last}, status="ok" if rows else "empty",
                          truncated=len(rows) > count or meta.get("provider_cap_reached", False))
        except (httpx.HTTPError, ValueError, OSError, TypeError, TimeoutError) as exc:
            result["error"] = _error(exc)
            result.update(error_kind=_quote_failure_kind(exc), failure_stage='benchmark_data',
                next_action='同基准、日期与指数口径缓存仍可复核；不要用其他指数或搜索摘要价格替代。')
        return result


_MARKET_CACHE: OrderedDict = OrderedDict()
_MARKET_INFLIGHT: dict = {}


async def _market_json(url: str, config: dict, *, params: dict | None = None, body: dict | None = None) -> tuple[dict, dict]:
    # Only fixed adapter endpoints call this helper. Raw JSON is bounded and
    # cache keys are hashes, so credentials never appear in diagnostics.
    key = hashlib.sha256(json.dumps([url, params, body], sort_keys=True).encode()).hexdigest()
    ttl = _bounded(config.get("research_cache_seconds"), 300, 0, 3600)
    cached = _MARKET_CACHE.get(key)
    if ttl and cached and time.monotonic() - cached[0] < ttl:
        _MARKET_CACHE.move_to_end(key)
        return json.loads(cached[1]), {"fetched_at": cached[2], "cache_hit": True, "cache_age_seconds": int(time.monotonic() - cached[0]), "cache_ttl_seconds": ttl, "cache_stale": False}
    failure_key = ('market', url)
    deferred = _deferred_failure(config, failure_key)
    if deferred:
        raise DeferredRequestError(deferred['kind'])
    task_key = (asyncio.get_running_loop(), key)
    task = _MARKET_INFLIGHT.get(task_key)
    if task is None:
        async def fetch():
            headers = {"User-Agent": "Mozilla/5.0 LocalStockResearch/2.0", "Referer": "https://quote.eastmoney.com/"}
            async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
                async with httpx.AsyncClient(timeout=float(config.get("network_timeout_seconds", 12)), follow_redirects=False) as client:
                    async with client.stream("POST" if body is not None else "GET", url, params=params, json=body, headers=headers) as response:
                        response.raise_for_status()
                        parts, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 5_000_000:
                                raise ValueError("市场数据响应超过 5 MB 上限，请缩小查询")
                            parts.append(chunk)
            encoded = b"".join(parts)
            payload = json.loads(encoded)
            if not isinstance(payload, dict):
                raise ValueError("市场数据响应不是对象")
            fetched_at = now_iso()
            success = payload.get("code", 0) == 0 and payload.get("rc", 0) == 0 and payload.get("success", True) is not False and isinstance(payload.get("data", payload.get("result")), dict)
            if ttl and success:
                _MARKET_CACHE[key] = (time.monotonic(), encoded, fetched_at)
                _MARKET_CACHE.move_to_end(key)
                while _MARKET_CACHE and (len(_MARKET_CACHE) > 128 or sum(len(row[1]) for row in _MARKET_CACHE.values()) > 16_000_000):
                    _MARKET_CACHE.popitem(last=False)
            return encoded, fetched_at
        task = asyncio.create_task(fetch())
        _MARKET_INFLIGHT[task_key] = task
        def finished(done):
            _MARKET_INFLIGHT.pop(task_key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    try:
        encoded, fetched_at = await asyncio.shield(task)
    except (httpx.RequestError, OSError, TimeoutError) as exc:
        # Several agents can await the same in-flight HTTP attempt. Count that
        # failure once in each task's memory, not once per waiting consumer.
        memory = config.get('_research_failure_memory')
        owners = getattr(exc, '_research_failure_owners', set())
        if memory is not None and id(memory) not in owners:
            _remember_failure(config, failure_key, _quote_failure_kind(exc), 'market_data')
            exc._research_failure_owners = owners | {id(memory)}
        raise
    if config.get('_research_failure_memory') is not None:
        config['_research_failure_memory'].pop(failure_key, None)
    return json.loads(encoded), {"fetched_at": fetched_at, "cache_hit": False, "cache_age_seconds": 0, "cache_ttl_seconds": ttl, "cache_stale": False}



def _validated_bars(rows: list[dict], first: str, last: str) -> list[dict]:
    valid = []
    for row in rows:
        if not row.get("time") or any(row.get(key) is None for key in ("open", "high", "low", "close")):
            raise ValueError("历史行情缺少有效日期或 OHLC 数值")
        date = datetime.fromisoformat(row["time"]).strftime("%Y%m%d")
        if first <= date <= last:
            valid.append(row)
    return sorted(valid, key=lambda row: row["time"])

def _date_range(start: str | None, end: str | None) -> tuple[str, str]:
    today = datetime.now(SHANGHAI).date()
    def parse(value, default):
        if value is None:
            return default
        try:
            return datetime.strptime(str(value).replace("-", ""), "%Y%m%d").date()
        except ValueError:
            raise ValueError("日期须为 YYYY-MM-DD 或 YYYYMMDD") from None
    first, last = parse(start, today - timedelta(days=365)), parse(end, today)
    if first > last or (last - first).days > 3660:
        raise ValueError("日期范围须顺序正确且不超过十年；长历史请分段读取")
    return first.strftime("%Y%m%d"), last.strftime("%Y%m%d")


def _date_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y%m%d").date().isoformat()
    except ValueError:
        raise ValueError("数据源日期格式异常") from None


def _epoch_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), SHANGHAI).isoformat() if value and float(value) > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _from_ts_code(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        code, exchange = value.split(".")
        return f"{exchange}:{code}"
    return None


def _financial_comparisons(rows: list[dict]) -> list[dict]:
    """Only compare matching report type and same calendar period one year apart."""
    latest = {}
    for row in rows:
        period = str(row.get("report_period") or "")[:10]
        key = (period, str(row.get("report_type")))
        if period and key not in latest:
            latest[key] = row
    comparisons = []
    for (period, report_type), row in latest.items():
        try:
            previous = f"{int(period[:4]) - 1}{period[4:]}"
        except ValueError:
            continue
        old = latest.get((previous, report_type))
        if not old:
            continue
        for metric in ("revenue", "parent_net_profit"):
            if row.get(metric) is None or old.get(metric) is None:
                continue
            current, baseline = Decimal(row[metric]), Decimal(old[metric])
            comparisons.append({"metric": metric, "period": period, "comparison_period": previous,
                                "difference": format(current - baseline, "f"),
                                "yoy_pct": format((current / baseline - 1) * 100, "f") if baseline > 0 else None,
                                "note": "基期大于零才计算同比百分比；同报告类型累计值比较，不计算跨累计期环比"})
    return comparisons


async def search(query: str, config: dict) -> dict:
    """Deprecated compatibility adapter; research runtime uses model-native search.

    Retained only for old direct integrations. No model/service fallback may call
    this function when native search is unavailable.
    """
    """Return raw search excerpts; they are not claims of full source access."""
    provider = config.get("search_provider", "disabled")
    result = {"query": query, "provider": provider, "results": [], "fetched_at": now_iso(), "status": "unavailable"}
    if provider not in ("tavily", "brave"):
        result["error"] = "搜索服务尚未配置（可选 Tavily 或 Brave）"
        return result
    env_name = config.get("search_key_env") or ("TAVILY_API_KEY" if provider == "tavily" else "BRAVE_API_KEY")
    key = os.environ.get(env_name)
    if not key:
        result["error"] = f"缺少搜索服务环境变量 {env_name}"
        return result
    if not query.strip():
        result["error"] = "搜索词不能为空"
        return result
    count = min(20, max(1, int(config.get("search_max_results", 6))))
    try:
        async with httpx.AsyncClient(timeout=float(config.get("network_timeout_seconds", 12))) as client:
            if provider == "tavily":
                response = await client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                                             json={"query": query[:2000], "max_results": count, "search_depth": "basic", "include_answer": False, "include_raw_content": False})
            else:
                response = await client.get("https://api.search.brave.com/res/v1/web/search", headers={"X-Subscription-Token": key},
                                            params={"q": query[:2000], "count": count, "extra_snippets": "true"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("搜索响应格式异常")
        web = payload.get("web") or {}
        if not isinstance(web, dict):
            raise ValueError("搜索响应中的 web 字段格式异常")
        rows = payload.get("results", []) if provider == "tavily" else web.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("搜索响应中的结果列表格式异常")
        result["results"] = [{"title": row.get("title"), "url": row.get("url"),
                              "snippet": row.get("content") if provider == "tavily" else row.get("description"),
                              "published_at": row.get("published_date") or row.get("page_age"),
                              "source": provider, "fetched_at": result["fetched_at"], "access": "excerpt"}
                             for row in rows[:count] if isinstance(row, dict)]
        result["status"] = "ok"
    except (httpx.HTTPError, ValueError, OSError) as exc:
        result["error"] = _error(exc)
    return result


class DocumentAccessError(ValueError):
    def __init__(self, kind, stage, message, action):
        super().__init__(message)
        self.kind, self.stage, self.action = kind, stage, action


async def _public_dns_fallback(hostname: str, config: dict) -> str:
    """Resolve a synthetic DNS answer through HTTPS; never connect to that answer.

    This resolver endpoint is infrastructure, not a content-source allowlist.
    Every result still requires a public address, pinned TCP and hostname TLS.
    """
    async with httpx.AsyncClient(timeout=min(5.0, float(config.get('network_timeout_seconds', 12))),
                                 follow_redirects=False, trust_env=False) as client:
        async with client.stream('GET', 'https://1.1.1.1/dns-query',
                                 params={'name': hostname, 'type': 'A'}, headers={'Accept': 'application/dns-json'}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > 64000:
                    raise ValueError('公开DNS响应超出上限')
                chunks.append(part)
    payload = json.loads(b''.join(chunks))
    questions = payload.get('Question') or []
    if payload.get('Status') != 0 or payload.get('TC') or len(questions) != 1 or questions[0].get('type') != 1 or str(questions[0].get('name', '')).rstrip('.').lower() != hostname.lower():
        raise ValueError('公开DNS未返回匹配的完整解析结果')
    addresses = [str(row.get('data', '')) for row in payload.get('Answer', []) if row.get('type') == 1]
    if not addresses or len(addresses) > 32 or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError('公开DNS未返回全部为公网的地址')
    return addresses[0]


async def _resolve_public_url(url: str) -> tuple[str, str]:
    """Resolve once and pin a public IP, preventing DNS rebinding/redirect SSRF."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅能读取无登录凭据的公开 HTTP/HTTPS 原文链接")
    if parts.port not in (None, 80, 443):
        raise ValueError("研究读取仅允许公开 HTTP/HTTPS 服务端口")
    hostname = parts.hostname.encode("idna").decode("ascii")
    if hostname.lower() in ("localhost", "localhost.localdomain") or hostname.lower().endswith((".localhost", ".local", ".internal")):
        raise ValueError("研究工具不能访问本机或内网地址")
    try:
        entries = await asyncio.get_running_loop().getaddrinfo(hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DocumentAccessError('dns_failure', 'dns', '公开原文域名解析失败', '可稍后重试，或用原生搜索找同份文档的其他公开发布地址') from exc
    addresses = list(dict.fromkeys(item[4][0] for item in entries))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        error = DocumentAccessError('non_public_dns', 'address_validation', '研究工具不能访问本机、内网或保留地址',
                                  '域名解析含非公网地址，可能受本地DNS/代理影响；未发出正文请求。可核对本机网络配置或寻找其他公开来源，不绕过地址校验')
        error.addresses, error.hostname = addresses, hostname
        raise error
    return hostname, addresses[0]


async def _fetch_public(url: str, config: dict) -> tuple[bytes, str, str]:
    max_bytes = min(20_000_000, max(10_000, int(config.get("read_max_bytes", 5_000_000))))
    async with httpx.AsyncClient(timeout=float(config.get("network_timeout_seconds", 12)), follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            try:
                hostname, address = await _resolve_public_url(url)
            except DocumentAccessError as exc:
                addresses = getattr(exc, 'addresses', [])
                hostname = getattr(exc, 'hostname', '')
                # Only the known synthetic benchmarking range is eligible. A
                # private/loopback/link-local answer never gains another route.
                synthetic = addresses and all(ipaddress.ip_address(x) in ipaddress.ip_network('198.18.0.0/15') for x in addresses)
                literal = False
                try:
                    ipaddress.ip_address(hostname)
                    literal = True
                except ValueError:
                    pass
                if not synthetic or literal or not config.get('document_public_dns_fallback', True):
                    raise
                diagnostic = {'host': hostname, 'stage': 'dns', 'system_dns': 'synthetic_198.18/15',
                              'resolver': 'cloudflare_https_public_dns', 'private_address_used': False}
                config.setdefault('_network_diagnostics', []).append(diagnostic)
                try:
                    address = await _public_dns_fallback(hostname, config)
                except (httpx.HTTPError, OSError, ValueError, TimeoutError) as fallback:
                    diagnostic.update(status='failed', error_kind=_quote_failure_kind(fallback))
                    raise DocumentAccessError('public_dns_fallback_failed', 'dns',
                        '本机返回代理虚拟地址，公开HTTPS DNS备用解析也未成功；未连接虚拟地址',
                        '运行 scripts/diagnose_research_network.py 查看分阶段结果；可尝试同份披露的其他公开地址。需要修改系统DNS/代理时先由用户确认。') from fallback
                diagnostic.update(status='resolved_public', tls_hostname_verification=True)
            original = httpx.URL(url)
            pinned = original.copy_with(host=address)
            # httpcore honors sni_hostname for TLS verification while TCP uses pinned IP.
            headers = {"Host": original.netloc.decode("ascii"), "User-Agent": "LocalStockResearch/1.0", "Accept": "text/html,application/pdf,text/plain"}
            async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": hostname}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise DocumentAccessError('invalid_redirect', 'redirect', '原文重定向缺少目标地址', '原生搜索其他公开发布入口')
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                parts = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > max_bytes:
                        raise DocumentAccessError('document_too_large', 'download', f"原文超过读取上限 {max_bytes} 字节", '寻找较小的公开分册或网页披露；不能假定分页会减少下载大小')
                    parts.append(chunk)
                return b"".join(parts), response.headers.get("content-type", ""), url
        raise DocumentAccessError('redirect_limit', 'redirect', '原文重定向次数超过上限', '原生搜索公开直接文档地址')


# Cached responses are immutable document versions. Each new fetch receives its
# own hash and original fetch timestamp; a cache hit never pretends to be fresh.
_DOCUMENT_CACHE: OrderedDict = OrderedDict()
_DOCUMENT_INFLIGHT: dict = {}


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    return min(high, max(low, int(default if value is None else value)))


def _body_limit(config: dict) -> int:
    return _bounded(config.get("read_max_bytes"), 5_000_000, 10_000, 20_000_000)


async def _download_document(url: str, config: dict, refresh: bool = False) -> dict:
    """Bounded in-memory LRU, shared across agents; never caches failed reads."""
    url = str(url).strip()
    if len(url) > 8192:
        raise ValueError("原文 URL 超过长度上限")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅能读取无登录凭据的公开 HTTP/HTTPS 原文链接")
    url = parts._replace(fragment="").geturl()
    observer = config.get("_material_observer")
    scope = config.get("_material_observation_scope", "public")
    account_id = config.get("_material_observation_account_id")
    # Durable tokens belong to one database and access scope. A global byte cache
    # must not smuggle a different database's trusted observation into a save.
    observer_key = (os.path.realpath(observer.path), scope, account_id) if observer is not None else None
    key = (url, _body_limit(config), observer_key)
    ttl = _bounded(config.get("read_cache_seconds"), 300, 0, 3600)
    cached = _DOCUMENT_CACHE.get(key)
    if not refresh and ttl and cached and time.monotonic() - cached[0] < ttl:
        _DOCUMENT_CACHE.move_to_end(key)
        return {**cached[1], "cache_hit": True, "retrieved_at": now_iso()}
    failure_key = ('document_dns', parts.hostname)
    deferred = _deferred_failure(config, failure_key, refresh)
    if deferred:
        raise DocumentAccessError(deferred['kind'], deferred['stage'],
            '本轮已确认该域名解析路径失败，暂缓重复下载；未发出本次正文请求',
            '可使用已存正文/片段或寻找另一公开发布域名；30秒后最多自动再验证一次，显式 refresh 或新任务可重新验证。')
    loop = asyncio.get_running_loop()
    task_key = (loop, key)
    task = _DOCUMENT_INFLIGHT.get(task_key)
    if task is None:
        async def fetch() -> dict:
            observation_id = observer.begin_material_observation(url, scope=scope, account_id=account_id) if observer is not None else None
            try:
                async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
                    body, mime, final_url = await _fetch_public(url, config)
            except Exception as exc:
                if isinstance(exc, DocumentAccessError) and exc.stage in ('dns', 'address_validation'):
                    _remember_failure(config, failure_key, exc.kind, exc.stage, exc.action)
                if observation_id is not None:
                    exc._material_observation_id = observation_id
                raise
            if len(body) > _body_limit(config):
                raise ValueError("原文超过读取字节上限")
            if config.get('_research_failure_memory') is not None:
                config['_research_failure_memory'].pop(failure_key, None)
            document = {"body": body, "content_type": mime, "final_url": final_url,
                        "fetched_at": now_iso(), "document_id": hashlib.sha256(body).hexdigest(),
                        "byte_length": len(body)}
            if observation_id is not None:
                document["_observation_id"] = observation_id
            if ttl:
                _DOCUMENT_CACHE[key] = (time.monotonic(), document)
                _DOCUMENT_CACHE.move_to_end(key)
                budget = _bounded(config.get("read_cache_max_bytes"), 32_000_000, 1_000_000, 100_000_000)
                while _DOCUMENT_CACHE and (len(_DOCUMENT_CACHE) > 32 or sum(len(row[1]["body"]) for row in _DOCUMENT_CACHE.values()) > budget):
                    _DOCUMENT_CACHE.popitem(last=False)
            return document
        task = asyncio.create_task(fetch())
        _DOCUMENT_INFLIGHT[task_key] = task
        def finished(done):
            _DOCUMENT_INFLIGHT.pop(task_key, None)
            # Retrieving an exception avoids an unhandled-task warning if every
            # waiting agent was cancelled before the bounded download finished.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    result = await asyncio.shield(task)
    return {**result, "cache_hit": False, "retrieved_at": now_iso()}


def _pdf_reader(body: bytes) -> PdfReader:
    if not body.lstrip().startswith(b'%PDF-'):
        raise DocumentAccessError('non_pdf_response', 'content_type', '响应不是PDF文档', '读取发布入口的附件链接，或搜索同份文档的公开直接地址')
    reader = PdfReader(io.BytesIO(body))
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentAccessError('encrypted_pdf', 'extraction', 'PDF 已加密，无法读取正文', '使用无需登录和密码的公开版本')
    return reader


def _pdf_info(reader: PdfReader) -> dict:
    meta = reader.metadata
    metadata = {str(k).lstrip("/"): str(v)[:2000] for k, v in list((meta or {}).items())[:50]}
    return {"title": str(meta.title)[:2000] if meta and meta.title else None,
            "page_count": len(reader.pages), "metadata": metadata,
            "page_numbering": "PDF 物理页，从 1 开始，可能不同于报告印刷页码"}


def _extract_document(body: bytes, mime: str, config: dict, *, url: str = "", start_page: int | None = None,
                      end_page: int | None = None, page: int | None = None, offset: int = 0,
                      limit: int | None = None) -> dict:
    max_chars = _bounded(config.get("read_max_chars"), 40_000, 1000, 200_000)
    if limit is not None:
        max_chars = min(max_chars, _bounded(limit, max_chars, 1, 200_000))
    if int(offset) < 0:
        raise ValueError("正文偏移量不能为负数")
    offset = int(offset)
    info = {"title": None, "published_at": None, "pages": None, "links": [], "attachments": []}
    truncated = False
    if "pdf" in mime.lower() or body.startswith(b"%PDF-"):
        reader = _pdf_reader(body)
        info.update(_pdf_info(reader))
        total = info["page_count"]
        max_pages = _bounded(config.get("read_max_pdf_pages"), 20, 1, 100)
        start = int(page if page is not None else (1 if start_page is None else start_page))
        end = int(page if page is not None else (end_page if end_page is not None else min(total, start + max_pages - 1)))
        if start < 1 or end < start or start > total:
            raise DocumentAccessError('page_out_of_range', 'page_selection', 'PDF 页码范围无效或超过总页数', '先用pdf_metadata核对物理总页数，再读取有效页码')
        bounded_end = min(total, end, start + max_pages - 1)
        segments, pages, cursor = [], [], 0
        deadline = time.monotonic() + max(1, float(config.get("read_timeout_seconds", 30)))
        for number in range(start, bounded_end + 1):
            if time.monotonic() > deadline:
                raise TimeoutError("PDF 文字提取超时")
            value = reader.pages[number - 1].extract_text() or ""
            header = f"[第 {number} 页]\n"
            segment = header + value + "\n\n"
            seg_start, seg_end = cursor, cursor + len(segment)
            take_start, take_end = max(offset, seg_start), min(offset + max_chars, seg_end)
            if take_end > take_start:
                segments.append(segment[take_start - seg_start:take_end - seg_start])
                text_start = max(0, take_start - seg_start - len(header))
                text_end = min(len(value), max(0, take_end - seg_start - len(header)))
                pages.append({"page": number, "text": value[text_start:text_end],
                              "text_start": text_start, "text_end": text_end,
                              "text_length": len(value), "truncated": text_start > 0 or text_end < len(value),
                              "source_url": f"{url.split('#')[0]}#page={number}" if url else None})
            cursor = seg_end
        text = "".join(segments)
        incomplete_text = not pages or any(not p["text"].strip() for p in pages)
        truncated = start != 1 or bounded_end < total or offset > 0 or cursor > offset + max_chars or incomplete_text
        info.update(pages=pages, requested_range={"start_page": start, "end_page": end},
                    extracted_range={"start_page": start, "end_page": bounded_end},
                    returned_range={"start_page": pages[0]["page"], "end_page": pages[-1]["page"]} if pages else None,
                    text_length=cursor, next_page=bounded_end + 1 if bounded_end < total else None,
                    incomplete_text=incomplete_text,
                    next_offset=offset + len(text) if offset + len(text) < cursor else None)
    elif any(value in mime.lower() for value in ("html", "text/", "xml")) or (not mime and body.lstrip().startswith(b"<")):
        soup = BeautifulSoup(body, "html.parser")
        info["title"] = soup.title.get_text(" ", strip=True)[:2000] if soup.title else None
        for name in ("article:published_time", "datePublished", "pubdate", "publishdate"):
            node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                info["published_at"] = node["content"]
                break
        base = soup.find("base", href=True)
        base_url = urljoin(url, base["href"]) if base else url
        seen = set()
        attachment_count = 0
        link_chars = attachment_chars = 0
        for node in soup.find_all("a", href=True):
            href = urljoin(base_url, str(node["href"]))
            parsed = urlsplit(href)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or href in seen or len(href) > 8192:
                continue
            seen.add(href)
            label = node.get_text(" ", strip=True)[:300]
            attachment = bool(re.search(r"\.(pdf|docx?|xlsx?|csv|zip)(?:$|[?#])", href, re.I) or node.has_attr("download") or "pdf" in node.get("type", "").lower() or re.search(r"附件|下载|报告全文", label))
            attachment_count += int(attachment)
            item = {"url": href, "title": label, "is_attachment": attachment, "content_type": node.get("type")}
            size = len(href) + len(label)
            if len(info["links"]) < 200 and link_chars + size <= 32000:
                info["links"].append(item)
                link_chars += size
            if attachment and len(info["attachments"]) < 100 and attachment_chars + size <= 32000:
                info["attachments"].append(item)
                attachment_chars += size
        info["links_truncated"] = len(seen) > len(info["links"])
        info["attachments_truncated"] = attachment_count > len(info["attachments"])
        for node in soup(["script", "style", "noscript", "iframe", "form"]):
            node.decompose()
        article = soup.find("article") or soup.find("main") or soup
        full_text = article.get_text("\n", strip=True)
        text = full_text[offset:offset + max_chars]
        truncated = offset > 0 or len(full_text) > offset + max_chars
        info.update(text_length=len(full_text), next_offset=offset + len(text) if offset + len(text) < len(full_text) else None)
    else:
        raise ValueError(f"不支持读取的原文格式：{mime[:100] or '未知'}")
    if not text.strip() or (info.get("pages") and not any(p["text"].strip() for p in info["pages"])):
        raise DocumentAccessError('no_extractable_text', 'extraction', '未提取到所选范围的原文文字；可能超出正文范围或需要扫描件 OCR，当前未读取全文', '尝试其他页或公开文字版；本工具不进行OCR')
    info.update(text=text, access="excerpt" if truncated else "full_text", truncated=truncated,
                offset=offset, returned_chars=len(text), requested_chars=max_chars,
                extraction_note="仅包含本次 HTTP 响应的可提取文字；不执行网页脚本，不进行 OCR；页码为 PDF 物理页")
    return info


async def _document_operation(url: str, config: dict, operation, refresh: bool) -> dict:
    result = {"url": url, "title": None, "text": None, "published_at": None, "fetched_at": now_iso(), "access": "failed", "error": None}
    config = {**config, '_network_diagnostics': []}
    document = None
    try:
        async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
            document = await _download_document(url, config, refresh)
            result.update({k: v for k, v in document.items() if k != "body"})
            extracted = await asyncio.to_thread(operation, document)
        result.update(extracted)
    except Exception as exc:
        result["error"] = _error(exc)
        if isinstance(exc, DocumentAccessError):
            kind, stage, action = exc.kind, exc.stage, exc.action
        elif isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            kind = 'access_denied' if code in (401,403) else 'not_found' if code == 404 else 'rate_limited' if code == 429 else 'http_error'
            stage, action = 'http', '可稍后重试或用原生搜索寻找其他公开发布地址；不得绕过登录或访问控制'
            result['http_status'] = code
        elif isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            kind, stage, action = 'timeout', 'extraction' if document else 'download', '缩小提取页数或稍后有界重试；下载超时可尝试其他公开地址'
        elif isinstance(exc, (httpx.RequestError, OSError)):
            kind, stage, action = _quote_failure_kind(exc), 'connection', '核对网络或原生搜索其他公开地址；正文未取得'
        else:
            kind, stage, action = ('extraction_failed', 'extraction', '尝试其他页、公开文本版或另一发布地址') if document else ('invalid_request', 'validation', '检查URL与读取范围')
        result.update(error_kind=kind, failure_stage=stage, next_action=action, returned_chars=0,
                      returned_range=None, alternative_source_status='not_searched')
        if getattr(exc, "_material_observation_id", None) is not None:
            result["_observation_id"] = exc._material_observation_id
    if config['_network_diagnostics']:
        result['network_diagnostics'] = config['_network_diagnostics']
    return result


async def read_url(url: str, config: dict, *, start_page: int | None = None, end_page: int | None = None,
                   page: int | None = None, offset: int = 0, limit: int | None = None, refresh: bool = False) -> dict:
    def extract(doc):
        expected_pdf = page is not None or start_page is not None or end_page is not None or urlsplit(url).path.lower().endswith('.pdf')
        if expected_pdf and not doc['body'].lstrip().startswith(b'%PDF-'):
            links = []
            if 'html' in doc['content_type']:
                web = _extract_document(doc['body'], doc['content_type'], config, url=doc['final_url'], offset=offset, limit=limit)
                links = web.get('attachments', [])
                if page is None and start_page is None and end_page is None:
                    # The visible HTML remains usable evidence of that webpage,
                    # never evidence that the expected PDF itself was read.
                    return {**web, 'document_format': 'html', 'pdf_available': False,
                            'error_kind': 'non_pdf_response', 'failure_stage': 'content_type',
                            'next_action': '已取得的是网页文字，不是PDF正文；可按附件链接继续读取并核对文档身份和时期',
                            'alternative_source_status': 'attachment_links_only' if links else 'not_searched'}
            return {'access': 'failed', 'text': None, 'error': '响应为网页或非PDF数据，未读取PDF正文',
                    'error_kind': 'non_pdf_response', 'failure_stage': 'content_type', 'returned_chars': 0,
                    'returned_range': None, 'attachments': links, 'alternative_source_status': 'attachment_links_only' if links else 'not_searched',
                    'next_action': '可读取返回的附件地址；链接不是已读正文。也可用原生搜索寻找同份公开文档，核对时期和身份。'}
        return _extract_document(doc['body'], doc['content_type'], config, url=doc['final_url'], start_page=start_page,
                                 end_page=end_page, page=page, offset=offset, limit=limit)
    result = await _document_operation(url, config, extract, refresh)
    result['requested_access_range'] = {'page': page, 'start_page': start_page, 'end_page': end_page, 'offset': offset, 'limit': limit}
    return result


async def pdf_metadata(url: str, config: dict, *, refresh: bool = False) -> dict:
    def inspect(doc):
        reader = _pdf_reader(doc["body"])
        return {**_pdf_info(reader), "access": "metadata", "truncated": True,
                "extraction_note": "仅读取 PDF 元数据；尚未提取正文，不代表已读全文"}
    return await _document_operation(url, config, inspect, refresh)


async def search_pdf(url: str, query: str, config: dict, *, start_page: int = 1,
                     end_page: int | None = None, max_results: int = 20, refresh: bool = False) -> dict:
    def locate(doc):
        if not query.strip() or len(query) > 300:
            raise ValueError("PDF 关键词须为 1–300 字符")
        reader = _pdf_reader(doc["body"])
        info = _pdf_info(reader)
        total, start = info["page_count"], int(start_page)
        if start < 1 or start > total or (end_page is not None and int(end_page) < start):
            raise ValueError("PDF 检索页码范围无效")
        scan_limit = _bounded(config.get("read_search_max_pages"), 100, 1, 300)
        end = min(total, int(end_page) if end_page is not None else total, start + scan_limit - 1)
        count = _bounded(max_results, 20, 1, 100)
        matches, scanned, empty = [], [], []
        hit_limit = False
        deadline = time.monotonic() + max(1, float(config.get("read_timeout_seconds", 30)))
        for number in range(start, end + 1):
            if time.monotonic() > deadline:
                raise TimeoutError("PDF 检索超时；请缩小页码范围")
            value = reader.pages[number - 1].extract_text() or ""
            scanned.append(number)
            if not value.strip():
                empty.append(number)
            for found in re.finditer(re.escape(query), value, re.IGNORECASE):
                matches.append({"page": number, "start": found.start(), "end": found.end(),
                                "snippet": value[max(0, found.start() - 120):found.end() + 180],
                                "source_url": f"{doc['final_url'].split('#')[0]}#page={number}"})
                if len(matches) >= count:
                    hit_limit = True
                    break
            if hit_limit:
                break
        last = scanned[-1]
        truncated = hit_limit or last < total or start != 1 or bool(empty)
        return {**info, "query": query, "matches": matches, "access": "excerpt", "truncated": truncated,
                "searched_range": {"start_page": start, "end_page": last}, "pages_without_text": empty,
                "search_complete": not truncated, "match_limit_reached": hit_limit,
                "next_page": last if hit_limit else last + 1 if last < total else None,
                "extraction_note": "仅返回关键词位置和上下文，不是全文；达到命中数上限时请直接读取最后命中页，再从下一页继续"}
    return await _document_operation(url, config, locate, refresh)
