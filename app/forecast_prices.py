"""Exact-date, reproducible closing-price evidence. Never reads the quotes cache."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .domain import TZ, now


def _row(payload, day):
    rows = [r for r in payload.get("rows", []) if str(r.get("time", ""))[:10] == day]
    if len(rows) != 1:
        return None
    return rows[0]


def _positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, TypeError):
        return None


async def closing_pair(provider, symbol, base_date, target_date):
    """Retrieve both ends in one raw and one adjusted series snapshot.

    A changing adjustment/raw ratio signals a corporate action. Both adjusted
    endpoints come from the same response. If the provider cannot supply that
    evidence, return a disclosed gap instead of silently treating an ex-date drop
    as a forecasting error. Tushare's current raw-only adapter is such a case.
    """
    raw = await provider.history(symbol, start=base_date, end=target_date, interval="daily", adjust="none", limit=1000)
    evidence = {"symbol": symbol, "base_date": base_date, "target_date": target_date,
                "fetched_at": now(), "source": raw.get("source"), "source_url": raw.get("source_url"),
                "price_policy": "same_source_pair_v1", "raw_status": raw.get("status")}
    if raw.get("symbol", symbol) != symbol:
        return {**evidence, "status": "unassessable", "reason": "security_identity_mismatch"}
    if raw.get("status") not in ("ok", "empty"):
        return {**evidence, "status": "waiting_data", "reason": "history_unavailable", "error": str(raw.get("error", ""))[:300]}
    first, last = _row(raw, base_date), _row(raw, target_date)
    evidence["raw_rows"] = {"base": first, "target": last}
    if first is None or last is None:
        return {**evidence, "status": "waiting_data", "reason": "exact_date_missing", "missing_dates":
                [day for day, row in ((base_date, first), (target_date, last)) if row is None]}
    for row in (first, last):
        if row.get("status") in ("suspended", "delisted", "no_trade"):
            return {**evidence, "status": "unassessable", "reason": row["status"]}
        if row.get("volume") is not None and _positive(row["volume"]) is None:
            return {**evidence, "status": "unassessable", "reason": "no_trade_or_invalid_volume"}
    a, b = _positive(first.get("close")), _positive(last.get("close"))
    if a is None or b is None:
        return {**evidence, "status": "unassessable", "reason": "invalid_close"}
    if base_date == target_date:
        result = {**evidence, "status": "ready", "base_price": str(a), "target_price": str(b), "basis": "raw_close"}
    else:
        adjusted = await provider.history(symbol, start=base_date, end=target_date, interval="daily", adjust="hfq", limit=1000)
        af, al = _row(adjusted, base_date), _row(adjusted, target_date)
        x, y = _positive(af.get("close")) if af else None, _positive(al.get("close")) if al else None
        evidence["adjustment_evidence"] = {"source": adjusted.get("source"), "status": adjusted.get("status"),
                "adjustment": adjusted.get("adjustment"), "rows": {"base": af, "target": al}, "fetched_together_at": now()}
        if (adjusted.get("status") != "ok" or adjusted.get("symbol", symbol) != symbol
                or adjusted.get("source") != raw.get("source") or adjusted.get("adjustment") != "hfq"
                or x is None or y is None):
            return {**evidence, "status": "waiting_data", "reason": "comparable_adjustment_unavailable"}
        # Any non-proportional adjustment uses the comparable pair; no heuristic
        # tolerance may hide a small distribution near a direction boundary.
        action = y * a != b * x
        result = {**evidence, "status": "ready", "base_price": str(x if action else a),
                  "target_price": str(y if action else b), "basis": "same_snapshot_hfq" if action else "raw_close",
                  "corporate_action_adjustment": action,
                  "adjustment_note": "复权/原始两端比例不一致时统一使用本次同源后复权两端价格"}
    stable = {k: v for k, v in result.items() if k != "fetched_at"}
    if isinstance(stable.get("adjustment_evidence"), dict):
        stable["adjustment_evidence"] = {k: v for k, v in stable["adjustment_evidence"].items() if k != "fetched_together_at"}
    result["data_version"] = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
    return result


class ClosingPriceSession:
    """Bounded per-cycle request coalescing, with immutable shared raw evidence.

    A fresh session performs a fresh supplier observation. It never combines
    adjusted endpoints from different fetches or reuses an old cycle as a new
    correction. Forecast-specific calculations remain in the forecast ledger.
    """
    def __init__(self, store, provider, *, concurrency=4, timeout=20):
        import asyncio
        self.store, self.provider = store, provider
        self.semaphore = asyncio.Semaphore(max(1, min(16, int(concurrency))))
        self.timeout = max(0.05, min(120, float(timeout)))
        self.tasks = {}
        self.requests = {}
        self.retired_tasks = set()
        self.closed = False
        self.metrics = {'requests_started': 0, 'requests_completed': 0,
                        'capacity_wait_cancelled': 0, 'shared_consumers': 0}

    async def pair(self, symbol, base_date, target_date, *, on_start=None):
        """Notify a consumer only after capacity is acquired for a real fetch.

        A consumer's cancellation never cancels a request still needed by another.
        Consumers attached to an in-flight shared request count one observation,
        while the session metrics count the one actual supplier fetch.
        """
        import asyncio
        from copy import deepcopy
        key = (symbol, base_date, target_date, "same_source_pair_v1")
        if self.closed:
            raise RuntimeError('closing price session is closed')
        token = object()
        entry = self.requests.get(key)
        if entry is not None and (entry['task'].cancelled() or entry['task'].cancelling()):
            # Cancellation can also finish before existing consumers run their
            # finally blocks. Replacement still owns a distinct local entry.
            self.retired_tasks.add(entry['task'])
            entry = None
        if entry is None:
            # Each generation owns its subscribers and start marker. A cancelled
            # generation cannot donate its started state to a later request.
            entry = {'subscribers': {}, 'started': False}
            task = asyncio.create_task(self._fetch(key, entry))
            entry['task'] = task
            self.requests[key], self.tasks[key] = entry, task
        else:
            self.metrics['shared_consumers'] += 1
        task, subscribers = entry['task'], entry['subscribers']
        subscribers[token] = on_start
        try:
            if entry['started'] and on_start is not None and on_start() is False:
                raise asyncio.CancelledError('consumer lease no longer owned')
            return deepcopy(await asyncio.shield(task))
        finally:
            subscribers.pop(token, None)
            if not subscribers and (not task.done() or task.cancelled()):
                if not task.done():
                    if not entry['started']:
                        self.metrics['capacity_wait_cancelled'] += 1
                    task.cancel()
                self.retired_tasks.add(task)
                # A later subscriber may already own a new generation. Never
                # let an older consumer's cleanup remove or cancel that task.
                if self.requests.get(key) is entry:
                    del self.requests[key]
                if self.tasks.get(key) is task:
                    del self.tasks[key]

    async def _fetch(self, key, entry):
        import asyncio
        import uuid
        async def fetch_started():
            subscribers = entry['subscribers']
            for token, callback in list(subscribers.items()):
                if callback is not None and callback() is False:
                    subscribers.pop(token, None)
            if not subscribers:
                raise asyncio.CancelledError('no current consumers')
            entry['started'] = True
            self.metrics['requests_started'] += 1
            return await closing_pair(self.provider, *key[:3])
        async with self.semaphore:
            result = await asyncio.wait_for(fetch_started(), timeout=self.timeout)
            self.metrics['requests_completed'] += 1
        request_key = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        stable = {k: v for k, v in result.items() if k != "fetched_at"}
        version = result.get("data_version") or hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
        # The snapshot is append-only. Individual forecast observations can refer
        # to the same snapshot, but never share account conclusions.
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO forecast_shared_price_evidence VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, request_key, version, now(), json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)))
            evidence_id = db.execute("SELECT id FROM forecast_shared_price_evidence WHERE request_key=? AND data_version=?", (request_key, version)).fetchone()[0]
        return {**result, "shared_evidence_id": evidence_id}

    async def close(self):
        import asyncio
        self.closed = True
        tasks = set(self.tasks.values()) | self.retired_tasks
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
