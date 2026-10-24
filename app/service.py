from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
import os
import time
from urllib.parse import urlsplit

from . import providers
from .calendar import market_state
from .domain import TZ, metrics, now
from .store import Store, dumps
from .materials import index_of, material_text

log = logging.getLogger("guanlan")


class ResearchTools:
    def __init__(self, store: Store, job: dict, config: dict):
        self.store, self.job, self.config = store, job, config

    async def save_material(self, material: dict):
        # A model-authored extraction may contain account context. Keep it scoped;
        # raw public pages/search results alone enter the cross-account cache.
        scope = "account" if self.job.get("account_id") else "public"
        if material.get("id") and not self.store.material_accessible(material["id"], self.job.get("account_id")):
            raise KeyError("材料不属于当前账户或公开研究范围")
        saved = self.store.save_material({**material, "scope": scope, "representation": "agent_extraction"}, self.job["id"], self.job.get("account_id"))
        return index_of(saved)

    async def search(self, query: str):
        result = await providers.search(query, self.config)
        for item in result.get("results", []):
            material = self.store.save_material({**item, "scope": "public", "source_title": item.get("title"),
                "content": item.get("snippet"), "classification": "uncertain", "access": "search_excerpt"}, self.job["id"], self.job.get("account_id"))
            item["material_id"] = material["id"]
            item["material_identity"] = {k: material[k] for k in ("document_version_id", "document_version", "representation_kind", "document_identity_kind", "read_completeness")}
        return result

    async def read(self, url: str, **options):
        result = await providers.read_url(url, {**self.config, "read_max_chars": min(self.config.get("read_max_chars", 12000), 12000)}, **options)
        material = self.store.save_material({**result, "scope": "public", "source_title": result.get("title"),
                                            "classification": "uncertain"}, self.job["id"], self.job.get("account_id"))
        return {**result, "material_id": material["id"], "material_identity": {
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
        return await providers.pdf_metadata(url, self.config, **options)

    async def search_pdf(self, url, query, **options):
        result = await providers.search_pdf(url, query, self.config, **options)
        saved = self.store.save_material({**result, "scope": "public", "access": "pdf_search",
                                         "content": dumps(result), "classification": "uncertain"}, self.job["id"], self.job.get("account_id"))
        return {**result, "material_id": saved["id"]}

    async def _market_data(self, method, **kwargs):
        result = await getattr(providers.MarketProvider(self.config), method)(**kwargs)
        saved = self.store.save_material({"scope": "public", "content": dumps(result), "source_title": method,
                                         "source": result.get("source"), "classification": "source_data",
                                         "published_at": result.get("as_of"), "access": "structured_data"}, self.job["id"], self.job.get("account_id"))
        if len(dumps(result)) > 16000:
            return {"material_id": saved["id"], "source": result.get("source"), "status": result.get("status"),
                    "note": "数据已保存，按材料字符范围逐段读取", **self.store.material_range(saved["id"], length=10000)}
        return {**result, "material_id": saved["id"]}

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
        self.last_tick = 0.0
        self.last_wall_tick = 0.0
        self.last_error = None
        self.running = False
        self.maintenance: set[asyncio.Task] = set()
        self.global_task = None
        self.news_task = None
        self.quote_task = None
        self.last_recommendation_check = 0.0

    def quotes(self):
        quotes = self.store.cache("quotes")
        stamp = datetime.now(TZ)
        for quote in quotes.values():
            if quote.get("as_of"):
                try:
                    delay = max(0, (stamp - datetime.fromisoformat(quote["as_of"])).total_seconds())
                    quote["delay_seconds"] = int(delay)
                    if delay > self.store.settings()["quote_stale_seconds"] and quote.get("status") == "ok":
                        quote["status"] = "stale"
                except (ValueError, TypeError):
                    quote["status"] = "unknown_time"
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
                        self.store.put_cache("quotes", s, q)
                        if q.get("name"):
                            existing = self.store.cache("securities").get(s, {})
                            if not existing or not existing.get("kind"):
                                existing = await provider.security(s)
                            self.store.put_cache("securities", s, {**existing, "symbol": s, "name": q["name"]})
                        if q.get("price") and previous.get("price"):
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
        cfg = self.store.settings()
        started = time.monotonic()
        try:
            account = self.store.snapshot(job["account_id"], job["input_version"])
            universe = self.store.meta("universe", {"watchlist": [], "candidates": []})
            target = job["reason"].split("manual_security:", 1)[-1].split(";", 1)[0] if "manual_security:" in job["reason"] else None
            target_symbols = list(dict.fromkeys([*[h["symbol"] for h in account["holdings"]], *universe["candidates"], *([target] if target else [])]))
            target_symbols = list(dict.fromkeys([*target_symbols, *self.store.recommendation_symbols(account['id'])]))
            if self.store.mode == "live" and self.runner is None:
                try:
                    client = ModelClient(cfg)
                    await client.aclose()
                except ModelConfigurationError:
                    self.spawn(self.refresh_quotes(target_symbols))
                    raise
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
            scope = self.store.universe_snapshot()
            context = {"account": account, "metrics": values, "candidates": universe["candidates"],
                       "candidate_quotes": {s: q for s, q in self.quotes().items() if s in universe["candidates"]},
                       "watchlist": universe["watchlist"], "reason": job["reason"], "data_as_of": data_as_of,
                       "previous_analysis": self.store.previous_analysis(account["id"]),
                       "target_security": {"symbol": target, "quote": self.quotes().get(target), "metadata": self.store.cache("securities").get(target)} if target else None,
                       "job_id": job["id"], "input_version": job["input_version"],
                       "calendar": market_state(), "available_public_material_count": self.store.material_count(),
                       "shared_research": self.store.research_records(limit=20),
                       "universe_version": scope['universe_version'],
                       "suggestions_to_review": suggestion_context['items'],
                       "suggestions_coverage": {k: v for k, v in suggestion_context.items() if k != 'items'},
                       "suggestion_index": {k: v for k, v in suggestion_context.items() if k != 'items'},
                       "business_demands": [d for d in self.store.demands(account["id"], limit=100) if d["job_id"] == job["id"]],
                       "generated_at": now(), "timezone": "Asia/Shanghai"}
            if self.store.mode == "demo":
                result = {"report": f"演示模式 · 本地数值摘要\n\n账户：{account['name']} · 输入版本 v{account['version']}\n\n已知价格持仓市值：¥{values['known_priced_value']}。\n\n" + "\n\n".join(values["notes"]) + "\n\n这是固定样例数据和代码计算结果。未调用模型、搜索或多 Agent，不构成真实投资研究或买卖建议。",
                          "metrics": values, "demo": True, "model_calls": 0}
                self.store.event(job["id"], {"type": "demo_summary", "message": "仅执行本地计算；没有模拟模型或 Agent 发言"})
            else:
                async def emit(event):
                    self.store.event(job["id"], event)
                research = self.runner or run_research
                result = await asyncio.wait_for(research(context, cfg, ResearchTools(self.store, job, cfg), emit), timeout=max(0.01, cfg["analysis_timeout_seconds"] - (time.monotonic() - started)))
                result["metrics"] = values
            result.update(input_version=job["input_version"], data_as_of=data_as_of,
                          account_id=account["id"], job_id=job["id"], mode=self.store.mode,
                          universe_version=scope['universe_version'],
                          shared_research_used=[{'id': r['id'], 'universe_version': r.get('universe_version'),
                              'scope': r.get('scope'), 'applicability': r.get('applicability')} for r in context['shared_research']])
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
            message = str(e) if isinstance(e, ModelError) else ("分析超过配置的总时限" if isinstance(e, TimeoutError) else f"分析失败：{type(e).__name__}")
            self.store.set_job_state(job["id"], "failed", message)
            self.store.event(job["id"], {"type": "failed", "message": message})

    async def news_scan(self):
        cfg = self.store.settings()
        if self.store.mode != "live" or not cfg["news_queries"] or cfg["search_provider"] == "disabled":
            return
        new_ids = []
        visited = set()
        errors = []
        for query in cfg["news_queries"]:
            result = await providers.search(query, cfg)
            if result.get("error"):
                errors.append(result["error"])
            for item in result.get("results", []):
                m = self.store.save_material({**item, "scope": "public", "content": item.get("snippet"), "classification": "uncertain", "access": "search_excerpt"}, notify=True)
                if m["_document_created"]:
                    new_ids.append(m["id"])
                # Search summaries can stay unchanged after a source correction.
                url = item.get("url")
                if cfg["news_read_sources"] and url and url not in visited:
                    visited.add(url)
                    document = await providers.read_url(url, cfg, refresh=True)
                    full = self.store.save_material({**document, "scope": "public", "classification": "uncertain",
                                                     "published_at": item.get("published_at")}, notify=True)
                    if full["_document_created"]:
                        new_ids.append(full["id"])
        new_ids = list(dict.fromkeys(new_ids))
        self.notify_material_changes()
        self.store.set_meta("last_news_scan", {"at": now(), "new_versions": len(new_ids), "errors": errors})
        return {"new_material_ids": new_ids, "errors": errors}

    def notify_material_changes(self):
        ids = self.store.pending_material_notifications()
        if not ids:
            return
        self.store.review_recommendations(material_ids=ids)
        accounts = self.store.accounts()
        for material_id in ids:
            material = self.store.material(material_id)
            kind = material.get("representation_kind")
            label = "原始文档新增或内容变化" if kind == "original_document" else "摘要表示新增或变化" if kind == "summary" else "文本表示新增或变化（原始身份未知）"
            self.store.request_analysis(None, "public_information", "public_information:" + material_id,
                                        reason=label + "：" + material_id)
            for acc in accounts:
                self.store.request_analysis(acc["id"], "public_information", f"news:{acc['id']}:{material_id}",
                                            reason="new_public_information:" + material_id + "；" + label, delay=2)
        # Every target has a durable idempotent demand before acknowledging delivery.
        self.store.complete_material_notifications(ids)

    async def execute_global(self, run):
        from .agents import run_research
        from .model import ModelConfigurationError, ModelError
        snapshot = run.get('input_snapshot') or self.store.global_run(run['id']).get('input_snapshot')
        cfg = snapshot['config'] if snapshot else self.store.settings()
        try:
            if not snapshot:
                self.store.finish_global(run['id'], 'superseded', error='旧任务没有保存候选范围快照，需按当前范围重新研究')
                self.store.request_analysis(None, 'universe_changed', f"legacy-global:{run['id']}")
                return
            universe = snapshot['universe']
            context = {"scope": "global", "account": None, "input_version": None, "job_id": run["id"],
                       "universe_version": snapshot['universe_version'], "input_snapshot": snapshot,
                       "reason": run["reason"], "candidates": universe["candidates"], "watchlist": universe["watchlist"],
                       "quotes": {s: q for s, q in self.quotes().items() if s in set(universe['candidates'] + universe['watchlist'])},
                       "shared_research": self.store.research_records(limit=30), "calendar": market_state(),
                       "generated_at": now(), "timezone": "Asia/Shanghai"}
            async def emit(event):
                self.store.event(run["id"], event)
            if self.store.mode == "demo":
                result = {"report": "演示模式：全局调度已执行；未调用模型或生成研究结论", "demo": True, "model_calls": 0}
            else:
                result = await asyncio.wait_for((self.runner or run_research)(context, cfg,
                         ResearchTools(self.store, {**run, "account_id": None}, cfg), emit), cfg["analysis_timeout_seconds"])
            self.store.finish_global(run["id"], "completed", result)
        except ModelConfigurationError as e:
            self.store.finish_global(run["id"], "waiting_config", error=str(e))
        except asyncio.CancelledError:
            raise  # Running state is recovered into durable pending demands at restart.
        except Exception as e:
            self.store.finish_global(run["id"], "failed", error=str(e) if isinstance(e, ModelError) else type(e).__name__)

    async def scheduler_tick(self, stamp=None):
        stamp = stamp or datetime.now(TZ)
        state, cfg = market_state(stamp), self.store.settings()
        prior_seen = self.store.meta("schedule:last_seen")
        if prior_seen:
            prior = datetime.fromisoformat(prior_seen)
            if prior.date() < stamp.date() and market_state(prior)["trading_day"] is True:
                for acc in self.store.accounts():
                    self.store.request_analysis(acc["id"], "close_summary", f"close:{acc['id']}:{prior.date()}",
                                                reason=f"close_summary:服务恢复补充 {prior.date()} 收盘复核，使用当前可得资料并注明时点")
        if not prior_seen or prior_seen[:16] != stamp.isoformat()[:16]:
            self.store.set_meta("schedule:last_seen", stamp.isoformat())
        if cfg["news_start_hour"] <= stamp.hour < cfg["news_end_hour"] and self.store.mode == "live" and cfg["news_queries"] and cfg["search_provider"] != "disabled":
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
        trading = state["phase"] in ("morning", "afternoon")
        timestamp = stamp.timestamp()
        if trading:
            for kind, seconds in (("quote", cfg["quote_interval_seconds"]), ("candidate_scan", cfg["scan_interval_minutes"] * 60), ("scheduled", cfg["analysis_interval_minutes"] * 60)):
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
            self.spawn(self.refresh_quotes())
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
            if job["attempts"] > self.store.settings()["max_job_attempts"]:
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

    def model_configured(self):
        cfg = self.store.settings()
        local = urlsplit(cfg["model_base_url"]).hostname in ("localhost", "127.0.0.1", "::1")
        return bool(self.runner or self.store.mode == "demo" or (cfg["model_name"] and (os.getenv(cfg["model_key_env"]) or (local and cfg.get("model_allow_no_key")))))

    def resume_waiting_config(self):
        ready = self.model_configured()
        if ready:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # Only waiting work is touched; a settings save must not requeue
                # active global studies or interrupt account research.
                db.execute("UPDATE jobs SET state='queued',error=NULL,due_at=?,updated_at=? WHERE state='waiting_config' AND EXISTS(SELECT 1 FROM accounts a WHERE a.latest_job_id=jobs.id AND a.version=jobs.input_version)", (now(), now()))
                db.execute("UPDATE accounts SET analysis_state='pending' WHERE latest_job_id IN (SELECT id FROM jobs WHERE state='queued')")
                db.execute("UPDATE research_demands SET state='pending',job_id=NULL,detail='configuration restored' WHERE job_id IN (SELECT id FROM global_runs WHERE state='waiting_config')")
                db.execute("UPDATE global_runs SET state='superseded' WHERE state='waiting_config'")
        return ready

    async def start(self):
        self.store.recover(config_ready=self.model_configured())
        last = self.store.meta("last_quote_refresh")
        try:
            expired = not last or (datetime.now(TZ) - datetime.fromisoformat(last)).total_seconds() > self.store.settings()["quote_stale_seconds"]
        except (ValueError, TypeError):
            expired = True
        if expired:
            for account in self.store.accounts():
                if account["analysis_state"] == "current":
                    self.store.enqueue(account["id"], "service_restart_refresh", delay=2)
        self.running = True
        self.loop_task = asyncio.create_task(self.loop())
        self.spawn(self.refresh_quotes())

    async def stop(self):
        self.running = False
        tasks = [*self.active.values(), *self.maintenance]
        if self.loop_task:
            tasks.append(self.loop_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
