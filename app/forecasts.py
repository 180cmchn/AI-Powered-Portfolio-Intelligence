"""Immutable manager forecasts and deterministic, independently scheduled scoring.

No model is called here. Forecast publication shares Store.publish's transaction;
the expected universe exists before a report and remains visible on failure.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import uuid

from .calendar import SHANGHAI, advance_trading_days, trading_date, calendar_revision
from .domain import now
from .providers import normalize_symbol
from .forecast_prices import closing_pair
from .forecast_identity import ForecastIdentityStore, SCHEMA as IDENTITY_SCHEMA, scoring_rule


SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_settings(
 account_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_batches(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL, base_date TEXT NOT NULL,
 rule_version TEXT NOT NULL, rule TEXT NOT NULL, symbols TEXT NOT NULL, expected_count INTEGER NOT NULL,
 created_at TEXT NOT NULL, deadline TEXT NOT NULL, state TEXT NOT NULL, job_id TEXT UNIQUE,
 input_version INTEGER NOT NULL, team_snapshot TEXT NOT NULL, baseline_evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_samples(
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
 horizon INTEGER NOT NULL, label TEXT NOT NULL, target_date TEXT, calendar_evidence TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, UNIQUE(batch_id,symbol,horizon));
CREATE TABLE IF NOT EXISTS forecast_job_bindings(
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, prior_job_id TEXT, job_id TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_preparations(
 batch_id TEXT NOT NULL, symbol TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, next_attempt_at TEXT,
 lease_until TEXT, ready_at TEXT, reason TEXT, PRIMARY KEY(batch_id,symbol));
CREATE TABLE IF NOT EXISTS forecast_research_scopes(
 job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, ready_symbols TEXT NOT NULL,
 assigned_at TEXT NOT NULL, purpose TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts(
 id TEXT PRIMARY KEY, sample_id TEXT NOT NULL, batch_id TEXT NOT NULL, account_id TEXT NOT NULL,
 job_id TEXT NOT NULL, report_id TEXT NOT NULL, role_id TEXT NOT NULL CHECK(role_id='manager'),
 kind TEXT NOT NULL, symbol TEXT NOT NULL, base_date TEXT NOT NULL, horizon INTEGER NOT NULL,
 target_date TEXT, direction TEXT NOT NULL CHECK(direction IN ('up','sideways','down','abstain')),
 reason TEXT NOT NULL, citations TEXT NOT NULL, rule_version TEXT NOT NULL, rule TEXT NOT NULL,
 team_id TEXT NOT NULL, team_snapshot TEXT NOT NULL, manager_model TEXT, manager_effort TEXT,
 published_at TEXT NOT NULL, information_cutoff TEXT NOT NULL, report TEXT NOT NULL,
 revises_id TEXT, UNIQUE(sample_id,job_id));
CREATE UNIQUE INDEX IF NOT EXISTS forecasts_formal_once ON forecasts(sample_id) WHERE kind IN ('formal','test');
CREATE INDEX IF NOT EXISTS forecasts_account_date ON forecasts(account_id,base_date);
CREATE TRIGGER IF NOT EXISTS forecasts_immutable BEFORE UPDATE ON forecasts
 BEGIN SELECT RAISE(ABORT,'published forecasts are immutable'); END;
CREATE TABLE IF NOT EXISTS forecast_evaluation_tasks(
 forecast_id TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt TEXT NOT NULL, last_error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_calendar_resolutions(
 forecast_id TEXT PRIMARY KEY, target_date TEXT NOT NULL, evidence TEXT NOT NULL, resolved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_evaluation_leases(
 forecast_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_evaluations(
 id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL, revision INTEGER NOT NULL, evaluated_at TEXT NOT NULL,
 data_version TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, return_ratio TEXT, actual_direction TEXT,
 hit INTEGER, evidence TEXT NOT NULL, correction_reason TEXT,
 UNIQUE(forecast_id,revision));
CREATE TABLE IF NOT EXISTS forecast_price_observations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, forecast_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 data_version TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL);
"""

DEFAULT_RULE = {"enabled": False, "horizons": [1, 5, 20], "labels": ["短线", "中线", "长线"],
                "threshold": "0.005", "start_time": "15:30", "deadline_time": "18:00",
                "price_policy": "same_source_pair_v1"}


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _load(value, default=None):
    return json.loads(value) if value else default


def _digest(obj):
    return hashlib.sha256(_dump(obj).encode()).hexdigest()


def _local(at=None):
    at = at or datetime.now(SHANGHAI)
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    return at.replace(tzinfo=SHANGHAI) if at.tzinfo is None else at.astimezone(SHANGHAI)


def _clock(value):
    try:
        result = time.fromisoformat(value)
        if len(value) != 5 or result.tzinfo:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError("批次时间须为 HH:MM（Asia/Shanghai）")


def validate_rule(payload):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULT_RULE):
        raise ValueError("不支持的预测规则字段")
    rule = {**DEFAULT_RULE, **payload}
    if not isinstance(rule["enabled"], bool):
        raise ValueError("enabled 须为布尔值")
    horizons = rule["horizons"]
    if (not isinstance(horizons, list) or not 1 <= len(horizons) <= 10
            or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000 for n in horizons)
            or len(set(horizons)) != len(horizons)):
        raise ValueError("期限须为 1 至 1000 个交易日、不重复，最多十项")
    labels = rule["labels"]
    if not isinstance(labels, list) or len(labels) != len(horizons) or any(not isinstance(s, str) or not 1 <= len(s) <= 20 for s in labels):
        raise ValueError("每个期限须有一个不超过 20 字的显示标签")
    try:
        threshold = Decimal(str(rule["threshold"]))
    except (InvalidOperation, TypeError):
        raise ValueError("震荡阈值无效")
    if not threshold.is_finite() or not Decimal(0) <= threshold < Decimal(1):
        raise ValueError("震荡阈值应为比例，0.5% 写作 0.005，范围 [0,1)")
    rule["threshold"] = str(threshold)
    start, end = _clock(rule["start_time"]), _clock(rule["deadline_time"])
    if not time(15) <= start < end <= time(23, 59):
        raise ValueError("收盘批次开始时间须不早于 15:00，截止须晚于开始且在当日")
    if rule["price_policy"] != "same_source_pair_v1":
        raise ValueError("不支持的价格比较规则")
    return rule


def classify_return(base, end, threshold="0.005"):
    a, b, tau = Decimal(str(base)), Decimal(str(end)), Decimal(str(threshold))
    if not all(n.is_finite() for n in (a, b, tau)) or a <= 0 or b <= 0 or tau < 0:
        raise ValueError("价格和阈值须为有效有限数值，价格大于零")
    # Classify by cross multiplication before decimal display division/rounding.
    with localcontext() as ctx:
        ctx.prec = max(60, len(a.as_tuple().digits) + len(b.as_tuple().digits) + len(tau.as_tuple().digits) + 10)
        delta = b - a
        direction = "up" if delta > a * tau else "down" if delta < -a * tau else "sideways"
        return {"return_ratio": str(b / a - 1), "actual_direction": direction}


def _valid_direction_baseline(batch, symbol):
    """Exact immutable close evidence is required for formal directions only."""
    baseline = batch["baseline_evidence"].get(symbol) or {}
    try:
        price = Decimal(str(baseline.get("base_price")))
        return (baseline.get("status") == "ready" and baseline.get("symbol") == symbol
            and baseline.get("base_date") == batch["base_date"]
            and baseline.get("target_date") == batch["base_date"]
            and baseline.get("price_policy") == batch["rule"]["price_policy"]
            and bool(baseline.get("source")) and price.is_finite() and price > 0)
    except (InvalidOperation, TypeError, ValueError):
        return False


def _safe_team(config):
    """Whitelist identity data; never persist process-only credentials."""
    config = config or {}
    team = config.get("_agent_team") or config.get("_team_snapshot") or config.get("team_snapshot") or config.get("agent_team") or {}
    if not team:
        team = {"roles": [{"role_id": "manager", "model_id": config.get("model_name"),
                 "reasoning_effort": config.get("reasoning_effort", "medium"), "provider_id": config.get("provider_id")}],
                "legacy": True}
    forbidden = {"api_key", "key", "secret", "credential", "model_api_key", "_api_key"}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k.lower() not in forbidden and "secret" not in k.lower()
                    and not k.lower().endswith("api_key")}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(team)


def _manager_identity(team):
    roles = team.get("roles", {})
    if isinstance(roles, list):
        manager = next((r for r in roles if r.get("role_id") == "manager"), {})
    else:
        manager = roles.get("manager", {})
    binding = manager.get("resolved") or manager.get("binding") or manager.get("effective") or manager.get("config") or manager
    return binding.get("model_id", binding.get("model_name")), binding.get("reasoning_effort", "medium")


SCHEMA += IDENTITY_SCHEMA


class ForecastStore(ForecastIdentityStore):
    def migrate_forecasts(self, db):
        columns = {r[1] for r in db.execute("PRAGMA table_info(forecast_preparations)")}
        for name in ('lease_token', 'request_started_at'):
            if name not in columns:
                db.execute(f"ALTER TABLE forecast_preparations ADD COLUMN {name} TEXT")
        self.migrate_forecast_identity(db)
        # No backfill from historical reports; old accounts inherit disabled rules.
        db.execute("INSERT OR IGNORE INTO meta VALUES('forecast_migration',?)", (_dump({
            "version": 1, "at": now(), "new_batches_enabled": False, "historical_backfill": False}),))

    def forecast_settings(self, account_id):
        self.account(account_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM forecast_settings WHERE account_id=?", (account_id,)).fetchone()
        return {"account_id": account_id, "version": row["version"] if row else 1,
                **(_load(row["body"]) if row else DEFAULT_RULE), "timezone": "Asia/Shanghai",
                "frequency": "每交易日一个固定批次；启用会产生模型费用"}

    def save_forecast_settings(self, account_id, payload):
        current = self.forecast_settings(account_id)
        rule = validate_rule({**{k: current[k] for k in DEFAULT_RULE}, **payload})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT version,body FROM forecast_settings WHERE account_id=?", (account_id,)).fetchone()
            version = old[0] + int(_load(old[1]) != rule) if old else 1
            db.execute("INSERT OR REPLACE INTO forecast_settings VALUES(?,?,?,?)", (account_id, version, _dump(rule), now()))
            self.record_window_configuration(db, account_id, source="forecast_settings_saved")
        return self.forecast_settings(account_id)

    def create_forecast_batch(self, account_id, *, at=None, kind="formal", symbols=None,
                              baseline_evidence=None, team_snapshot=None, calendar_path=None, expected_input_version=None):
        at = _local(at)
        settings = self.forecast_settings(account_id)
        rule = validate_rule({k: settings[k] for k in DEFAULT_RULE})
        if kind not in ("formal", "exploratory", "test"):
            raise ValueError("未知预测批次类型")
        if kind == "formal" and self.mode != "live":
            kind = "test"
        if trading_date(at.date(), calendar_path)["trading_day"] is not True:
            raise ValueError("批次基准必须是已覆盖的交易日")
        if kind in ("formal", "test") and not _clock(rule["start_time"]) <= at.time().replace(tzinfo=None) < _clock(rule["deadline_time"]):
            raise ValueError("不在当日固定批次生成窗口内，禁止补写过去预测")
        account = self.account(account_id)
        if symbols is None:
            universe = self.universe_snapshot()["universe"]
            symbols = [h["symbol"] for h in account["holdings"]] + universe.get("candidates", [])
        symbols = sorted({normalize_symbol(s) for s in symbols})
        # A finite product is persisted in full; UI pagination never drops samples.
        rule_version = scoring_rule(rule)[0]
        deadline = datetime.combine(at.date(), _clock(rule["deadline_time"]), SHANGHAI).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_account = self.account(account_id, db)
            if current_account["version"] != account["version"] or (expected_input_version is not None and current_account["version"] != expected_input_version):
                raise ValueError("采集基准期间账户输入已变化，等待下一轮重新锁定批次")
            existing = None
            # Rule edits during a day may not create a second official batch.
            if not existing and kind in ("formal", "test"):
                existing = db.execute("SELECT id FROM forecast_batches WHERE account_id=? AND kind=? AND base_date=?",
                    (account_id, kind, at.date().isoformat())).fetchone()
            if existing:
                return self._forecast_batch(db, existing[0])
            batch_id = uuid.uuid4().hex
            db.execute("INSERT INTO forecast_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                (batch_id, account_id, kind, at.date().isoformat(), rule_version, _dump(rule), _dump(symbols),
                 len(symbols) * len(rule["horizons"]), at.isoformat(), deadline, "open", account["version"],
                 _dump(team_snapshot or {}), _dump(baseline_evidence or {})))
            self._register_scoring_rule(db, {"id": batch_id, "rule_version": rule_version, "rule": rule})
            for symbol in symbols:
                for horizon, label in zip(rule["horizons"], rule["labels"]):
                    try:
                        target = advance_trading_days(at.date().isoformat(), horizon, calendar_path)
                    except (ValueError, OSError, KeyError):
                        target = {"target_date": None, "status": "calendar_gap", "reason": "交易日历不可用"}
                    db.execute("INSERT INTO forecast_samples VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex,
                        batch_id, account_id, symbol, horizon, label, target["target_date"], _dump(target),
                        "uncovered", target.get("reason")))
            self._ensure_forecast_preparations(db, batch_id)
            return self._forecast_batch(db, batch_id)

    def _ensure_forecast_preparations(self, db, batch_id):
        row = db.execute("SELECT symbols,baseline_evidence FROM forecast_batches WHERE id=?", (batch_id,)).fetchone()
        evidence = _load(row["baseline_evidence"], {})
        for symbol in _load(row["symbols"], []):
            old = evidence.get(symbol) or {}
            state = "ready" if old.get("status") == "ready" else "terminal" if old.get("status") == "unassessable" else "waiting_data" if old else "unattempted"
            # Legacy observation proves availability only, not historical attempt counts.
            db.execute("INSERT OR IGNORE INTO forecast_preparations(batch_id,symbol,state,reason) VALUES(?,?,?,?)",
                       (batch_id, symbol, state, old.get("reason")))

    def _forecast_batch(self, db, batch_id, *, include_attribution=False):
        row = db.execute("SELECT * FROM forecast_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError("预测批次不存在")
        batch = dict(row)
        for field in ("rule", "symbols", "team_snapshot", "baseline_evidence"):
            batch[field] = _load(batch[field])
        batch["coverage"] = dict(db.execute("SELECT state,count(*) FROM forecast_samples WHERE batch_id=? GROUP BY state", (batch_id,)))
        projection = db.execute("SELECT scoring_rule_id,status FROM forecast_rule_projections WHERE batch_id=?", (batch_id,)).fetchone()
        batch["scoring_rule_id"] = projection[0] if projection else None
        batch["rule_identity_status"] = projection[1] if projection else "unknown"
        batch["preparation"] = dict(db.execute("SELECT state,count(*) FROM forecast_preparations WHERE batch_id=? GROUP BY state", (batch_id,)))
        research = db.execute("SELECT state FROM jobs WHERE id=?", (batch["job_id"],)).fetchone() if batch["job_id"] else None
        batch["research_state"] = research[0] if research else "not_queued"
        batch["attempts"] = [{**dict(r), "team_snapshot": _load(r["team_snapshot"]), "requests": _load(r["requests"])} for r in db.execute("SELECT * FROM forecast_execution_attempts WHERE batch_id=? ORDER BY id", (batch_id,))]
        if include_attribution:
            batch['sample_attribution'] = self.forecast_sample_attribution(db, batch_id)
        return batch

    def forecast_batches(self, account_id=None, limit=100, offset=0, summary=False):
        with self.connect() as db:
            if summary:
                rows = db.execute("""SELECT b.id,b.account_id,b.kind,b.base_date,b.rule_version,b.expected_count,
                    b.created_at,b.deadline,b.state,b.job_id,b.input_version,p.scoring_rule_id,p.status AS rule_identity_status,
                    json_extract(b.team_snapshot,'$.team_id') AS team_id,
                    json_extract(b.team_snapshot,'$.manager_model') AS manager_model,
                    (SELECT count(*) FROM forecast_execution_attempts a WHERE a.batch_id=b.id) AS attempt_count
                    FROM forecast_batches b LEFT JOIN forecast_rule_projections p ON p.batch_id=b.id
                    WHERE (? IS NULL OR b.account_id=?) ORDER BY b.created_at DESC,b.id LIMIT ? OFFSET ?""",
                    (account_id, account_id, min(200, max(1, limit)), max(0, offset))).fetchall()
                return [{**dict(row), **self.forecast_sample_attribution(db, row['id']), "coverage": dict(db.execute("SELECT state,count(*) FROM forecast_samples WHERE batch_id=? GROUP BY state", (row["id"],))),
                         "preparation": dict(db.execute("SELECT state,count(*) FROM forecast_preparations WHERE batch_id=? GROUP BY state", (row["id"],))),
                         "research_state": (db.execute("SELECT state FROM jobs WHERE id=?", (row["job_id"],)).fetchone() or ["not_queued"])[0]} for row in rows]
            rows = db.execute("SELECT id FROM forecast_batches WHERE (? IS NULL OR account_id=?) ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (account_id, account_id, min(200, max(1, limit)), max(0, offset))).fetchall()
            return [self._forecast_batch(db, r[0], include_attribution=True) for r in rows]

    def bind_forecast_job(self, batch_id, job_id, *, symbols=None, horizons=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._forecast_batch(db, batch_id)
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["account_id"] != batch["account_id"] or job["input_version"] != batch["input_version"]:
                raise ValueError("批次与任务账户/输入版本不一致")
            if batch["job_id"] and batch["job_id"] != job_id:
                raise ValueError("批次已经绑定任务")
            db.execute("UPDATE forecast_batches SET job_id=? WHERE id=?", (job_id, batch_id))
            if symbols is not None or horizons is not None:
                if job['state'] != 'queued' or job['config_snapshot']:
                    raise ValueError('仅尚未领取且未冻结配置的任务可设置证券/期限子范围')
                db.execute('DELETE FROM forecast_research_scope_samples WHERE job_id=?', (job_id,))
                db.execute("""INSERT INTO forecast_research_scopes(job_id,batch_id,ready_symbols,assigned_at,purpose)
                    VALUES(?,?,?,?,'explicit_subset') ON CONFLICT(job_id) DO UPDATE SET
                    ready_symbols=excluded.ready_symbols,purpose='explicit_subset'""",
                    (job_id, batch_id, _dump(sorted(symbols if symbols is not None else batch['symbols'])), now()))
            self.assign_forecast_scope(db, job_id, batch_id, symbols=symbols, horizons=horizons)
            if job["config_snapshot"]:
                self.bind_forecast_attempt(db, job_id, job["attempts"], _load(job["config_snapshot"]))
            if not batch["job_id"]:
                db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,NULL,?,'initial',?)", (batch_id, job_id, now()))

    def enqueue_forecast_batch(self, batch_id, at=None):
        """Initial execution plus at most one coalesced, newly-ready supplement.

        Scopes are assignment records, not claims that the model read data. Actual
        context/tool delivery returns the current rows and immutable evidence IDs.
        """
        stamp = _local(at).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._forecast_batch(db, batch_id)
            prior_job = db.execute("SELECT * FROM jobs WHERE id=?", (batch["job_id"],)).fetchone() if batch["job_id"] else None
            if prior_job and prior_job["state"] in ("queued", "running", "waiting_config"):
                return dict(prior_job)
            if stamp >= batch["deadline"] or batch["state"] in ("failed", "closed"):
                return None
            self._ensure_forecast_preparations(db, batch_id)
            ready = {s for s, v in batch["baseline_evidence"].items() if v.get("status") == "ready"}
            eligible = {r[0] for r in db.execute("""SELECT DISTINCT s.symbol FROM forecast_samples s
                WHERE s.batch_id=? AND NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id)""", (batch_id,))} & ready
            if not eligible:
                return dict(prior_job) if prior_job else None
            account = self.account(batch["account_id"], db)
            if account["version"] != batch["input_version"]:
                db.execute("UPDATE forecast_samples SET state='failed',reason='基准等待期间账户输入已变化' WHERE batch_id=? AND state NOT IN ('predicted','abstain')", (batch_id,))
                db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch_id,))
                return None
            purpose = "initial"
            if prior_job:
                # A user cancellation does not authorize an automatic paid retry.
                if prior_job["state"] not in ("completed", "failed"):
                    return None
                scopes = list(db.execute("SELECT ready_symbols,purpose FROM forecast_research_scopes WHERE batch_id=?", (batch_id,)))
                assigned = {s for scope in scopes for s in _load(scope["ready_symbols"], [])}
                new_ready = eligible - assigned
                # Historical tasks with unknown assigned scope are never guessed.
                if not scopes:
                    db.execute("UPDATE forecast_samples SET reason='历史任务缺少已分配范围记录，不能自动推断补充授权；可在原截止前显式重试' WHERE batch_id=? AND state='uncovered'", (batch_id,))
                    return None
                if not new_ready:
                    if prior_job["state"] == "completed":
                        db.execute("UPDATE forecast_samples SET reason='研究已结束且无新增基准，此项未发布；可在原截止前显式重试' WHERE batch_id=? AND state='uncovered'", (batch_id,))
                    return None
                if any(scope["purpose"] == "supplement" for scope in scopes):
                    db.execute("UPDATE forecast_samples SET reason='基准已补齐，但本批次自动补充研究预算已用尽；未发布' WHERE batch_id=? AND state='uncovered'", (batch_id,))
                    return None
                if db.execute("SELECT 1 FROM jobs WHERE account_id=? AND state IN ('queued','running','waiting_config')", (account["id"],)).fetchone():
                    return None
                pending = db.execute("SELECT 1 FROM forecast_preparations WHERE batch_id=? AND state IN ('unattempted','waiting_data','preparing')", (batch_id,)).fetchone()
                # Merge arrivals across one scheduler interval; never one team per quote.
                latest = db.execute("SELECT max(ready_at) FROM forecast_preparations WHERE batch_id=? AND state='ready'", (batch_id,)).fetchone()[0]
                if pending and latest and _local(stamp) < _local(latest) + timedelta(seconds=60):
                    return None
                purpose = "supplement"
            event_key = "forecast_batch:" + batch_id + (":supplement:1" if purpose == "supplement" else "")
            if prior_job:
                # Keep the generic explicit-retry hook from consuming this
                # automatic supplement's identity or budget record.
                db.execute("UPDATE forecast_batches SET state='supplement_pending' WHERE id=?", (batch_id,))
            job = self._enqueue(db, account, event_key, event_key=event_key)
            if job["state"] not in ("queued", "running", "waiting_config"):
                db.execute("UPDATE forecast_samples SET state='failed',reason='原批次任务在绑定前已结束' WHERE batch_id=? AND state NOT IN ('predicted','abstain')", (batch_id,))
                db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch_id,))
                return None
            db.execute("UPDATE forecast_batches SET job_id=?,state='open' WHERE id=?", (job["id"], batch_id))
            db.execute("INSERT OR IGNORE INTO forecast_research_scopes VALUES(?,?,?,?,?)", (job["id"], batch_id, _dump(sorted(ready)), stamp, purpose))
            db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,?,?,?,?)",
                       (batch_id, batch["job_id"], job["id"], "bounded_baseline_supplement" if prior_job else "atomic_enqueue", stamp))
            self.assign_forecast_scope(db, job["id"], batch_id, symbols=eligible, assigned_at=stamp)
            return job

    def reconcile_forecast_jobs(self, db, account_id, new_job_id, input_version, at=None):
        """Store._enqueue hook after superseding old executions, in its transaction."""
        stamp = _local(at).isoformat()
        rows = db.execute("""SELECT b.* FROM forecast_batches b JOIN jobs j ON j.id=b.job_id
            WHERE b.account_id=? AND j.state IN ('superseded','cancelled','failed','completed')
            AND b.state IN ('open','published') ORDER BY b.created_at DESC""", (account_id,)).fetchall()
        already_bound = db.execute("SELECT 1 FROM forecast_batches WHERE job_id=?", (new_job_id,)).fetchone() is not None
        requested = db.execute("SELECT reason FROM jobs WHERE id=?", (new_job_id,)).fetchone()
        explicit_retry = bool(requested and (requested[0] in ("manual", "retry", "manual_retry") or requested[0].startswith("retry:")))
        for batch in rows:
            previous_state = db.execute("SELECT state FROM jobs WHERE id=?", (batch["job_id"],)).fetchone()
            if previous_state and previous_state[0] == "completed" and not explicit_retry:
                continue
            unfinished = db.execute("SELECT 1 FROM forecast_samples s WHERE batch_id=? AND NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id)", (batch["id"],)).fetchone()
            if (not already_bound and batch["kind"] in ("formal", "test") and unfinished
                    and batch["input_version"] == input_version and stamp < batch["deadline"]):
                db.execute("UPDATE forecast_batches SET job_id=?,state='open' WHERE id=?", (new_job_id, batch["id"]))
                db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (batch["id"], batch["job_id"], new_job_id, "同账户输入版本的执行重试/合并；保持原固定批次规则与范围", stamp))
                db.execute("INSERT OR IGNORE INTO forecast_research_scopes VALUES(?,?,?,?,?)", (new_job_id, batch["id"],
                    _dump(sorted(s for s, v in _load(batch["baseline_evidence"], {}).items() if v.get("status") == "ready")), stamp, "explicit_retry"))
                self.assign_forecast_scope(db, new_job_id, batch["id"], assigned_at=stamp)
                already_bound = True
            else:
                reason = "账户输入版本更新使原批次任务过时" if batch["input_version"] != input_version else "原批次执行已取消或被替代"
                db.execute("UPDATE forecast_samples SET state='failed',reason=? WHERE batch_id=? AND state='uncovered'", (reason, batch["id"]))

    def prepare_exploratory_forecast_job(self, job_id, at=None, calendar_path=None):
        """Freeze a separate exploratory scope before normal research starts.

        Intraday studies use the most recent completed session close. They never
        replace a fixed after-close formal batch or enter its accuracy metric.
        """
        at = _local(at)
        with self.connect() as db:
            existing = db.execute("SELECT id FROM forecast_batches WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                return self._forecast_batch(db, existing[0])
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or not job["account_id"]:
                return None
        account = self.account(job["account_id"])
        universe = self.universe_snapshot()["universe"]
        if not account["holdings"] and not universe.get("candidates"):
            return None
        day = at.date() if at.time().replace(tzinfo=None) >= time(15) else at.date() - timedelta(days=1)
        # No heuristic weekdays outside verified coverage.
        for _ in range(370):
            try:
                state = trading_date(day, calendar_path)
            except (OSError, ValueError, KeyError):
                return None
            if state["trading_day"] is None:
                return None
            if state["trading_day"]:
                break
            day -= timedelta(days=1)
        else:
            return None
        batch = self.create_forecast_batch(job["account_id"], at=datetime.combine(day, time(15, 30), SHANGHAI),
                                           kind="exploratory", calendar_path=calendar_path)
        # Exploration has a real pre-run lock time and a bounded session deadline;
        # the original close date is explicitly distinct from publication date.
        with self.connect() as db:
            db.execute("UPDATE forecast_batches SET created_at=?,deadline=?,input_version=? WHERE id=?", (
                at.isoformat(), (at + timedelta(hours=24)).isoformat(), job["input_version"], batch["id"]))
        self.bind_forecast_job(batch["id"], job_id)
        return self.forecast_job_context(job_id)

    def forecast_job_context(self, job_id, limit=100, offset=0):
        with self.connect() as db:
            row = db.execute("SELECT id FROM forecast_batches WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            batch = self._forecast_batch(db, row[0])
            items = [dict(r) for r in db.execute("SELECT id,symbol,horizon,label,target_date,state,reason FROM forecast_samples WHERE batch_id=? ORDER BY symbol,horizon LIMIT ? OFFSET ?",
                     (row[0], min(200, max(1, limit)), max(0, offset)))]
            summary = {k: batch[k] for k in ("id", "account_id", "kind", "base_date", "rule_version", "rule",
                        "expected_count", "created_at", "deadline", "state", "input_version", "coverage", "scoring_rule_id", "rule_identity_status")}
            publication_open = (_local().isoformat() < batch["deadline"]
                and self.account(batch["account_id"], db)["version"] == batch["input_version"]
                and batch["state"] not in ("closed", "failed"))
            summary["publication_open"] = publication_open
            summary["baseline_required_for_direction"] = batch["kind"] == "formal"
            preparations = {r["symbol"]: dict(r) for r in db.execute("SELECT symbol,state,attempts,last_attempt_at,next_attempt_at,ready_at,reason FROM forecast_preparations WHERE batch_id=?", (row[0],))}
            for item in items:
                baseline = batch["baseline_evidence"].get(item["symbol"])
                item["preparation"] = preparations.get(item["symbol"], {"state": "unknown", "attempts": None})
                item["baseline"] = baseline
                item["already_published"] = bool(db.execute("SELECT 1 FROM forecasts WHERE sample_id=?", (item["id"],)).fetchone())
                item["baseline_ready"] = bool(baseline and baseline.get("status") == "ready")
                item["baseline_valid_for_direction"] = _valid_direction_baseline(batch, item["symbol"])
                item["baseline_required_for_direction"] = batch["kind"] == "formal"
                item["direction_eligible"] = bool(publication_open and not item["already_published"]
                    and (not item["baseline_required_for_direction"] or item["baseline_valid_for_direction"]))
            scope = db.execute("SELECT ready_symbols,purpose FROM forecast_research_scopes WHERE job_id=?", (job_id,)).fetchone()
            if scope and scope['purpose'] != 'explicit_subset':
                delivered = set(_load(scope[0], [])) | {item["symbol"] for item in items if item["baseline_ready"]}
                db.execute("UPDATE forecast_research_scopes SET ready_symbols=? WHERE job_id=?", (_dump(sorted(delivered)), job_id))
                self.assign_forecast_scope(db, job_id, batch['id'], sample_ids=[item['id'] for item in items if item['baseline_ready']])
                attempt = db.execute('SELECT id FROM forecast_execution_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1', (job_id,)).fetchone()
                if attempt:
                    self.bind_sample_attempt(db, job_id, attempt['id'])
            if scope and scope['purpose'] == 'explicit_subset':
                allocated = {r[0] for r in db.execute('SELECT sample_id FROM forecast_research_scope_samples WHERE job_id=?', (job_id,))}
                for item in items:
                    item['assigned_to_execution'] = item['id'] in allocated
                    item['direction_eligible'] = item['direction_eligible'] and item['assigned_to_execution']
            summary["preparation"] = batch["preparation"]
            summary["research_state"] = batch["research_state"]
            active = db.execute('SELECT team_snapshot FROM forecast_execution_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1', (job_id,)).fetchone()
            team = _load(active[0]) if active else {}
            summary.update(team_id=team.get("team_id"), manager_model=team.get("manager_model"), manager_effort=team.get("manager_effort"))
            return {**summary, "items": items, "offset": offset, "has_more": offset + len(items) < batch["expected_count"],
                    "next_offset": offset + len(items) if offset + len(items) < batch["expected_count"] else None,
                    "delivered_at": now(), "delivered_symbols": sorted({item["symbol"] for item in items}),
                    "instruction": "本次仅交付 items 对应的目标、准备状态和基准证据；其他页未交付。研究运行中基准仍会补采，可再次调用 list_forecast_targets 获取新增数据。仅 manager 提交新判断；already_published 的方向与弃权不可再改。未发布项可在原截止前补充；未来目标日价格只用于到期评分。"}

    def publish_forecasts(self, db, job, result, analysis_id, published_at=None):
        """Called only inside the successful current-report publication transaction."""
        at = _local(published_at)
        batch_row = db.execute("SELECT id FROM forecast_batches WHERE job_id=?", (job["id"],)).fetchone()
        if not batch_row:
            return []
        batch = self._forecast_batch(db, batch_row[0])
        if at.isoformat() >= batch["deadline"]:
            db.execute("UPDATE forecast_samples SET state='missed',reason='超过固定批次截止' WHERE batch_id=? AND state IN ('uncovered','waiting_data')", (batch["id"],))
            db.execute("UPDATE forecast_batches SET state='closed' WHERE id=?", (batch["id"],))
            return []
        if batch["input_version"] != job["input_version"]:
            raise ValueError("预测批次输入版本过时")
        config = _load(job.get("config_snapshot"), {}) if isinstance(job.get("config_snapshot"), str) else job.get("config_snapshot", {})
        team = _safe_team(config)
        # Actual called instances retain their own role/model provenance.
        team["actual_instances"] = result.get("agent_instances", result.get("team_instances", result.get("agents", [])))
        team = _safe_team({"team_snapshot": team})
        team_id = team.get("team_id") or team.get("configuration_id") or _digest({k: v for k, v in team.items() if k != "actual_instances"})
        model, effort = _manager_identity(team)
        team["manager_model"], team["manager_effort"] = model, effort
        team["team_id"] = team_id
        submitted = result.get("forecasts", [])
        if not isinstance(submitted, list) or len(submitted) > 10000:
            raise ValueError("forecasts 必须是有限列表")
        saved, seen = [], set()
        for item in submitted:
            if isinstance(item, dict):
                item = dict(item)
                resolved_references = item.pop("references", None)
                if "security_id" in item:
                    item["symbol"] = item.pop("security_id")
                if "material_ids" in item:
                    item["citations"] = item.pop("material_ids")
                if resolved_references is not None:
                    # Store.publish's resolver replaces model-authored references
                    # with account-scoped system evidence before this hook.
                    item["citations"] = {"material_ids": item.get("citations", []), "references": resolved_references}
            if not isinstance(item, dict) or set(item) - {"symbol", "horizon", "direction", "reason", "citations", "revises_id"}:
                raise ValueError("预测只接受 symbol/horizon/direction/reason/citations/revises_id，身份和时间由服务端确定")
            symbol, horizon = normalize_symbol(item.get("symbol", "")), item.get("horizon")
            if isinstance(horizon, bool) or not isinstance(horizon, int):
                raise ValueError("预测 horizon 须为整数")
            if (symbol, horizon) in seen:
                raise ValueError("同一报告重复提交证券/期限")
            seen.add((symbol, horizon))
            sample = db.execute("SELECT * FROM forecast_samples WHERE batch_id=? AND symbol=? AND horizon=?", (batch["id"], symbol, horizon)).fetchone()
            if not sample:
                raise ValueError("预测不在服务端冻结的批次证券/期限范围内")
            direction, reason = item.get("direction"), item.get("reason", "")
            if direction not in ("up", "sideways", "down", "abstain") or not isinstance(reason, str) or not reason.strip() or len(reason) > 16000:
                raise ValueError("预测须提供有效方向和非空理由")
            prior = db.execute("SELECT id FROM forecasts WHERE sample_id=? AND (kind IN ('formal','test') OR job_id=?)", (sample["id"], job["id"])).fetchone()
            if prior:
                saved.append(prior[0]); continue
            restricted = db.execute("SELECT 1 FROM forecast_research_scopes WHERE job_id=? AND purpose='explicit_subset'", (job['id'],)).fetchone()
            if restricted and not db.execute('SELECT 1 FROM forecast_research_scope_samples WHERE job_id=? AND sample_id=?', (job['id'], sample['id'])).fetchone():
                raise ValueError('预测不在本次执行分配的证券/期限子范围内')
            # A submitted decision proves this exact target was attempted, even
            # when its baseline later prevents directional publication.
            self.assign_forecast_scope(db, job['id'], batch['id'], sample_ids=[sample['id']])
            current_attempt = db.execute('SELECT id FROM forecast_execution_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1', (job['id'],)).fetchone()
            if current_attempt:
                self.bind_sample_attempt(db, job['id'], current_attempt['id'])
            if batch["kind"] == "formal" and direction != "abstain":
                if not _valid_direction_baseline(batch, symbol):
                    db.execute("UPDATE forecast_samples SET state='waiting_data',reason='未取得本证券基准日精确收盘证据，方向预测未发布' WHERE id=?", (sample["id"],))
                    continue
            forecast_id = uuid.uuid4().hex
            revises_id = item.get("revises_id")
            if revises_id:
                original = db.execute("SELECT account_id,symbol,horizon,published_at FROM forecasts WHERE id=?", (revises_id,)).fetchone()
                if not original or original["account_id"] != job["account_id"] or original["symbol"] != symbol or original["horizon"] != horizon or original["published_at"] > at.isoformat():
                    raise ValueError("修订关联须为本账户同证券同周期的已有预测")
            elif batch["kind"] == "exploratory":
                original = db.execute("SELECT id FROM forecasts WHERE account_id=? AND symbol=? AND horizon=? AND kind='formal' AND published_at<=? ORDER BY published_at DESC LIMIT 1", (job["account_id"], symbol, horizon, at.isoformat())).fetchone()
                revises_id = original[0] if original else None
            citations = item.get("citations", [])
            citations = {**citations, "baseline_at_publication": batch["baseline_evidence"].get(symbol)} if isinstance(citations, dict) else {"material_ids": citations, "baseline_at_publication": batch["baseline_evidence"].get(symbol)}
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                forecast_id, sample["id"], batch["id"], job["account_id"], job["id"], analysis_id, "manager", batch["kind"],
                symbol, batch["base_date"], horizon, sample["target_date"], direction, reason, _dump(citations),
                batch["rule_version"], _dump(batch["rule"]), team_id, _dump(team), model, effort, at.isoformat(), at.isoformat(),
                result.get("report", ""), revises_id))
            db.execute("UPDATE forecast_samples SET state=?,reason=? WHERE id=?", ("abstain" if direction == "abstain" else "predicted", reason if direction == "abstain" else None, sample["id"]))
            if direction != "abstain":
                due = (sample["target_date"] or batch["base_date"]) + "T15:30:00+08:00"
                db.execute("INSERT OR IGNORE INTO forecast_evaluation_tasks VALUES(?,'pending',0,?,NULL,?)", (forecast_id, due, at.isoformat()))
            saved.append(forecast_id)
        db.execute("UPDATE forecast_batches SET state='published',team_snapshot=? WHERE id=?", (_dump(team), batch["id"]))
        db.execute("UPDATE forecast_execution_attempts SET published_at=?,state='published' WHERE job_id=? AND attempt=(SELECT max(attempt) FROM forecast_execution_attempts WHERE job_id=?)", (at.isoformat(), job["id"], job["id"]))
        return saved

    def fail_forecast_job(self, job_id, reason="任务失败"):
        with self.connect() as db:
            db.execute("""UPDATE forecast_samples SET state='failed',reason=? WHERE batch_id IN
                (SELECT id FROM forecast_batches WHERE job_id=?) AND state='uncovered' AND id IN
                (SELECT sa.sample_id FROM forecast_sample_attempts sa JOIN forecast_execution_attempts a ON a.id=sa.attempt_id
                 WHERE a.job_id=?)""", (reason[:1000], job_id, job_id))

    def expire_forecast_batches(self, at=None):
        stamp = _local(at).isoformat()
        with self.connect() as db:
            db.execute("UPDATE forecast_samples SET state='missed',reason='超过截止仍未发布' WHERE state IN ('uncovered','waiting_config') AND batch_id IN (SELECT id FROM forecast_batches WHERE deadline<=?)", (stamp,))
            db.execute("UPDATE forecast_samples SET state='missed',reason='超过截止仍无可用基准收盘数据，未生成预测' WHERE state='waiting_data' AND batch_id IN (SELECT id FROM forecast_batches WHERE deadline<=?)", (stamp,))
            db.execute("UPDATE forecast_batches SET state='closed' WHERE deadline<=?", (stamp,))
            db.execute("UPDATE forecast_preparations SET state='expired',lease_until=NULL,reason='原批次截止已到，停止基准准备' WHERE state IN ('unattempted','waiting_data','preparing') AND batch_id IN (SELECT id FROM forecast_batches WHERE deadline<=?)", (stamp,))

    def forecast_detail(self, forecast_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
            if not row:
                raise KeyError("预测不存在")
            result = dict(row)
            for field in ("citations", "rule", "team_snapshot"):
                result[field] = _load(result[field])
            result["evaluations"] = [{**dict(r), "evidence": _load(r["evidence"])} for r in db.execute("SELECT * FROM forecast_evaluations WHERE forecast_id=? ORDER BY revision", (forecast_id,))]
            result["price_observations"] = [{**dict(r), "evidence": _load(r["evidence"])} for r in db.execute("SELECT * FROM forecast_price_observations WHERE forecast_id=? ORDER BY id DESC LIMIT 20", (forecast_id,))]
            result["evaluation_task"] = dict(r) if (r := db.execute("SELECT * FROM forecast_evaluation_tasks WHERE forecast_id=?", (forecast_id,)).fetchone()) else None
            result["calendar_revalidation_pending"] = bool(result["evaluation_task"] and result["evaluation_task"].get("last_error") == "calendar_revalidation_pending")
            result["effective_evaluation"] = ({"status": "waiting_data", "reason": "calendar_revalidation_pending", "hit": None}
                if result["calendar_revalidation_pending"] else result["evaluations"][-1] if result["evaluations"] else None)
            resolution = db.execute("SELECT * FROM forecast_calendar_resolutions WHERE forecast_id=?", (forecast_id,)).fetchone()
            result["calendar_resolution"] = {**dict(resolution), "evidence": _load(resolution["evidence"])} if resolution else None
            result["effective_target_date"] = result["target_date"] or (resolution["target_date"] if resolution else None)
            projection = db.execute("SELECT * FROM forecast_rule_projections WHERE batch_id=?", (result["batch_id"],)).fetchone()
            result["rule_projection"] = {**dict(projection), "canonical_rule": _load(projection["canonical_rule"])} if projection else None
            result["execution_attempts"] = [{**dict(r), "team_snapshot": _load(r["team_snapshot"]), "requests": _load(r["requests"])} for r in db.execute("SELECT * FROM forecast_execution_attempts WHERE job_id=? ORDER BY attempt", (result["job_id"],))]
            return result

    @staticmethod
    def _forecast_display_state(row):
        if row["state"] != "predicted":
            return row["state"]
        if row.get("evaluation_status"):
            return row["evaluation_status"]
        if row.get("waiting_reason"):
            return "waiting_data"
        if not row.get("target_date"):
            return "waiting_data"
        return "pending_maturity"

    def record_forecast_evaluation(self, forecast_id, evidence, *, at=None, lease_token=None, request_counted=False):
        at = _local(at)
        forecast = self.forecast_detail(forecast_id)
        target = forecast["effective_target_date"]
        if not target or at < datetime.combine(date.fromisoformat(target), time(15), SHANGHAI):
            return None
        if evidence.get("symbol") != forecast["symbol"] or evidence.get("base_date") != forecast["base_date"] or evidence.get("target_date") != target:
            raise ValueError("评估行情证券或精确日期与锁定预测不匹配")
        status, reason, calculation, hit = evidence.get("status"), evidence.get("reason"), {}, None
        if _local(forecast["published_at"]) >= datetime.combine(date.fromisoformat(target), time(15), SHANGHAI):
            status, reason = "unassessable", "published_after_target_close"
            evidence = {**evidence, "status": status, "reason": reason}
        if status == "ready":
            if evidence.get("price_policy") != forecast["rule"]["price_policy"]:
                raise ValueError("行情口径与发布前规则不匹配")
            calculation = classify_return(evidence["base_price"], evidence["target_price"], forecast["rule"]["threshold"])
            hit = int(calculation["actual_direction"] == forecast["direction"])
            status = "evaluated"
        if status not in ("evaluated", "waiting_data", "unassessable"):
            raise ValueError("无效评估状态")
        stable = {k: v for k, v in evidence.items() if k not in ("fetched_at", "data_version")}
        version = _digest({"evidence_version": evidence.get("data_version") or _digest(stable),
                           "status": status, "reason": reason})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease_token is not None and not db.execute("SELECT 1 FROM forecast_evaluation_leases WHERE forecast_id=? AND token=? AND expires_at>?", (forecast_id, lease_token, at.isoformat())).fetchone():
                return None
            previous = db.execute("SELECT revision,data_version,id,status FROM forecast_evaluations WHERE forecast_id=? ORDER BY revision DESC LIMIT 1", (forecast_id,)).fetchone()
            db.execute("INSERT INTO forecast_price_observations(forecast_id,observed_at,data_version,status,evidence) VALUES(?,?,?,?,?)", (forecast_id, at.isoformat(), version, status, _dump(evidence)))
            # A network outage is not a supplier correction. Retain a completed
            # evaluation while recording the unsuccessful refresh separately.
            if previous and (previous["data_version"] == version or (previous["status"] == "evaluated" and status == "waiting_data")):
                evaluation_id = previous["id"]
            else:
                evaluation_id = uuid.uuid4().hex
                db.execute("INSERT INTO forecast_evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (evaluation_id, forecast_id,
                    previous[0] + 1 if previous else 1, at.isoformat(), version, status, reason,
                    calculation.get("return_ratio"), calculation.get("actual_direction"), hit, _dump(evidence),
                    ("交易日历更正；保留原目标及上一评估" if str(reason or "").startswith("calendar_") else "供应商历史数据或可用性更正；保留上一评估") if previous else None))
            task = db.execute("SELECT attempts FROM forecast_evaluation_tasks WHERE forecast_id=?", (forecast_id,)).fetchone()
            attempts = task[0] if task else 0
            next_try = at + (timedelta(days=1) if status in ("evaluated", "unassessable") else timedelta(minutes=min(1440, 15 * 2 ** min(attempts, 7))))
            db.execute("UPDATE forecast_evaluation_tasks SET state=?,attempts=attempts+?,next_attempt=?,last_error=?,updated_at=? WHERE forecast_id=?",
                (status, 0 if request_counted else 1, next_try.isoformat(), reason, at.isoformat(), forecast_id))
        return evaluation_id

    def request_forecast_recheck(self, forecast_id, *, at=None):
        """Explicit correction refresh; preserves the current valid score."""
        at = _local(at)
        self.forecast_detail(forecast_id)
        with self.connect() as db:
            if not db.execute("UPDATE forecast_evaluation_tasks SET state='recheck_requested',next_attempt=?,updated_at=? WHERE forecast_id=?", (at.isoformat(), at.isoformat(), forecast_id)).rowcount:
                raise ValueError("弃权记录没有方向评分任务")
        return {"forecast_id": forecast_id, "state": "recheck_requested", "requested_at": at.isoformat()}

    async def evaluate_due_forecasts(self, config, *, at=None, provider=None, limit=None, price_session=None):
        """Reserve work for first scoring and bound historical correction work."""
        import time as runtime_time
        from .forecast_prices import ClosingPriceSession
        at, started = _local(at), runtime_time.monotonic()
        current_time = lambda: at + timedelta(seconds=runtime_time.monotonic() - started)
        total_limit = min(200, max(1, int(limit if limit is not None else config.get("forecast_evaluation_batch_size", 30))))
        review_limit = min(total_limit, max(0, int(config.get("forecast_review_batch_size", 6))))
        if provider is None and price_session is None:
            from .providers import MarketProvider
            provider = MarketProvider(config)
        own_session = price_session is None
        prices = price_session or ClosingPriceSession(self, provider,
            concurrency=config.get("forecast_evaluation_concurrency", 4), timeout=config.get("forecast_request_timeout_seconds", 20))
        # Latest assessment only; revisions never multiply queue entries. Round
        # robin by account prevents a large account from occupying the whole batch.
        query = """WITH candidates AS (
            SELECT t.forecast_id,t.next_attempt,f.account_id,
                CASE WHEN t.state='recheck_requested' THEN 0 WHEN e.status IN ('evaluated','unassessable') THEN 1 ELSE 0 END AS review,
                ROW_NUMBER() OVER (PARTITION BY f.account_id,CASE WHEN t.state='recheck_requested' THEN 0 WHEN e.status IN ('evaluated','unassessable') THEN 1 ELSE 0 END ORDER BY t.next_attempt,t.forecast_id) AS account_rank
            FROM forecast_evaluation_tasks t JOIN forecasts f ON f.id=t.forecast_id
            LEFT JOIN forecast_evaluations e ON e.forecast_id=f.id AND e.revision=(SELECT max(x.revision) FROM forecast_evaluations x WHERE x.forecast_id=f.id)
            WHERE t.next_attempt<=?)
            SELECT forecast_id FROM candidates WHERE review=? ORDER BY account_rank,next_attempt,forecast_id LIMIT ?"""
        with self.connect() as db:
            first = [r[0] for r in db.execute(query, (at.isoformat(), 0, total_limit))]
            reviews = [r[0] for r in db.execute(query, (at.isoformat(), 1, review_limit))]
        ids = first[:max(1, total_limit - len(reviews))] + reviews if first else reviews
        ids = ids[:total_limit]
        concurrency = max(1, min(16, int(config.get("forecast_evaluation_concurrency", 4))))
        semaphore = asyncio.Semaphore(concurrency if own_session else max(1, concurrency // 2))

        async def evaluate_one(forecast_id):
            async with semaphore:
                token, lease_at = uuid.uuid4().hex, current_time()
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    lease = db.execute("SELECT expires_at FROM forecast_evaluation_leases WHERE forecast_id=?", (forecast_id,)).fetchone()
                    if lease and lease["expires_at"] > lease_at.isoformat():
                        return None
                    db.execute("INSERT OR REPLACE INTO forecast_evaluation_leases VALUES(?,?,?)", (forecast_id, token, (lease_at + timedelta(minutes=5)).isoformat()))
                calendar_validated, network_started = False, False

                def owns_lease(db):
                    return db.execute('SELECT 1 FROM forecast_evaluation_leases WHERE forecast_id=? AND token=? AND expires_at>?',
                        (forecast_id, token, current_time().isoformat())).fetchone() is not None

                def begin_request():
                    nonlocal network_started
                    with self.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        if not owns_lease(db):
                            return False
                        task = db.execute('SELECT attempts FROM forecast_evaluation_tasks WHERE forecast_id=?', (forecast_id,)).fetchone()
                        delay = min(1440, 15 * 2 ** min(task[0] if task else 0, 7))
                        db.execute('UPDATE forecast_evaluation_tasks SET attempts=attempts+1,next_attempt=?,updated_at=? WHERE forecast_id=?',
                            ((current_time() + timedelta(minutes=delay)).isoformat(), current_time().isoformat(), forecast_id))
                        network_started = True
                    return True

                def record_failure(error):
                    with self.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        if not owns_lease(db):
                            return
                        task = db.execute('SELECT attempts FROM forecast_evaluation_tasks WHERE forecast_id=?', (forecast_id,)).fetchone()
                        delay = min(1440, 15 * 2 ** min(max(0, (task[0] if task else 0) - int(network_started)), 7))
                        db.execute("UPDATE forecast_evaluation_tasks SET state='waiting_data',attempts=attempts+?,last_error=?,next_attempt=?,updated_at=? WHERE forecast_id=?",
                            (0 if network_started else 1, error, (current_time() + timedelta(minutes=delay)).isoformat(), current_time().isoformat(), forecast_id))

                try:
                    with self.connect() as db:
                        row = db.execute("SELECT f.*,COALESCE(f.target_date,c.target_date) AS effective_target_date FROM forecasts f LEFT JOIN forecast_calendar_resolutions c ON c.forecast_id=f.id WHERE f.id=?", (forecast_id,)).fetchone()
                        forecast = dict(row)
                    target_date = forecast["effective_target_date"]
                    try:
                        target = advance_trading_days(forecast["base_date"], forecast["horizon"], config.get("calendar_path"))
                    except (ValueError, OSError, KeyError, TypeError):
                        target = {"target_date": None, "status": "calendar_gap"}
                    target["calendar_revision"] = calendar_revision(config.get("calendar_path"))
                    if target_date and target.get("target_date") != target_date:
                        # A calendar correction is not an unavailable price refresh:
                        # supersede any effective score but retain original dates.
                        reason = "calendar_target_changed" if target.get("target_date") else "calendar_coverage_changed"
                        correction = {"symbol": forecast["symbol"], "base_date": forecast["base_date"],
                            "target_date": target_date, "status": "unassessable", "reason": reason,
                            "calendar_validation": target, "original_target_date": forecast["target_date"],
                            "note": "原预测目标日不可改写；新日历解析另存依据，停止按旧日期评分"}
                        completed = self.record_forecast_evaluation(forecast_id, correction, at=current_time(), lease_token=token)
                        with self.connect() as db:
                            db.execute('BEGIN IMMEDIATE')
                            if not owns_lease(db):
                                return None
                            db.execute("UPDATE forecast_evaluation_tasks SET state='waiting_data',last_error=?,next_attempt=?,updated_at=? WHERE forecast_id=?",
                                (reason, (current_time() + timedelta(hours=6)).isoformat(), current_time().isoformat(), forecast_id))
                        return forecast_id if completed else None
                    if not target_date:
                        if not target.get("target_date"):
                            with self.connect() as db:
                                db.execute('BEGIN IMMEDIATE')
                                if not owns_lease(db):
                                    return None
                                db.execute("UPDATE forecast_evaluation_tasks SET state='waiting_data',last_error='calendar_gap',next_attempt=?,updated_at=? WHERE forecast_id=?", ((current_time() + timedelta(hours=6)).isoformat(), current_time().isoformat(), forecast_id))
                            return None
                        target_date = target["target_date"]
                        with self.connect() as db:
                            db.execute('BEGIN IMMEDIATE')
                            if not owns_lease(db):
                                return None
                            db.execute("INSERT OR IGNORE INTO forecast_calendar_resolutions VALUES(?,?,?,?)", (forecast_id, target_date, _dump(target), current_time().isoformat()))
                            db.execute("UPDATE forecast_samples SET target_date=? WHERE id=? AND target_date IS NULL", (target_date, forecast["sample_id"]))
                    calendar_validated = True
                    due = datetime.combine(date.fromisoformat(target_date), time(15, 30), SHANGHAI)
                    if current_time() < due:
                        with self.connect() as db:
                            db.execute('BEGIN IMMEDIATE')
                            if not owns_lease(db):
                                return None
                            db.execute("UPDATE forecast_evaluation_tasks SET next_attempt=? WHERE forecast_id=?", (due.isoformat(), forecast_id))
                        return None
                    evidence = await prices.pair(forecast["symbol"], forecast["base_date"], target_date, on_start=begin_request)
                    completed = self.record_forecast_evaluation(forecast_id, evidence, at=current_time(), lease_token=token, request_counted=network_started)
                    with self.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        if not owns_lease(db):
                            return None
                        task = db.execute("SELECT state FROM forecast_evaluation_tasks WHERE forecast_id=?", (forecast_id,)).fetchone()
                        if task and task["state"] in ("evaluated", "unassessable"):
                            days = config.get("forecast_review_interval_days", 7) if task["state"] == "evaluated" else config.get("forecast_unassessable_review_days", 14)
                            db.execute("UPDATE forecast_evaluation_tasks SET next_attempt=? WHERE forecast_id=?", ((current_time() + timedelta(days=max(1, min(365, int(days))))).isoformat(), forecast_id))
                    return forecast_id if completed else None
                except asyncio.CancelledError:
                    if network_started:
                        record_failure('request_started_result_unknown')
                    raise
                except Exception as exc:
                    record_failure(type(exc).__name__ if calendar_validated else 'calendar_revalidation_pending')
                    return None
                finally:
                    with self.connect() as db:
                        db.execute("DELETE FROM forecast_evaluation_leases WHERE forecast_id=? AND token=?", (forecast_id, token))
        tasks = [asyncio.create_task(evaluate_one(forecast_id)) for forecast_id in ids]
        try:
            if not tasks:
                return []
            done, pending = await asyncio.wait(tasks, timeout=max(0.05, min(300, float(config.get("forecast_cycle_timeout_seconds", 45)))))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return [task.result() for task in tasks if task in done and not task.cancelled() and task.exception() is None and task.result()]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if own_session:
                await prices.close()

    async def forecast_tick(self, config, *, at=None, provider=None, create_batches=True):
        """Persist today's scopes before network work; score and prepare in parallel."""
        import time as runtime_time
        from .forecast_prices import ClosingPriceSession
        at, started = _local(at), runtime_time.monotonic()
        current_time = lambda: at + timedelta(seconds=runtime_time.monotonic() - started)
        self.expire_forecast_batches(at)
        self.reconcile_forecast_windows(at=at, calendar_path=config.get("calendar_path"), scheduled_enabled=config.get("scheduled_research_enabled", self.settings().get("scheduled_research_enabled", True)))
        candidates, created = [], []
        try:
            trading = trading_date(at.date(), config.get("calendar_path"))["trading_day"]
        except (OSError, ValueError, KeyError):
            trading = None
        if create_batches and trading is True and at.time().replace(tzinfo=None) >= time(15):
            for account in self.accounts():
                settings = self.forecast_settings(account["id"])
                with self.connect() as db:
                    existing = db.execute("SELECT id FROM forecast_batches WHERE account_id=? AND kind=? AND base_date=?", (account["id"], "formal" if self.mode == "live" else "test", at.date().isoformat())).fetchone()
                    batch = self._forecast_batch(db, existing["id"]) if existing else None
                if batch:
                    if batch["state"] in ("failed", "closed") or at.isoformat() >= batch["deadline"]:
                        continue
                    if account["version"] != batch["input_version"]:
                        with self.connect() as db:
                            db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch["id"],))
                            db.execute("UPDATE forecast_samples SET state='failed',reason='账户输入已变化，原批次范围失效' WHERE batch_id=? AND state NOT IN ('predicted','abstain')", (batch["id"],))
                            db.execute("UPDATE forecast_preparations SET state='terminal',lease_until=NULL,reason='account_version_changed' WHERE batch_id=? AND state!='ready'", (batch["id"],))
                        continue
                    if batch["job_id"]:
                        created.append(batch)
                else:
                    if not settings["enabled"] or not _clock(settings["start_time"]) <= at.time().replace(tzinfo=None) < _clock(settings["deadline_time"]):
                        continue
                    universe = self.universe_snapshot()["universe"]
                    symbols = sorted({h["symbol"] for h in account["holdings"]} | set(universe.get("candidates", [])))
                    if not symbols:
                        continue
                    try:
                        batch = self.create_forecast_batch(account["id"], at=current_time(), symbols=symbols,
                            calendar_path=config.get("calendar_path"), expected_input_version=account["version"])
                    except ValueError as exc:
                        self.set_meta("forecast_baseline_wait:" + account["id"], {"at": current_time().isoformat(), "date": at.date().isoformat(), "reason": str(exc), "symbols": symbols})
                        continue
                    with self.connect() as db:
                        db.execute("UPDATE forecast_batches SET state='waiting_data' WHERE id=?", (batch["id"],))
                        db.execute("UPDATE forecast_samples SET state='waiting_data',reason='等待本证券精确基准；其他证券可先研究' WHERE batch_id=? AND state='uncovered'", (batch["id"],))
                with self.connect() as db:
                    self._ensure_forecast_preparations(db, batch["id"])
                candidates.append(batch)
        if provider is None:
            from .providers import MarketProvider
            provider = MarketProvider(config)
        prices = ClosingPriceSession(self, provider, concurrency=config.get("forecast_evaluation_concurrency", 4),
                                     timeout=config.get("forecast_request_timeout_seconds", 20))
        budget = max(1, min(500, int(config.get("forecast_baseline_batch_size", 40))))
        # Unattempted targets precede retry targets, globally across accounts.
        # Durable timestamps rotate equally-ranked accounts across bounded ticks.
        queues = []
        for batch in candidates:
            with self.connect() as db:
                rows = [dict(r) for r in db.execute("""SELECT p.* FROM forecast_preparations p WHERE p.batch_id=?
                    AND p.state IN ('unattempted','waiting_data','preparing')
                    AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=?)
                    AND (p.lease_until IS NULL OR p.lease_until<=?)
                    AND EXISTS(SELECT 1 FROM forecast_samples s WHERE s.batch_id=p.batch_id AND s.symbol=p.symbol
                        AND NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id))
                    ORDER BY p.attempts,p.last_attempt_at,p.symbol""", (batch["id"], at.isoformat(), at.isoformat()))]
            if rows:
                queues.append((batch, rows))
        queues.sort(key=lambda pair: (pair[1][0]["attempts"], pair[1][0]["last_attempt_at"] or "", pair[0]["id"]))
        work = [(batch, rows[rank]["symbol"]) for rank in range(max((len(rows) for _, rows in queues), default=0))
                for batch, rows in queues if rank < len(rows)][:budget]

        async def prepare(batch, symbol):
            claim_at, token = current_time(), uuid.uuid4().hex
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._forecast_batch(db, batch["id"])
                row = db.execute("SELECT * FROM forecast_preparations WHERE batch_id=? AND symbol=?", (batch["id"], symbol)).fetchone()
                if (not row or row["state"] not in ("unattempted", "waiting_data", "preparing")
                        or (row["lease_until"] and row["lease_until"] > claim_at.isoformat())
                        or (row["next_attempt_at"] and row["next_attempt_at"] > claim_at.isoformat())
                        or claim_at.isoformat() >= current["deadline"]):
                    return
                attempts = row['attempts']
                # A crashed, genuinely started fifth request still exhausts the
                # budget. A capacity waiter has never incremented this counter.
                if attempts >= 5:
                    db.execute("UPDATE forecast_preparations SET state='terminal',lease_until=NULL,lease_token=NULL,reason='baseline_retry_budget_exhausted' WHERE batch_id=? AND symbol=?", (batch['id'], symbol))
                    db.execute("UPDATE forecast_samples SET state='waiting_data',reason='基准准备已终止：baseline_retry_budget_exhausted' WHERE batch_id=? AND symbol=? AND state NOT IN ('predicted','abstain')", (batch['id'], symbol))
                    return
                db.execute("""UPDATE forecast_preparations SET state='preparing',lease_until=?,lease_token=?,request_started_at=NULL
                    WHERE batch_id=? AND symbol=?""",
                    ((claim_at + timedelta(seconds=max(0.05, min(300, float(config.get("forecast_cycle_timeout_seconds", 45)))) + 5)).isoformat(), token, batch['id'], symbol))
            started_request = False

            def on_start():
                nonlocal attempts, started_request
                began = current_time()
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    owned = db.execute("""SELECT p.attempts,b.deadline,b.input_version,b.account_id FROM forecast_preparations p
                        JOIN forecast_batches b ON b.id=p.batch_id WHERE p.batch_id=? AND p.symbol=?
                        AND p.lease_token=? AND p.state='preparing' AND p.lease_until>?""", (batch['id'], symbol, token, began.isoformat())).fetchone()
                    if not owned:
                        return False
                    if began.isoformat() >= owned['deadline']:
                        db.execute("UPDATE forecast_preparations SET state='expired',lease_until=NULL,lease_token=NULL,reason='deadline_before_request_start' WHERE batch_id=? AND symbol=? AND lease_token=?", (batch['id'], symbol, token))
                        db.execute("UPDATE forecast_samples SET state='missed',reason='等待请求容量时已超过批次截止' WHERE batch_id=? AND symbol=? AND state NOT IN ('predicted','abstain')", (batch['id'], symbol))
                        return False
                    if self.account(owned['account_id'], db)['version'] != owned['input_version']:
                        db.execute("UPDATE forecast_preparations SET state='terminal',lease_until=NULL,lease_token=NULL,reason='account_version_changed' WHERE batch_id=? AND symbol=? AND lease_token=?", (batch['id'], symbol, token))
                        db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch['id'],))
                        db.execute("UPDATE forecast_samples SET state='failed',reason='等待请求容量期间账户输入已变化' WHERE batch_id=? AND state NOT IN ('predicted','abstain')", (batch['id'],))
                        return False
                    attempts = owned['attempts'] + 1
                    db.execute("""UPDATE forecast_preparations SET attempts=?,last_attempt_at=?,request_started_at=?,next_attempt_at=?
                        WHERE batch_id=? AND symbol=? AND lease_token=?""", (attempts, began.isoformat(), began.isoformat(),
                        (began + timedelta(seconds=min(600, 60 * 2 ** min(attempts - 1, 4)))).isoformat(), batch['id'], symbol, token))
                    started_request = True
                return True

            try:
                evidence = await prices.pair(symbol, batch['base_date'], batch['base_date'], on_start=on_start)
            except asyncio.CancelledError:
                with self.connect() as db:
                    db.execute("""UPDATE forecast_preparations SET state=?,lease_until=NULL,lease_token=NULL,reason=?,
                        next_attempt_at=CASE WHEN ? THEN next_attempt_at ELSE NULL END
                        WHERE batch_id=? AND symbol=? AND lease_token=? AND state='preparing'""",
                        ('terminal' if attempts >= 5 else 'waiting_data' if attempts else 'unattempted',
                         'request_started_result_unknown' if started_request else 'capacity_wait_cancelled',
                         started_request, batch['id'], symbol, token))
                raise
            except Exception as exc:
                evidence = {'symbol': symbol, 'base_date': batch['base_date'], 'target_date': batch['base_date'],
                            'status': 'waiting_data', 'reason': 'baseline_unavailable', 'error': type(exc).__name__}
            check_at = current_time()
            evidence['last_checked_at'] = check_at.isoformat()
            start_job = False
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                owned = db.execute("SELECT 1 FROM forecast_preparations WHERE batch_id=? AND symbol=? AND lease_token=? AND state='preparing' AND lease_until>?",
                    (batch['id'], symbol, token, check_at.isoformat())).fetchone()
                if not owned:
                    return  # A late result cannot mutate another lease's state.
                current = self._forecast_batch(db, batch['id'])
                if check_at.isoformat() >= current['deadline']:
                    db.execute("UPDATE forecast_preparations SET state='expired',lease_until=NULL,lease_token=NULL,reason='request_completed_after_deadline' WHERE batch_id=? AND symbol=? AND lease_token=?", (batch['id'], symbol, token))
                    db.execute("UPDATE forecast_samples SET state='missed',reason='精确基准请求完成时已超过批次截止' WHERE batch_id=? AND symbol=? AND state NOT IN ('predicted','abstain')", (batch['id'], symbol))
                    return
                if self.account(batch['account_id'], db)['version'] != current['input_version']:
                    db.execute("UPDATE forecast_preparations SET state='terminal',lease_until=NULL,lease_token=NULL,reason='account_version_changed' WHERE batch_id=? AND symbol=? AND lease_token=?", (batch['id'], symbol, token))
                    db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch['id'],))
                    db.execute("UPDATE forecast_samples SET state='failed',reason='等待基准期间账户输入已变化' WHERE batch_id=? AND state NOT IN ('predicted','abstain')", (batch['id'],))
                    return
                prior = current['baseline_evidence'].get(symbol)
                if prior and prior.get('status') == 'ready':
                    evidence = prior
                baseline = {**current['baseline_evidence'], symbol: evidence}
                ready = evidence.get('status') == 'ready'
                state = 'ready' if ready else 'terminal' if evidence.get('status') == 'unassessable' or attempts >= 5 else 'waiting_data'
                reason = evidence.get('reason') if attempts < 5 or ready else 'baseline_retry_budget_exhausted'
                db.execute("UPDATE forecast_preparations SET state=?,lease_until=NULL,lease_token=NULL,ready_at=?,reason=? WHERE batch_id=? AND symbol=? AND lease_token=?",
                           (state, check_at.isoformat() if ready else None, reason, batch['id'], symbol, token))
                db.execute('UPDATE forecast_batches SET baseline_evidence=? WHERE id=?', (_dump(baseline), batch['id']))
                if ready:
                    db.execute("UPDATE forecast_samples SET state='uncovered',reason='基准就绪，等待实际判断与发布' WHERE batch_id=? AND symbol=? AND state IN ('waiting_data','failed')", (batch['id'], symbol))
                    if current['state'] == 'waiting_data':
                        db.execute("UPDATE forecast_batches SET state='open' WHERE id=?", (batch['id'],))
                    start_job = not current['job_id']
                elif state == 'terminal':
                    db.execute("UPDATE forecast_samples SET state='waiting_data',reason=? WHERE batch_id=? AND symbol=? AND state NOT IN ('predicted','abstain')",
                               ('基准准备已终止：' + str(reason), batch['id'], symbol))
                if ready and current['job_id']:
                    db.execute('INSERT INTO events(job_id,body) VALUES(?,?)', (current['job_id'], _dump({'type': 'forecast_baseline_ready', 'at': check_at.isoformat(), 'agent': 'system', 'message': '新增基准已可通过 list_forecast_targets 读取；数据库到达不代表模型已经读取', 'payload': {'batch_id': batch['id'], 'symbol': symbol, 'shared_evidence_id': evidence.get('shared_evidence_id')}})))
            if start_job:
                self.enqueue_forecast_batch(batch['id'], at=current_time())

        evaluation = asyncio.create_task(self.evaluate_due_forecasts(config, at=at, price_session=prices)) if config.get("forecast_evaluation_enabled", True) else None
        if evaluation:
            await asyncio.sleep(0)  # reserve scoring slots before baseline requests
        preparations = [asyncio.create_task(prepare(batch, symbol)) for batch, symbol in work]
        tasks = preparations + ([evaluation] if evaluation else [])
        evaluated = []
        try:
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=max(0.05, min(300, float(config.get("forecast_cycle_timeout_seconds", 45)))))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if evaluation and evaluation in done and not evaluation.cancelled() and evaluation.exception() is None:
                    evaluated = evaluation.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await prices.close()
            self.expire_forecast_batches(current_time())
            self.reconcile_forecast_windows(at=current_time(), calendar_path=config.get("calendar_path"), scheduled_enabled=config.get("scheduled_research_enabled", self.settings().get("scheduled_research_enabled", True)))
        # Coalesce supplemental scheduling after the bounded preparation cycle.
        # Enqueue remains transactional and checks the completion-time deadline.
        for candidate in candidates:
            self.enqueue_forecast_batch(candidate["id"], at=current_time())
        created = []
        with self.connect() as db:
            for candidate in candidates:
                latest = self._forecast_batch(db, candidate["id"])
                if latest["job_id"]:
                    created.append(latest)
        return {"evaluated": evaluated, "batches": created, "price_requests": dict(prices.metrics), "window_runs": self.forecast_window_statistics()}
