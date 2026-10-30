"""Exact public-directory protocol doubles; separate live evidence is archived."""
import asyncio

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.domain import AccountInput, HoldingInput, UniverseInput, symbol
from app.store import Store
from app.symbols import SymbolResolver, SymbolResolutionError


NAMES = {"518880": "黄金ETF华安", "688825": "长鑫科技", "562500": "机器人ETF华夏",
         "159278": "机器人ETF鹏华", "000001": "平安银行"}


def directory_row(code, *, name=None, market=None, security_type=None):
    market = market or ("2" if code.startswith(("0", "15")) else "1")
    return {"Code": code, "Name": name or NAMES.get(code, "协议替身证券"),
            "MarketType": market, "SecurityType": security_type or (
                "8" if code.startswith(("51", "56", "15")) else "2" if market == "2" else "25"),
            "QuoteID": f"{'1' if market == '1' else '0'}.{code}"}


def response(rows):
    return httpx.Response(200, json={"QuotationCodeTable": {
        "Status": 0, "Data": rows or None, "TotalCount": len(rows)}})


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "symbols.sqlite3")
    result.set_meta("settings", {"quote_provider": "eastmoney"})
    return result


def resolver(store, handler=None):
    return SymbolResolver(store, transport=httpx.MockTransport(handler or (
        lambda request: response([directory_row(request.url.params["input"])]))))


def test_input_format_preserves_zero_and_accepts_all_separators():
    assert symbol(" 000001 ") == "000001"
    assert symbol(" sh:518880 ") == "SH:518880"
    universe = UniverseInput(watchlist="518880\n688825，562500;159278；000001", candidates=[])
    assert universe.watchlist == list(NAMES)
    assert HoldingInput(symbol="000001", quantity="10000", cost_price="").quantity == "10000"


@pytest.mark.parametrize("raw", ["12345", "1234567", "abc123", "１２３４５６", "SH:00000A", "US:000001"])
def test_bad_syntax_has_actionable_original_index(raw):
    with pytest.raises(ValidationError) as exc:
        UniverseInput(watchlist=["518880", raw])
    assert "第 2 项" in str(exc.value) and raw in str(exc.value)


@pytest.mark.asyncio
async def test_user_codes_resolve_once_and_mixed_duplicates_normalize(store):
    calls = []
    def handler(request):
        calls.append(request.url.params["input"])
        return response([directory_row(calls[-1])])
    data = UniverseInput(watchlist="518880\n688825\n562500\nSH:518880", candidates="159278,000001,SZ:000001")
    normalized, result = await resolver(store, handler).universe(data)
    assert normalized.watchlist == ["SH:518880", "SH:688825", "SH:562500"]
    assert normalized.candidates == ["SZ:159278", "SZ:000001"]
    assert sorted(calls) == sorted(NAMES)
    assert result["ok"]
    assert all(item["name"] == NAMES[item["code"]] for item in result["items"])
    assert not store.monitored(), "resolution preview must not change saved watchlist or holdings"
    store.save_universe(normalized)
    assert store.monitored() == sorted(normalized.watchlist + normalized.candidates)


@pytest.mark.asyncio
async def test_code_context_selects_stock_or_index_without_fuzzy_name(store):
    rows = [directory_row("000001"), directory_row("000001", name="上证指数", market="1", security_type="5"),
            directory_row("000001", name="华夏成长混合", market="6", security_type="17"),
            directory_row("600001", name="同名平安银行", market="1", security_type="1")]
    target = resolver(store, lambda request: response(rows))
    stock = await target.resolve(["000001"])
    assert stock["items"][0]["symbol"] == "SZ:000001"
    index = await target.resolve(["000001"], context="index")
    assert index["items"][0]["symbol"] == "SH:000001"
    ambiguous = await target.resolve(["000001"], context="all")
    assert ambiguous["items"][0]["status"] == "ambiguous"
    assert {row["symbol"] for row in ambiguous["items"][0]["matches"]} == {"SZ:000001", "SH:000001"}


@pytest.mark.asyncio
async def test_existing_confirmed_cache_works_offline_and_keeps_timestamp(store):
    online = await resolver(store).resolve(["159278"])
    first = online["items"][0]
    def offline(request):
        pytest.fail("accurate identity cache should not require network")
    cached = await resolver(store, offline).resolve(["159278", "SZ:159278"])
    assert cached["ok"] and cached["symbols"] == ["SZ:159278"]
    assert all(row["cache_hit"] and row["fetched_at"] == first["fetched_at"] for row in cached["items"])
    assert "实时行情" in cached["items"][0]["message"]


@pytest.mark.asyncio
async def test_failed_or_nameless_cache_not_mistaken_for_confirmed_identity(store):
    store.put_cache("securities", "SH:518880", {"symbol": "SH:518880", "name": "错误占位名", "kind": "etf", "status": "unavailable"})
    def offline(request):
        raise httpx.ConnectError("fixture offline", request=request)
    result = await resolver(store, offline).resolve(["518880"])
    assert not result["ok"] and result["items"][0]["status"] == "unavailable"
    assert "not_found" not in str(result)


@pytest.mark.asyncio
async def test_directory_not_found_is_distinct_from_network_failure(store):
    absent = await resolver(store, lambda request: response([])).resolve(["999999"])
    assert absent["items"][0]["status"] == "not_found"
    assert "本次目录" in absent["items"][0]["message"]
    def offline(request):
        raise httpx.ConnectError("fixture offline", request=request)
    failed = await resolver(store, offline).resolve(["999999"])
    assert failed["items"][0]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_partial_invalid_batch_does_not_modify_universe(store):
    store.save_universe({"watchlist": ["SZ:000001"], "candidates": []})
    before = store.universe_snapshot()
    target = resolver(store, lambda request: response([]) if request.url.params["input"] == "999999" else response([directory_row("518880")]))
    with pytest.raises(SymbolResolutionError) as exc:
        await target.universe(UniverseInput(watchlist=["518880", "999999"]))
    assert exc.value.detail["atomic"]
    assert "第 2 项「999999」" in str(exc.value)
    assert store.universe_snapshot() == before


@pytest.mark.asyncio
async def test_duplicate_inputs_do_not_shift_invalid_item_number(store):
    target = resolver(store, lambda request: response([]) if request.url.params["input"] == "999999" else response([directory_row("518880")]))
    with pytest.raises(SymbolResolutionError) as exc:
        await target.universe(UniverseInput(watchlist=["518880", "518880", "999999"]))
    assert "第 3 项「999999」" in str(exc.value)


@pytest.mark.asyncio
async def test_holding_duplicate_is_never_silently_summed_or_lost(store):
    same = AccountInput(name="fixture", cash="10000", holdings=[
        {"symbol": "159278", "quantity": "10000"}, {"symbol": "SZ:159278", "quantity": "10000"}])
    normalized, _ = await resolver(store).account(same)
    assert len(normalized.holdings) == 1 and normalized.holdings[0].quantity == "10000"
    assert normalized.cash == "10000" and normalized.holdings[0].cost_price is None
    different = same.model_copy(update={"holdings": [same.holdings[0], same.holdings[1].model_copy(update={"quantity": "20000"})]})
    with pytest.raises(SymbolResolutionError, match="数量或成本不同"):
        await resolver(store).account(different)


@pytest.mark.asyncio
async def test_wrong_explicit_market_is_not_silently_rewritten(store):
    result = await resolver(store).resolve(["SH:000001"])
    assert not result["ok"] and result["items"][0]["status"] == "not_found"


@pytest.mark.asyncio
async def test_same_name_different_codes_preserve_identities(store):
    target = resolver(store, lambda request: response([directory_row(request.url.params["input"], name="同名测试")]))
    result = await target.resolve(["518880", "562500"])
    assert result["symbols"] == ["SH:518880", "SH:562500"]


@pytest.mark.asyncio
async def test_batch_network_failure_has_bounded_attempts_and_explicit_retry(store):
    calls = []
    def offline(request):
        calls.append(request)
        raise httpx.ConnectError("fixture offline", request=request)
    target = resolver(store, offline)
    values = [str(600000 + index) for index in range(50)]
    result = await target.resolve(values)
    assert all(item["status"] == "unavailable" for item in result["items"])
    assert len(calls) <= 4
    previous = len(calls)
    await target.resolve(["518880"])
    assert len(calls) == previous + 1, "user retry is allowed to revalidate"


@pytest.mark.asyncio
async def test_bounded_deadline_does_not_leave_background_lookup_tasks(store):
    store.set_meta("settings", {"network_timeout_seconds": .1})
    async def slow(request):
        await asyncio.sleep(5)
    result = await resolver(store, slow).resolve(["518880", "159278"])
    assert all(item["status"] == "unavailable" for item in result["items"])


@pytest.mark.asyncio
async def test_manual_explicit_compatibility_is_not_fake_directory_confirmation(store):
    store.set_meta("settings", {"quote_provider": "manual"})
    result = await resolver(store).resolve(["SH:600519", "518880"])
    assert result["items"][0]["status"] == "explicit_unverified"
    assert result["items"][0]["name"] is None
    assert result["items"][1]["status"] == "resolved", "identity lookup is independent of the quote provider"


def api_fixture(tmp_path, handler=None):
    from app.main import create_app
    app = create_app(tmp_path / "symbols-api.sqlite3", worker=False)
    app.state.store.set_meta("settings", {"quote_provider": "manual"})
    app.state.symbol_resolver.transport = httpx.MockTransport(handler or (
        lambda request: response([]) if request.url.params["input"] == "999999"
        else response([directory_row(request.url.params["input"])])))
    async def no_quote_request(*args, **kwargs):
        return {"status": "fixture_no_network"}
    app.state.service.refresh_quotes = no_quote_request
    return app


def test_fastapi_user_three_lines_save_monitored_and_preview(tmp_path):
    app = api_fixture(tmp_path)
    with TestClient(app) as client:
        preview = client.post("/api/securities/resolve", json={"symbols": list(NAMES), "context": "stock_etf"})
        assert preview.status_code == 200 and preview.json()["ok"]
        assert client.get("/api/universe").json()["monitored"] == []
        saved = client.put("/api/universe", json={"watchlist": "518880\n688825\n562500", "candidates": ["159278", "000001", "SZ:000001"]})
        assert saved.status_code == 200
        value = saved.json()
        assert value["watchlist"] == ["SH:518880", "SH:688825", "SH:562500"]
        assert value["candidates"] == ["SZ:159278", "SZ:000001"]
        assert len(value["monitored"]) == 5
        assert len(value["symbol_resolution"]["items"]) == 6
        found = client.get("/api/securities", params={"q": "000001"}).json()
        assert found[0]["symbol"] == "SZ:000001" and found[0]["name"] == "平安银行"


def test_fastapi_atomic_invalid_batch_reports_original_index(tmp_path):
    app = api_fixture(tmp_path)
    with TestClient(app) as client:
        original = client.put("/api/universe", json={"watchlist": ["000001"]}).json()
        rejected = client.put("/api/universe", json={"watchlist": ["518880", "999999"]})
        assert rejected.status_code == 422
        assert "第 2 项「999999」" in rejected.json()["detail"]["message"]
        after = client.get("/api/universe").json()
        assert after["watchlist"] == original["watchlist"]
        assert after["universe_version"] == original["universe_version"]


def test_fastapi_account_switch_versions_quantities_and_single_security(tmp_path):
    app = api_fixture(tmp_path)
    with TestClient(app) as client:
        a = client.post("/api/accounts", json={"name": "虚拟 A", "cash": "10000", "holdings": [{"symbol": "159278", "quantity": "10000"}]}).json()
        b = client.post("/api/accounts", json={"name": "虚拟 B", "cash": "30000"}).json()
        assert a["holdings"][0] == {"symbol": "SZ:159278", "quantity": "10000", "cost_price": None}
        before_b = client.get(f"/api/accounts/{b['id']}").json()
        updated = client.put(f"/api/accounts/{a['id']}", json={"name": "虚拟 A", "cash": "10000", "expected_version": 1,
            "holdings": [{"symbol": "SZ:159278", "quantity": "20000"}]}).json()
        assert updated["version"] == 2 and updated["cash"] == "10000"
        after_b = client.get(f"/api/accounts/{b['id']}").json()
        for field in ("id", "version", "holdings", "cash", "preferences", "latest_job_id", "updated_at"):
            assert after_b[field] == before_b[field]
        conflict = client.put(f"/api/accounts/{a['id']}", json={"name": "虚拟 A", "cash": "10000", "expected_version": 1,
            "holdings": [{"symbol": "159278", "quantity": "10000"}]})
        assert conflict.status_code == 409
        task = client.post(f"/api/accounts/{b['id']}/analyze", json={"symbol": "518880", "operation_id": "tenth-symbol-fixture-1"})
        assert task.status_code == 200
        job = task.json()
        assert job["account_id"] == b["id"] and "SH:518880" in str(job)
