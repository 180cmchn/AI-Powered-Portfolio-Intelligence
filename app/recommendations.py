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
    @staticmethod
    def _recommendation_body(row):
        from .store import loads
        body = loads(row["body"])
        body.setdefault("review_status", "needs_review" if row["state"] in ("needs_review", "triggered") else
                        row["state"] if row["state"] in ("revoked", "expired") else "valid")
        body.setdefault("price_state", "unknown")
        return body

    def _write_recommendation(self, db, row, body, *, event=True):
        from .store import dumps
        body["status"] = body["state"]
        body["updated_at"] = now()
        db.execute("UPDATE recommendations SET job_id=?,input_version=?,state=?,valid_until=?,body=?,updated_at=? WHERE id=?",
                   (body["job_id"], body["input_version"], body["state"], body.get("valid_until"), dumps(body), body["updated_at"], row["id"]))
        if event:
            db.execute("INSERT INTO recommendation_events(recommendation_id,state,reason,created_at) VALUES(?,?,?,?)",
                       (row["id"], body["state"], body.get("review_reason", "状态更新"), body["updated_at"]))

    def _review_demand(self, db, row, body, cause, reason, *, key=None):
        """One persisted event may run once; an unresolved pass remains visible for explicit retry."""
        account = self.account(row["account_id"], db)
        key = key or f"recommendation:{row['id']}:{cause}"
        db.execute("""INSERT OR IGNORE INTO research_demands
            (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
            VALUES(?,?,?,'recommendation_review',?,'pending',?,?)""",
            (key, row["account_id"], account["version"], reason, now(), now()))
        # A repeated unresolved output in the same input version does not buy
        # another automatic model call. Keep the remaining work visible instead.
        db.execute("""UPDATE research_demands SET state='waiting_review',detail='同一事项已有复核执行；等待明确重试或新的外部事件',updated_at=?
            WHERE event_key=? AND state='completed'""", (now(), key))
        body["review_event_key"] = key
        body.setdefault("review_pending_since", now())
        return key

    def _close_review_demands(self, db, row, body, job_id=None):
        # A publication only covers the work assigned before that execution.
        # New events arriving while the model runs remain pending, even if the
        # model explicitly maintains or updates an older-context suggestion.
        db.execute("""UPDATE research_demands SET state='completed',detail='recommendation explicitly resolved',updated_at=?
            WHERE account_id=? AND (event_key LIKE ? OR event_key=?) AND state IN ('pending','assigned','waiting_review','failed','cancelled')
            AND (? IS NULL OR (state<>'pending' AND (state<>'assigned' OR job_id=?)))""",
            (now(), row["account_id"], f"recommendation:{row['id']}:%", body.get("review_event_key", ""), job_id, job_id))
        uncovered = [dict(demand) for demand in db.execute("""SELECT * FROM research_demands
            WHERE account_id=? AND (event_key LIKE ? OR event_key=?) AND state IN ('pending','assigned') ORDER BY id""",
            (row["account_id"], f"recommendation:{row['id']}:%", body.get("review_event_key", ""))).fetchall()]
        if not uncovered:
            body.pop("review_pending_since", None)
        return uncovered

    @staticmethod
    def _retain_uncovered_review(body, uncovered):
        if uncovered:
            body.update(state="needs_review", review_status="needs_review",
                        review_reason="本次任务未覆盖后到的新事件，价格观察继续，等待后续复核",
                        review_event_key=uncovered[-1]["event_key"],
                        review_pending_since=uncovered[0]["created_at"])

    def _recommendation_state(self, db, row, state, reason, job_id=None):
        body = self._recommendation_body(row)
        body.update(state=state, review_reason=reason)
        uncovered = []
        if state in ("revoked", "expired", "needs_review"):
            body["review_status"] = state
        if state in ("revoked", "expired"):
            uncovered = self._close_review_demands(db, row, body, job_id)
        self._write_recommendation(db, row, body)
        return uncovered

    def migrate_recommendations(self, db):
        """Legacy price observations are unknown, never fabricated from missing quote data."""
        from .store import loads
        for row in db.execute("SELECT * FROM recommendations").fetchall():
            saved = loads(row["body"])
            if "review_status" in saved and "price_state" in saved:
                continue
            body = self._recommendation_body(row)
            if body["review_status"] == "needs_review" and row["state"] not in ("revoked", "expired"):
                self._review_demand(db, row, body, f"legacy:v{row['input_version']}", body.get("review_reason") or "旧格式建议等待明确复核")
            self._write_recommendation(db, row, body, event=False)

    def _invalidate_recommendations(self, db, account_id, reason, state="needs_review"):
        account = self.account(account_id, db)
        for row in db.execute("SELECT * FROM recommendations WHERE account_id=? AND state IN ('watching','triggered','needs_review')", (account_id,)).fetchall():
            body = self._recommendation_body(row)
            body.update(state=state, review_status=state, review_reason=reason)
            self._review_demand(db, row, body, f"input:v{account['version']}", reason)
            self._write_recommendation(db, row, body)

    def _save_suggestions(self, db, job, result):
        from .store import dumps, uid
        target = job["reason"].split("manual_security:", 1)[-1].split(";", 1)[0] if "manual_security:" in job["reason"] else None
        if target:
            # A manual single-security execution can absorb previously queued work
            # during supersession. It cannot discharge another security's review
            # or an account-wide information/schedule/configuration obligation.
            db.execute("""UPDATE research_demands SET state='pending',job_id=NULL,
                detail='局部证券研究未覆盖此事项，留待全账户研究',updated_at=?
                WHERE account_id=? AND job_id=? AND state='assigned' AND
                (kind<>'recommendation_review' OR EXISTS
                    (SELECT 1 FROM recommendations r WHERE r.account_id=research_demands.account_id
                     AND r.security_id<>? AND (research_demands.event_key LIKE 'recommendation:' || r.id || ':%'
                     OR research_demands.event_key=json_extract(r.body,'$.review_event_key'))))""",
                (now(), job["account_id"], job["id"], target))
        rows = db.execute("SELECT * FROM recommendations WHERE account_id=? AND state IN ('watching','triggered','needs_review')", (job["account_id"],)).fetchall()
        current = {row["id"]: row for row in rows}
        def ids(name):
            values = result.get(name)
            return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()
        revoked, maintained, pending = ids("revoked_suggestion_ids"), ids("maintained_suggestion_ids"), ids("review_suggestion_ids")
        suggestions = result.get("suggestions") or []
        prepared = []
        for item in suggestions[:100] if isinstance(suggestions, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                security_id = symbol(str(item.get("security_id") or item.get("symbol") or ""))
            except ValueError:
                continue
            # IDs are account-scoped; neither stale nor foreign IDs can create an update.
            old = current.get(item.get("id")) if item.get("id") else None
            if item.get("id") and (not old or old["security_id"] != security_id):
                continue
            if target and security_id != target:
                continue
            prepared.append((item, security_id, old))
        updated_ids = {old["id"] for _, _, old in prepared if old}
        new_symbols = {security for _, security, old in prepared if not old}
        replaced = {}
        replacement_pending = {}
        for row in rows:
            if target and target != row["security_id"]:
                continue
            body = self._recommendation_body(row)
            if row["id"] in revoked or row["security_id"] in new_symbols and row["id"] not in updated_ids:
                replaced.setdefault(row["security_id"], []).append(row["id"])
                uncovered = self._recommendation_state(db, row, "revoked", "主 Agent 明确撤销" if row["id"] in revoked else "该证券的新结构化建议已替代旧建议",
                    job["id"] if row["security_id"] in new_symbols else None)
                replacement_pending.setdefault(row["security_id"], []).extend(uncovered)
                continue
            if row["id"] in updated_ids:
                continue
            if row["id"] in maintained and row["id"] not in pending:
                try:
                    expired = bool(row["valid_until"] and parse_time(row["valid_until"]) <= parse_time(now()))
                except (ValueError, TypeError):
                    expired = True
                conditions = body.get("price_conditions") or []
                computable = isinstance(conditions, list) and all(isinstance(c, dict) and price_condition(c, 0) is not None for c in conditions)
                if not expired and computable:
                    body.update(review_status="valid", state="triggered" if body["price_state"] == "met" else "watching",
                                input_version=job["input_version"], job_id=job["id"], reviewed_at=now(), review_reason="主 Agent 明确维持；已绑定本次核实的账户输入版本")
                    uncovered = self._close_review_demands(db, row, body, job["id"])
                    self._retain_uncovered_review(body, uncovered)
                    self._write_recommendation(db, row, body)
                    continue
            reason = "主 Agent 明确保留待复核" if row["id"] in pending else "新报告未明确处理这条建议；价格观察继续，投资判断待复核"
            was_pending = body["review_status"] == "needs_review"
            body.update(state="needs_review", review_status="needs_review")
            if not was_pending or not body.get("review_event_key"):
                body["review_reason"] = reason
                self._review_demand(db, row, body, f"unresolved:v{job['input_version']}", reason)
            # Finishing a follow-up without resolving the recommendation is an observable
            # unresolved result, not an invitation to call the model indefinitely.
            db.execute("""UPDATE research_demands SET state='waiting_review',detail='Agent 未明确维持、更新或撤销；等待人工重试或新的外部事件',updated_at=?
                WHERE job_id=? AND state='assigned' AND (event_key LIKE ? OR event_key=?)""",
                (now(), job["id"], f"recommendation:{row['id']}:%", body.get("review_event_key", "")))
            self._write_recommendation(db, row, body, event=not was_pending)
        for item, security_id, old in prepared:
            if old and old["id"] in revoked:
                continue
            stamp = now()
            previous = self._recommendation_body(old) if old else {}
            raw_expiry = item.get("valid_until", previous.get("valid_until", result.get("valid_until")))
            state, review_status, reason = "watching", "valid", "等待条件变化；定性条件由 Agent 复核"
            try:
                expiry = parse_time(raw_expiry)
                valid_until = expiry.isoformat() if expiry else None
                if expiry and expiry <= parse_time(stamp):
                    state, review_status, reason = "expired", "expired", "发布时有效期已过"
            except (ValueError, TypeError):
                valid_until, state, review_status, reason = None, "needs_review", "needs_review", "有效期无法解析，需重新评估"
            conditions = item.get("price_conditions", previous.get("price_conditions")) or []
            if state != "expired" and (not isinstance(conditions, list) or any(not isinstance(c, dict) or price_condition(c, 0) is None for c in conditions)):
                state, review_status, reason = "needs_review", "needs_review", "行情条件无法自动计算，保留原条件供 Agent 复核"
            authored = {key: value for key, value in item.items() if key in {
                "security_id", "symbol", "action", "basis", "material_ids", "references",
                "valid_until", "price_conditions", "agent_conditions"}}
            body = {**previous, **authored, "id": old["id"] if old else uid(), "account_id": job["account_id"], "job_id": job["id"],
                    "input_version": job["input_version"], "security_id": security_id, "symbol": security_id,
                    "state": state, "status": state, "review_status": review_status, "price_state": "unknown" if conditions else "no_conditions",
                    "valid_until": valid_until, "price_conditions": conditions, "generated_at": previous.get("generated_at", stamp),
                    "updated_at": stamp, "reviewed_at": stamp, "review_reason": reason}
            if old:
                body["revision"] = previous.get("revision", 1) + 1
                body["previous_job_id"] = old["job_id"]
                uncovered = self._close_review_demands(db, old, body, job["id"])
                if body["state"] != "expired":
                    self._retain_uncovered_review(body, uncovered)
            else:
                body["revision"] = 1
                body["replaces_ids"] = replaced.get(security_id, [])
                uncovered = replacement_pending.get(security_id, [])
                if uncovered and body["state"] != "expired":
                    body.update(state="needs_review", review_status="needs_review",
                                review_reason="新建议承接旧建议尚未被本次任务覆盖的新事件，等待后续复核",
                                inherited_review_event_keys=list(dict.fromkeys(demand["event_key"] for demand in uncovered)))
                    # Preserve original event identities and link the replacement
                    # to the same next execution without depending on history pages.
                    self._review_demand(db, body, body, "inherited:" + hashlib.sha256(
                        "|".join(body["inherited_review_event_keys"]).encode()).hexdigest(), body["review_reason"])
            if review_status == "needs_review":
                self._review_demand(db, body, body, "invalid_tracking", reason,
                    key=f"invalid_tracking:{job['account_id']}:{job['input_version']}:{security_id}")
            body["status"] = body["state"]
            if old:
                self._write_recommendation(db, old, body)
            else:
                db.execute("INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?,?,?)", (body["id"], job["account_id"], job["id"], job["input_version"], security_id, body["state"], valid_until, dumps(body), stamp, stamp))
                db.execute("INSERT INTO recommendation_events(recommendation_id,state,reason,created_at) VALUES(?,?,?,?)", (body["id"], body["state"], body["review_reason"], stamp))
                for replaced_id in body["replaces_ids"]:
                    prior = db.execute("SELECT * FROM recommendations WHERE id=?", (replaced_id,)).fetchone()
                    old_body = self._recommendation_body(prior)
                    old_body["replaced_by_id"] = body["id"]
                    self._write_recommendation(db, prior, old_body, event=False)

    @staticmethod
    def _recommendation_filter(view):
        active = "state IN ('watching','triggered','needs_review')"
        filters = {"all": "1=1", "active": active, "history": "state IN ('revoked','expired')",
                   "triggered": active + " AND json_extract(body,'$.price_state')='met'",
                   "needs_review": active + " AND (json_extract(body,'$.review_status')='needs_review' OR state='needs_review')"}
        if view not in filters:
            raise ValueError("建议范围必须为 active、triggered、needs_review、history 或 all")
        return filters[view]

    def _recommendation_detail(self, db, row):
        body = self._recommendation_body(row)
        demand = db.execute("SELECT id,state,detail,job_id,event_key FROM research_demands WHERE account_id=? AND (event_key LIKE ? OR event_key=?) ORDER BY CASE WHEN state IN ('pending','assigned','waiting_review','failed') THEN 0 ELSE 1 END,id DESC LIMIT 1",
            (row["account_id"], f"recommendation:{row['id']}:%", body.get("review_event_key", ""))).fetchone()
        body["review_followup"] = dict(demand) if demand else None
        return body

    def recommendations_page(self, account_id, limit=100, offset=0, view="active"):
        limit, offset = min(max(int(limit), 1), 500), max(int(offset), 0)
        where = self._recommendation_filter(view)
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM recommendations WHERE account_id=? AND ({where})", (account_id,)).fetchone()[0]
            rows = db.execute(f"SELECT * FROM recommendations WHERE account_id=? AND ({where}) ORDER BY CASE WHEN state IN ('revoked','expired') THEN 1 ELSE 0 END,generated_at DESC,id DESC LIMIT ? OFFSET ?", (account_id, limit, offset)).fetchall()
            items = [self._recommendation_detail(db, row) for row in rows]
            counts = {name: db.execute(f"SELECT count(*) FROM recommendations WHERE account_id=? AND ({self._recommendation_filter(name)})", (account_id,)).fetchone()[0]
                      for name in ("active", "history", "triggered", "needs_review")}
        return {"items": items, "total": total, "offset": offset, "limit": limit, "next_offset": offset + len(items) if offset + len(items) < total else None,
                "has_more": offset + len(items) < total, "view": view, "counts": counts}

    def recommendations(self, account_id, limit=100, offset=0, view="all"):
        return self.recommendations_page(account_id, limit, offset, view)["items"]

    def recommendation(self, account_id, recommendation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recommendations WHERE account_id=? AND id=?", (account_id, recommendation_id)).fetchone()
            if not row:
                raise KeyError("建议不属于当前账户")
            return self._recommendation_detail(db, row)

    def recommendation_symbols(self, account_id):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT security_id FROM recommendations WHERE account_id=? AND state IN ('watching','triggered','needs_review')", (account_id,))]

    def recommendation_context(self, account_id, job_id, limit=30, target=None):
        page = self.recommendations_page(account_id, limit=limit, view="active")
        with self.connect() as db:
            rows = db.execute("""SELECT DISTINCT r.* FROM recommendations r JOIN research_demands d ON d.account_id=r.account_id
                AND (d.event_key LIKE 'recommendation:' || r.id || ':%' OR d.event_key=json_extract(r.body,'$.review_event_key'))
                WHERE r.account_id=? AND d.job_id=?""", (account_id, job_id)).fetchall()
            if target:
                rows = list({row["id"]: row for row in [*rows, *db.execute("SELECT * FROM recommendations WHERE account_id=? AND security_id=? AND state IN ('watching','triggered','needs_review')", (account_id, target)).fetchall()]}.values())
            page["event_linked_items"] = [self._recommendation_detail(db, row) for row in rows]
        return page

    def retry_recommendation_review(self, account_id, recommendation_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recommendations WHERE account_id=? AND id=?", (account_id, recommendation_id)).fetchone()
            if not row:
                raise KeyError("建议不属于当前账户")
            if row["state"] in ("revoked", "expired"):
                raise ValueError("历史建议不能恢复为活跃建议，请提交新的研究建议")
            body = self._recommendation_body(row)
            body.update(state="needs_review", review_status="needs_review", review_reason="用户请求重新复核")
            key = self._review_demand(db, row, body, f"manual:v{self.account(account_id, db)['version']}", body["review_reason"])
            db.execute("UPDATE research_demands SET state='pending',job_id=NULL,detail='explicit user retry',updated_at=? WHERE event_key=? AND state NOT IN ('assigned','pending')", (now(), key))
            self._write_recommendation(db, row, body)
            self._dispatch_account(db, self.account(account_id, db))
            return dict(db.execute("SELECT * FROM research_demands WHERE event_key=?", (key,)).fetchone())

    def recommendation_events(self, recommendation_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM recommendation_events WHERE recommendation_id=? ORDER BY seq", (recommendation_id,))]

    def review_recommendations(self, quotes=None, stamp=None, material_ids=None, material_account_ids=None):
        """Observe price independently of judgment validity; persist follow-up in the same transaction."""
        from .store import loads
        stamp = stamp or datetime.now(TZ)
        quotes = quotes or {}
        changed = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM recommendations WHERE state IN ('watching','triggered','needs_review')").fetchall()
            for row in rows:
                body, account = self._recommendation_body(row), self.account(row["account_id"], db)
                before = {k: body.get(k) for k in ("state", "review_status", "price_state", "review_reason")}
                try:
                    expired = bool(row["valid_until"] and parse_time(row["valid_until"]) <= stamp)
                except (ValueError, TypeError):
                    expired = False
                if expired:
                    body.update(state="expired", review_status="expired", review_reason="建议有效期已到")
                    self._close_review_demands(db, row, body)
                    self._review_demand(db, row, body, "expired", body["review_reason"])
                else:
                    if row["input_version"] != account["version"]:
                        body.update(state="needs_review", review_status="needs_review", review_reason="账户持仓或偏好版本已变更")
                        self._review_demand(db, row, body, f"input:v{account['version']}", body["review_reason"])
                    if material_ids and (material_account_ids is None or row['account_id'] in material_account_ids):
                        materials = sorted(set(material_ids))
                        new_materials = []
                        for material_id in materials:
                            # An outbox retry may contain a smaller or larger batch.
                            # Identity belongs to each document anchor, never the batch.
                            cause = "material:" + material_id
                            exists = db.execute("SELECT 1 FROM research_demands WHERE event_key=?", (f"recommendation:{row['id']}:{cause}",)).fetchone()
                            if not exists:
                                new_materials.append(material_id)
                                self._review_demand(db, row, body, cause, "公开材料新增或更正：" + material_id)
                        if new_materials:
                            body.update(state="needs_review", review_status="needs_review", review_reason="公开材料新增或更正：" + ",".join(new_materials[:8]))
                    # Needs-review recommendations continue observing every explicit price rule.
                    if row["security_id"] in quotes:
                        quote = quotes[row["security_id"]]
                        fresh = quote.get("status") in ("ok", "live") and quote.get("fresh_for_triggers") is not False
                        try:
                            quote_time = parse_time(quote.get("as_of"))
                            fresh = fresh and quote_time is not None and 0 <= (stamp - quote_time).total_seconds() <= self.settings()["quote_stale_seconds"]
                        except (ValueError, TypeError):
                            fresh = False
                        conditions = body.get("price_conditions") or []
                        if not conditions:
                            body["price_state"] = "no_conditions"
                        elif not fresh:
                            was_fresh = body["price_state"] in ("met", "not_met")
                            body.update(price_state="stale", review_status="needs_review", review_reason="报价过期或时间未知；价格条件尚不能确认，判断待复核")
                            # A new suggestion's first observation of already closed,
                            # stale, or unknown-time quotes is a baseline, not a new
                            # research event. Persist the unknown condition without
                            # buying another report immediately after publication.
                            # A previously usable observation becoming stale remains
                            # a real transition, deduplicated durably by input version.
                            if was_fresh:
                                self._review_demand(db, row, body, f"stale:v{account['version']}", body["review_reason"])
                        else:
                            values = [price_condition(c, quote.get("price")) for c in conditions] if isinstance(conditions, list) else [None]
                            if any(value is None for value in values):
                                body.update(price_state="uncomputable", review_status="needs_review", review_reason="价格条件或报价无法计算，判断待复核")
                                self._review_demand(db, row, body, f"uncomputable:v{account['version']}", body["review_reason"])
                            else:
                                met = all(values)
                                key = condition_key(conditions)
                                latch = db.execute("SELECT met FROM condition_latches WHERE account_id=? AND security_id=? AND condition_key=?", (row["account_id"], row["security_id"], key)).fetchone()
                                db.execute("INSERT OR REPLACE INTO condition_latches VALUES(?,?,?,?,?)", (row["account_id"], row["security_id"], key, int(met), stamp.isoformat()))
                                body.update(price_state="met" if met else "not_met", observed_price=quote.get("price"), price_observed_at=quote.get("as_of"))
                                if body["state"] != "needs_review":
                                    body["state"] = "triggered" if met else "watching"
                                if met and (not latch or not latch[0]):
                                    body.update(review_status="needs_review", review_reason="数值行情条件已满足；投资逻辑仍待 Agent 复核", price_crossing=body.get("price_crossing", 0) + 1)
                                    self._review_demand(db, row, body, f"price:{body['price_crossing']}", body["review_reason"])
                after = {k: body.get(k) for k in before}
                meaningful = before != after
                self._write_recommendation(db, row, body, event=meaningful)
                if meaningful:
                    changed.append({"id": row["id"], "state": body["state"], "review_status": body["review_status"], "price_state": body["price_state"], "reason": body.get("review_reason")})
            # Reports also expire even when the model submitted no structured suggestions.
            for row in db.execute("SELECT id,job_id,result,account_id,input_version,created_at FROM analyses WHERE status='current'").fetchall():
                raw = loads(row["result"]).get("valid_until")
                unusable_on_creation = False
                try:
                    expiry = parse_time(raw)
                    unusable_on_creation = bool(expiry and expiry <= parse_time(row["created_at"]))
                except (ValueError, TypeError):
                    expiry = stamp
                    unusable_on_creation = True
                if expiry and expiry <= stamp:
                    db.execute("UPDATE analyses SET status='expired' WHERE id=?", (row["id"],))
                    db.execute("UPDATE accounts SET analysis_state='expired' WHERE id=? AND latest_job_id=? AND analysis_state='current'", (row["account_id"], row["job_id"]))
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
        run = db.execute('SELECT * FROM global_runs WHERE id=?', (job_id,)).fetchone() if not account_id else None
        current_version = self.universe_snapshot(db)['universe_version']
        version = run['universe_version'] if run else current_version
        applicability = ('current' if version == current_version and (not run or run['applicability'] == 'current') else 'stale')
        for record in records[:100]:
            if not isinstance(record, dict) or record.get("kind") not in ("market", "sector", "candidate"):
                continue
            identity = str(record.get("security_id") or record.get("sector_id") or record.get("title") or "market")
            logical_identity = record["kind"] + ":" + identity
            # Preserve each public synthesis and scope. A late old job never
            # upserts the row belonging to a newer scope or job.
            identity = logical_identity if account_id else logical_identity + ':run:' + str(job_id)
            old = db.execute("SELECT * FROM research_records WHERE identity=? AND account_id=?", (identity, account_id or "")).fetchone()
            if not account_id and applicability == 'current':
                db.execute("""UPDATE research_records SET applicability='superseded'
                    WHERE account_id='' AND logical_identity=? AND universe_version=? AND identity<>? AND applicability='current'""",
                    (logical_identity, version, identity))
            stamp = now()
            body = {**record, "id": old["id"] if old else uid(), "account_id": account_id,
                    "scope": "account" if account_id else "public", "job_id": job_id,
                    'input_version': self.account(account_id, db)['version'] if account_id else None,
                    'universe_version': version, 'applicability': applicability,
                    'logical_identity': logical_identity,
                    'input_snapshot': loads(run['input_snapshot']) if run else None,
                    "first_seen_at": old["first_seen_at"] if old else stamp, "reviewed_at": stamp}
            if old:
                previous = loads(old["body"])
                body["previous_findings"] = str(previous.get("findings", ""))[:4000]
            body = self.resolve_result_references(body, account_id, db)
            db.execute("""INSERT INTO research_records
                (id,identity,account_id,kind,body,first_seen_at,reviewed_at,universe_version,logical_identity,applicability)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity,account_id)
                DO UPDATE SET body=excluded.body,reviewed_at=excluded.reviewed_at,
                universe_version=excluded.universe_version,applicability=excluded.applicability""",
                (body["id"], identity, account_id or "", record["kind"], dumps(body), body["first_seen_at"], stamp,
                 version, logical_identity, applicability))

    def research_records(self, account_id=None, limit=30, offset=0, current_only=True):
        from .store import loads
        with self.connect() as db:
            version = self.universe_snapshot(db)['universe_version']
            rows = db.execute("""SELECT r.* FROM research_records r WHERE r.account_id=?
                AND (?=0 OR (r.applicability='current' AND r.universe_version=? AND NOT EXISTS
                (SELECT 1 FROM research_records n WHERE n.account_id=r.account_id
                 AND n.logical_identity=r.logical_identity AND n.universe_version=r.universe_version
                 AND n.applicability='current' AND n.rowid>r.rowid)))
                ORDER BY r.reviewed_at DESC,r.rowid DESC LIMIT ? OFFSET ?""",
                (account_id or '', int(current_only), version, min(max(limit, 1), 100), max(offset, 0))).fetchall()
            return [self.resolve_result_references({**loads(r['body']), 'universe_version': r['universe_version'],
                     'applicability': r['applicability']}, account_id, db) for r in rows]
