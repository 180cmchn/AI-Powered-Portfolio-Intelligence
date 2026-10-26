"""Sina website protocol substitutes, with controlled clocks; not live evidence."""
import asyncio
from datetime import datetime
import time

import httpx
import pytest

from app import providers


def body(symbol="sh600519", *, price="1349.980", date="2026-10-26", clock="10:00:00"):
    fields = ["0"] * 34
    fields[0], fields[2], fields[3], fields[30], fields[31], fields[32] = "贵州茅台", "1335.000", price, date, clock, "00"
    return ('var hq_str_' + symbol + '="' + ','.join(fields) + '";\n').encode("gb18030")


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-26T10:00:30+08:00").astimezone(tz)
    monkeypatch.setattr(providers, "datetime", Clock)


def install(monkeypatch, content):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "push2.eastmoney.com":
            raise httpx.RemoteProtocolError("controlled disconnect", request=request)
        assert request.url.host == "hq.sinajs.cn"
        assert request.headers["Referer"] == "https://finance.sina.com.cn/"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=content)
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,price", [("SH:600519", "1349.980"), ("SZ:000001", "11.300"), ("SH:510300", "4.107")])
async def test_explicit_sina_fallback_identity_precision_time_and_cached_security(monkeypatch, symbol, price):
    calls = install(monkeypatch, body(symbol.replace(":", "").lower(), price=price))
    market = providers.MarketProvider({"quote_fallback_provider": "sina"})
    quote = await market.quote(symbol)
    security = await market.security(symbol)
    assert len(calls) == 3
    assert quote["symbol"] == symbol and quote["price"] == price
    assert quote["source"] == "sina:public-website"
    assert quote["as_of"] == "2026-10-26T10:00:00+08:00" and quote["as_of"] != quote["fetched_at"]
    assert quote["status"] == "ok" and quote["fresh_for_triggers"]
    assert quote["fallback_diagnostic"]["http_status"] == 200
    assert quote["live_quote_error"] and "error" not in quote
    assert security["symbol"] == symbol and security["cache_hit"]
    assert security["sector"] is None


@pytest.mark.asyncio
async def test_explicit_none_never_contacts_sina(monkeypatch):
    calls = install(monkeypatch, body())
    quote = await providers.MarketProvider({"quote_fallback_provider": "none"}).quote("SH:600519")
    assert len(calls) == 2 and all(r.url.host == "push2.eastmoney.com" for r in calls)
    assert quote["price"] is None and quote["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    body("sz000001"), body(price="0"), body(price="NaN"), body(date="not-a-date"),
    body(clock="25:00:00"), body() + b'alert("never execute");', b'var hq_str_sh600519="";'
])
async def test_bad_identity_price_time_or_script_is_rejected(monkeypatch, content):
    calls = install(monkeypatch, content)
    quote = await providers.MarketProvider({"quote_fallback_provider": "sina"}).quote("SH:600519")
    assert len(calls) == 3
    assert quote["price"] is None and quote["as_of"] is None
    assert quote["status"] == "unavailable" and not quote["fresh_for_triggers"]
    assert quote["fallback_diagnostics"][0]["failure_kind"] == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("date,status", [("2026-10-23", "stale"), ("2026-10-27", "future_timestamp")])
async def test_alternative_source_never_promotes_old_or_future_time(monkeypatch, date, status):
    install(monkeypatch, body(date=date))
    quote = await providers.MarketProvider({"quote_fallback_provider": "sina"}).quote("SH:600519")
    assert quote["price"] == "1349.980" and quote["status"] == status
    assert not quote["fresh_for_triggers"]


@pytest.mark.asyncio
async def test_primary_throttle_does_not_call_fallback(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    quote = await providers.MarketProvider({"quote_fallback_provider": "sina"}).quote("SH:600519")
    assert len(calls) == 1
    assert quote["price"] is None and quote["error"] == "HTTP 429"


@pytest.mark.asyncio
async def test_total_budget_covers_primary_and_all_fallback_paths(monkeypatch):
    market = providers.MarketProvider({"quote_fallback_provider": "sina", "quote_refresh_timeout_seconds": 0.1})
    async def delayed(symbol):
        await asyncio.sleep(1)
    monkeypatch.setattr(market, "_data", delayed)
    started = time.monotonic()
    quote = await market.quote("SH:600519")
    assert time.monotonic() - started < 0.5
    assert quote["price"] is None and quote["status"] == "unavailable"
    assert quote["request_diagnostics"][0]["stage"] == "quote_total"
