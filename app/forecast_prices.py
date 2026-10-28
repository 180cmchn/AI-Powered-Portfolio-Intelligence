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
