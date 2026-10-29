"""Daily execution visibility, kept separate from security × horizon samples.

Only configurations observed after this migration are reconstructed. A missed day
never creates a forecast, and an unknown scope never becomes a sample count.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import hashlib
import json

from .calendar import SHANGHAI, trading_date, calendar_revision
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
CREATE TABLE IF NOT EXISTS forecast_window_checks(
 account_id TEXT NOT NULL, planned_date TEXT NOT NULL, calendar_revision TEXT NOT NULL,
 checked_at TEXT NOT NULL, next_check TEXT NOT NULL, attempts INTEGER NOT NULL,
 PRIMARY KEY(account_id,planned_date));
CREATE INDEX IF NOT EXISTS forecast_window_checks_due ON forecast_window_checks(account_id,next_check,planned_date);
CREATE TABLE IF NOT EXISTS forecast_window_revisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, planned_date TEXT NOT NULL,
 observed_at TEXT NOT NULL, previous_status TEXT, status TEXT NOT NULL,
 reason TEXT NOT NULL, calendar_revision TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forecast_window_revisions_day ON forecast_window_revisions(account_id,planned_date,id);
CREATE TRIGGER IF NOT EXISTS forecast_window_revisions_immutable BEFORE UPDATE ON forecast_window_revisions
 BEGIN SELECT RAISE(ABORT,'window revisions are immutable'); END;
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
        revision_id = calendar_revision(calendar_path)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_revision = db.execute("SELECT value FROM meta WHERE key='window_calendar_revision'").fetchone()
            if old_revision and json.loads(old_revision[0]) != revision_id:
                db.execute("""UPDATE forecast_evaluation_tasks SET state='recheck_requested',next_attempt=?,updated_at=?,
                    last_error='calendar_revalidation_pending' WHERE forecast_id IN (SELECT id FROM forecasts WHERE direction<>'abstain')""", (stamp, stamp))
            db.execute("INSERT OR REPLACE INTO meta VALUES('window_calendar_revision',?)", (_dump(revision_id),))
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
                cursor_key = 'window_scan_cursor:' + account['id']
                cursor_row = db.execute('SELECT value FROM meta WHERE key=?', (cursor_key,)).fetchone()
                cursor = json.loads(cursor_row[0]) if cursor_row else previous
                first = date.fromisoformat(cursor) if cursor else min(history_day, _local(revisions[0]["effective_at"]).date())
                first = min(first, at.date())
                # The forward cursor is independent from max(planned_date): today's
                # observation must not skip a large sleep gap still being drained.
                forward_end = min(at.date(), first + timedelta(days=127))
                days = {first + timedelta(days=n) for n in range((forward_end - first).days + 1)}
                days.add(at.date())
                old_days = db.execute('''SELECT r.planned_date FROM forecast_window_runs r
                    LEFT JOIN forecast_window_checks c USING(account_id,planned_date)
                    WHERE r.account_id=? AND r.planned_date<? AND
                    (c.calendar_revision IS NULL OR c.calendar_revision<>? OR
                     (r.status IN ('calendar_unknown','history_unknown','pending','batch_created') AND c.next_check<=?))
                    ORDER BY COALESCE(c.checked_at,''),r.planned_date LIMIT 32''',
                    (account['id'], first.isoformat(), revision_id, stamp)).fetchall()
                days.update(date.fromisoformat(r[0]) for r in old_days)
                for day in sorted(days):
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
                        detail["batch_execution_status"] = status
                        if calendar["trading_day"] is None:
                            status = "calendar_unknown"
                            detail["calendar_issue"] = "已有批次的当前日历覆盖缺失；保留原执行事实与样本，不按未知日历新增预测"
                        elif calendar["trading_day"] is False:
                            status = "calendar_conflict"
                            detail["calendar_issue"] = "日历更正为非交易日，与已有批次冲突；保留原账本，评分另行重核"
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
                    prior_run = db.execute("SELECT * FROM forecast_window_runs WHERE account_id=? AND planned_date=?", (account["id"], day.isoformat())).fetchone()
                    old_detail = json.loads(prior_run["detail"]) if prior_run else {}
                    detail["status_observed_at"] = stamp
                    # Preserve the first gap even after it is resolved.
                    if old_detail.get('gap_discovered_at'):
                        detail['gap_discovered_at'] = old_detail['gap_discovered_at']
                    if status in ("missed", "incomplete", "failed", "history_unknown", "calendar_unknown", "calendar_conflict"):
                        detail["gap_discovered_at"] = old_detail.get("gap_discovered_at", stamp)
                    changed = (not prior_run or prior_run['status'] != status or old_detail.get('calendar') != calendar
                               or prior_run['scope_status'] != scope_status or prior_run['expected_count'] != expected
                               or prior_run['batch_id'] != batch_id)
                    if changed:
                        reason = ('first_observation' if not prior_run else 'calendar_reconciled'
                                  if old_detail.get('calendar') != calendar else 'window_state_changed')
                        db.execute('''INSERT INTO forecast_window_revisions(account_id,planned_date,observed_at,previous_status,
                            status,reason,calendar_revision,evidence) VALUES(?,?,?,?,?,?,?,?)''',
                            (account['id'], day.isoformat(), stamp, prior_run['status'] if prior_run else None,
                             status, reason, revision_id, _dump({'previous_detail': old_detail if prior_run else None,
                             'detail': detail, 'scope_status': scope_status, 'expected_count': expected})))
                        detail['last_revision_reason'] = reason
                    elif old_detail.get('last_revision_reason'):
                        detail['last_revision_reason'] = old_detail['last_revision_reason']
                    check = db.execute('SELECT * FROM forecast_window_checks WHERE account_id=? AND planned_date=?',
                                       (account['id'], day.isoformat())).fetchone()
                    attempts = (check['attempts'] + 1) if check and check['calendar_revision'] == revision_id and not changed else 1
                    next_check = (at + timedelta(minutes=min(1440, 5 * 2 ** min(attempts - 1, 9)))).isoformat()
                    db.execute('INSERT OR REPLACE INTO forecast_window_checks VALUES(?,?,?,?,?,?)',
                               (account['id'], day.isoformat(), revision_id, stamp, next_check, attempts))
                    db.execute("""INSERT INTO forecast_window_runs VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(account_id,planned_date) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,
                        batch_id=excluded.batch_id,scope_status=excluded.scope_status,expected_count=excluded.expected_count,detail=excluded.detail""",
                        (account["id"], day.isoformat(), status, stamp, stamp, batch_id, scope_status, expected, _dump(detail)))
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (cursor_key, _dump(forward_end.isoformat())))
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
            items = []
            for r in rows:
                history = db.execute('''SELECT id,observed_at,previous_status,status,reason,calendar_revision,evidence
                    FROM forecast_window_revisions WHERE account_id=? AND planned_date=? ORDER BY id DESC LIMIT 20''',
                    (r['account_id'],r['planned_date'])).fetchall()
                count = db.execute('SELECT count(*) FROM forecast_window_revisions WHERE account_id=? AND planned_date=?',
                                   (r['account_id'],r['planned_date'])).fetchone()[0]
                items.append({**dict(r), 'detail': json.loads(r['detail']), 'revision_count': count,
                              'revisions': [{**dict(h), 'evidence': json.loads(h['evidence'])} for h in history]})
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
                "calendar_conflict_windows": counts.get("calendar_conflict", 0),
                "history_started_at": self.meta("forecast_windows_history_started_at"),
                "method": "时间运行完整性以账户交易日窗口计数；未知范围不计入证券×期限覆盖或方向准确率。日历与已有批次冲突单独计数，保留原批次样本但不计为当前确认的应运行窗口。模型归属未知的日期不归入当前模型。"}
