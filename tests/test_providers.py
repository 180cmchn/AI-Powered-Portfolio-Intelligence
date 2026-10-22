from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import providers
from app.calendar import market_state


def mocked_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_quote_etf_precision_and_unknown_market_time(monkeypatch):
    def handler(request):
        assert request.url.params["secid"] == "1.510300"
        return httpx.Response(200, json={"rc": 0, "data": {"f57": "510300", "f58": "沪深300ETF", "f59": 3, "f43": 4107, "f170": -186}})
    mocked_client(monkeypatch, handler)
    result = await providers.MarketProvider({}).quote("SH:510300")
    assert result["price"] == "4.107"
    assert result["change_pct"] == "-1.86"
    assert result["as_of"] is None
    assert result["status"] == "unknown_time"
    assert result["security_status"] == "unknown"


@pytest.mark.asyncio
async def test_quote_retains_actual_timestamp_and_stale_status(monkeypatch):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"rc": 0, "data": {"f57": "000001", "f58": "平安银行", "f59": 2, "f43": 1130, "f170": 0, "f86": 1700000000}}))
    result = await providers.MarketProvider({}).quote("SZ:000001")
    assert result["price"] == "11.3"
    assert result["change_pct"] == "0"
    assert result["as_of"] != result["fetched_at"]
    assert result["status"] == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"rc": 0, "data": None},
    {"rc": 0, "data": {"f57": "000001", "f58": "同名证券", "f59": 2, "f43": 1100}},
    {"rc": 0, "data": {"f57": "510300", "f58": "ETF", "f43": 4107}},
])
async def test_invalid_quote_is_not_a_price(monkeypatch, payload):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = await providers.MarketProvider({}).quote("SH:510300")
    assert result["status"] == "unavailable"
    assert result["price"] is None
    assert result["error"]


@pytest.mark.asyncio
async def test_network_timeout_is_explicit(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)
    mocked_client(monkeypatch, handler)
    result = await providers.MarketProvider({}).quote("SH:600519")
    assert result["status"] == "unavailable"
    assert "超时" in result["error"]
    assert result["as_of"] is None


@pytest.mark.asyncio
async def test_security_preserves_identity_and_sector(monkeypatch):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"rc": 0, "data": {"f57": "600519", "f58": "贵州茅台", "f127": "白酒Ⅱ"}}))
    result = await providers.MarketProvider({}).security("SH:600519")
    assert result["symbol"] == "SH:600519"
    assert result["sector"] == "白酒Ⅱ"
    assert result["kind"] == "stock"


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,payload", [
    ("SH:000001", {"rc": 0, "data": {"f57": "000001", "f58": "上证指数"}}),
    ("SH:600519", {"rc": 0, "data": None}),
    ("SH:600519", []),
])
async def test_security_unavailable_never_invents_identity(monkeypatch, symbol, payload):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = await providers.MarketProvider({}).security(symbol)
    assert result["name"] is None
    assert result["status"] == "unavailable"
    assert result["error"]


@pytest.mark.asyncio
async def test_search_missing_key_never_returns_fake_results(monkeypatch):
    monkeypatch.delenv("TEST_SEARCH_KEY", raising=False)
    result = await providers.search("公司 财报", {"search_provider": "tavily", "search_key_env": "TEST_SEARCH_KEY"})
    assert result["status"] == "unavailable"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_tavily_preserves_mixed_snippet_and_missing_date(monkeypatch):
    monkeypatch.setenv("TEST_SEARCH_KEY", "test-secret")
    snippet = "报告披露收入 10 亿元；评论认为现在应该加仓。"
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"results": [{"title": "混合材料", "url": "https://example.com/a", "content": snippet}]}))
    result = await providers.search("财报", {"search_provider": "tavily", "search_key_env": "TEST_SEARCH_KEY"})
    assert result["results"][0]["snippet"] == snippet
    assert result["results"][0]["published_at"] is None
    assert result["results"][0]["access"] == "excerpt"
    assert "test-secret" not in str(result)


@pytest.mark.asyncio
async def test_search_http_error_does_not_echo_key(monkeypatch):
    monkeypatch.setenv("TEST_SEARCH_KEY", "test-secret")
    mocked_client(monkeypatch, lambda request: httpx.Response(401, text="secret response test-secret"))
    result = await providers.search("财报", {"search_provider": "brave", "search_key_env": "TEST_SEARCH_KEY"})
    assert result["error"] == "HTTP 401"
    assert "test-secret" not in str(result)


@pytest.mark.asyncio
async def test_read_failure_preserves_unknown_fields(monkeypatch):
    async def fail(url, config):
        raise httpx.ReadTimeout("network timeout")
    monkeypatch.setattr(providers, "_fetch_public", fail)
    result = await providers.read_url("https://example.com/report.pdf", {})
    assert result["access"] == "failed"
    assert result["text"] is None
    assert result["published_at"] is None
    assert result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/", "http://192.168.1.1/", "file:///etc/passwd", "http://[::1]/", "https://example.com:8888/"])
async def test_research_cannot_reach_local_services(url):
    result = await providers.read_url(url, {})
    assert result["access"] == "failed"
    assert result["text"] is None


@pytest.mark.asyncio
async def test_redirect_rechecks_target_and_pins_ip(monkeypatch):
    seen = []
    async def resolve(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("内网地址禁止访问")
        return "example.com", "93.184.216.34"
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
    monkeypatch.setattr(providers, "_resolve_public_url", resolve)
    mocked_client(monkeypatch, handler)
    result = await providers.read_url("https://example.com/report", {})
    assert len(seen) == 2
    assert result["access"] == "failed"


def test_html_mixed_content_is_not_filtered_and_date_unknown():
    result = providers._extract_document("<title>混合材料</title><article><p>公司披露收入 10 亿元。</p><p>作者建议现在买入。</p></article>".encode(), "text/html", {})
    assert "收入 10 亿元" in result["text"]
    assert "作者建议现在买入" in result["text"]
    assert result["published_at"] is None
    assert result["access"] == "full_text"


def test_truncated_document_is_excerpt():
    result = providers._extract_document(("<article>" + "a" * 3000 + "</article>").encode(), "text/html", {"read_max_chars": 1000})
    assert len(result["text"]) == 1000
    assert result["access"] == "excerpt"


def test_calendar_holidays_weekends_sessions_and_unknown_year():
    tz = ZoneInfo("Asia/Shanghai")
    assert market_state(datetime(2026, 2, 23, 10, tzinfo=tz))["trading_day"] is False
    assert market_state(datetime(2026, 2, 28, 10, tzinfo=tz))["trading_day"] is False
    assert market_state(datetime(2026, 10, 22, 10, tzinfo=tz))["phase"] == "morning"
    assert market_state(datetime(2026, 10, 22, 12, tzinfo=tz))["phase"] == "closed"
    assert market_state(datetime(2026, 10, 22, 14, tzinfo=tz))["phase"] == "afternoon"
    assert market_state(datetime(2026, 10, 22, 15, tzinfo=tz))["phase"] == "closed"
    assert market_state(datetime(2027, 1, 4, 10, tzinfo=tz))["trading_day"] is None
