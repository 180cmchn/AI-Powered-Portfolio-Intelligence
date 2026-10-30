"""Bounded, exact-code security identity resolution at the HTTP boundary.

The public directory identifies an instrument, independently of quote access.
Its success is never presented as a live price. Existing confirmed identities
remain usable offline; no name search or prefix heuristic creates an identity.
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .domain import AccountInput, UniverseInput, now, symbol
from .providers import _kind

DIRECTORY_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SOURCE = "eastmoney:public-security-directory"


class ResolveInput(BaseModel):
    symbols: list[str] = Field(max_length=1000)
    context: Literal["stock_etf", "index", "all"] = "stock_etf"


class SymbolResolutionError(ValueError):
    def __init__(self, items: list[dict]):
        self.items = items
        issues = [item for item in items if item["status"] not in ("resolved", "explicit_unverified")]
        self.detail = {"message": "整批未保存，请修正以下证券后重试：" + "；".join(
            f"{item.get('field', '证券')}第 {item['index']} 项「{str(item['input'])[:40]}」：{item['message']}"
            for item in issues[:20]), "items": items, "atomic": True}
        super().__init__(self.detail["message"])


def _context_matches(kind: str, context: str) -> bool:
    return context == "all" or kind in (("stock", "etf") if context == "stock_etf" else ("index",))


def _directory_rows(payload: dict, code: str, context: str, fetched_at: str) -> list[dict]:
    """Exact code + observed market + applicable type, never a fuzzy name hit."""
    table = payload.get("QuotationCodeTable") if isinstance(payload, dict) else None
    if not isinstance(table, dict) or table.get("Status") != 0:
        raise ValueError("证券目录响应格式或服务状态异常")
    rows = table.get("Data")
    if rows is None and table.get("TotalCount") == 0:
        rows = []
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("证券目录响应条数或格式异常")
    output = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("Code") != code:
            continue
        # MarketType _TB is the observed Beijing A-share directory market.
        exchange = {"1": "SH", "2": "SZ", "_TB": "BJ"}.get(str(row.get("MarketType")))
        if not exchange or not isinstance(row.get("Name"), str) or not row["Name"].strip():
            continue
        sid = f"{exchange}:{code}"
        if row.get("QuoteID") != f"{'1' if exchange == 'SH' else '0'}.{code}":
            continue
        security_type = str(row.get("SecurityType"))
        band_kind = _kind(sid)
        kind = "index" if security_type == "5" else (
            "stock" if security_type in ("1", "2", "25", "27") and band_kind == "stock" else
            "etf" if security_type == "8" and band_kind == "etf" else None)
        if kind is None or not _context_matches(kind, context):
            continue
        output[sid] = {"symbol": sid, "code": code, "name": row["Name"].strip(),
                       "market": exchange, "exchange": exchange, "kind": kind,
                       "source": SOURCE, "source_url": DIRECTORY_URL,
                       "fetched_at": fetched_at, "identity_verified_at": fetched_at,
                       "identity_status": "confirmed", "status": "ok"}
    return list(output.values())


class SymbolResolver:
    def __init__(self, store, *, transport=None):
        self.store = store
        self.transport = transport

    def _cached(self, value: str, context: str, cache: dict) -> list[dict]:
        code = value.split(":")[-1]
        explicit = ":" in value
        matches = []
        for sid, row in cache.items():
            if not re.fullmatch(r"(?:SH|SZ|BJ):[0-9]{6}", sid) or sid.split(":")[-1] != code:
                continue
            if explicit and sid != value:
                continue
            if row.get("symbol") != sid or not isinstance(row.get("name"), str) or not row["name"].strip() or row.get("status") not in ("ok", "live", "closed", "demo"):
                continue
            kind = row.get("kind") or _kind(sid)
            if not kind or not _context_matches(kind, context) or (kind in ("stock", "etf") and _kind(sid) != kind):
                continue
            # A metadata-only or failed placeholder must never become proof of identity.
            if row.get("identity_status") in ("unverified", "unavailable"):
                continue
            matches.append({**row, "market": sid[:2], "code": code, "kind": kind,
                            "cache_hit": True,
                            "identity_verified_at": row.get("identity_verified_at") or row.get("fetched_at")})
        return matches

    async def resolve(self, values: list[str], context: str = "stock_etf") -> dict:
        if context not in ("stock_etf", "index", "all"):
            raise ValueError("不支持的证券解析上下文")
        settings = self.store.settings()
        cache = self.store.cache("securities")
        timeout = min(8.0, max(0.1, float(settings.get("network_timeout_seconds", 8))))
        semaphore = asyncio.Semaphore(4)
        inflight = {}
        # This request owns its failure memory. A deliberate retry/configuration
        # change always has the opportunity to revalidate the public directory.
        network_failure = None
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport,
                                     follow_redirects=False) as client:
            async def lookup(code):
                nonlocal network_failure
                async with semaphore:
                    if network_failure:
                        return network_failure
                    try:
                        async with asyncio.timeout(timeout):
                            response = await client.get(DIRECTORY_URL,
                                params={"input": code, "type": "14", "count": "30"},
                                headers={"User-Agent": "Mozilla/5.0 LocalStockResearch/1.0"})
                            response.raise_for_status()
                            if len(response.content) > 150_000:
                                raise ValueError("证券目录响应超过 150 KB 上限")
                            found = _directory_rows(response.json(), code, context, now())
                            for row in found:
                                previous = cache.get(row["symbol"], {})
                                self.store.put_cache("securities", row["symbol"], {**previous, **row})
                            return {"matches": found}
                    except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
                        failure = {"status": "unavailable", "message": "证券目录暂不可达或响应异常，未确认证券身份；保留输入后稍后重试。已有准确缓存仍可使用。",
                                   "failure_stage": "security_directory", "error_type": type(exc).__name__}
                        if isinstance(exc, httpx.HTTPStatusError):
                            failure["http_status"] = exc.response.status_code
                        # A network/HTTP fault is shared by this bounded batch;
                        # an unexpected individual response does not condemn other codes.
                        if isinstance(exc, (httpx.HTTPError, OSError, TimeoutError)):
                            network_failure = failure
                        return failure

            async def one(raw, index):
                base = {"input": raw, "index": index, "context": context}
                try:
                    value = symbol(raw)
                except (ValueError, AttributeError, TypeError) as exc:
                    return {**base, "status": "invalid", "message": str(exc) if isinstance(exc, ValueError) else "代码须为保留前导零的六位字符串"}
                cached = self._cached(value, context, cache)
                # A broad asset query cannot infer uniqueness from a partial
                # cache, e.g. a saved Shenzhen stock does not exclude an index.
                if len(cached) == 1 and (context != "all" or ":" in value):
                    chosen = cached[0]
                    return {**base, **chosen, "status": "resolved", "message": "使用已确认的证券缓存；识别不代表实时行情已取得。"}
                if len(cached) > 1:
                    return {**base, "status": "ambiguous", "matches": cached, "message": "此代码有多个证券匹配，请从名称和市场中选择。"}
                # Preserve the deliberate, existing manual-data workflow. This
                # is explicit identity input, never a claim that a directory was read.
                if settings.get("quote_provider") == "manual":
                    if ":" in value and (context == "all" or _context_matches(_kind(value) or "unknown", context)):
                        return {**base, "symbol": value, "market": value[:2], "name": None,
                                "kind": _kind(value), "status": "explicit_unverified", "cache_hit": False,
                                "message": "手工行情模式：保留明确市场代码；证券名称和存在性尚未由目录确认。"}
                code = value.split(":")[-1]
                if code not in inflight:
                    inflight[code] = asyncio.create_task(lookup(code))
                result = await inflight[code]
                if result.get("status"):
                    return {**base, **result}
                matches = result["matches"]
                if ":" in value:
                    matches = [row for row in matches if row["symbol"] == value]
                if not matches:
                    return {**base, "status": "not_found", "matches": [],
                            "message": "本次目录未找到代码与市场、证券类型相符的记录；请核对代码或稍后刷新目录。"}
                if len(matches) > 1:
                    return {**base, "status": "ambiguous", "matches": matches,
                            "message": "此代码有多个证券匹配，请从名称和市场中选择。"}
                return {**base, **matches[0], "status": "resolved", "cache_hit": False,
                        "message": "已从证券目录识别；实时行情状态另行获取。"}

            tasks = [asyncio.create_task(one(raw, index)) for index, raw in enumerate(values, 1)]
            try:
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=min(25.0, timeout * 3))
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                # A disconnected preview must not leave paid-work-adjacent
                # background directory requests or tasks using a closed client.
                unfinished = [task for task in [*tasks, *inflight.values()] if not task.done()]
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
            items = [task.result() if not task.cancelled() else {
                "input": raw, "index": index, "context": context, "status": "unavailable",
                "failure_stage": "batch_deadline", "message": "本批证券目录查询达到时限；整批未保存，请保留输入稍后重试或分批确认。"}
                for index, (raw, task) in enumerate(zip(values, tasks), 1)]
        acceptable = ("resolved", "explicit_unverified")
        return {"items": items, "ok": all(item["status"] in acceptable for item in items),
                "symbols": list(dict.fromkeys(item["symbol"] for item in items if item["status"] in acceptable))}

    async def account(self, data: AccountInput) -> tuple[AccountInput, dict]:
        resolution = await self.resolve([h.symbol for h in data.holdings])
        if not resolution["ok"]:
            raise SymbolResolutionError(resolution["items"])
        holdings, by_symbol = [], {}
        for original, resolved in zip(data.holdings, resolution["items"]):
            item = original.model_copy(update={"symbol": resolved["symbol"]})
            prior = by_symbol.get(item.symbol)
            if prior and (prior.quantity != item.quantity or prior.cost_price != item.cost_price):
                raise SymbolResolutionError([{**resolved, "status": "invalid", "field": "持仓",
                    "message": "与另一行识别为同一证券，但数量或成本不同；请合并为一行，系统不会擅自加总或丢弃。"}])
            if prior is None:
                holdings.append(item)
                by_symbol[item.symbol] = item
        return data.model_copy(update={"holdings": holdings}), resolution

    async def universe(self, data: UniverseInput) -> tuple[UniverseInput, dict]:
        keys = ("watchlist", "candidates")
        results = await self.resolve(data.watchlist + data.candidates)
        split = len(data.watchlist)
        for index, item in enumerate(results["items"]):
            item["field"] = "自选" if index < split else "候选"
            item["index"] = index + 1 if index < split else index - split + 1
        if not results["ok"]:
            raise SymbolResolutionError(results["items"])
        values = {}
        for key, rows in zip(keys, (results["items"][:split], results["items"][split:])):
            values[key] = list(dict.fromkeys(row["symbol"] for row in rows))
        return data.model_copy(update=values), results
