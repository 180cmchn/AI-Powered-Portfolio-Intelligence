"""Replaceable market and research I/O adapters.

These adapters preserve source metadata; they do not rank or admit evidence.
Research URLs are untrusted input and cannot access the local machine/network.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import os
import re
import socket
from datetime import datetime
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


def _extract_document(body: bytes, mime: str, config: dict) -> dict:
    max_chars = min(200_000, max(1000, int(config.get("read_max_chars", 40_000))))
    title, published_at, pages = None, None, None
    truncated = False
    if "pdf" in mime.lower() or body.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(body))
        max_pages = min(100, max(1, int(config.get("read_max_pdf_pages", 20))))
        page_count = len(reader.pages)
        pages = [{"page": number + 1, "text": reader.pages[number].extract_text() or ""} for number in range(min(page_count, max_pages))]
        text = "\n\n".join(f"[第 {page['page']} 页]\n{page['text']}" for page in pages)
        title = str(reader.metadata.title) if reader.metadata and reader.metadata.title else None
        truncated = page_count > max_pages
        # The returned page text is bounded by the same limit as the main text.
        remaining = max_chars
        bounded_pages = []
        for page in pages:
            if remaining <= 0:
                break
            bounded_pages.append({"page": page["page"], "text": page["text"][:remaining]})
            remaining -= len(page["text"])
        pages = bounded_pages
    elif any(value in mime.lower() for value in ("html", "text/", "xml")) or (not mime and body.lstrip().startswith(b"<")):
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else None
        for name in ("article:published_time", "datePublished", "pubdate", "publishdate"):
            node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                published_at = node["content"]
                break
        for node in soup(["script", "style", "noscript", "iframe", "form"]):
            node.decompose()
        article = soup.find("article") or soup.find("main") or soup
        text = article.get_text("\n", strip=True)
    else:
        raise ValueError(f"不支持读取的原文格式：{mime or '未知'}")
    if not text.strip():
        raise ValueError("未提取到原文文字；可能需要浏览器、登录或扫描件 OCR，当前未读取全文")
    truncated = truncated or len(text) > max_chars
    return {"title": title, "text": text[:max_chars], "published_at": published_at,
            "access": "excerpt" if truncated else "full_text", "truncated": truncated,
            "pages": pages, "extraction_note": "仅包含 HTTP 响应可提取文字；不执行网页脚本，不进行 OCR"}


async def read_url(url: str, config: dict) -> dict:
    result = {"url": url, "title": None, "text": None, "published_at": None, "fetched_at": now_iso(), "access": "failed", "error": None}
    try:
        async with asyncio.timeout(float(config.get("read_timeout_seconds", 30))):
            body, mime, final_url = await _fetch_public(url, config)
            extracted = await asyncio.to_thread(_extract_document, body, mime, config)
        result.update(extracted, final_url=final_url, content_type=mime)
    except Exception as exc:
        result["error"] = _error(exc)
    return result
