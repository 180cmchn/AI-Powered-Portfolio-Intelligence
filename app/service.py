from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
import hashlib
import os
import time
from urllib.parse import urlsplit

from . import providers
from .calendar import market_state
from .config import model_config_summary
from .domain import TZ, metrics, now, quote_display_identity
from .store import Store, dumps
from .materials import index_of, material_text

log = logging.getLogger("guanlan")


class ResearchTools:
    def __init__(self, store: Store, job: dict, config: dict, context=None):
        self.store, self.job, self.config = store, job, {**config, '_research_failure_memory': {}}
        self.context = context

    async def list_forecast_targets(self, limit=30, offset=0):
        return self.store.forecast_job_context(self.job['id'], limit=limit, offset=offset)

    async def save_checkpoint(self, checkpoint):
        if self.context is not None:
            self.store.save_synthesis_checkpoint(self.job, checkpoint, self.context)

    def _track_public_material(self, material):
        if self.job.get('account_id') or self.context is None:
            return
        with self.store.connect() as db:
            row = db.execute('''SELECT d.anchor_material_id FROM material_document_versions d
                JOIN material_notifications n ON n.material_id=d.anchor_material_id
                WHERE d.id=? AND n.state='pending' ''', (material.get('document_version_id'),)).fetchone()
        if row:
            self.store.bind_public_information(self.job['id'], [row[0]])
            pending = self.context.setdefault('pending_information', [])
            if row[0] not in {m['id'] for m in pending}:
                pending.append(index_of(self.store.material(row[0])))

    async def record_native_search(self, agent_id, events, sources, annotations, *, model_config=None):
        from .model import native_search_fingerprint
        from .native_sources import normalize_native_source
        if any(event.get('status') == 'completed' for event in events):
            self.store.set_meta('native_search_capability', {
                'status': 'observed', 'checked_at': now(),
                'fingerprint': native_search_fingerprint(model_config or self.config),
                'evidence': {'event_types': list(dict.fromkeys(e.get('type') for e in events)),
                             'event_count': len(events), 'source_count': len(sources)},
                'note': '服务返回了原生联网调用项；未返回的工具用量与访问范围保持未知'})
        saved = []
        for raw in sources:
            item = normalize_native_source(raw)
            if item is None:
                continue
            snippet = item.get('visible_excerpt', '')
            # A hosted citation never proves a local download or full document hash.
            material = self.store.save_material({
                'url': item['url'], 'source_title': item.get('title'), 'scope': 'public',
                'content': snippet, 'source': 'model_native_search', 'classification': 'unclassified',
                'classification_origin': 'unclassified_default', 'source_type': 'native_search_source',
                'access': 'search_excerpt' if snippet else 'source_link',
                'representation': 'native_search_source', 'fetched_at': now(),
                'truncated': item.get('excerpt_truncated', False), 'returned_chars': len(snippet),
                'returned_range': {'start': 0, 'end': len(snippet), 'unit': 'character',
                                   'basis': 'merged_visible_search_text'},
                'native_search': {'agent_id': agent_id, 'visible_metadata': item,
                                  'annotations': [a for a in annotations if a.get('url') == item['url']],
                                  'local_document_downloaded': False}},
                self.job['id'], self.job.get('account_id'), notify=not bool(self.job.get('account_id')))
            saved.append(index_of(material))
            self._track_public_material(material)
        return saved

    async def save_material(self, material: dict):
        # A model-authored extraction may contain account context. Keep it scoped;
        # raw public pages/search results alone enter the cross-account cache.
        scope = "account" if self.job.get("account_id") else "public"
        if material.get("id") and not self.store.material_accessible(material["id"], self.job.get("account_id")):
            raise KeyError("材料不属于当前账户或公开研究范围")
        saved = self.store.save_material({**material, "scope": scope, "representation": "agent_extraction",
            'source_type': 'model_extraction', 'classification_origin': 'agent_selected' if material.get('classification') or material.get('category') else 'unclassified_default'}, self.job["id"], self.job.get("account_id"))
        return index_of(saved)

    async def search(self, query: str):
        # Legacy callable only. No credentials are sent to independent search APIs.
        return {'available': False, 'error': '独立搜索工具已退出；请使用当前模型会话的原生联网工具'}

    async def read(self, url: str, **options):
        result = await providers.read_url(url, {**self.config, '_material_observer': self.store,
                    "read_max_chars": min(self.config.get("read_max_chars", 12000), 12000)}, **options)
        material = self.store.save_material({**result, "scope": "public", "source_title": result.get("title"),
                                            "classification": "unclassified", 'classification_origin': 'unclassified_default',
                                            'source_type': 'external_original'}, self.job["id"], self.job.get("account_id"),
                                            observation_id=result.get('_observation_id'), notify=not bool(self.job.get('account_id')))
        self._track_public_material(material)
        return {**{k: v for k, v in result.items() if not k.startswith('_')}, "material_id": material["id"], "material_identity": {
            k: material[k] for k in ("document_version_id", "document_version", "representation_kind", "document_identity_kind", "read_completeness")}}

    async def list_materials(self, query: str = "", limit=20, offset=0):
        return self.store.material_index(self.job.get("account_id"), query, min(limit, 50), offset,
                                         public_only=not bool(self.job.get("account_id")))

    async def resolve_references(self, result):
        return self.store.resolve_result_references(result, self.job.get('account_id'))

    async def list_recommendations(self, view='active', limit=30, offset=0):
        if not self.job.get('account_id'):
            return {'items': [], 'total': 0, 'has_more': False, 'next_offset': None}
        return self.store.recommendations_page(self.job['account_id'], limit, offset, view)

    async def list_business_demands(self, limit=25, offset=0):
        if not self.job.get('account_id'):
            return {'items': [], 'total': 0, 'has_more': False, 'next_offset': None}
        return self.store.task_demands(self.job['id'], self.job['account_id'], limit, offset)

    async def get_recommendation(self, recommendation_id):
        if not self.job.get('account_id'):
            raise KeyError('公共研究没有账户建议访问权限')
        return self.store.recommendation(self.job['account_id'], recommendation_id)

    async def get_material(self, material_id):
        return await self.read_material_range(material_id, length=4000)

    async def read_material_range(self, material_id, start=0, length=8000):
        if not self.store.material_accessible(material_id, self.job.get("account_id")):
            raise KeyError("材料不属于当前账户或公开研究范围")
        # Only materials actually accessed become evidence for this task.
        material = self.store.material(material_id)
        self.store.save_material(material, self.job["id"], self.job.get("account_id"))
        return self.store.material_range(material_id, start, length)

    async def search_materials(self, query, limit=20, offset=0):
        rows = await self.list_materials(query, limit, offset)
        for row in rows:
            text = material_text(self.store.material(row["id"]))
            at = text.lower().find(query.lower())
            row.update(match_offset=max(0, at), match_excerpt=text[max(0, at-100):max(0, at)+500])
        return rows

    async def pdf_metadata(self, url, **options):
        result = await providers.pdf_metadata(url, {**self.config, '_material_observer': self.store}, **options)
        return {k: v for k, v in result.items() if not k.startswith('_')}

    async def search_pdf(self, url, query, **options):
        result = await providers.search_pdf(url, query, {**self.config, '_material_observer': self.store}, **options)
        saved = self.store.save_material({**result, "scope": "public", "access": "failed" if result.get('access') == 'failed' else "pdf_search",
                                         "content": dumps({k: v for k, v in result.items() if not k.startswith('_')}),
                                         'classification': 'unclassified', 'classification_origin': 'unclassified_default',
                                         'source_type': 'external_original'}, self.job["id"], self.job.get("account_id"),
                                         observation_id=result.get('_observation_id'), notify=not bool(self.job.get('account_id')))
        self._track_public_material(saved)
        return {**{k: v for k, v in result.items() if not k.startswith('_')}, "material_id": saved["id"]}

    async def _market_data(self, method, **kwargs):
        result = await getattr(providers.MarketProvider(self.config), method)(**kwargs)
        if result.get('status') == 'unavailable':
            from .research_cache import reusable_history
            cached = reusable_history(self.store, method, kwargs, self.config)
            if cached:
                material, reuse = cached
                dates = {key: datetime.strptime(reuse['cache_requested_range'][key], '%Y%m%d').date().isoformat() for key in ('start', 'end')}
                view = await self.query_market_rows(material['id'], **dates, limit=min(30, int(kwargs.get('limit', 30))))
                return {**view, **reuse, 'refresh_failure': {k: result.get(k) for k in ('error', 'error_kind', 'failure_stage', 'next_action')}}
        saved = self.store.save_material({"scope": "public", "content": dumps(result), "source_title": method,
                                         "source_identifier": 'market:' + dumps([method, result.get('source'),
                                             result.get('symbol') or result.get('benchmark') or result.get('sector_id'),
                                             result.get('interval'), result.get('adjustment'), result.get('requested_range')]),
                                         "symbol": result.get('symbol') or result.get('benchmark') or result.get('sector_id'),
                                         "source": result.get("source"), "classification": "source_data", 'source_type': 'external_original',
                                         "fetched_at": result.get('fetched_at'), "truncated": result.get('truncated', False),
                                         "published_at": result.get("as_of"), "access": "structured_data"}, self.job["id"], self.job.get("account_id"))
        if method in ('history', 'benchmark_history', 'sector_history'):
            # Keep the entire returned snapshot. Deliver a recent, navigable row
            # view instead of an arbitrary JSON prefix from its oldest dates.
            return await self.query_market_rows(saved['id'], limit=min(30, int(kwargs.get('limit', 30))))
        if len(dumps(result)) > 16000:
            return {"material_id": saved["id"], "source": result.get("source"), "status": result.get("status"),
                    "note": "数据已保存，按材料字符范围逐段读取", **self.store.material_range(saved["id"], length=10000)}
        return {**result, "material_id": saved["id"]}

    def _market_material(self, material_id):
        if not self.store.material_accessible(material_id, self.job.get('account_id')):
            raise KeyError('材料不属于当前账户或公开研究范围')
        material = self.store.material(material_id)
        self.store.save_material(material, self.job['id'], self.job.get('account_id'))
        return material

    async def query_market_rows(self, material_id, start=None, end=None, fields=None, limit=30, offset=0, order='desc', symbol=None):
        from .market_materials import query_rows
        return query_rows(self._market_material(material_id), start=start, end=end, fields=fields, limit=limit,
                          offset=offset, order=order, symbol=symbol,
                          max_chars=self.config.get('max_tool_result_chars', 6000))

    async def market_indicators(self, material_id, start=None, end=None, window=20, benchmark_material_id=None):
        from .market_materials import indicators
        return indicators(self._market_material(material_id), start=start, end=end, window=window,
                          benchmark=self._market_material(benchmark_material_id) if benchmark_material_id else None,
                          max_chars=self.config.get('max_tool_result_chars', 6000))

    async def history(self, symbol, **options):
        return await self._market_data("history", symbol=symbol, **options)

    async def sectors(self, **options):
        return await self._market_data("sectors", **options)

    async def sector_members(self, sector_id, **options):
        return await self._market_data("sector_members", sector_id=sector_id, **options)

    async def sector_history(self, sector_id, **options):
        return await self._market_data("sector_history", sector_id=sector_id, **options)

    async def benchmark_history(self, **options):
        return await self._market_data("benchmark_history", **options)

    async def fundamentals(self, symbol, **options):
        return await self._market_data("fundamentals", symbol=symbol, **options)

    async def calculate(self, operation: str, values: list):
        try:
            nums = [Decimal(str(v)) for v in values]
            if len(nums) > 1000 or not nums or any(not v.is_finite() or abs(v) > Decimal("1e30") for v in nums):
                raise ValueError("计算输入为空、过长或超出数值范围")
            if operation == "sum":
                result = sum(nums, Decimal(0))
            elif operation in ("multiply", "product"):
                result = Decimal(1)
                for v in nums:
                    result *= v
            elif operation in ("divide", "ratio", "percent") and len(nums) == 2:
                result = nums[0] / nums[1] * (100 if operation == "percent" else 1)
            elif operation in ("subtract", "difference") and len(nums) == 2:
                result = nums[0] - nums[1]
            else:
                raise ValueError("支持 sum、multiply、divide、percent、subtract")
            return {"operation": operation, "inputs": [str(v) for v in nums], "result": str(result), "classification": "calculation"}
        except (ArithmeticError, ValueError) as e:
            return {"error": str(e)[:200]}


class Service:
    def __init__(self, store: Store, runner=None):
        self.store, self.runner = store, runner
        self.active: dict[str, asyncio.Task] = {}
        self.loop_task = None
        self.refresh_lock = asyncio.Lock()
        self.native_probe_lock = asyncio.Lock()
        self.last_tick = 0.0
        self.last_wall_tick = 0.0
        self.last_error = None
        self.running = False
        self.maintenance: set[asyncio.Task] = set()
        self.global_task = None
        self.news_task = None
        self.quote_task = None
        self.last_recommendation_check = 0.0
        self.forecast_task = None
        self.last_forecast_tick = 0.0

    def record_model_event(self, job_id, event):
        self.store.event(job_id, event)
        self.store.observe_forecast_model_event(job_id, event)
        payload = event.get('payload') or {}
        capability = payload.get('capability') or payload.get('native_search')
        if isinstance(capability, dict) and capability.get('fingerprint') and capability.get('status'):
            self.store.set_meta('native_search_capability', capability)

    def quotes(self):
        quotes = self.store.cache("quotes")
        stamp = datetime.now(TZ)
        for quote in quotes.values():
            if quote.get('status') in ('ok', 'closed', 'stale') and quote.get('as_of'):
                quote.update(providers.quote_time_state(quote['as_of'], self.store.settings(), stamp=stamp,
                    source_precision=quote.get('as_of_precision', 'timestamp')))
            elif quote.get('as_of') and quote.get('as_of_precision') != 'date':
                try:
                    quote['delay_seconds'] = max(0, int((stamp - datetime.fromisoformat(quote['as_of'])).total_seconds()))
                except (ValueError, TypeError):
                    quote['delay_seconds'] = None
        return quotes

    def account(self, account_id):
        account = self.store.account(account_id)
        account["metrics"] = metrics(account, self.quotes(), self.store.cache("securities"))
        return account

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.maintenance.add(task)
        def done(completed):
            self.maintenance.discard(completed)
            if not completed.cancelled() and completed.exception():
                self.last_error = type(completed.exception()).__name__
                log.error("maintenance failed: %s", self.last_error)
        task.add_done_callback(done)
        return task

    async def refresh_quotes(self, symbols=None, force=False):
        async with self.refresh_lock:
            if self.store.mode == "demo":
                return {"mode": "demo", "updated": 0, "message": "演示使用固定历史快照，不连接行情服务"}
            provider, cfg = providers.MarketProvider(self.store.settings()), self.store.settings()
            symbols = symbols if symbols is not None else self.store.monitored()
            semaphore = asyncio.Semaphore(4)
            old = self.quotes()

            async def refresh(s):
                async with semaphore:
                    try:
                        previous = old.get(s, {})
                        fetched = previous.get("last_attempt_at") or previous.get("fetched_at") or previous.get("as_of")
                        try:
                            reusable = fetched and 0 <= (datetime.now(TZ) - datetime.fromisoformat(fetched)).total_seconds() < cfg["quote_cache_seconds"]
                        except (TypeError, ValueError):
                            reusable = False
                        if reusable and not force:
                            return {**previous, "cache_hit": True}
                        q = await provider.quote(s)
                        if q.get("price") is None and previous.get("price") is not None:
                            q = {**previous, "status": "error_cached", "last_error": q.get("error", "行情更新失败"), "last_attempt_at": now()}
                        q = quote_display_identity(q, self.store.cache("securities").get(s, {}))
                        self.store.put_cache("quotes", s, q)
                        if q.get("name"):
                            existing = self.store.cache("securities").get(s, {})
                            if not existing or not existing.get("kind"):
                                existing = await provider.security(s)
                            self.store.put_cache("securities", s, {**existing, "symbol": s, "name": q["name"]})
                        if q.get("price") and previous.get("price") and q.get('status') == 'ok' and q.get('fresh_for_triggers') is not False and market_state(calendar_path=cfg.get('calendar_path'))['quote_trigger_eligible']:
                            baseline = self.store.meta(f"price_baseline:{s}", previous["price"])
                            p, b = Decimal(q["price"]), Decimal(baseline)
                            if b and abs(p / b - 1) * 100 >= Decimal(str(cfg["price_trigger_pct"])):
                                self.store.set_meta(f"price_baseline:{s}", q["price"])
                                for acc in self.store.accounts():
                                    if any(h["symbol"] == s for h in acc["holdings"]):
                                        # Event deduplication and one-minute debounce avoid price-tick LLM loops.
                                        bucket = int(time.time() // 900)
                                        self.store.request_analysis(acc["id"], "price_change", f"price:{acc['id']}:{s}:{bucket}", reason=f"price_change:{s}", delay=2)
                            elif baseline == previous["price"]:
                                self.store.set_meta(f"price_baseline:{s}", baseline)
                        return q
                    except Exception as e:
                        # Never roll back account inputs because a public source is unavailable.
                        failed = {**old.get(s, {}), "symbol": s, "status": "error_cached" if old.get(s, {}).get("price") else "error", "error": type(e).__name__, "last_attempt_at": now()}
                        self.store.put_cache("quotes", s, failed)
                        return failed
            results = await asyncio.gather(*(refresh(s) for s in symbols))
            self.store.set_meta("last_quote_refresh", now())
            self.store.review_recommendations(self.quotes())
            return {"mode": self.store.mode, "updated": sum(q.get("price") is not None for q in results), "quotes": results}

    async def execute(self, job):
        from .agents import run_research
        from .model import ModelClient, ModelConfigurationError
        cfg = job.get("config_snapshot") or self.store.settings()
        started = time.monotonic()
        try:
            # Bind planned identity before resolving secrets, clients or prices.
            self.store.prepare_exploratory_forecast_job(job['id'], calendar_path=cfg.get('calendar_path'))
            self.store.ensure_forecast_attempt(job['id'])
            account = self.store.snapshot(job["account_id"], job["input_version"])
            universe = job.get('universe') or self.store.meta("universe", {"watchlist": [], "candidates": []})
            target = job["reason"].split("manual_security:", 1)[-1].split(";", 1)[0] if "manual_security:" in job["reason"] else None
            target_symbols = list(dict.fromkeys([*[h["symbol"] for h in account["holdings"]], *universe["candidates"], *([target] if target else [])]))
            target_symbols = list(dict.fromkeys([*target_symbols, *self.store.recommendation_symbols(account['id'])]))
            if not target_symbols and job.get('research_scope') != 'cash_only' and self.store.mode == 'live' and self.runner is None:
                self.store.set_job_state(job['id'], 'waiting_scope', '没有已保存持仓、候选或明确证券目标；请选择现金与偏好分析，或先添加研究证券')
                return
            if self.store.mode == "live" and self.runner is None:
                try:
                    if cfg.get('_agent_team'):
                        try:
                            cfg = {**self.store.runtime_role_config(cfg['_agent_team'], 'manager', cfg), '_agent_team': cfg['_agent_team']}
                        except ValueError as exc:
                            raise ModelConfigurationError(str(exc)) from exc
                    client = ModelClient(cfg)
                    await client.aclose()
                except ModelConfigurationError:
                    self.spawn(self.refresh_quotes(target_symbols))
                    raise
            self.store.task_stage(job['id'], 'fetching')
            try:
                await asyncio.wait_for(self.refresh_quotes(target_symbols), timeout=min(cfg["quote_refresh_timeout_seconds"], cfg["analysis_timeout_seconds"]))
            except TimeoutError:
                self.store.event(job["id"], {"type": "quote_timeout", "message": "研究前行情刷新超时，使用已保存且注明时间的缓存继续研究"})
            if self.store.job(job["id"])["state"] != "running":
                return
            values = metrics(account, self.quotes(), self.store.cache("securities"))
            quote_times = [p["quote"].get("as_of") for p in values["positions"] if p["quote"].get("as_of")]
            data_as_of = min(quote_times) if quote_times else None
            suggestion_context = self.store.recommendation_context(account['id'], job['id'], target=target)
            demand_page = self.store.task_demands(job['id'], account['id'])
            scope = {'universe_version': job.get('universe_version')} if job.get('universe_version') else self.store.universe_snapshot()
            context = {"account": account, "metrics": values, "candidates": universe["candidates"],
                       'research_scope': job.get('research_scope', 'securities'),
                       "candidate_quotes": {s: q for s, q in self.quotes().items() if s in universe["candidates"]},
                       "watchlist": universe["watchlist"], "reason": job["reason"], "data_as_of": data_as_of,
                       "previous_analysis": self.store.previous_analysis(account["id"]),
                       "target_security": {"symbol": target, "quote": self.quotes().get(target), "metadata": self.store.cache("securities").get(target)} if target else None,
                       "job_id": job["id"], "input_version": job["input_version"],
                       "model_config": model_config_summary(cfg),
                       "calendar": market_state(calendar_path=cfg.get("calendar_path")), "available_public_material_count": self.store.material_count(),
                       "shared_research": [],
                       "universe_version": scope['universe_version'],
                       "suggestions_to_review": suggestion_context['items'],
                       "suggestions_coverage": {k: v for k, v in suggestion_context.items() if k != 'items'},
                       "suggestion_index": {k: v for k, v in suggestion_context.items() if k != 'items'},
                       "business_demands": demand_page['items'],
                       "business_demand_coverage": {k: v for k, v in demand_page.items() if k != 'items'},
                       "generated_at": now(), "timezone": "Asia/Shanghai"}
            context['team_id'] = (cfg.get('_agent_team') or {}).get('team_id')
            context['forecast_batch'] = self.store.forecast_job_context(job['id'])
            context['_research_input_guard'] = self.store.research_guard(context)
            if self.store.mode == "demo":
                result = {"report": f"演示模式 · 本地数值摘要\n\n账户：{account['name']} · 输入版本 v{account['version']}\n\n已知价格持仓市值：¥{values['known_priced_value']}。\n\n" + "\n\n".join(values["notes"]) + "\n\n这是固定样例数据和代码计算结果。未调用模型、搜索或多 Agent，不构成真实投资研究或买卖建议。",
                          "metrics": values, "demo": True, "model_calls": 0}
                self.store.event(job["id"], {"type": "demo_summary", "message": "仅执行本地计算；没有模拟模型或 Agent 发言"})
            else:
                async def emit(event):
                    self.record_model_event(job["id"], event)
                research = self.runner or run_research
                self.store.task_stage(job['id'], 'researching')
                checkpoint = self.store.reusable_synthesis_checkpoint(job, context)
                if checkpoint:
                    context['synthesis_checkpoint'] = checkpoint
                    self.store.event(job['id'], {'type': 'synthesis_resuming', 'message': '正在复用已保存的研究成果重试汇总',
                        'checkpoint_job_id': checkpoint['checkpoint_job_id'], 'attempt': checkpoint['resume_attempt']})
                result = await asyncio.wait_for(research(context, cfg, ResearchTools(self.store, job, cfg, context), emit), timeout=max(0.01, cfg["analysis_timeout_seconds"] - (time.monotonic() - started)))
                result["metrics"] = values
            result.update(input_version=job["input_version"], data_as_of=data_as_of,
                          model_config=model_config_summary(cfg),
                          synthesis_checkpoint_job_id=(context.get('synthesis_checkpoint') or {}).get('checkpoint_job_id'),
                          account_id=account["id"], job_id=job["id"], mode=self.store.mode,
                          universe_version=scope['universe_version'],
                          shared_research_used=[{'id': r['id'], 'universe_version': r.get('universe_version'),
                              'scope': r.get('scope'), 'applicability': r.get('applicability')} for r in context['shared_research']])
            self.store.task_stage(job['id'], 'publishing')
            valid = self.store.publish(job["id"], result, data_as_of)
            self.store.review_recommendations(self.quotes())
            self.store.event(job["id"], {"type": "published" if valid else "stale_result", "message": "结果已发布" if valid else "输入或任务已更新，迟到结果仅保留为过期历史"})
        except ModelConfigurationError as e:
            self.store.set_job_state(job["id"], "waiting_config", str(e))
            self.store.event(job["id"], {"type": "configuration_missing", "message": str(e)})
        except asyncio.CancelledError:
            # State was durably updated by cancellation or supersession; restart can recover running work.
            self.store.event(job["id"], {"type": "interrupted", "message": "任务已中断；已保存持仓不受影响"})
            raise
        except Exception as e:
            from .model import ModelError
            from .store import ReportPublicationError
            message = str(e) if isinstance(e, (ModelError, ReportPublicationError)) else ("分析超过配置的总时限" if isinstance(e, TimeoutError) else f"分析失败：{type(e).__name__}")
            if '已完成研究可从汇总检查点恢复' in message and not self.store.checkpoint_status(job['id']).get('available'):
                message = message.replace('已完成研究可从汇总检查点恢复', '本任务尚无汇总检查点；已保存材料和事件可查看，重试将按当前版本研究')
            self.store.set_job_state(job["id"], "failed", message)
            self.store.event(job["id"], {"type": "failed", "message": message})

    async def news_scan(self):
        cfg = self.store.settings()
        if self.store.mode != "live" or not cfg["news_queries"] or not cfg["model_native_search"]:
            return {'state': 'disabled'}
        bucket = int(time.time() // (cfg['news_interval_minutes'] * 60))
        demand = self.store.request_analysis(None, 'news_observation', f'news-observation:{bucket}',
            reason='公开信息观察：' + '；'.join(cfg['news_queries']) + '。自行决定查询与补查，使用本模型原生联网工具。')
        self.store.set_meta('last_news_scan', {'at': now(), 'state': 'queued', 'demand_id': demand['id']})
        return {'state': 'queued', 'demand_id': demand['id']}

    def notify_material_changes(self):
        # Persist first, then ask the existing public manager about relevance.
        # Failed or incomplete public work leaves the outbox pending.
        with self.store.connect() as db:
            # Owned discoveries retain their pending outbox entry. Skip them
            # before pagination so a failed run's backlog cannot starve a new
            # external event or repeatedly enqueue its own research inputs.
            candidates = [row[0] for row in db.execute("""SELECT n.material_id FROM material_notifications n
                LEFT JOIN information_routes r ON r.material_id=n.material_id
                WHERE n.state='pending' AND (r.state IS NULL OR r.state IN ('pending','ready','routed'))
                ORDER BY n.created_at,n.material_id LIMIT 100""")]
        for mid in candidates:
            with self.store.connect() as db:
                ready = db.execute("SELECT public_run_id,decision FROM information_routes WHERE material_id=? AND state IN ('ready','routed')", (mid,)).fetchone()
                db.execute("INSERT OR IGNORE INTO information_routes VALUES(?,NULL,'pending',NULL,?,?)", (mid, now(), now()))
            if ready:
                from .store import loads
                self.route_material_changes(ready['public_run_id'], {'information_impacts': [loads(ready['decision'])]}, [mid])
                continue
            self.store.request_analysis(None, 'public_information', 'public_information:' + mid,
                reason='待判断关联的公开材料：' + mid)

    def route_material_changes(self, run_id, result, material_ids):
        from .scheduling import information_impact
        decisions = {d.get('material_id'): d for d in (result.get('information_impacts') or [])
                     if isinstance(d, dict) and isinstance(d.get('material_id'), str)} if isinstance(result.get('information_impacts', []), list) else {}
        securities = self.store.cache('securities')
        scope = self.store.universe_snapshot()
        # The product currently has one global candidate pool and no structured
        # per-account exclusions. Watchlists alone do not grant candidate scope.
        candidates = set(scope['universe'].get('candidates') or [])
        for mid in material_ids:
            with self.store.connect() as db:
                prior = db.execute("SELECT state FROM information_routes WHERE material_id=?", (mid,)).fetchone()
            if prior and prior['state'] == 'routed':
                # Every downstream demand was committed before this state. A
                # crash before acknowledgement must not re-evaluate a new scope.
                self.store.complete_material_notifications([mid])
                continue
            decision = information_impact(decisions.get(mid), mid)
            # Semantic event identity comes from the public manager, with its
            # explanation retained for inspection; unknown identity keeps mid.
            event = 'event:' + hashlib.sha256(decision['event_key'].encode()).hexdigest() if decision.get('event_key') else mid
            delivery_key = 'information_delivery_event:' + mid
            semantic_key = 'information_semantic_event:' + event if decision.get('event_key') else None
            # A partially resolved material may return through a later public
            # run with a refined semantic event_key. Keep its first delivery
            # identity so already-covered accounts do not receive a second job.
            event = self.store.meta(delivery_key) or (self.store.meta(semantic_key) if semantic_key else None) or event
            anchor_key = 'information_event_anchor:' + event
            anchor = self.store.meta(anchor_key)
            if not anchor:
                anchor = mid
                self.store.set_meta(anchor_key, anchor)
            relevant = set(decision.get('symbols') or [])
            sectors = set(decision.get('sectors') or [])
            selected, unresolved = [], []
            unknown = decision['status'] == 'uncertain' or (
                decision['status'] == 'relevant' and not (relevant or sectors or decision['market_wide']))
            for account in self.store.accounts():
                symbols = ({h['symbol'] for h in account['holdings'] if Decimal(h['quantity']) > 0}
                           | set(self.store.recommendation_symbols(account['id'])) | candidates)
                known_sectors = {securities.get(s, {}).get('sector') for s in symbols}
                if unknown:
                    unresolved.append(account['id'])
                elif decision['status'] == 'relevant' and (
                        decision['market_wide'] or symbols & relevant or (known_sectors - {None, ''}) & sectors):
                    selected.append(account['id'])
                elif decision['status'] == 'relevant' and sectors and known_sectors & {None, ''}:
                    # Missing classification is a research gap, not evidence
                    # that an unrelated sector affects every account.
                    unresolved.append(account['id'])
            if selected:
                self.store.set_meta(delivery_key, event)
                # A semantic identity learned after partial delivery also needs
                # to point future reprints to that original demand identity.
                if semantic_key and not self.store.meta(semantic_key):
                    self.store.set_meta(semantic_key, event)
            plan = {**decision, 'material_id': mid, 'account_ids': selected, 'delivery_event': event,
                    'unresolved_account_ids': unresolved, 'universe_version': scope['universe_version']}
            with self.store.connect() as db:
                db.execute("""INSERT INTO information_routes VALUES(?,?,'ready',?,?,?)
                    ON CONFLICT(material_id) DO UPDATE SET state='ready',public_run_id=excluded.public_run_id,
                    decision=excluded.decision,updated_at=excluded.updated_at""",
                           (mid, run_id, dumps(plan), now(), now()))
            for account_id in selected:
                self.store.request_analysis(account_id, 'public_information', f"news:{account_id}:{event}",
                    reason=f'来源索引关联原始材料 {mid}；请独立读取并研究，路由不代表投资结论', delay=2)
            self.store.review_recommendations(material_ids=[anchor], material_account_ids=selected)
            pending = unknown or bool(unresolved)
            with self.store.connect() as db:
                db.execute("UPDATE information_routes SET state=?,updated_at=? WHERE material_id=?",
                           ('awaiting_retry' if pending else 'routed', now(), mid))
                db.execute("UPDATE research_demands SET state='completed',detail=?,updated_at=? WHERE event_key=?",
                           ('relevance unresolved; retained for later public research' if pending else 'covered by public research',
                            now(), 'public_information:' + mid))
            # The selected account demands are durable before acknowledgement.
            # Unknown relevance remains available to a later explicit/new-event
            # public run, without broadcasting or polling itself into a loop.
            if not pending:
                self.store.complete_material_notifications([mid])

    async def execute_global(self, run):
        from .agents import run_research
        from .model import ModelConfigurationError, ModelError
        snapshot = run.get('input_snapshot') or self.store.global_run(run['id']).get('input_snapshot')
        cfg = snapshot['config'] if snapshot else self.store.settings()
        try:
            if cfg.get('_agent_team') and self.store.mode == 'live' and self.runner is None:
                try:
                    cfg = {**self.store.runtime_role_config(cfg['_agent_team'], 'manager', cfg), '_agent_team': cfg['_agent_team']}
                except ValueError as exc:
                    raise ModelConfigurationError(str(exc)) from exc
            if not snapshot:
                self.store.finish_global(run['id'], 'superseded', error='旧任务没有保存候选范围快照，需按当前范围重新研究')
                self.store.request_analysis(None, 'universe_changed', f"legacy-global:{run['id']}")
                return
            universe = snapshot['universe']
            context = {"scope": "global", "account": None, "input_version": None, "job_id": run["id"],
                       "model_config": model_config_summary(cfg),
                       "universe_version": snapshot['universe_version'], "input_snapshot": {k: v for k, v in snapshot.items() if k != "config"},
                       "reason": run["reason"], "candidates": universe["candidates"], "watchlist": universe["watchlist"],
                       "quotes": {s: q for s, q in self.quotes().items() if s in set(universe['candidates'] + universe['watchlist'])},
                       "shared_research": [], "calendar": market_state(calendar_path=cfg.get("calendar_path")),
                       "pending_information": [index_of(self.store.material(mid)) for mid in self.store.public_information_for_run(run['id'])],
                       "news_topics": cfg.get('news_queries', []),
                       "generated_at": now(), "timezone": "Asia/Shanghai"}
            self.store.bind_public_information(run['id'], [m['id'] for m in context['pending_information']])
            context['_research_input_guard'] = self.store.research_guard(context)
            checkpoint = self.store.reusable_synthesis_checkpoint({**run, 'account_id': None}, context)
            if checkpoint:
                context['synthesis_checkpoint'] = checkpoint
            async def emit(event):
                self.record_model_event(run["id"], event)
            if self.store.mode == "demo":
                result = {"report": "演示模式：全局调度已执行；未调用模型或生成研究结论", "demo": True, "model_calls": 0}
            else:
                result = await asyncio.wait_for((self.runner or run_research)(context, cfg,
                         ResearchTools(self.store, {**run, "account_id": None}, cfg, context), emit), cfg["analysis_timeout_seconds"])
            result['model_config'] = model_config_summary(cfg)
            result['_information_material_ids'] = [m['id'] for m in context['pending_information']]
            result['synthesis_checkpoint_job_id'] = (context.get('synthesis_checkpoint') or {}).get('checkpoint_job_id')
            published = self.store.finish_global(run["id"], "completed", result)
            if published:
                self.route_material_changes(run['id'], result, [m['id'] for m in context['pending_information']])
        except ModelConfigurationError as e:
            self.store.finish_global(run["id"], "waiting_config", error=str(e))
        except asyncio.CancelledError:
            raise  # Running state is recovered into durable pending demands at restart.
        except Exception as e:
            self.store.finish_global(run["id"], "failed", error=str(e) if isinstance(e, ModelError) else type(e).__name__)

    async def scheduler_tick(self, stamp=None):
        stamp = stamp or datetime.now(TZ)
        cfg = self.store.settings()
        state = market_state(stamp, cfg.get('calendar_path'))
        # Market maintenance and due prediction scoring are independent of LLM research.
        trading = state['is_continuous_trading']
        if cfg.get('market_refresh_enabled', True) and trading:
            bucket = int(stamp.timestamp() // cfg['quote_interval_seconds'])
            if self.store.meta('schedule:quote') != bucket and not self.refresh_lock.locked() and (not self.quote_task or self.quote_task.done()):
                async def refresh_market():
                    await self.refresh_quotes()
                    self.store.set_meta('schedule:quote', bucket)
                self.quote_task = self.spawn(refresh_market())
        if (cfg.get('forecast_evaluation_enabled', True) or cfg.get('scheduled_research_enabled', True)) and (not self.forecast_task or self.forecast_task.done()) and (not self.last_forecast_tick or stamp.timestamp() - self.last_forecast_tick >= 60):
            self.last_forecast_tick = stamp.timestamp()
            async def forecast_cycle():
                batches = await self.store.forecast_tick(cfg, at=stamp, create_batches=cfg.get('scheduled_research_enabled', True))
                for batch in batches['batches']:
                    self.store.enqueue_forecast_batch(batch['id'], at=max(stamp, datetime.now(TZ)))
            self.forecast_task = self.spawn(forecast_cycle())
        elif not cfg.get('forecast_evaluation_enabled', True) and not cfg.get('scheduled_research_enabled', True) and (not self.last_forecast_tick or stamp.timestamp() - self.last_forecast_tick >= 60):
            # A pause is a visible run fact even when no network work is enabled.
            self.last_forecast_tick = stamp.timestamp()
            self.store.reconcile_forecast_windows(at=stamp, calendar_path=cfg.get('calendar_path'), scheduled_enabled=False)
        if not cfg.get('scheduled_research_enabled', True):
            return
        prior_seen = self.store.meta("schedule:last_seen")
        if prior_seen:
            prior = datetime.fromisoformat(prior_seen)
            if prior.date() < stamp.date() and market_state(prior, cfg.get("calendar_path"))["trading_day"] is True:
                for acc in self.store.accounts():
                    self.store.request_analysis(acc["id"], "close_summary", f"close:{acc['id']}:{prior.date()}",
                                                reason=f"close_summary:服务恢复补充 {prior.date()} 收盘复核，使用当前可得资料并注明时点")
        if not prior_seen or prior_seen[:16] != stamp.isoformat()[:16]:
            self.store.set_meta("schedule:last_seen", stamp.isoformat())
        if cfg["news_start_hour"] <= stamp.hour < cfg["news_end_hour"] and self.store.mode == "live" and cfg["news_queries"] and cfg["model_native_search"]:
            bucket = int(stamp.timestamp() // (cfg["news_interval_minutes"] * 60))
            if self.store.meta("schedule:news") != bucket and (not self.news_task or self.news_task.done()):
                async def scan():
                    try:
                        await self.news_scan()
                    except Exception as e:
                        self.store.set_meta("last_news_scan", {"at": now(), "errors": [type(e).__name__]})
                    finally:
                        # Failed calls have an explicit attempt record. Retry on the next
                        # configured interval; no tight loop can spend search credits.
                        self.store.set_meta("schedule:news", bucket)
                self.news_task = self.spawn(scan())
        if state["trading_day"] is not True:
            return
        trading = state["is_continuous_trading"]
        timestamp = stamp.timestamp()
        if trading:
            for kind, seconds in (("candidate_scan", cfg["scan_interval_minutes"] * 60), ("scheduled", cfg["analysis_interval_minutes"] * 60)):
                bucket = int(timestamp // seconds)
                key = f"schedule:{kind}"
                if self.store.meta(key) == bucket:
                    continue
                if kind == "quote":
                    if not self.refresh_lock.locked() and (not self.quote_task or self.quote_task.done()):
                        async def refresh(schedule_key=key, scheduled_bucket=bucket):
                            await self.refresh_quotes()
                            self.store.set_meta(schedule_key, scheduled_bucket)
                        self.quote_task = self.spawn(refresh())
                else:
                    if kind == "candidate_scan":
                        self.store.request_analysis(None, kind, f"{kind}:{bucket}")
                    else:
                        for acc in self.store.accounts():
                            self.store.request_analysis(acc["id"], kind, f"{kind}:{acc['id']}:{bucket}")
                    # This is a persisted scheduling cursor, not a completion flag.
                    # Completion, deferral, and merging live on research_demands.
                    prior = self.store.meta(key)
                    if prior is not None and bucket > prior + 1:
                        self.store.set_meta(f"schedule_coalesced:{kind}", {"from_bucket": prior + 1, "through_bucket": bucket, "reason": "离线或休市期间窗口合并至最新数据研究", "at": stamp.isoformat()})
                    self.store.set_meta(key, bucket)
        if stamp.strftime("%H:%M") >= cfg["close_summary_time"]:
            for acc in self.store.accounts():
                self.store.request_analysis(acc["id"], "close_summary", f"close:{acc['id']}:{stamp.date()}")

    async def tick(self):
        current = time.monotonic()
        wall = time.time()
        if self.last_tick and (current - self.last_tick > 180 or wall - self.last_wall_tick > 180):
            self.store.set_meta("last_resume", now())
            cfg = self.store.settings()
            if cfg.get('market_refresh_enabled', True):
                self.spawn(self.refresh_quotes())
            if cfg.get('scheduled_research_enabled', True):
                for a in self.store.accounts():
                    self.store.request_analysis(a["id"], "resume_refresh", delay=2)
        self.last_tick = current
        self.last_wall_tick = wall
        for job_id, task in list(self.active.items()):
            try:
                state = self.store.job(job_id)["state"]
            except KeyError:
                state = "deleted"
            if state not in ("running",) and not task.done():
                task.cancel()
            if task.done():
                self.active.pop(job_id)
        await self.scheduler_tick()
        if current - self.last_recommendation_check >= 1:
            self.notify_material_changes()
            self.store.review_recommendations(self.quotes())
            self.last_recommendation_check = current
        self.store.dispatch_demands()
        if not self.global_task or self.global_task.done():
            global_run = self.store.claim_global()
            if global_run:
                self.global_task = self.spawn(self.execute_global(global_run))
        for job in self.store.claim_jobs(max(0, 2 - len(self.active))):
            if job.get('recovery_attempts', 0) >= self.store.settings()["max_job_attempts"]:
                self.store.set_job_state(job["id"], "failed", "服务中断后重试次数已达上限，请手动重试")
                continue
            self.active[job["id"]] = asyncio.create_task(self.execute(job))

    async def loop(self):
        while self.running:
            try:
                await self.tick()
            except Exception as e:
                self.last_error = type(e).__name__
                log.error("worker tick failed: %s", type(e).__name__)
            await asyncio.sleep(0.5)

    def manager_config(self, account_id=None, *, credentials=True):
        cfg = self.store.settings()
        team = self.store.freeze_agent_team(account_id)
        if credentials:
            try:
                return self.store.runtime_role_config(team, 'manager', cfg)
            except ValueError as exc:
                from .model import ModelConfigurationError
                raise ModelConfigurationError(str(exc)) from exc
        role = team['roles']['manager']
        binding = role['resolved']
        provider = team['connections'].get(binding.get('provider_id'), {})
        cfg.update(model_protocol=provider.get('protocol', cfg['model_protocol']),
                   model_base_url=provider.get('base_url', cfg['model_base_url']),
                   model_name=binding.get('model_id', ''), reasoning_effort=binding.get('reasoning_effort', 'medium'),
                   provider_id=provider.get('id'), provider_revision=provider.get('revision'),
                   credential_revision=provider.get('credential_revision'), role_id='manager',
                   role_version=role['version'], prompt_version=role['prompt_version'], tool_version=role['tool_version'],
                   team_id=team['team_id'], account_id=account_id, _credential_fingerprint=(provider.get('credential_reference') or {}).get('fingerprint'))
        return cfg, role.get('config_error')

    def model_configured(self):
        if self.runner or self.store.mode == 'demo':
            return True
        try:
            cfg = self.store.settings()
            team = self.store.freeze_agent_team()
            self.store.runtime_role_config(team, 'manager', cfg)
            return True
        except (ValueError, KeyError):
            return False

    def resume_waiting_config(self):
        ready = self.model_configured()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.resume_configuration_waiters(db)
            db.execute("UPDATE accounts SET analysis_state='pending' WHERE latest_job_id IN (SELECT id FROM jobs WHERE state='queued')")
        return ready

    async def start(self):
        self.store.recover(config_ready=self.model_configured())
        last = self.store.meta("last_quote_refresh")
        try:
            expired = not last or (datetime.now(TZ) - datetime.fromisoformat(last)).total_seconds() > self.store.settings()["quote_stale_seconds"]
        except (ValueError, TypeError):
            expired = True
        if expired and self.store.settings().get('scheduled_research_enabled', True):
            for account in self.store.accounts():
                if account["analysis_state"] == "current":
                    self.store.enqueue(account["id"], "service_restart_refresh", delay=2)
        self.running = True
        self.loop_task = asyncio.create_task(self.loop())
        if self.store.settings().get('market_refresh_enabled', True):
            self.spawn(self.refresh_quotes())

    async def stop(self):
        self.running = False
        tasks = [*self.active.values(), *self.maintenance]
        if self.loop_task:
            tasks.append(self.loop_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
