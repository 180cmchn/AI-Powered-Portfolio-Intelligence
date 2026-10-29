"""Daily execution visibility, kept separate from security × horizon samples.

Only configurations observed after this migration are reconstructed. A missed day
never creates a forecast, and an unknown scope never becomes a sample count.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import hashlib
import json

from .calendar import SHANGHAI, trading_date
from .domain import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_window_configurations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, effective_at TEXT NOT NULL,
 fingerprint TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forecast_window_config_account_time ON forecast_window_configurations(account_id,effective_at,id);
CREATE TABLE IF NOT EXISTS forecast_window_runs(
 account_id TEXT NOT NULL, planned_date TEXT NOT NULL, status TEXT NOT NULL,
 discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL, batch_id TEXT,
 scope_status TEXT NOT NULL, expected_count INTEGER, detail TEXT NOT NULL,
 PRIMARY KEY(account_id,planned_date));
CREATE INDEX IF NOT EXISTS forecast_window_runs_date ON forecast_window_runs(planned_date DESC,account_id);
CREATE TABLE IF NOT EXISTS forecast_shared_price_evidence(
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL, data_version TEXT NOT NULL,
 observed_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(request_key,data_version));
CREATE TRIGGER IF NOT EXISTS forecast_shared_evidence_immutable BEFORE UPDATE ON forecast_shared_price_evidence
 BEGIN SELECT RAISE(ABORT,'shared price evidence is immutable'); END;
CREATE INDEX IF NOT EXISTS forecast_evaluation_tasks_due ON forecast_evaluation_tasks(next_attempt,forecast_id);
CREATE INDEX IF NOT EXISTS forecast_evaluations_latest ON forecast_evaluations(forecast_id,revision DESC);
"""


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _local(at=None):
    value = at or now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


class ForecastWindowStore:
    def migrate_forecast_windows(self, db):
        inserted = db.execute("INSERT OR IGNORE INTO meta VALUES('forecast_windows_history_started_at',?)", (_dump(now()),)).rowcount
        if inserted:
            self.record_window_configuration(db, source="migration_observation")

    def record_window_configuration(self, db, account_id=None, *, at=None, source="scheduler_observation", scheduled_enabled=None):
        """Call inside the settings/account transaction; contains no credentials."""
        from .forecasts import DEFAULT_RULE
        stamp = _local(at).isoformat()
        settings = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        settings = json.loads(settings[0]) if settings else {}
        enabled = settings.get("scheduled_research_enabled", True) if scheduled_enabled is None else bool(scheduled_enabled)
        universe = self.universe_snapshot(db)
        accounts = db.execute("SELECT id,version,holdings,created_at FROM accounts WHERE (? IS NULL OR id=?)", (account_id, account_id)).fetchall()
        for account in accounts:
            saved = db.execute("SELECT version,body FROM forecast_settings WHERE account_id=?", (account["id"],)).fetchone()
            rule = json.loads(saved["body"]) if saved else dict(DEFAULT_RULE)
            symbols = sorted({h["symbol"] for h in json.loads(account["holdings"])} | set(universe["universe"].get("candidates", [])))
            body = {"rule": rule, "settings_version": saved["version"] if saved else 1,
                    "scheduled_research_enabled": enabled, "account_version": account["version"],
                    "universe_version": universe["universe_version"], "symbols": symbols,
                    "scope_status": "known", "account_created_at": account["created_at"]}
            fingerprint = hashlib.sha256(_dump(body).encode()).hexdigest()
            old = db.execute("SELECT fingerprint FROM forecast_window_configurations WHERE account_id=? ORDER BY effective_at DESC,id DESC LIMIT 1", (account["id"],)).fetchone()
            if not old or old[0] != fingerprint:
                db.execute("INSERT INTO forecast_window_configurations(account_id,effective_at,fingerprint,body,source) VALUES(?,?,?,?,?)",
                           (account["id"], stamp, fingerprint, _dump(body), source))

    def reconcile_forecast_windows(self, *, at=None, calendar_path=None, scheduled_enabled=None):
        """Idempotently observe elapsed windows, including service sleep gaps.

        A day's obligation is established by any enabled configuration interval
        intersecting its configured window. Later disable/rule edits cannot erase
        an already elapsed eligible interval. A still-open window stays pending.
        """
        at = _local(at)
        stamp = at.isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.record_window_configuration(db, at=at, scheduled_enabled=scheduled_enabled)
            for account in db.execute("SELECT id,created_at FROM accounts").fetchall():
                revisions = [dict(r) for r in db.execute("SELECT * FROM forecast_window_configurations WHERE account_id=? AND effective_at<=? ORDER BY effective_at,id", (account["id"], stamp))]
                for revision in revisions:
                    revision["body"] = json.loads(revision["body"])
                if not revisions:
                    continue
                previous = db.execute("SELECT max(planned_date) FROM forecast_window_runs WHERE account_id=?", (account["id"],)).fetchone()[0]
                # Revisit the most recent day to reflect publication and setting
                # changes; older completed observations remain historical facts.
                history_row = db.execute("SELECT value FROM meta WHERE key='forecast_windows_history_started_at'").fetchone()
                history_day = _local(json.loads(history_row[0])).date() if history_row else _local(revisions[0]["effective_at"]).date()
                first = date.fromisoformat(previous) if previous else min(history_day, _local(revisions[0]["effective_at"]).date())
                first = min(first, at.date())
                for n in range((at.date() - first).days + 1):
                    day = first + timedelta(days=n)
                    day_start = datetime.combine(day, time.min, SHANGHAI)
                    day_end = day_start + timedelta(days=1)
                    try:
                        calendar = trading_date(day, calendar_path)
                    except (ValueError, KeyError, OSError, TypeError):
                        calendar = {"date": day.isoformat(), "trading_day": None, "reason": "calendar_unavailable"}
                    batch = db.execute("SELECT id,state,expected_count,symbols,deadline FROM forecast_batches WHERE account_id=? AND base_date=? AND kind=? ORDER BY created_at LIMIT 1", (account["id"], day.isoformat(), "formal" if self.mode == "live" else "test")).fetchone()
                    detail = {"calendar": calendar, "configuration_ids": [], "windows": [],
                              "model_attribution": "unknown", "unit": "account_trading_day", "disabled_reasons": [],
                              "history_complete_from": revisions[0]["effective_at"]}
                    scope_status, expected, batch_id = "unknown", None, batch["id"] if batch else None
                    intervals, disabled = [], False
                    for i, revision in enumerate(revisions):
                        start = max(day_start, _local(revision["effective_at"]))
                        end = min(day_end, _local(revisions[i + 1]["effective_at"]) if i + 1 < len(revisions) else at)
                        if start > end or (start == end and not (day == at.date() and start == at)):
                            continue
                        body, rule = revision["body"], revision["body"]["rule"]
                        if not rule["enabled"] or not body["scheduled_research_enabled"]:
                            disabled = True
                            reason = "forecast_disabled" if not rule["enabled"] else "scheduled_research_paused"
                            if reason not in detail["disabled_reasons"]:
                                detail["disabled_reasons"].append(reason)
                            continue
                        window_start = datetime.combine(day, time.fromisoformat(rule["start_time"]), SHANGHAI)
                        window_end = datetime.combine(day, time.fromisoformat(rule["deadline_time"]), SHANGHAI)
                        if max(start, window_start) < min(end, window_end) or (day == at.date() and start <= at < window_end and at >= window_start):
                            intervals.append((revision, window_start, window_end))
                            detail["configuration_ids"].append(revision["id"])
                            detail["windows"].append({"start": window_start.isoformat(), "deadline": window_end.isoformat(), "effective_from": start.isoformat(), "effective_until": end.isoformat()})
                    created = _local(account["created_at"])
                    if batch:
                        scope_status, expected = "known_batch", batch["expected_count"]
                        counts = Counter(r[0] for r in db.execute("SELECT state FROM forecast_samples WHERE batch_id=?", (batch["id"],)))
                        detail["batch_states"] = dict(counts)
                        detail["symbols"] = json.loads(batch["symbols"])
                        if counts and all(k in ("predicted", "abstain") for k in counts):
                            status = "completed"
                        elif at.isoformat() >= batch["deadline"] or batch["state"] in ("failed", "closed"):
                            status = "failed" if counts.get("failed") else "incomplete"
                        else:
                            status = "batch_created"
                    elif created >= day_end:
                        status = "not_created"
                    elif calendar["trading_day"] is None:
                        status = "calendar_unknown"
                    elif not calendar["trading_day"]:
                        status = "non_trading_day"
                    elif intervals:
                        status = "missed" if all(at >= end for _, _, end in intervals) else "pending"
                        scopes = {(_dump(r["body"].get("symbols")), tuple(r["body"]["rule"]["horizons"])) for r, _, _ in intervals if r["body"].get("scope_status") == "known"}
                        if len(scopes) == 1 and all(r["body"].get("scope_status") == "known" for r, _, _ in intervals):
                            symbols_json, horizons = next(iter(scopes))
                            detail["symbols"] = json.loads(symbols_json)
                            expected = len(detail["symbols"]) * len(horizons)
                            scope_status = "known_snapshot"
                        elif len(scopes) > 1:
                            scope_status = "changed_during_window"
                        if expected == 0:
                            status = "empty_scope"
                    else:
                        before = [r for r in revisions if _local(r["effective_at"]) <= day_start]
                        latest = next((r for r in reversed(revisions) if _local(r["effective_at"]) <= min(at, day_end)), None)
                        known_start = bool(before) or (created >= day_start and _local(revisions[0]["effective_at"]) <= created)
                        if day == at.date() and latest and latest["body"]["rule"]["enabled"] and latest["body"]["scheduled_research_enabled"] and at.time().replace(tzinfo=None) < time.fromisoformat(latest["body"]["rule"]["start_time"]):
                            status = "scheduled"
                        elif not known_start:
                            status = "history_unknown"
                        else:
                            status = "disabled" if disabled or (latest and not latest["body"]["rule"]["enabled"]) else "outside_enabled_window"
                    prior_run = db.execute("SELECT status,detail FROM forecast_window_runs WHERE account_id=? AND planned_date=?", (account["id"], day.isoformat())).fetchone()
                    detail["status_observed_at"] = stamp
                    if status in ("missed", "incomplete", "failed", "history_unknown", "calendar_unknown"):
                        old_detail = json.loads(prior_run["detail"]) if prior_run else {}
                        detail["gap_discovered_at"] = old_detail.get("gap_discovered_at", stamp)
                    db.execute("""INSERT INTO forecast_window_runs VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(account_id,planned_date) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,
                        batch_id=excluded.batch_id,scope_status=excluded.scope_status,expected_count=excluded.expected_count,detail=excluded.detail""",
                        (account["id"], day.isoformat(), status, stamp, stamp, batch_id, scope_status, expected, _dump(detail)))
        return self.forecast_window_statistics()

    def forecast_window_runs(self, account_id=None, limit=50, offset=0, start_date=None, end_date=None):
        clauses, args = [], []
        for key, value, op in (("account_id", account_id, "="), ("planned_date", start_date, ">="), ("planned_date", end_date, "<=")):
            if value:
                if key == "planned_date":
                    date.fromisoformat(value)
                clauses.append(key + op + "?"); args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit, offset = min(200, max(1, limit)), max(0, offset)
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM forecast_window_runs" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM forecast_window_runs" + where + " ORDER BY planned_date DESC,account_id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        items = [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]
        return {"items": items, "total": total, "has_more": offset + len(items) < total,
                "next_offset": offset + len(items) if offset + len(items) < total else None,
                "history_started_at": self.meta("forecast_windows_history_started_at"),
                "history_note": "升级前未保存的启用状态不倒推；运行日期与证券×期限样本分别统计。"}

    def forecast_window_statistics(self, account_id=None, start_date=None, end_date=None):
        clauses, args = [], []
        for key, value, op in (("account_id", account_id, "="), ("planned_date", start_date, ">="), ("planned_date", end_date, "<=")):
            if value:
                if key == "planned_date":
                    date.fromisoformat(value)
                clauses.append(key + op + "?"); args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute("SELECT status,count(*) AS n FROM forecast_window_runs" + where + " GROUP BY status", args).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        return {"unit": "account_trading_day", "states": counts,
                "known_expected_windows": sum(counts.get(k, 0) for k in ("completed", "batch_created", "failed", "incomplete", "missed", "pending")),
                "history_started_at": self.meta("forecast_windows_history_started_at"),
                "method": "时间运行完整性以账户交易日窗口计数；未知范围不计入证券×期限覆盖或方向准确率。模型归属未知的日期不归入当前模型。"}
