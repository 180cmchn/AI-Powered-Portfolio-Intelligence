from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT, db_path
from .domain import AccountInput, UniverseInput, TZ, now, symbol
from .store import Conflict, Store
from .service import Service
from .calendar import market_state


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_protocol: str = "responses"
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = Field(default="", max_length=200)
    model_key_env: str = "MODEL_API_KEY"
    model_allow_no_key: bool = False
    model_request_timeout_seconds: int = Field(default=90, ge=5, le=600)
    model_native_search: bool = True
    synthesis_request_timeout_seconds: int = Field(default=120, ge=5, le=600)
    synthesis_max_calls: int = Field(default=4, ge=1, le=8)
    synthesis_checkpoint_max_age_seconds: int = Field(default=1800, ge=60, le=86400)
    chat_use_max_completion_tokens: bool = False
    model_retries: int = Field(default=1, ge=0, le=3)
    max_total_agents: int = Field(default=16, ge=1, le=64)
    max_context_chars: int = Field(default=240000, ge=10000, le=2000000)
    quote_provider: str = "eastmoney"
    max_concurrent_agents: int = Field(default=3, ge=1, le=8)
    max_model_calls: int = Field(default=40, ge=2, le=300)
    agent_timeout_seconds: int = Field(default=120, ge=5, le=1800)
    analysis_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_agent_turns: int = Field(default=12, ge=2, le=60)
    max_output_tokens: int = Field(default=4000, ge=256, le=32000)
    quote_interval_seconds: int = Field(default=60, ge=30, le=3600)
    scheduled_research_enabled: bool = True
    scan_interval_minutes: int = Field(default=5, ge=1, le=240)
    analysis_interval_minutes: int = Field(default=15, ge=1, le=1440)
    close_summary_time: str = "15:15"
    price_trigger_pct: str = "3"
    quote_stale_seconds: int = Field(default=180, ge=30, le=86400)
    network_timeout_seconds: int = Field(default=12, ge=2, le=60)
    news_queries: list[str] = Field(default_factory=list, max_length=20)
    read_timeout_seconds: int = Field(default=30, ge=5, le=120)
    read_max_bytes: int = Field(default=5000000, ge=10000, le=20000000)
    read_max_chars: int = Field(default=40000, ge=1000, le=200000)
    read_max_pdf_pages: int = Field(default=20, ge=1, le=100)
    debounce_seconds: float = Field(default=1.5, ge=0, le=30)
    max_job_attempts: int = Field(default=2, ge=1, le=5)
    news_interval_minutes: int = Field(default=30, ge=1, le=1440)
    news_start_hour: int = Field(default=8, ge=0, le=24)
    news_end_hour: int = Field(default=23, ge=0, le=24)
    quote_cache_seconds: int = Field(default=30, ge=0, le=3600)
    quote_refresh_timeout_seconds: int = Field(default=45, ge=5, le=300)
    read_cache_seconds: int = Field(default=300, ge=0, le=3600)
    read_cache_max_bytes: int = Field(default=32000000, ge=1000000, le=100000000)
    read_search_max_pages: int = Field(default=100, ge=1, le=300)
    research_cache_seconds: int = Field(default=300, ge=0, le=3600)
    research_provider: str = "eastmoney"
    tushare_token_env: str = "TUSHARE_TOKEN"

    @field_validator("model_key_env", "tushare_token_env")
    @classmethod
    def env_name(cls, v):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", v):
            raise ValueError("填写密钥所在的环境变量名，不是密钥值")
        return v

    @field_validator("model_protocol")
    @classmethod
    def protocol(cls, v):
        if v not in ("responses", "chat_completions"):
            raise ValueError("不支持的模型接口协议")
        return v

    @field_validator("quote_provider")
    @classmethod
    def quote(cls, v):
        if v not in ("manual", "eastmoney"):
            raise ValueError("行情服务应为 eastmoney 或 manual")
        return v

    @field_validator("research_provider")
    @classmethod
    def research_provider_name(cls, v):
        if v not in ("eastmoney", "tushare"):
            raise ValueError("研究数据服务应为 eastmoney 或 tushare")
        return v

    @field_validator("model_base_url")
    @classmethod
    def url(cls, v):
        p = urlsplit(v)
        if not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("模型地址不能包含密钥、查询参数或用户凭据")
        if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("远端服务须使用 HTTPS；HTTP 仅限本机模型")
        return v.rstrip("/")

    @field_validator("close_summary_time")
    @classmethod
    def clock(cls, v):
        if not re.fullmatch(r"(?:15|16|17|18|19):[0-5][0-9]", v):
            raise ValueError("收盘总结时间应在 15:00–19:59，Asia/Shanghai")
        return v

    @field_validator("price_trigger_pct", mode="before")
    @classmethod
    def percent(cls, v):
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(str(v))
            if not d.is_finite() or not 0 < d <= 100:
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError("价格触发百分比应大于 0 且不超过 100")
        return str(d)

    @field_validator("news_queries")
    @classmethod
    def queries(cls, v):
        if any(len(x) > 300 for x in v):
            raise ValueError("单条公开信息检索词最多 300 字")
        return [x.strip() for x in v if x.strip()]


class AnalyzeInput(BaseModel):
    symbol: str | None = None


def create_app(path=None, mode=None, worker=True, runner=None):
    mode = mode or os.getenv("APP_MODE", "live")
    if mode not in ("live", "demo"):
        raise ValueError("APP_MODE 必须为 live 或 demo")
    store = Store(path or db_path(mode), mode)
    service = Service(store, runner)

    @asynccontextmanager
    async def lifespan(app):
        lock = None
        if worker:
            lock = open(str(store.path) + ".lock", "a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeError("该数据库已有后台服务运行；请勿使用多 worker 启动")
            await service.start()
        try:
            yield
        finally:
            if worker:
                await service.stop()
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    app = FastAPI(title="观澜 · 本地投资研究台", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.service = store, service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "拒绝跨站修改本机数据"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/status")
    async def status():
        from .model import native_search_status
        cfg = store.settings()
        p = urlsplit(cfg["model_base_url"])
        key_ready = bool(os.getenv(cfg["model_key_env"])) or (cfg.get("model_allow_no_key", False) and p.hostname in ("localhost", "127.0.0.1", "::1"))
        return {"mode": mode, "timezone": "Asia/Shanghai", "model_configured": bool(cfg["model_name"] and key_ready),
                "native_search": native_search_status(cfg, store.meta('native_search_capability')),
                "calendar": market_state(), "worker": {"running": service.running, "active": len(service.active), "last_error": service.last_error,
                "last_resume": store.meta("last_resume")}, "settings": cfg, "time": now(),
                "model_probe": store.meta("model_probe"), "last_quote_refresh": store.meta("last_quote_refresh")}

    @app.get("/api/accounts")
    async def accounts():
        return [service.account(a["id"]) for a in store.accounts()]

    @app.post("/api/accounts", status_code=201)
    async def create_account(data: AccountInput):
        a = store.save_account(data)
        return service.account(a["id"])

    @app.get("/api/accounts/{account_id}")
    async def account(account_id: str):
        return service.account(account_id)

    @app.put("/api/accounts/{account_id}")
    async def update_account(account_id: str, data: AccountInput):
        store.account(account_id)
        a = store.save_account(data, account_id)
        return service.account(a["id"])

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str):
        store.delete_account(account_id)
        return {"deleted": True}

    @app.post("/api/accounts/{account_id}/analyze")
    async def analyze(account_id: str, data: AnalyzeInput | None = None):
        target = None
        if data and data.symbol:
            try:
                target = symbol(data.symbol)
            except ValueError as e:
                raise HTTPException(422, str(e))
        return store.enqueue(account_id, f"manual_security:{target}" if target else "manual")

    @app.get("/api/accounts/{account_id}/analyses")
    async def analyses(account_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), summary: bool = False):
        store.account(account_id)
        return store.analyses(account_id, limit=limit, offset=offset, summary=summary)

    @app.get("/api/analyses/{analysis_id}")
    async def analysis_detail(analysis_id: str):
        return store.analysis(analysis_id)

    @app.get("/api/accounts/{account_id}/jobs")
    async def jobs(account_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), events: bool = False):
        store.account(account_id)
        return store.jobs(account_id, limit=limit, offset=offset, events=events, summary=not events)

    @app.get("/api/accounts/{account_id}/recommendations")
    async def recommendations(account_id: str, view: str = Query('all', pattern='^(active|history|triggered|needs_review|all)$'),
                              limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                              page: bool = False, envelope: bool = False):
        store.account(account_id)
        if page or envelope:
            return store.recommendations_page(account_id, limit, offset, view)
        return store.recommendations(account_id, limit, offset, view)

    @app.get('/api/accounts/{account_id}/recommendations/{recommendation_id}')
    async def recommendation(account_id: str, recommendation_id: str):
        store.account(account_id)
        return store.recommendation(account_id, recommendation_id)

    @app.post('/api/accounts/{account_id}/recommendations/{recommendation_id}/retry')
    async def retry_recommendation(account_id: str, recommendation_id: str):
        store.account(account_id)
        try:
            return store.retry_recommendation_review(account_id, recommendation_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/research")
    async def research_records(account_id: str | None = None, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), current_only: bool = True):
        if account_id:
            store.account(account_id)
        return store.research_records(account_id=account_id, limit=limit, offset=offset, current_only=current_only)

    @app.get("/api/demands")
    async def demands(account_id: str | None = None, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        if account_id:
            store.account(account_id)
        return store.demands(account_id=account_id, limit=limit, offset=offset)

    @app.get("/api/global-research")
    async def global_research(limit: int = Query(20, ge=1, le=100)):
        return [{k: v for k, v in run.items() if k != "result"} for run in store.global_runs(limit=limit)]

    @app.get("/api/global-research/{run_id}")
    async def global_research_detail(run_id: str):
        return store.global_run(run_id)

    @app.get("/api/global-research/{run_id}/events")
    async def global_research_events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        store.global_run(run_id)
        return store.events_since(run_id, after=after, limit=limit)

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str):
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        store.job(job_id)
        return store.events_since(job_id, after=after, limit=limit)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        store.job(job_id)
        store.set_job_state(job_id, "cancelled", "用户取消")
        task = service.active.get(job_id)
        if task:
            task.cancel()
        return store.job(job_id)

    @app.post("/api/jobs/{job_id}/retry")
    async def retry(job_id: str):
        job = store.job(job_id)
        return store.enqueue(job["account_id"], f"retry:{job['reason']}")

    @app.get("/api/universe")
    async def universe():
        return {**store.meta("universe", {"watchlist": [], "candidates": []}), "monitored": store.monitored(),
                'universe_version': store.universe_snapshot()['universe_version'],
                "quotes": list(service.quotes().values())}

    @app.put("/api/universe")
    async def save_universe(data: UniverseInput):
        store.save_universe(data)
        service.spawn(service.refresh_quotes())
        return await universe()

    @app.get("/api/securities")
    async def securities(q: str = ""):
        from .providers import MarketProvider
        query = q.strip().upper()
        cache = store.cache("securities")
        if re.fullmatch(r"(SH|SZ|BJ):[0-9]{6}", query) and query not in cache and mode == "live":
            value = await MarketProvider(store.settings()).security(query)
            if value.get("name"):
                store.put_cache("securities", query, value)
                cache[query] = value
        return [value for key, value in cache.items() if not query or query in key or query in str(value.get("name", "")).upper()][:100]

    @app.post("/api/quotes/refresh")
    async def refresh():
        return await service.refresh_quotes()

    @app.get("/api/materials")
    async def materials(account_id: str | None = None, query: str = Query("", max_length=500), limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), index: bool = True):
        if account_id:
            store.account(account_id)
        if index:
            return store.material_index(account_id, query, limit=limit, offset=offset, public_only=not bool(account_id))
        # Keep the legacy body-bearing response available for existing callers.
        return [m for m in store.materials(account_id, query) if store.material_accessible(m['id'], account_id)][offset:offset + limit]

    @app.get("/api/materials/{material_id}")
    async def material(material_id: str, account_id: str | None = None):
        if not store.material_accessible(material_id, account_id):
            raise KeyError('材料不存在或不属于当前范围')
        body = store.material(material_id)
        ref = store.resolve_result_references({'material_ids': [material_id]}, account_id)['references'][0]
        return {**body, **{k: ref[k] for k in ('origin_status', 'origin_references') if k in ref}}

    @app.get("/api/materials/{material_id}/range")
    async def material_range(material_id: str, start: int = Query(0, ge=0), length: int = Query(6000, ge=1, le=12000), account_id: str | None = None):
        if not store.material_accessible(material_id, account_id):
            raise KeyError('材料不存在或不属于当前范围')
        body = store.material_range(material_id, start=start, length=length)
        ref = store.resolve_result_references({'material_ids': [material_id]}, account_id)['references'][0]
        return {**body, **{k: ref[k] for k in ('origin_status', 'origin_references') if k in ref}}

    @app.get("/api/settings")
    async def settings():
        return store.settings()

    @app.put("/api/settings")
    async def update_settings(data: Settings):
        # Preserve deprecated values for audit; they are never exposed to runtime.
        store.set_meta("settings", {**store.meta('settings', {}), **data.model_dump()})
        store.set_meta("model_probe", None)
        service.resume_waiting_config()
        for a in store.accounts():
            store.request_analysis(a["id"], "configuration_changed")
        return store.settings()

    @app.post("/api/settings/probe")
    async def probe():
        from .model import probe_model, ModelError
        if mode == "demo":
            return {"ok": False, "message": "演示数据库不调用真实模型；请切换 live 服务进行能力探测"}
        try:
            result = await probe_model(store.settings())
            store.set_meta("model_probe", {**result, "checked_at": now()})
            return result
        except ModelError as e:
            return {"ok": False, "error": str(e)}

    @app.post('/api/settings/probe-native-search')
    async def probe_native():
        from .model import native_search_status, probe_native_search
        if mode == 'demo':
            return {'status': 'disabled', 'note': '演示模式不调用模型服务'}
        async with service.native_probe_lock:
            cached = native_search_status(store.settings(), store.meta('native_search_capability'))
            if cached.get('checked_at'):
                age = (datetime.now(TZ) - datetime.fromisoformat(cached['checked_at'])).total_seconds()
                if 0 <= age < 600:
                    return {**cached, 'cache_hit': True}
            result = await probe_native_search(store.settings())
            store.set_meta('native_search_capability', {**result, 'checked_at': now()})
            return result

    @app.post("/api/backup")
    async def backup():
        filename = f"{mode}-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"
        destination = ROOT / "data" / "backups" / filename
        store.backup(destination)
        return {"filename": filename, "path": str(destination), "message": "恢复请先停止服务，再使用 scripts/restore.py"}

    @app.post("/api/demo/seed")
    async def demo_seed():
        if mode != "demo":
            raise HTTPException(409, "演示样例只能写入 demo 数据库")
        if store.accounts():
            return {"created": False, "message": "已有账户，不重复创建演示样例"}
        sample_time = "2026-10-21T15:00:00+08:00"
        for s, n, kind, price, sector in [("SH:600519", "贵州茅台", "stock", "1300.00", "白酒"), ("SH:510300", "沪深300ETF", "etf", "4.000", "宽基 ETF（未穿透）"), ("SZ:000001", "平安银行", "stock", "11.00", "银行")]:
            store.put_cache("securities", s, {"symbol": s, "name": n, "kind": kind, "sector": sector, "source": "演示样例"})
            store.put_cache("quotes", s, {"symbol": s, "name": n, "price": price, "as_of": sample_time, "source": "演示固定样例（非真实历史行情）", "status": "demo", "fetched_at": now()})
        store.set_meta("universe", {"watchlist": ["SZ:000001"], "candidates": ["SH:510300", "SZ:000001"]})
        store.save_account(AccountInput(name="演示 · 稳健观察", cash="20000", preferences={"risk": "conservative"}, holdings=[{"symbol": "SH:510300", "quantity": "10000", "cost_price": "3.8"}, {"symbol": "SH:600519", "quantity": "100"}]))
        store.save_account(AccountInput(name="演示 · 长期组合", cash=None, holdings=[{"symbol": "SH:510300", "quantity": "3000"}]))
        return {"created": True, "message": "样例仅用于界面和计算展示，不执行模拟 Agent 研究"}

    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    return app


app = create_app()
