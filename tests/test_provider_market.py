"""Protocol/field regressions use MockTransport, not a live-data claim."""
import asyncio
import json

import httpx
import pytest

from app import providers


@pytest.fixture(autouse=True)
def reset_cache():
    providers._MARKET_CACHE.clear()
    yield
    providers._MARKET_CACHE.clear()


def mocked_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def kline(code="600519"):
    return {"rc": 0, "data": {"code": code, "name": "贵州茅台", "klines": [
        "2026-10-19,1300,1320,1330,1290,123,456000,3,1.2,20,0.2",
        "2026-10-20,1320,1340,1350,1310,456,789000,3,1.5,20,0.3",
        "2026-10-21,1340,1330,1360,1320,789,900000,3,-0.75,-10,0.4"]}}


@pytest.mark.asyncio
async def test_eastmoney_history_ohlc_units_range_and_cache(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params["secid"] == "1.600519"
        assert request.url.params["fqt"] == "1"
        return httpx.Response(200, json=kline())
    mocked_client(monkeypatch, handler)
    market = providers.MarketProvider({})
    result = await market.history("SH:600519", "2026-10-01", "2026-10-22", adjust="qfq", limit=2)
    assert result["status"] == "ok"
    assert result["rows"][0]["open"] == "1320"
    assert result["rows"][0]["close"] == "1340"
    assert result["rows"][0]["amount"] == "789000"
    assert result["as_of"] == "2026-10-21"
    assert result["truncated"]
    assert result["amount_unit"] == "人民币元"
    cached = await providers.MarketProvider({}).history("SH:600519", "2026-10-01", "2026-10-22", adjust="qfq", limit=2)
    assert len(calls) == 1
    assert cached["cache_hit"]
    assert cached["fetched_at"] == result["fetched_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"rc": 0, "data": None}, kline("000001"), {"rc": 0, "data": {"code": "600519", "klines": ["broken"]}}])
async def test_history_invalid_response_does_not_manufacture_data(monkeypatch, payload):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = await providers.MarketProvider({}).history("SH:600519")
    assert result["status"] == "unavailable"
    assert result["rows"] == []
    assert result["as_of"] is None


@pytest.mark.asyncio
async def test_research_unavailable_without_provider_or_token(monkeypatch):
    monkeypatch.delenv("TEST_TUSHARE_TOKEN", raising=False)
    market = providers.MarketProvider({"research_provider": "tushare", "tushare_token_env": "TEST_TUSHARE_TOKEN"})
    results = await asyncio.gather(market.history("SH:600519"), market.sectors(), market.sector_members("801010.SI"), market.sector_history("801010.SI"), market.fundamentals("SH:600519"))
    for result in results:
        assert result["status"] == "unavailable"
        assert result["rows"] == []
        assert "环境变量" in result["error"]


@pytest.mark.asyncio
async def test_tushare_real_protocol_units_and_permission_error(monkeypatch):
    monkeypatch.setenv("TEST_TUSHARE_TOKEN", "sample-secret-never-echo")
    def handler(request):
        body = json.loads(request.content)
        assert body["token"] == "sample-secret-never-echo"
        assert request.url.scheme == "https"
        assert body["params"]["ts_code"] == "600519.SH"
        if body["api_name"] == "daily":
            return httpx.Response(200, json={"code": 0, "data": {"fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"], "items": [["600519.SH", "20261021", 1300, 1350, 1290, 1340, 123, 456, 1.5]]}})
        return httpx.Response(200, json={"code": 2002, "msg": "permission sample-secret-never-echo"})
    mocked_client(monkeypatch, handler)
    market = providers.MarketProvider({"research_provider": "tushare", "tushare_token_env": "TEST_TUSHARE_TOKEN"})
    history = await market.history("SH:600519")
    assert history["status"] == "ok"
    assert history["rows"][0]["time"] == "2026-10-21"
    assert history["amount_unit"] == "人民币千元"
    failure = await market.fundamentals("SH:600519")
    assert failure["status"] == "unavailable"
    assert "2002" in failure["error"]
    assert "sample-secret" not in str(history) + str(failure)


@pytest.mark.asyncio
async def test_sector_catalog_pagination_and_member_partial_breadth(monkeypatch):
    def handler(request):
        assert request.url.params["fltt"] == "2"
        if request.url.params["fs"].startswith("m:"):
            return httpx.Response(200, json={"rc": 0, "data": {"total": 3, "diff": {"0": {"f12": "BK0420", "f14": "航空机场", "f3": -1.39}}}})
        return httpx.Response(200, json={"rc": 0, "data": {"total": 9, "diff": [
            {"f12": "600519", "f13": 1, "f14": "贵州茅台", "f2": 1318.49, "f3": 0.67, "f124": 1700000000},
            {"f12": "920001", "f13": 0, "f14": "北交所示例", "f2": 10, "f3": -2.1}]}})
    mocked_client(monkeypatch, handler)
    market = providers.MarketProvider({})
    sectors = await market.sectors(limit=1)
    assert sectors["rows"][0]["change_pct"] == "-1.39"
    assert sectors["next_offset"] == 1
    members = await market.sector_members("BK0420", limit=2)
    assert members["rows"][0]["symbol"] == "SH:600519"
    assert members["rows"][1]["symbol"] == "BJ:920001"
    assert members["breadth"] == {"advancing": 1, "declining": 1, "unchanged": 0, "priced_members": 2, "scope": "returned_members_only"}
    assert members["truncated"]
    assert members["next_offset"] == 2


@pytest.mark.asyncio
async def test_sector_history_and_benchmark_are_distinct_identities(monkeypatch):
    def handler(request):
        secid = request.url.params["secid"]
        assert secid in ("90.BK0420", "1.000300")
        return httpx.Response(200, json=kline(secid.split(".")[1]))
    mocked_client(monkeypatch, handler)
    market = providers.MarketProvider({})
    sector = await market.sector_history("BK0420", "2026-10-01", "2026-10-22")
    benchmark = await market.benchmark_history("CSI300", "2026-10-01", "2026-10-22")
    assert sector["sector_id"] == "BK0420"
    assert benchmark["benchmark"] == "CSI300"
    assert sector["rows"] == benchmark["rows"]
    assert sector["status"] == benchmark["status"] == "ok"


@pytest.mark.asyncio
async def test_fundamental_report_period_not_fetch_time_and_conservative_yoy(monkeypatch):
    raw = [{"SECURITY_CODE": "600519", "SECUCODE": "600519.SH", "REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-15 00:00:00", "REPORT_TYPE_CODE": "001", "TOTAL_OPERATE_INCOME": 120, "PARENT_NETPROFIT": 20},
           {"SECURITY_CODE": "600519", "SECUCODE": "600519.SH", "REPORT_DATE": "2025-06-30 00:00:00", "NOTICE_DATE": "2025-08-15 00:00:00", "REPORT_TYPE_CODE": "001", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": -5}]
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"result": {"pages": 4, "data": raw}}))
    result = await providers.MarketProvider({}).fundamentals("SH:600519", periods=2)
    assert result["status"] == "ok"
    assert result["rows"][0]["report_period"].startswith("2026-06-30")
    assert result["as_of"].startswith("2026-08-15")
    assert result["as_of"] != result["fetched_at"]
    assert result["comparisons"][0]["yoy_pct"] == "20.0"
    assert result["comparisons"][1]["yoy_pct"] is None
    assert result["comparisons"][1]["difference"] == "25"
    assert result["truncated"]


@pytest.mark.asyncio
async def test_fundamental_wrong_security_fails(monkeypatch):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"result": {"pages": 1, "data": [{"SECURITY_CODE": "000001", "SECUCODE": "000001.SZ"}]}}))
    result = await providers.MarketProvider({}).fundamentals("SH:600519")
    assert result["status"] == "unavailable"
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_market_deadline_is_reported(monkeypatch):
    async def handler(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=kline())
    mocked_client(monkeypatch, handler)
    result = await providers.MarketProvider({"read_timeout_seconds": 0.001}).history("SH:600519")
    assert result["status"] == "unavailable"
    assert "超时" in result["error"]


@pytest.mark.asyncio
async def test_data_range_and_unsupported_adjustment_are_errors():
    market = providers.MarketProvider({"research_provider": "tushare"})
    result = await market.history("SH:600519", "2026-10-22", "2025-01-01")
    assert result["status"] == "unavailable"
    result = await market.history("SH:600519", adjust="qfq")
    assert result["status"] == "unavailable"
    assert "未复权" in result["error"]


@pytest.mark.asyncio
async def test_provider_ignoring_list_limit_is_still_bounded(monkeypatch):
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"rc": 0, "data": {"total": 50, "diff": [{"f12": f"BK{number:04}", "f14": "行业"} for number in range(50)]}}))
    result = await providers.MarketProvider({}).sectors(limit=3)
    assert len(result["rows"]) == 3
    assert result["truncated"]
    assert result["response_truncated"]


@pytest.mark.asyncio
async def test_tushare_missing_ohlc_does_not_claim_success(monkeypatch):
    monkeypatch.setenv("TEST_TUSHARE_TOKEN", "example")
    mocked_client(monkeypatch, lambda request: httpx.Response(200, json={"code": 0, "data": {"fields": ["ts_code", "trade_date", "open", "high", "low", "close"], "items": [["600519.SH", "20261022", 1300, 1350, 1250, None]]}}))
    result = await providers.MarketProvider({"research_provider": "tushare", "tushare_token_env": "TEST_TUSHARE_TOKEN"}).history("SH:600519")
    assert result["status"] == "unavailable"
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_expired_market_cache_does_not_hide_network_failure(monkeypatch):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json=kline())
    mocked_client(monkeypatch, handler)
    market = providers.MarketProvider({"research_cache_seconds": 30})
    first = await market.history("SH:600519", "2026-10-01", "2026-10-22")
    key = next(iter(providers._MARKET_CACHE))
    value = providers._MARKET_CACHE[key]
    providers._MARKET_CACHE[key] = (value[0] - 100, value[1], value[2])
    second = await market.history("SH:600519", "2026-10-01", "2026-10-22")
    assert first["status"] == "ok"
    assert second["status"] == "unavailable"
    assert second["rows"] == []
    assert second["as_of"] is None
