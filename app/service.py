from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
import os
import time

from . import providers
from .calendar import market_state
from .domain import TZ, metrics, now
from .store import Store, dumps

log = logging.getLogger("guanlan")


class ResearchTools:
    def __init__(self, store: Store, job: dict, config: dict):
        self.store, self.job, self.config = store, job, config

    async def save_material(self, material: dict):
        # A model-authored extraction may contain account context. Keep it scoped;
        # raw public pages/search results alone enter the cross-account cache.
        return self.store.save_material({**material, "scope": "account"}, self.job["id"], self.job["account_id"])

    async def search(self, query: str):
        result = await providers.search(query, self.config)
        for item in result.get("results", []):
            material = self.store.save_material({**item, "scope": "public", "source_title": item.get("title"),
                "content": item.get("snippet"), "classification": "uncertain", "access": "search_excerpt"}, self.job["id"], self.job["account_id"])
            item["material_id"] = material["id"]
        return result

    async def read(self, url: str):
        result = await providers.read_url(url, self.config)
        material = self.store.save_material({**result, "scope": "public", "source_title": result.get("title"),
                                            "classification": "uncertain"}, self.job["id"], self.job["account_id"])
        return {**result, "material_id": material["id"]}

    async def list_materials(self, query: str = ""):
        # Global cache contains public research only; no account contexts/reports are added here.
        own = self.store.materials(self.job["account_id"], query=query)
        public = [r for r in self.store.materials(query=query) if r.get("scope") == "public"]
        rows = list({r["id"]: r for r in [*public, *own]}.values())
        for row in rows:
            self.store.save_material(row, self.job["id"], self.job["account_id"])
        return rows

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
        task.add_done_callback(self.maintenance.discard)
        return task

    async def refresh_quotes(self, symbols=None):
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
                        q = await provider.quote(s)
                        previous = old.get(s, {})
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
                                        self.store.enqueue(acc["id"], f"price_change:{s}", f"price:{acc['id']}:{s}:{bucket}", delay=2)
                            elif baseline == previous["price"]:
                                self.store.set_meta(f"price_baseline:{s}", baseline)
                        return q
                    except Exception as e:
                        # Never roll back account inputs because a public source is unavailable.
                        return {"symbol": s, "status": "error", "error": type(e).__name__}
            results = await asyncio.gather(*(refresh(s) for s in symbols))
            self.store.set_meta("last_quote_refresh", now())
            return {"mode": self.store.mode, "updated": sum(q.get("price") is not None for q in results), "quotes": results}

    async def execute(self, job):
        from .agents import run_research
        from .model import ModelClient, ModelConfigurationError
        cfg = self.store.settings()
        try:
            account = self.store.snapshot(job["account_id"], job["input_version"])
            universe = self.store.meta("universe", {"watchlist": [], "candidates": []})
            target = job["reason"].split("manual_security:", 1)[-1] if "manual_security:" in job["reason"] else None
            target_symbols = list(dict.fromkeys([*[h["symbol"] for h in account["holdings"]], *universe["candidates"], *([target] if target else [])]))
            if self.store.mode == "live" and self.runner is None:
                try:
                    client = ModelClient(cfg)
                    await client.aclose()
                except ModelConfigurationError:
                    self.spawn(self.refresh_quotes(target_symbols))
                    raise
            await self.refresh_quotes(target_symbols)
            if self.store.job(job["id"])["state"] != "running":
                return
            values = metrics(account, self.quotes(), self.store.cache("securities"))
            quote_times = [p["quote"].get("as_of") for p in values["positions"] if p["quote"].get("as_of")]
            data_as_of = min(quote_times) if quote_times else None
            context = {"account": account, "metrics": values, "candidates": universe["candidates"],
                       "candidate_quotes": {s: q for s, q in self.quotes().items() if s in universe["candidates"]},
                       "watchlist": universe["watchlist"], "reason": job["reason"], "data_as_of": data_as_of,
                       "previous_analysis": self.store.previous_analysis(account["id"]),
                       "target_security": {"symbol": target, "quote": self.quotes().get(target), "metadata": self.store.cache("securities").get(target)} if target else None,
                       "job_id": job["id"], "input_version": job["input_version"],
                       "calendar": market_state(), "available_public_material_count": len(self.store.materials()),
                       "generated_at": now(), "timezone": "Asia/Shanghai"}
            if self.store.mode == "demo":
                result = {"report": f"演示模式 · 本地数值摘要\n\n账户：{account['name']} · 输入版本 v{account['version']}\n\n已知价格持仓市值：¥{values['known_priced_value']}。\n\n" + "\n\n".join(values["notes"]) + "\n\n这是固定样例数据和代码计算结果。未调用模型、搜索或多 Agent，不构成真实投资研究或买卖建议。",
                          "metrics": values, "demo": True, "model_calls": 0}
                self.store.event(job["id"], {"type": "demo_summary", "message": "仅执行本地计算；没有模拟模型或 Agent 发言"})
            else:
                async def emit(event):
                    self.store.event(job["id"], event)
                research = self.runner or run_research
                result = await asyncio.wait_for(research(context, cfg, ResearchTools(self.store, job, cfg), emit), timeout=cfg["analysis_timeout_seconds"])
                result["metrics"] = values
            result.update(input_version=job["input_version"], data_as_of=data_as_of,
                          account_id=account["id"], job_id=job["id"], mode=self.store.mode)
            valid = self.store.publish(job["id"], result, data_as_of)
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
        for query in cfg["news_queries"]:
            result = await providers.search(query, cfg)
            for item in result.get("results", []):
                key = "news:" + str(item.get("url")) + ":" + str(item.get("published_at"))
                if self.store.meta(key):
                    continue
                self.store.set_meta(key, True)
                m = self.store.save_material({**item, "scope": "public", "content": item.get("snippet"), "classification": "uncertain", "access": "search_excerpt"})
                new_ids.append(m["id"])
        if new_ids:
            for acc in self.store.accounts():
                self.store.enqueue(acc["id"], "new_public_information:" + ",".join(new_ids[:8]), delay=2)

    async def scheduler_tick(self, stamp=None):
        stamp = stamp or datetime.now(TZ)
        state, cfg = market_state(stamp), self.store.settings()
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
                self.store.set_meta(key, bucket)
                if kind == "quote":
                    if not self.refresh_lock.locked():
                        self.spawn(self.refresh_quotes())
                else:
                    for acc in self.store.accounts():
                        # An already-running study handles its current snapshot; don't cancel it every five minutes.
                        if acc["analysis_state"] in ("pending", "running", "waiting_config"):
                            continue
                        self.store.enqueue(acc["id"], kind, f"{kind}:{acc['id']}:{bucket}")
                    if kind == "candidate_scan":
                        self.spawn(self.news_scan())
        if stamp.strftime("%H:%M") >= cfg["close_summary_time"] and stamp.hour < 20:
            for acc in self.store.accounts():
                self.store.enqueue(acc["id"], "close_summary", f"close:{acc['id']}:{stamp.date()}")

    async def tick(self):
        current = time.monotonic()
        wall = time.time()
        if self.last_tick and (current - self.last_tick > 180 or wall - self.last_wall_tick > 180):
            self.store.set_meta("last_resume", now())
            self.spawn(self.refresh_quotes())
            for a in self.store.accounts():
                self.store.enqueue(a["id"], "resume_refresh", delay=2)
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

    async def start(self):
        self.store.recover()
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
