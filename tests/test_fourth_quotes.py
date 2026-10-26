"""Quote protocol substitutes and controlled calendar cases, not live prices."""
from datetime import datetime

import httpx
import pytest

from app import providers


@pytest.fixture(autouse=True)
def reset_market_cache():
    providers._MARKET_CACHE.clear()
    yield
    providers._MARKET_CACHE.clear()


def freeze(monkeypatch, value):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(value).astimezone(tz)
    monkeypatch.setattr(providers, "datetime", Clock)


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def payload(code="600519", price=134998):
    return {"rc": 0, "data": {"f57": code, "f58": "贵州茅台", "f59": 2,
            "f43": price, "f170": 112, "f86": 1793002310, "f127": "白酒Ⅱ"}}


@pytest.mark.asyncio
async def test_disconnect_retries_once_with_sanitized_diagnostics(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError("private-proxy-password must not appear", request=request)
        return httpx.Response(200, json=payload())
    mock_client(monkeypatch, handler)
    quote = await providers.MarketProvider({}).quote("SH:600519")
    assert quote["price"] == "1349.98"
    assert len(requests) == 2
    assert quote["request_diagnostics"][0]["failure_kind"] == "remote_protocol"
    assert quote["request_diagnostics"][1]["http_status"] == 200
    assert "private-proxy-password" not in str(quote)


@pytest.mark.asyncio
async def test_rate_limit_is_bounded_and_no_fabricated_quote(monkeypatch):
    freeze(monkeypatch, "2026-10-26T23:00:00+08:00")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, text="sensitive upstream error")
    mock_client(monkeypatch, handler)
    quote = await providers.MarketProvider({}).quote("SH:600519")
    assert len(requests) == 1  # No immediate retry or alternate endpoint on explicit throttle.
    assert quote["price"] is None and quote["as_of"] is None
    assert quote["status"] == "unavailable"
    assert quote["error"] == "HTTP 429"
    assert not quote["fresh_for_triggers"]
    assert all(row["failure_kind"] == "rate_limited" for row in quote["request_diagnostics"])
    assert "sensitive" not in str(quote)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, json=payload("000001")),
    httpx.Response(200, text="<html>response shape changed</html>"),
    httpx.Response(403, text="access blocked"),
])
async def test_identity_parse_or_access_errors_do_not_retry(monkeypatch, response):
    requests = []
    def handler(request):
        requests.append(request)
        return response
    mock_client(monkeypatch, handler)
    quote = await providers.MarketProvider({}).quote("SH:600519")
    assert len(requests) == 1
    assert quote["price"] is None
    assert quote["status"] == "unavailable"
    assert quote["request_diagnostics"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_short_cache_preserves_original_fetch_time_and_identity(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload())
    mock_client(monkeypatch, handler)
    market = providers.MarketProvider({})
    first = await market.quote("SH:600519")
    cached = await market.quote("SH:600519")
    identity = await market.security("SH:600519")
    assert len(requests) == 1
    assert first["cache_hit"] is False and cached["cache_hit"] is True
    assert first["fetched_at"] == cached["fetched_at"] == identity["fetched_at"]
    assert first["as_of"] == cached["as_of"]
    assert identity["symbol"] == first["symbol"]
    assert identity["sector"] == "白酒Ⅱ"


@pytest.mark.parametrize("stamp,source,status,kind,fresh", [
    ("2026-10-26T16:30:00+08:00", "2026-10-26T15:34:57+08:00", "closed", "last_close", False),
    ("2026-10-25T20:00:00+08:00", "2026-10-23T16:11:50+08:00", "closed", "last_close", False),
    ("2026-10-26T09:00:00+08:00", "2026-10-23T15:00:00+08:00", "closed", "last_close", False),
    ("2026-10-26T16:30:00+08:00", "2026-10-23T15:00:00+08:00", "stale", "unknown", False),
    ("2026-10-26T16:30:00+08:00", "2026-10-26T10:00:00+08:00", "stale", "unknown", False),
    ("2026-10-26T10:00:30+08:00", "2026-10-26T10:00:00+08:00", "ok", "session_snapshot", True),
    ("2026-10-26T11:31:00+08:00", "2026-10-26T11:30:00+08:00", "ok", "session_snapshot", False),
    ("2026-10-26T10:00:00+08:00", "2026-10-26T10:05:00+08:00", "future_timestamp", "unknown", False),
    ("2027-01-04T16:30:00+08:00", "2027-01-04T15:30:00+08:00", "stale", "unknown", False),
])
def test_closed_stale_and_fresh_tick_are_distinct(stamp, source, status, kind, fresh):
    state = providers.quote_time_state(source, {}, stamp=datetime.fromisoformat(stamp))
    assert (state["status"], state["quote_kind"], state["fresh_for_triggers"]) == (status, kind, fresh)


@pytest.mark.parametrize("source", [None, "", "not a time", "2026-10-26T15:00:00"])
def test_unknown_source_time_does_not_become_live(source):
    state = providers.quote_time_state(source, {}, stamp=datetime.fromisoformat("2026-10-26T16:00:00+08:00"))
    assert state["status"] == "unknown_time"
    assert not state["fresh_for_triggers"]


@pytest.mark.asyncio
async def test_nonpositive_price_cannot_claim_freshness(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload(price=0)))
    quote = await providers.MarketProvider({}).quote("SH:600519")
    assert quote["price"] is None and quote["status"] == "unavailable"
    assert not quote["fresh_for_triggers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SH:600519", "SZ:000001"])
async def test_closed_network_failure_uses_explicit_same_provider_daily_close(monkeypatch, symbol):
    freeze(monkeypatch, "2026-10-26T23:00:00+08:00")
    requests = []
    code = symbol.split(":")[1]
    def handler(request):
        requests.append(request)
        if request.url.host == "push2.eastmoney.com":
            raise httpx.RemoteProtocolError("disconnected", request=request)
        assert request.url.host == "push2his.eastmoney.com"
        assert request.url.params["fqt"] == "0"
        assert request.url.params["beg"] == request.url.params["end"] == "20261026"
        return httpx.Response(200, json={"rc": 0, "data": {"code": code, "name": "证券", "klines": [
            "2026-10-26,10,10.2,10.3,9.9,1000,10000,4,2,0.2,1"]}})
    mock_client(monkeypatch, handler)
    market = providers.MarketProvider({})
    quote = await market.quote(symbol)
    security = await market.security(symbol)
    assert len(requests) == 3
    assert quote["price"] == "10.2" and quote["symbol"] == symbol
    assert quote["as_of"] == "2026-10-26" and quote["as_of_precision"] == "date"
    assert quote["source"] == "eastmoney:daily-unadjusted-close"
    assert quote["status"] == "closed" and quote["quote_kind"] == "last_close"
    assert quote["delay_seconds"] is None and not quote["fresh_for_triggers"]
    assert quote["session_close_time_basis"] == "exchange_calendar_not_supplier_timestamp"
    assert "live_quote_error" in quote and "error" not in quote
    assert security["name"] == "证券" and security["sector"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", ["2026-10-26T10:00:00+08:00", "2027-01-04T23:00:00+08:00"])
async def test_active_or_unknown_calendar_cannot_fallback_to_close(monkeypatch, stamp):
    freeze(monkeypatch, stamp)
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("disconnected", request=request)
    mock_client(monkeypatch, handler)
    quote = await providers.MarketProvider({}).quote("SH:600519")
    assert len(requests) == 2
    assert quote["price"] is None and quote["status"] == "unavailable"


@pytest.mark.parametrize("stamp,status", [
    ("2026-10-26T23:00:00+08:00", "closed"),
    ("2026-10-27T08:00:00+08:00", "closed"),
    ("2026-10-27T10:00:00+08:00", "stale"),
    ("2026-10-27T23:00:00+08:00", "stale"),
])
def test_date_precision_close_remains_honest_when_reused(stamp, status):
    result = providers.quote_time_state("2026-10-26", {}, stamp=datetime.fromisoformat(stamp), source_precision="date")
    assert result["status"] == status
    assert result["delay_seconds"] is None and not result["fresh_for_triggers"]
