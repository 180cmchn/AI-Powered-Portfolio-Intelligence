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
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "网络请求超时"
    if isinstance(exc, (httpx.RequestError, OSError)):
        return f"网络连接失败 ({type(exc).__name__})"
    return str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__


class MarketProvider:
    """A public website adapter, not an exchange-authorized real-time feed."""

    def __init__(self, config: dict):
        self.config = config
        self.provider = config.get("quote_provider", "eastmoney")

    async def _data(self, symbol: str) -> dict:
        if self.provider != "eastmoney":
            raise ValueError("行情数据源未启用；manual 模式只使用显式录入的行情快照")
        exchange, code = normalize_symbol(symbol).split(":")
        secid = f"{'1' if exchange == 'SH' else '0'}.{code}"
        async with httpx.AsyncClient(timeout=float(self.config.get("network_timeout_seconds", 12))) as client:
            response = await client.get(
                EASTMONEY_ENDPOINT,
                params={"secid": secid, "fields": EASTMONEY_FIELDS},
                headers={"User-Agent": "Mozilla/5.0 LocalStockResearch/1.0", "Referer": "https://quote.eastmoney.com/"},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("行情响应格式异常，预期证券数据对象")
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise ValueError("行情服务未返回证券数据；证券代码可能无效或服务暂不可用")
        if str(data.get("f57", "")) != code or not data.get("f58"):
            raise ValueError("行情响应中的证券身份与请求不匹配")
        # Reject unsupported market/code combinations instead of silently pricing an index.
        if _kind(symbol, str(data.get("f58", ""))) is None:
            raise ValueError("首版仅支持可识别的 A 股与场内 ETF，请核对交易所和代码")
        return data

    async def quote(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        result = {"symbol": symbol, "name": None, "price": None, "change_pct": None,
                  "as_of": None, "fetched_at": now_iso(), "source": self.provider,
                  "status": "unavailable", "delay_seconds": None, "security_status": "unknown"}
        try:
            data = await self._data(symbol)
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
                    age = int((datetime.now(SHANGHAI) - observed).total_seconds())
                    result.update(as_of=observed.isoformat(), delay_seconds=max(0, age))
                    result["status"] = "future_timestamp" if age < -60 else "stale" if age > int(self.config.get("quote_stale_seconds", 180)) else "ok"
                except (OverflowError, OSError, ValueError):
                    result["status"] = "unknown_time"
            else:
                result["status"] = "unknown_time"
            if result["price"] is None or Decimal(result["price"]) <= 0:
                result.update(price=None, status="unavailable", error="数据源未返回有效价格；不能据此推断停牌状态")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            result["error"] = _error(exc)
        result["fetched_at"] = now_iso()
        return result

    async def security(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        exchange, code = symbol.split(":")
        result = {"symbol": symbol, "code": code, "exchange": exchange, "name": None,
                  "kind": _kind(symbol), "sector": None, "source": self.provider,
                  "fetched_at": now_iso(), "status": "unavailable"}
        try:
            data = await self._data(symbol)
            result.update(name=data["f58"], kind=_kind(symbol, data["f58"]), sector=data.get("f127") or None,
                          source="eastmoney:public-website", status="ok")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            result["error"] = _error(exc)
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
                rows, meta = await self._eastmoney_history(f"{'1' if exchange == 'SH' else '0'}.{code}", first, last, interval, adjust)
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
                rows, meta = await self._eastmoney_history(codes[benchmark][0], first, last, "daily", "none")
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
    encoded, fetched_at = await asyncio.shield(task)
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
    entries = await asyncio.get_running_loop().getaddrinfo(hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(item[4][0] for item in entries))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("研究工具不能访问本机、内网或保留地址")
    return hostname, addresses[0]


async def _fetch_public(url: str, config: dict) -> tuple[bytes, str, str]:
    max_bytes = min(20_000_000, max(10_000, int(config.get("read_max_bytes", 5_000_000))))
    async with httpx.AsyncClient(timeout=float(config.get("network_timeout_seconds", 12)), follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            hostname, address = await _resolve_public_url(url)
            original = httpx.URL(url)
            pinned = original.copy_with(host=address)
            # httpcore honors sni_hostname for TLS verification while TCP uses pinned IP.
            headers = {"Host": original.netloc.decode("ascii"), "User-Agent": "LocalStockResearch/1.0", "Accept": "text/html,application/pdf,text/plain"}
            async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": hostname}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("原文重定向缺少目标地址")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                parts = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > max_bytes:
                        raise ValueError(f"原文超过读取上限 {max_bytes} 字节")
                    parts.append(chunk)
                return b"".join(parts), response.headers.get("content-type", ""), url
        raise ValueError("原文重定向次数超过上限")


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
    key = (url, _body_limit(config))
    ttl = _bounded(config.get("read_cache_seconds"), 300, 0, 3600)
    cached = _DOCUMENT_CACHE.get(key)
    if not refresh and ttl and cached and time.monotonic() - cached[0] < ttl:
        _DOCUMENT_CACHE.move_to_end(key)
        return {**cached[1], "cache_hit": True, "retrieved_at": now_iso()}
    loop = asyncio.get_running_loop()
    task_key = (loop, key)
    task = _DOCUMENT_INFLIGHT.get(task_key)
    if task is None:
        async def fetch() -> dict:
            async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
                body, mime, final_url = await _fetch_public(url, config)
            if len(body) > _body_limit(config):
                raise ValueError("原文超过读取字节上限")
            document = {"body": body, "content_type": mime, "final_url": final_url,
                        "fetched_at": now_iso(), "document_id": hashlib.sha256(body).hexdigest(),
                        "byte_length": len(body)}
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
    reader = PdfReader(io.BytesIO(body))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 已加密，无法读取正文")
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
            raise ValueError("PDF 页码范围无效或超过总页数")
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
        raise ValueError("未提取到所选范围的原文文字；可能超出正文范围或需要扫描件 OCR，当前未读取全文")
    info.update(text=text, access="excerpt" if truncated else "full_text", truncated=truncated,
                offset=offset, returned_chars=len(text), requested_chars=max_chars,
                extraction_note="仅包含本次 HTTP 响应的可提取文字；不执行网页脚本，不进行 OCR；页码为 PDF 物理页")
    return info


async def _document_operation(url: str, config: dict, operation, refresh: bool) -> dict:
    result = {"url": url, "title": None, "text": None, "published_at": None, "fetched_at": now_iso(), "access": "failed", "error": None}
    try:
        async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
            document = await _download_document(url, config, refresh)
            result.update({k: v for k, v in document.items() if k != "body"})
            extracted = await asyncio.to_thread(operation, document)
        result.update(extracted)
    except Exception as exc:
        result["error"] = _error(exc)
    return result


async def read_url(url: str, config: dict, *, start_page: int | None = None, end_page: int | None = None,
                   page: int | None = None, offset: int = 0, limit: int | None = None, refresh: bool = False) -> dict:
    return await _document_operation(url, config, lambda doc: _extract_document(
        doc["body"], doc["content_type"], config, url=doc["final_url"], start_page=start_page,
        end_page=end_page, page=page, offset=offset, limit=limit), refresh)


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
