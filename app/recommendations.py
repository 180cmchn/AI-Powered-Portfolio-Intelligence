"""Track model-authored suggestions; code evaluates only explicit numeric conditions."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .domain import TZ, now, symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, job_id TEXT NOT NULL, input_version INTEGER NOT NULL,
 security_id TEXT NOT NULL, state TEXT NOT NULL, valid_until TEXT, body TEXT NOT NULL,
 generated_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS recommendation_account ON recommendations(account_id,state);
CREATE TABLE IF NOT EXISTS condition_latches(
 account_id TEXT NOT NULL, security_id TEXT NOT NULL, condition_key TEXT NOT NULL,
 met INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(account_id,security_id,condition_key));
CREATE TABLE IF NOT EXISTS recommendation_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, recommendation_id TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_records(
 id TEXT PRIMARY KEY, identity TEXT NOT NULL, account_id TEXT NOT NULL, kind TEXT NOT NULL,
 body TEXT NOT NULL, first_seen_at TEXT NOT NULL, reviewed_at TEXT NOT NULL,
 UNIQUE(identity,account_id));
"""


def parse_time(value):
    if not value:
        return None
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def price_condition(condition, price):
    """None means uncomputable; never silently turns a prose condition into a price rule."""
    try:
        if not isinstance(condition, dict):
            return None
        if condition.get("field", "price") != "price":
            return None
        p = Decimal(str(price))
        op = condition.get("operator")
        values = [p]
        if op == "between":
            lo, hi = Decimal(str(condition["lower"])), Decimal(str(condition["upper"]))
            values += [lo, hi]
            result = lo <= p <= hi if lo <= hi else None
        elif op in ("gte", "lte", "gt", "lt", "eq"):
            v = Decimal(str(condition["value"]))
            values.append(v)
            result = {"gte": p >= v, "lte": p <= v, "gt": p > v, "lt": p < v, "eq": p == v}[op]
        else:
            return None
        return result if all(v.is_finite() and v >= 0 for v in values) else None
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


def condition_key(conditions):
    """Numeric representation/order must not create a new crossing."""
    normalized = []
    for c in conditions:
        item = {"field": c.get("field", "price"), "operator": c["operator"]}
        for k in (("lower", "upper") if c["operator"] == "between" else ("value",)):
            item[k] = str(Decimal(str(c[k])).normalize())
        normalized.append(item)
    normalized.sort(key=lambda c: json.dumps(c, sort_keys=True))
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


class RecommendationStore:
    def _recommendation_state(self, db, row, state, reason):
        from .store import dumps, loads
        body = loads(row["body"])
        body.update(state=state, status=state, review_reason=reason, updated_at=now())
        db.execute("UPDATE recommendations SET state=?,body=?,updated_at=? WHERE id=?", (state, dumps(body), now(), row["id"]))
        db.execute("INSERT INTO recommendation_events(recommendation_id,state,reason,created_at) VALUES(?,?,?,?)", (row["id"], state, reason, now()))

    def _invalidate_recommendations(self, db, account_id, reason, state="needs_review"):
        for row in db.execute("SELECT * FROM recommendations WHERE account_id=? AND state IN ('watching','triggered','needs_review')", (account_id,)).fetchall():
            self._recommendation_state(db, row, state, reason)

    def _save_suggestions(self, db, job, result):
        from .store import dumps, uid
        suggestions = result.get("suggestions") or []
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = suggestions[:100]
        # Structured suggestions are optional updates, not an implicit replacement
        # snapshot of every security in an account. Withdrawals must be explicit.
        revoked_ids = result.get("revoked_suggestion_ids") or []
        if isinstance(revoked_ids, list):
            for rid in revoked_ids[:100]:
                if not isinstance(rid, str):
                    continue
                row = db.execute("SELECT * FROM recommendations WHERE id=? AND account_id=? AND state IN ('watching','triggered','needs_review')", (rid, job["account_id"])).fetchone()
                if row:
                    self._recommendation_state(db, row, "revoked", "主 Agent 在本次分析中明确撤销")
        replaced_symbols = set()
        for item in suggestions:
            if isinstance(item, dict):
                try:
                    replaced_symbols.add(symbol(str(item.get("security_id") or item.get("symbol") or "")))
                except ValueError:
                    pass
        target = job["reason"].split("manual_security:", 1)[-1].split(";", 1)[0] if "manual_security:" in job["reason"] else None
        for row in db.execute("SELECT * FROM recommendations WHERE account_id=? AND state IN ('watching','triggered','needs_review')", (job["account_id"],)).fetchall():
            if row["security_id"] in replaced_symbols:
                self._recommendation_state(db, row, "revoked", "该证券的新结构化建议已替代旧建议")
            elif not target or target == row["security_id"]:
                self._recommendation_state(db, row, "needs_review", "新报告未明确更新这条建议，保留条件并等待 Agent 复核")
        for item in suggestions[:100]:
            if not isinstance(item, dict):
                continue
            try:
                security_id = symbol(str(item.get("security_id") or item.get("symbol") or ""))
            except ValueError:
                continue
            stamp = now()
            raw_expiry = item.get("valid_until") or result.get("valid_until")
            state, reason = "watching", "等待条件变化；定性条件由 Agent 复核"
            try:
                expiry = parse_time(raw_expiry)
                valid_until = expiry.isoformat() if expiry else None
                if expiry and expiry <= parse_time(stamp):
                    state, reason = "expired", "发布时有效期已过"
            except (ValueError, TypeError):
                valid_until, state, reason = None, "needs_review", "有效期无法解析，需重新评估"
            conditions = item.get("price_conditions") or []
            if state != "expired" and (not isinstance(conditions, list) or any(not isinstance(c, dict) or price_condition(c, 0) is None for c in conditions)):
                state, reason = "needs_review", "行情条件无法自动计算，保留原条件供 Agent 复核"
            body = {**item, "id": uid(), "account_id": job["account_id"], "job_id": job["id"],
                    "input_version": job["input_version"], "security_id": security_id, "symbol": security_id,
                    "state": state, "status": state, "valid_until": valid_until, "generated_at": stamp,
                    "updated_at": stamp, "review_reason": reason}
            db.execute("INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?,?,?)", (body["id"], job["account_id"], job["id"], job["input_version"], security_id, state, valid_until, dumps(body), stamp, stamp))
            db.execute("INSERT INTO recommendation_events(recommendation_id,state,reason,created_at) VALUES(?,?,?,?)", (body["id"], state, reason, stamp))

    def recommendations(self, account_id, limit=100, offset=0):
        from .store import loads
        with self.connect() as db:
            rows = db.execute("SELECT body FROM recommendations WHERE account_id=? ORDER BY generated_at DESC LIMIT ? OFFSET ?", (account_id, min(max(limit, 1), 500), max(offset, 0))).fetchall()
        return [loads(r[0]) for r in rows]

    def recommendation_events(self, recommendation_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM recommendation_events WHERE recommendation_id=? ORDER BY seq", (recommendation_id,))]

    def review_recommendations(self, quotes=None, stamp=None, material_ids=None):
        """State changes and follow-up demands commit together (crash-safe outbox)."""
        from .store import loads
        stamp = stamp or datetime.now(TZ)
        quotes = quotes or {}
        changed = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM recommendations WHERE state IN ('watching','triggered','needs_review')").fetchall()
            for row in rows:
                body, account = loads(row["body"]), self.account(row["account_id"], db)
                state, reason, enqueue = row["state"], None, True
                if row["valid_until"] and parse_time(row["valid_until"]) <= stamp:
                    state, reason = "expired", "建议有效期已到"
                elif row["input_version"] != account["version"] and row["state"] != "needs_review":
                    state, reason = "needs_review", "账户持仓或偏好版本已变更"
                elif material_ids:
                    # Relation is a research question, not a hard-coded bullish/bearish decision.
                    state, reason = "needs_review", "公开材料新增或更正：" + ",".join(material_ids[:8])
                    if body.get("review_reason") == reason:
                        reason = None
                elif row["state"] in ("watching", "triggered"):
                    quote = quotes.get(row["security_id"], {})
                    fresh = quote.get("status") in ("ok", "live")
                    try:
                        quote_time = parse_time(quote.get("as_of"))
                        fresh = fresh and quote_time is not None and 0 <= (stamp - quote_time).total_seconds() <= self.settings()["quote_stale_seconds"]
                    except (ValueError, TypeError):
                        fresh = False
                    conditions = body.get("price_conditions") or []
                    if fresh and isinstance(conditions, list) and conditions and all(isinstance(c, dict) for c in conditions):
                        values = [price_condition(c, quote.get("price")) for c in conditions]
                        if all(v is not None for v in values):
                            met = all(values)
                            key = condition_key(conditions)
                            latch = db.execute("SELECT met FROM condition_latches WHERE account_id=? AND security_id=? AND condition_key=?", (row["account_id"], row["security_id"], key)).fetchone()
                            db.execute("INSERT OR REPLACE INTO condition_latches VALUES(?,?,?,?,?)", (row["account_id"], row["security_id"], key, int(met), now()))
                            if met and row["state"] == "watching":
                                state, reason = "triggered", "数值行情条件已触发；投资逻辑与定性条件待 Agent 复核"
                                enqueue = not latch or not latch[0]
                                if not enqueue:
                                    reason = "行情条件仍满足；本次分析已延续此前触发条件，避免重复研究"
                            elif not met and row["state"] == "triggered":
                                state, reason, enqueue = "watching", "行情已离开触发区间，继续观察", False
                if reason:
                    self._recommendation_state(db, row, state, reason)
                    changed.append({"id": row["id"], "state": state, "reason": reason})
                    event_suffix = ":".join(material_ids or []) if material_ids else state
                    if state == "triggered":
                        event_suffix += ":" + now()  # each fresh false -> true crossing
                    key = f"recommendation:{row['id']}:{event_suffix}"
                    if enqueue:
                        db.execute("""INSERT OR IGNORE INTO research_demands
                         (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
                         VALUES(?,?,?,'recommendation_review',?,'pending',?,?)""",
                         (key, row["account_id"], account["version"], reason, now(), now()))
            # Reports also have expiry semantics even if no structured suggestions exist.
            for row in db.execute("SELECT id,job_id,result,account_id,input_version,created_at FROM analyses WHERE status='current'").fetchall():
                raw = loads(row["result"]).get("valid_until")
                unusable_on_creation = False
                try:
                    expiry = parse_time(raw)
                    unusable_on_creation = bool(expiry and expiry <= parse_time(row["created_at"]))
                except (ValueError, TypeError):
                    expiry = stamp  # An unreadable explicit expiry is not an active report.
                    unusable_on_creation = True
                if expiry and expiry <= stamp:
                    db.execute("UPDATE analyses SET status='expired' WHERE id=?", (row["id"],))
                    db.execute("UPDATE accounts SET analysis_state='expired' WHERE id=? AND latest_job_id=? AND analysis_state='current'", (row["account_id"], row["job_id"]))
                    # At most one corrective pass per input for an already unusable
                    # expiry. A model repeating bad dates must not consume endless calls.
                    expiry_key = f"invalid_expiry:{row['account_id']}:{row['input_version']}" if unusable_on_creation else f"report_expired:{row['id']}"
                    db.execute("""INSERT OR IGNORE INTO research_demands
                         (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
                         VALUES(?,?,?,'report_expired','报告有效期已到','pending',?,?)""",
                         (expiry_key, row["account_id"], row["input_version"], now(), now()))
        self.dispatch_demands()
        return changed

    def save_research_records(self, records, job_id, account_id=None, db=None):
        from .store import dumps, loads, uid
        if db is None:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                return self.save_research_records(records, job_id, account_id, conn)
        if not isinstance(records, list):
            return
        for record in records[:100]:
            if not isinstance(record, dict) or record.get("kind") not in ("market", "sector", "candidate"):
                continue
            identity = str(record.get("security_id") or record.get("sector_id") or record.get("title") or "market")
            identity = record["kind"] + ":" + identity
            old = db.execute("SELECT * FROM research_records WHERE identity=? AND account_id=?", (identity, account_id or "")).fetchone()
            stamp = now()
            body = {**record, "id": old["id"] if old else uid(), "account_id": account_id,
                    "scope": "account" if account_id else "public", "job_id": job_id,
                    "first_seen_at": old["first_seen_at"] if old else stamp, "reviewed_at": stamp}
            if old:
                previous = loads(old["body"])
                body["previous_findings"] = str(previous.get("findings", ""))[:4000]
            db.execute("""INSERT INTO research_records VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity,account_id)
                DO UPDATE SET body=excluded.body,reviewed_at=excluded.reviewed_at""",
                (body["id"], identity, account_id or "", record["kind"], dumps(body), body["first_seen_at"], stamp))

    def research_records(self, account_id=None, limit=30, offset=0):
        from .store import loads
        with self.connect() as db:
            rows = db.execute("SELECT body FROM research_records WHERE account_id='' OR account_id=? ORDER BY reviewed_at DESC LIMIT ? OFFSET ?", (account_id or "", min(max(limit, 1), 100), max(offset, 0))).fetchall()
        return [loads(r[0]) for r in rows]
