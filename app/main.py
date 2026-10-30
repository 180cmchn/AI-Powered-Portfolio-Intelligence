from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import mimetypes
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT, db_path
from .domain import AccountInput, UniverseInput, TZ, now, symbol
from .store import Conflict, Store
from .service import Service
from .calendar import market_state
from .symbols import SymbolResolver, ResolveInput, SymbolResolutionError


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_protocol: str = "responses"
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = Field(default="", max_length=200)
    model_key_env: str = "MODEL_API_KEY"
    model_allow_no_key: bool = False
    model_request_timeout_seconds: int = Field(default=90, ge=5, le=600)
    model_responses_stream: bool = True
    model_native_search: bool = True
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    synthesis_request_timeout_seconds: int = Field(default=120, ge=5, le=600)
    synthesis_max_calls: int = Field(default=4, ge=1, le=8)
    synthesis_checkpoint_max_age_seconds: int = Field(default=1800, ge=60, le=86400)
    chat_use_max_completion_tokens: bool = False
    model_retries: int = Field(default=1, ge=0, le=3)
    max_total_agents: int = Field(default=16, ge=1, le=64)
    max_context_chars: int = Field(default=240000, ge=10000, le=2000000)
    quote_provider: str = "eastmoney"
    quote_fallback_provider: str = "sina"
    max_concurrent_agents: int = Field(default=3, ge=1, le=8)
    max_model_calls: int = Field(default=40, ge=2, le=300)
    agent_timeout_seconds: int = Field(default=120, ge=5, le=1800)
    analysis_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_agent_turns: int = Field(default=12, ge=2, le=60)
    max_output_tokens: int = Field(default=4000, ge=256, le=32000)
    quote_interval_seconds: int = Field(default=60, ge=30, le=3600)
    scheduled_research_enabled: bool = True
    market_refresh_enabled: bool = True
    forecast_evaluation_enabled: bool = True
    forecast_evaluation_batch_size: int = Field(default=30, ge=1, le=500)
    forecast_review_batch_size: int = Field(default=6, ge=1, le=100)
    forecast_evaluation_concurrency: int = Field(default=4, ge=1, le=16)
    forecast_request_timeout_seconds: int = Field(default=20, ge=1, le=120)
    forecast_cycle_timeout_seconds: int = Field(default=45, ge=1, le=300)
    forecast_review_interval_days: int = Field(default=7, ge=1, le=365)
    forecast_unassessable_review_days: int = Field(default=14, ge=1, le=365)
    forecast_baseline_batch_size: int = Field(default=40, ge=1, le=500)
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

    @field_validator("quote_fallback_provider")
    @classmethod
    def quote_fallback(cls, v):
        if v not in ("none", "sina"):
            raise ValueError("备用行情服务应为 sina 或 none")
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


class TaskOrigin(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: str | None = Field(default=None, max_length=80)
    action: str | None = Field(default=None, max_length=120)


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str | None = None
    operation_id: str | None = Field(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    origin: TaskOrigin | None = None
    research_scope: Literal['securities', 'cash_only'] | None = None


def create_app(path=None, mode=None, worker=True, runner=None):
    mode = mode or os.getenv("APP_MODE", "live")
    if mode not in ("live", "demo"):
        raise ValueError("APP_MODE 必须为 live 或 demo")
    from .runtime_lock import DatabaseLease
    target = Path(path or db_path(mode)).resolve()
    # The old order migrated the file before detecting another worker. Keep
    # ownership for the entire worker lifetime, including initialization.
    lock = DatabaseLease(target) if worker else None
    try:
        store = Store(target, mode)
        service = Service(store, runner)
    except BaseException:
        if lock:
            lock.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        try:
            if worker:
                await service.start()
            yield
        finally:
            if worker:
                try:
                    await service.stop()
                finally:
                    lock.close()

    app = FastAPI(title="观澜 · 本地投资研究台", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.service = store, service
    resolver = SymbolResolver(store)
    app.state.symbol_resolver = resolver
    from .runtime_version import startup_version, static_snapshot, REQUIRED_ROUTES
    assets = static_snapshot()
    app.state.runtime_version = startup_version(store, assets)
    app.state.database_lease = lock
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
        response.headers['X-Guanlan-API-Contract'] = str(app.state.runtime_version['api_contract'])
        response.headers['X-Guanlan-Instance'] = app.state.runtime_version['instance_id']
        return response

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Pydantic includes submitted input in its default errors. Keys are write-only.
        return JSONResponse({'detail': [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    @app.exception_handler(SymbolResolutionError)
    async def symbol_resolution_handler(request, exc):
        return JSONResponse({'detail': exc.detail}, status_code=422)

    from .sixth_api import register_sixth_routes
    register_sixth_routes(app, store, service)

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/status")
    async def status(account_id: str | None = None):
        from .model import native_search_status, native_search_fingerprint
        cfg, binding_error = service.manager_config(account_id=account_id, credentials=False)
        global_cfg, global_binding_error = service.manager_config(credentials=False) if account_id else (cfg, binding_error)
        from .config import reasoning_effort
        configuration_errors = [binding_error] if binding_error else []
        try:
            reasoning_effort(cfg)
        except ValueError as exc:
            configuration_errors.append(str(exc))
        p = urlsplit(cfg["model_base_url"])
        key_ready = bool(os.getenv(cfg["model_key_env"])) or (cfg.get("model_allow_no_key", False) and p.hostname in ("localhost", "127.0.0.1", "::1"))
        probe = store.meta("model_probe")
        global_probe = probe
        if global_probe and global_probe.get('fingerprint') != native_search_fingerprint(global_cfg):
            global_probe = {"ok": False, "status": "unverified", "note": "全局主 Agent 配置已变化，请重新明确检测。"}
        if probe and probe.get('fingerprint') != native_search_fingerprint(cfg):
            probe = {"ok": False, "status": "unverified", "note": "配置已变化，历史协议探测不适用于当前模型、端点或思考强度。"}
        return {"mode": mode, "timezone": "Asia/Shanghai", "model_configured": bool(cfg["model_name"] and not configuration_errors),
                "configuration_errors": configuration_errors,
                "configuration_readiness": store.configuration_readiness(account_id),
                "native_search": native_search_status(cfg, store.meta('native_search_capability')),
                "global_native_search": native_search_status(global_cfg, store.meta('native_search_capability')),
                "global_model_configured": bool(global_cfg['model_name'] and not global_binding_error),
                "global_model_probe": global_probe,
                "calendar": market_state(calendar_path=store.settings().get('calendar_path')), "runtime_version": app.state.runtime_version,
                "worker": {"running": service.running, "active": len(service.active), "last_error": service.last_error,
                "last_resume": store.meta("last_resume")}, "settings": store.settings(), "time": now(),
                "global_manager": {k: v for k, v in global_cfg.items() if k in ("model_name", "reasoning_effort", "provider_id", "team_id")},
                "effective_manager": {k: v for k, v in cfg.items() if k in ("model_name", "reasoning_effort", "provider_id", "team_id", "account_id")},
                "model_probe": probe, "running_model_configs": store.running_model_configs(),
                "last_quote_refresh": store.meta("last_quote_refresh")}

    @app.get('/api/health')
    async def health():
        registered = {(route.path, method.lower()) for route in app.routes for method in getattr(route, 'methods', [])}
        missing = [f'{method.upper()} {path}' for path, method in REQUIRED_ROUTES.items() if (path, method) not in registered]
        return {'ready': not missing and (service.running or not worker), 'mode': mode,
                'worker_running': service.running, 'runtime_version': app.state.runtime_version,
                'required_routes': REQUIRED_ROUTES, 'missing_routes': missing}

    @app.get("/api/accounts")
    async def accounts():
        return [service.account(a["id"]) for a in store.accounts()]

    @app.post("/api/accounts", status_code=201)
    async def create_account(data: AccountInput, x_operation_id: str | None = Header(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')):
        data, resolution = await resolver.account(data)
        a = store.save_account_operation(data, operation_id=x_operation_id)
        return {**service.account(a["id"]), 'symbol_resolution': resolution, 'save_receipt':a['save_receipt']}

    @app.get('/api/account-operations/{operation_id}')
    async def account_operation(operation_id: str):
        return store.account_operation(operation_id)

    @app.get("/api/accounts/{account_id}")
    async def account(account_id: str):
        return service.account(account_id)

    @app.put("/api/accounts/{account_id}")
    async def update_account(account_id: str, data: AccountInput, x_operation_id: str | None = Header(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')):
        store.account(account_id)
        data, resolution = await resolver.account(data)
        a = store.save_account_operation(data, account_id, x_operation_id)
        return {**service.account(a["id"]), 'symbol_resolution': resolution, 'save_receipt':a['save_receipt']}

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str):
        store.delete_account(account_id)
        return {"deleted": True}

    @app.post("/api/accounts/{account_id}/analyze")
    async def analyze(account_id: str, data: AnalyzeInput | None = None):
        target = None
        if data and data.symbol:
            resolved = await resolver.resolve([data.symbol])
            if not resolved['ok']:
                raise SymbolResolutionError(resolved['items'])
            target = resolved['symbols'][0]
        return store.submit_task(account_id, target=target, operation_id=data.operation_id if data else None,
            origin=data.origin.model_dump() if data and data.origin else None,
            research_scope=data.research_scope if data else None)

    @app.get('/api/task-operations/{operation_id}')
    async def task_operation(operation_id: str, account_id: str):
        return store.task_operation(account_id, operation_id)

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
        return store.global_runs(limit=limit, summary=True)

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
    async def retry(job_id: str, data: AnalyzeInput | None = None):
        job = store.job(job_id)
        return store.submit_task(job['account_id'], target=job.get('target_security'), parent_job_id=job_id,
            operation_id=data.operation_id if data else None,
            origin=data.origin.model_dump() if data and data.origin else None,
            research_scope=data.research_scope if data and data.research_scope is not None else job.get('research_scope'))

    @app.get("/api/universe")
    async def universe():
        return {**store.meta("universe", {"watchlist": [], "candidates": []}), "monitored": store.monitored(),
                'universe_version': store.universe_snapshot()['universe_version'],
                "quotes": list(service.quotes().values())}

    @app.put("/api/universe")
    async def save_universe(data: UniverseInput):
        data, resolution = await resolver.universe(data)
        store.save_universe(data)
        service.spawn(service.refresh_quotes())
        return {**await universe(), 'symbol_resolution': resolution}

    @app.post('/api/securities/resolve')
    async def resolve_securities(data: ResolveInput):
        return await resolver.resolve(data.symbols, data.context)

    @app.get("/api/securities")
    async def securities(q: str = ""):
        query = q.strip().upper()
        if re.fullmatch(r'(?:(?:SH|SZ|BJ):)?[0-9]{6}', query):
            result = await resolver.resolve([query])
            return [item for item in result['items'] if item['status'] == 'resolved']
        cache = store.cache("securities")
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
        # Missing fields are not explicit choices. In particular, do not write
        # medium over REASONING_EFFORT or an earlier explicitly saved value.
        store.set_meta("settings", {**store.meta('settings', {}), **data.model_dump(exclude_unset=True)})
        store.set_meta("model_probe", None)
        service.resume_waiting_config()
        for a in store.accounts():
            store.request_analysis(a["id"], "configuration_changed")
        return store.settings()

    @app.post("/api/settings/probe")
    async def probe():
        from .model import probe_model, ModelError, native_search_fingerprint
        if mode == "demo":
            return {"ok": False, "message": "演示数据库不调用真实模型；请切换 live 服务进行能力探测"}
        config = store.settings()
        try:
            config = service.manager_config()
            result = await probe_model(config)
            store.set_meta("model_probe", {**result, "checked_at": now()})
            return result
        except ModelError as e:
            result = getattr(e, 'probe_diagnostics', {"ok": False, "error": str(e),
                "fingerprint": native_search_fingerprint(config), "requests": []})
            store.set_meta('model_probe', {**result, 'checked_at': now()})
            return result

    @app.post('/api/settings/probe-native-search')
    async def probe_native():
        from .model import native_search_status, probe_native_search, ModelConfigurationError
        if mode == 'demo':
            return {'status': 'disabled', 'note': '演示模式不调用模型服务'}
        async with service.native_probe_lock:
            try:
                config = service.manager_config()
            except ModelConfigurationError as exc:
                return {'status': 'configuration_error', 'note': str(exc), 'checked_at': now()}
            cached = native_search_status(config, store.meta('native_search_capability'))
            if cached.get('checked_at'):
                age = (datetime.now(TZ) - datetime.fromisoformat(cached['checked_at'])).total_seconds()
                if 0 <= age < 600:
                    return {**cached, 'cache_hit': True}
            result = await probe_native_search(config)
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

    @app.get('/static/{asset:path}')
    async def static_asset(asset: str):
        # A running backend always serves the assets captured with that backend.
        # Disk updates take effect only on an explicit safe restart.
        if asset not in assets:
            raise HTTPException(404, '静态资源不存在')
        return Response(assets[asset], media_type=mimetypes.guess_type(asset)[0] or 'application/octet-stream')

    @app.get("/")
    async def index():
        return Response(assets['index.html'], media_type='text/html')

    return app


class _LazyApplication:
    """ASGI compatibility without creating/migrating a database on import."""
    def __init__(self):
        self._application = None

    async def __call__(self, scope, receive, send):
        if self._application is None:
            self._application = create_app()
        await self._application(scope, receive, send)


app = _LazyApplication()
