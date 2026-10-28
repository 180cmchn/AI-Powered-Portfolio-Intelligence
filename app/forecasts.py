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

from .calendar import SHANGHAI, advance_trading_days, trading_date
from .domain import now
from .providers import normalize_symbol
from .forecast_prices import closing_pair


SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_settings(
 account_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_batches(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL, base_date TEXT NOT NULL,
 rule_version TEXT NOT NULL, rule TEXT NOT NULL, symbols TEXT NOT NULL, expected_count INTEGER NOT NULL,
 created_at TEXT NOT NULL, deadline TEXT NOT NULL, state TEXT NOT NULL, job_id TEXT UNIQUE,
 input_version INTEGER NOT NULL, team_snapshot TEXT NOT NULL, baseline_evidence TEXT NOT NULL,
 UNIQUE(account_id,kind,base_date,rule_version));
CREATE TABLE IF NOT EXISTS forecast_samples(
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
 horizon INTEGER NOT NULL, label TEXT NOT NULL, target_date TEXT, calendar_evidence TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, UNIQUE(batch_id,symbol,horizon));
CREATE TABLE IF NOT EXISTS forecast_job_bindings(
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, prior_job_id TEXT, job_id TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
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


class ForecastStore:
    def migrate_forecasts(self, db):
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
        rule_version = _digest({k: v for k, v in rule.items() if k != "enabled"})
        if kind == "exploratory":
            rule_version += ":" + uuid.uuid4().hex
        deadline = datetime.combine(at.date(), _clock(rule["deadline_time"]), SHANGHAI).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_account = self.account(account_id, db)
            if current_account["version"] != account["version"] or (expected_input_version is not None and current_account["version"] != expected_input_version):
                raise ValueError("采集基准期间账户输入已变化，等待下一轮重新锁定批次")
            existing = db.execute("SELECT id FROM forecast_batches WHERE account_id=? AND kind=? AND base_date=? AND rule_version=?",
                (account_id, kind, at.date().isoformat(), rule_version)).fetchone()
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
            for symbol in symbols:
                for horizon, label in zip(rule["horizons"], rule["labels"]):
                    try:
                        target = advance_trading_days(at.date().isoformat(), horizon, calendar_path)
                    except (ValueError, OSError, KeyError):
                        target = {"target_date": None, "status": "calendar_gap", "reason": "交易日历不可用"}
                    db.execute("INSERT INTO forecast_samples VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex,
                        batch_id, account_id, symbol, horizon, label, target["target_date"], _dump(target),
                        "uncovered", target.get("reason")))
            return self._forecast_batch(db, batch_id)

    def _forecast_batch(self, db, batch_id):
        row = db.execute("SELECT * FROM forecast_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError("预测批次不存在")
        batch = dict(row)
        for field in ("rule", "symbols", "team_snapshot", "baseline_evidence"):
            batch[field] = _load(batch[field])
        batch["coverage"] = dict(Counter(r[0] for r in db.execute("SELECT state FROM forecast_samples WHERE batch_id=?", (batch_id,))))
        return batch

    def forecast_batches(self, account_id=None, limit=100, offset=0):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM forecast_batches WHERE (? IS NULL OR account_id=?) ORDER BY created_at DESC LIMIT ? OFFSET ?",
                              (account_id, account_id, min(200, max(1, limit)), max(0, offset))).fetchall()
            return [self._forecast_batch(db, r[0]) for r in rows]

    def bind_forecast_job(self, batch_id, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._forecast_batch(db, batch_id)
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["account_id"] != batch["account_id"] or job["input_version"] != batch["input_version"]:
                raise ValueError("批次与任务账户/输入版本不一致")
            if batch["job_id"] and batch["job_id"] != job_id:
                raise ValueError("批次已经绑定任务")
            db.execute("UPDATE forecast_batches SET job_id=? WHERE id=?", (job_id, batch_id))
            if not batch["job_id"]:
                db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,NULL,?,'initial',?)", (batch_id, job_id, now()))

    def enqueue_forecast_batch(self, batch_id, at=None):
        """Create the durable worker job and bind its fixed batch atomically."""
        stamp = _local(at).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._forecast_batch(db, batch_id)
            if batch["job_id"]:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (batch["job_id"],)).fetchone()
                return dict(row) if row else None
            if stamp >= batch["deadline"] or batch["state"] != "open":
                return None
            if not any(v.get("status") == "ready" for v in batch["baseline_evidence"].values()):
                return None
            account = self.account(batch["account_id"], db)
            if account["version"] != batch["input_version"]:
                db.execute("UPDATE forecast_samples SET state='failed',reason='基准等待期间账户输入已变化' WHERE batch_id=? AND state IN ('uncovered','waiting_data')", (batch_id,))
                db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch_id,))
                return None
            event_key = "forecast_batch:" + batch_id
            job = self._enqueue(db, account, event_key, event_key=event_key)
            # The legacy crash window can have left this event's execution in a
            # terminal state. Do not rebrand its old exploration as formal.
            if job["state"] not in ("queued", "running", "waiting_config"):
                db.execute("UPDATE forecast_samples SET state='failed',reason='原批次任务在绑定前已结束' WHERE batch_id=? AND state IN ('uncovered','waiting_data')", (batch_id,))
                db.execute("UPDATE forecast_batches SET state='failed' WHERE id=?", (batch_id,))
                return None
            db.execute("UPDATE forecast_batches SET job_id=? WHERE id=?", (job["id"], batch_id))
            db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,NULL,?,'atomic_enqueue',?)", (batch_id, job["id"], stamp))
            return job

    def reconcile_forecast_jobs(self, db, account_id, new_job_id, input_version, at=None):
        """Store._enqueue hook after superseding old executions, in its transaction."""
        stamp = _local(at).isoformat()
        rows = db.execute("""SELECT b.* FROM forecast_batches b JOIN jobs j ON j.id=b.job_id
            WHERE b.account_id=? AND j.state IN ('superseded','cancelled','failed')
            AND b.state IN ('open','published') ORDER BY b.created_at DESC""", (account_id,)).fetchall()
        already_bound = db.execute("SELECT 1 FROM forecast_batches WHERE job_id=?", (new_job_id,)).fetchone() is not None
        for batch in rows:
            has_prediction = db.execute("SELECT 1 FROM forecasts WHERE batch_id=? LIMIT 1", (batch["id"],)).fetchone()
            if (not already_bound and batch["kind"] in ("formal", "test") and not has_prediction
                    and batch["input_version"] == input_version and stamp < batch["deadline"]):
                db.execute("UPDATE forecast_batches SET job_id=?,team_snapshot='{}',state='open' WHERE id=?", (new_job_id, batch["id"]))
                db.execute("UPDATE forecast_samples SET state='uncovered',reason=NULL WHERE batch_id=? AND state='failed'", (batch["id"],))
                db.execute("INSERT INTO forecast_job_bindings(batch_id,prior_job_id,job_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (batch["id"], batch["job_id"], new_job_id, "同账户输入版本的执行重试/合并；保持原固定批次规则与范围", stamp))
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
            if not batch["team_snapshot"]:
                job = db.execute("SELECT config_snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job and job[0]:
                    team = _safe_team(_load(job[0]))
                    model, effort = _manager_identity(team)
                    team.setdefault("team_id", _digest(team))
                    team["manager_model"], team["manager_effort"] = model, effort
                    db.execute("UPDATE forecast_batches SET team_snapshot=? WHERE id=? AND team_snapshot='{}'", (_dump(team), batch["id"]))
                    batch["team_snapshot"] = team
            items = [dict(r) for r in db.execute("SELECT id,symbol,horizon,label,target_date,state,reason FROM forecast_samples WHERE batch_id=? ORDER BY symbol,horizon LIMIT ? OFFSET ?",
                     (row[0], min(200, max(1, limit)), max(0, offset)))]
            summary = {k: batch[k] for k in ("id", "account_id", "kind", "base_date", "rule_version", "rule",
                        "expected_count", "created_at", "deadline", "state", "input_version", "coverage")}
            team = batch["team_snapshot"]
            summary.update(team_id=team.get("team_id"), manager_model=team.get("manager_model"), manager_effort=team.get("manager_effort"))
            return {**summary, "items": items, "offset": offset, "has_more": offset + len(items) < batch["expected_count"],
                    "next_offset": offset + len(items) if offset + len(items) < batch["expected_count"] else None,
                    "instruction": "仅 manager 在 finish_analysis.forecasts 按 symbol、horizon 提交 up/sideways/down，或 abstain 并说明原因；未覆盖项保留缺口。期限、发布时间、基准与规则均由服务端绑定。"}

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
            forecast_id = uuid.uuid4().hex
            revises_id = item.get("revises_id")
            if revises_id:
                original = db.execute("SELECT account_id,symbol,horizon,published_at FROM forecasts WHERE id=?", (revises_id,)).fetchone()
                if not original or original["account_id"] != job["account_id"] or original["symbol"] != symbol or original["horizon"] != horizon or original["published_at"] > at.isoformat():
                    raise ValueError("修订关联须为本账户同证券同周期的已有预测")
            elif batch["kind"] == "exploratory":
                original = db.execute("SELECT id FROM forecasts WHERE account_id=? AND symbol=? AND horizon=? AND kind='formal' AND published_at<=? ORDER BY published_at DESC LIMIT 1", (job["account_id"], symbol, horizon, at.isoformat())).fetchone()
                revises_id = original[0] if original else None
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                forecast_id, sample["id"], batch["id"], job["account_id"], job["id"], analysis_id, "manager", batch["kind"],
                symbol, batch["base_date"], horizon, sample["target_date"], direction, reason, _dump(item.get("citations", [])),
                batch["rule_version"], _dump(batch["rule"]), team_id, _dump(team), model, effort, at.isoformat(), at.isoformat(),
                result.get("report", ""), revises_id))
            db.execute("UPDATE forecast_samples SET state=?,reason=? WHERE id=?", ("abstain" if direction == "abstain" else "predicted", reason if direction == "abstain" else None, sample["id"]))
            if direction != "abstain":
                due = (sample["target_date"] or batch["base_date"]) + "T15:30:00+08:00"
                db.execute("INSERT OR IGNORE INTO forecast_evaluation_tasks VALUES(?,'pending',0,?,NULL,?)", (forecast_id, due, at.isoformat()))
            saved.append(forecast_id)
        db.execute("UPDATE forecast_batches SET state='published',team_snapshot=? WHERE id=?", (_dump(team), batch["id"]))
        return saved

    def fail_forecast_job(self, job_id, reason="任务失败"):
        with self.connect() as db:
            db.execute("UPDATE forecast_samples SET state='failed',reason=? WHERE batch_id IN (SELECT id FROM forecast_batches WHERE job_id=?) AND state='uncovered'", (reason[:1000], job_id))

    def expire_forecast_batches(self, at=None):
        stamp = _local(at).isoformat()
        with self.connect() as db:
            db.execute("UPDATE forecast_samples SET state='missed',reason='超过截止仍未发布' WHERE state='uncovered' AND batch_id IN (SELECT id FROM forecast_batches WHERE deadline<?)", (stamp,))
            db.execute("UPDATE forecast_samples SET state='missed',reason='超过截止仍无可用基准收盘数据，未生成预测' WHERE state='waiting_data' AND batch_id IN (SELECT id FROM forecast_batches WHERE deadline<?)", (stamp,))
            db.execute("UPDATE forecast_batches SET state='closed' WHERE deadline<?", (stamp,))

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
            resolution = db.execute("SELECT * FROM forecast_calendar_resolutions WHERE forecast_id=?", (forecast_id,)).fetchone()
            result["calendar_resolution"] = {**dict(resolution), "evidence": _load(resolution["evidence"])} if resolution else None
            result["effective_target_date"] = result["target_date"] or (resolution["target_date"] if resolution else None)
            return result

    def _forecast_rows(self, db, filters):
        allowed = {"account_id", "kind", "team_id", "manager_model", "manager_effort", "symbol", "start_date", "end_date", "rule_version", "batch_id", "horizon"}
        if set(filters) - allowed:
            raise ValueError("不支持的预测筛选条件")
        clauses, values = [], []
        for key, value in filters.items():
            if value in (None, ""):
                continue
            if key in ("start_date", "end_date"):
                date.fromisoformat(value)
                clauses.append("b.base_date" + (">=?" if key == "start_date" else "<=?")); values.append(value)
            else:
                if key in ("team_id", "manager_model", "manager_effort"):
                    clauses.append(f"COALESCE(f.{key},json_extract(b.team_snapshot,'$.{key}'))=?")
                else:
                    alias = "b" if key in ("account_id", "kind", "rule_version") else "s" if key in ("symbol", "horizon", "batch_id") else "f"
                    clauses.append(f"{alias}.{key}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return db.execute("""SELECT s.*,b.kind,b.base_date,b.rule_version,b.rule,b.deadline,b.expected_count,
            f.id AS forecast_id,f.direction,f.published_at,f.report_id,f.reason AS forecast_reason,f.team_id,
            f.manager_model,f.manager_effort,e.status AS evaluation_status,e.reason AS evaluation_reason,
            e.actual_direction,e.return_ratio,e.hit,e.revision AS evaluation_revision,t.last_error AS waiting_reason
            FROM forecast_samples s JOIN forecast_batches b ON b.id=s.batch_id
            LEFT JOIN forecasts f ON f.sample_id=s.id
            LEFT JOIN forecast_evaluations e ON e.forecast_id=f.id AND e.revision=(SELECT max(x.revision) FROM forecast_evaluations x WHERE x.forecast_id=f.id)
            LEFT JOIN forecast_evaluation_tasks t ON t.forecast_id=f.id""" + where + " ORDER BY b.base_date DESC,s.symbol,s.horizon", values).fetchall()

    def forecasts_page(self, limit=50, offset=0, **filters):
        with self.connect() as db:
            rows = self._forecast_rows(db, filters)
        limit, offset = min(200, max(1, limit)), max(0, offset)
        items = []
        for row in rows[offset:offset + limit]:
            item = dict(row)
            item["rule"] = _load(item["rule"])
            item["calendar_evidence"] = _load(item["calendar_evidence"])
            item["display_state"] = self._forecast_display_state(item)
            items.append(item)
        return {"items": items, "total": len(rows), "has_more": offset + len(items) < len(rows),
                "next_offset": offset + len(items) if offset + len(items) < len(rows) else None}

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

    def forecast_comparison(self, account_ids, **filters):
        """User-facing comparison only; never injected into account research."""
        if not isinstance(account_ids, list) or not 2 <= len(set(account_ids)) <= 10:
            raise ValueError("请选择 2 至 10 个不同账户")
        filters = {"kind": "formal", **filters}
        if "account_id" in filters:
            raise ValueError("比较使用 account_ids")
        accounts = {account_id: self.account(account_id)["name"] for account_id in account_ids}
        rows = {}
        with self.connect() as db:
            for account_id in accounts:
                rows[account_id] = {(r["base_date"], r["symbol"], r["horizon"], r["rule_version"]): dict(r)
                    for r in self._forecast_rows(db, {**filters, "account_id": account_id})}
        shared = set.intersection(*(set(r) for r in rows.values()))
        evaluated = {key for key in shared if all(rows[a][key]["evaluation_status"] == "evaluated" for a in accounts)}
        groups = []
        for horizon, rule_version in sorted({(key[2], key[3]) for key in shared}):
            keys = {k for k in shared if k[2:] == (horizon, rule_version)}
            matured = keys & evaluated
            groups.append({"horizon": horizon, "rule_version": rule_version, "overlap_expected": len(keys),
                "overlap_evaluated": len(matured), "accounts": [{"account_id": a, "name": accounts[a],
                    "hits": sum(rows[a][key]["hit"] or 0 for key in matured), "evaluated": len(matured),
                    "accuracy": sum(rows[a][key]["hit"] or 0 for key in matured) / len(matured) if matured else None}
                    for a in accounts]})
        return {"matching_dimensions": ["base_date", "symbol", "horizon", "rule_version"], "groups": groups,
                "overlap_expected": len(shared), "overlap_evaluated": len(evaluated),
                "unmatched_counts": {a: len(rows[a]) - len(shared) for a in accounts},
                "note": "仅统计全部选定账户共同且均已到期评估的样本；共享范围仍不代表材料清单、额外搜索或发布时间完全一致，不是严格对照实验。"}

    def forecast_statistics(self, **filters):
        # Main metric defaults to genuine formal records; fixtures are separate.
        filters = {"kind": "formal", **filters}
        with self.connect() as db:
            rows = [dict(r) for r in self._forecast_rows(db, filters)]
        groups = defaultdict(list)
        for row in rows:
            groups[(row["horizon"], row["rule_version"])].append(row)
        output = []
        for (horizon, version), samples in sorted(groups.items()):
            counts = Counter(self._forecast_display_state(row) for row in samples)
            evaluated = [r for r in samples if r["evaluation_status"] == "evaluated"]
            hits = sum(r["hit"] or 0 for r in evaluated)
            matrix = {pred: {actual: 0 for actual in ("up", "sideways", "down")} for pred in ("up", "sideways", "down")}
            days = defaultdict(lambda: {"hits": 0, "evaluated": 0})
            for row in evaluated:
                matrix[row["direction"]][row["actual_direction"]] += 1
                days[row["base_date"]]["hits"] += row["hit"]
                days[row["base_date"]]["evaluated"] += 1
            predicted = sum(r["state"] == "predicted" for r in samples)
            addressed = predicted + sum(r["state"] == "abstain" for r in samples)
            output.append({"horizon": horizon, "label": samples[0]["label"], "rule_version": version,
                "threshold": _load(samples[0]["rule"])["threshold"], "expected": len(samples), "predicted": predicted,
                "hits": hits, "evaluated": len(evaluated), "accuracy": hits / len(evaluated) if evaluated else None,
                "coverage": addressed / len(samples), "directional_coverage": predicted / len(samples),
                "states": dict(counts), "confusion_matrix": matrix,
                "predicted_classes": dict(Counter(r["direction"] for r in samples if r["direction"])),
                "actual_classes": dict(Counter(r["actual_direction"] for r in evaluated)),
                "by_day": [{"date": day, **stats, "accuracy": stats["hits"] / stats["evaluated"]} for day, stats in sorted(days.items())]})
        return {"groups": output, "expected": len(rows), "filters": filters,
                "comparison_note": "仅相同基准批次日、证券、周期和规则的重叠样本可作并排比较；团队准确率不是主模型独立能力、交易胜率或收益。",
                "method": "D 收盘为基准，D 后第 N 个交易日收盘为终点。首次成功正式预测锁定，阈值边界计震荡；未成熟及缺失不进入准确率分母。"}

    def record_forecast_evaluation(self, forecast_id, evidence, *, at=None):
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
                    "供应商历史数据或可用性更正；保留上一评估" if previous else None))
            task = db.execute("SELECT attempts FROM forecast_evaluation_tasks WHERE forecast_id=?", (forecast_id,)).fetchone()
            attempts = task[0] if task else 0
            next_try = at + (timedelta(days=1) if status in ("evaluated", "unassessable") else timedelta(minutes=min(1440, 15 * 2 ** min(attempts, 7))))
            db.execute("UPDATE forecast_evaluation_tasks SET state=?,attempts=attempts+1,next_attempt=?,last_error=?,updated_at=? WHERE forecast_id=?",
                (status, next_try.isoformat(), reason, at.isoformat(), forecast_id))
        return evaluation_id

    async def evaluate_due_forecasts(self, config, *, at=None, provider=None, limit=30):
        at = _local(at)
        if provider is None:
            from .providers import MarketProvider
            provider = MarketProvider(config)
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT forecast_id FROM forecast_evaluation_tasks WHERE next_attempt<=? ORDER BY next_attempt LIMIT ?", (at.isoformat(), min(200, max(1, limit))))]
        results = []
        for forecast_id in ids:
            forecast = self.forecast_detail(forecast_id)
            if not forecast["target_date"]:
                # A later calendar extension may resolve the originally frozen
                # horizon, without changing the immutable forecast record.
                try:
                    target = advance_trading_days(forecast["base_date"], forecast["horizon"], config.get("calendar_path"))
                except (ValueError, OSError, KeyError):
                    target = {"target_date": None}
                with self.connect() as db:
                    db.execute("UPDATE forecast_evaluation_tasks SET state='waiting_data',last_error='calendar_gap',next_attempt=?,updated_at=? WHERE forecast_id=?", ((at + timedelta(hours=6)).isoformat(), at.isoformat(), forecast_id))
                # Target resolution is persisted as evidence, not retrospectively
                # invented from weekdays. record_evaluation handles it below.
                if not target["target_date"]:
                    continue
                with self.connect() as db:
                    db.execute("INSERT OR IGNORE INTO forecast_calendar_resolutions VALUES(?,?,?,?)", (forecast_id, target["target_date"], _dump(target), at.isoformat()))
                    db.execute("UPDATE forecast_samples SET target_date=? WHERE id=? AND target_date IS NULL", (target["target_date"], forecast["sample_id"]))
                forecast["target_date"] = target["target_date"]
            due = datetime.combine(date.fromisoformat(forecast["target_date"]), time(15, 30), SHANGHAI)
            if at < due:
                continue
            token, lease_at = uuid.uuid4().hex, _local()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                lease = db.execute("SELECT expires_at FROM forecast_evaluation_leases WHERE forecast_id=?", (forecast_id,)).fetchone()
                if lease and lease["expires_at"] > lease_at.isoformat():
                    continue
                db.execute("INSERT OR REPLACE INTO forecast_evaluation_leases VALUES(?,?,?)", (forecast_id, token, (lease_at + timedelta(minutes=5)).isoformat()))
            try:
                evidence = await asyncio.wait_for(closing_pair(provider, forecast["symbol"], forecast["base_date"], forecast["target_date"]),
                    timeout=min(120, max(10, int(config.get("network_timeout_seconds", 12)) * 4)))
                if self.record_forecast_evaluation(forecast_id, evidence, at=at):
                    results.append(forecast_id)
            except Exception as exc:
                with self.connect() as db:
                    db.execute("UPDATE forecast_evaluation_tasks SET state='waiting_data',attempts=attempts+1,last_error=?,next_attempt=?,updated_at=? WHERE forecast_id=?",
                        (type(exc).__name__ + ": " + str(exc)[:300], (at + timedelta(minutes=15)).isoformat(), at.isoformat(), forecast_id))
            finally:
                with self.connect() as db:
                    db.execute("DELETE FROM forecast_evaluation_leases WHERE forecast_id=? AND token=?", (forecast_id, token))
        return results

    async def forecast_tick(self, config, *, at=None, provider=None, create_batches=True):
        at = _local(at)
        self.expire_forecast_batches(at)
        evaluated = []
        if config.get("forecast_evaluation_enabled", True):
            evaluated = await self.evaluate_due_forecasts(config, at=at, provider=provider)
        created = []
        if not create_batches:
            return {"evaluated": evaluated, "batches": created}
        try:
            trading = trading_date(at.date(), config.get("calendar_path"))["trading_day"]
        except (OSError, ValueError, KeyError):
            trading = None
        if trading is not True or at.time().replace(tzinfo=None) < time(15):
            return {"evaluated": evaluated, "batches": created}
        for account in self.accounts():
            settings = self.forecast_settings(account["id"])
            with self.connect() as db:
                existing = db.execute("SELECT id,job_id FROM forecast_batches WHERE account_id=? AND kind=? AND base_date=?", (account["id"], "formal" if self.mode == "live" else "test", at.date().isoformat())).fetchone()
                batch = self._forecast_batch(db, existing["id"]) if existing else None
            if existing:
                if existing["job_id"] or batch["state"] in ("failed", "closed") or at.isoformat() >= batch["deadline"]:
                    continue
                if batch["state"] == "open" and any(v.get("status") == "ready" for v in batch["baseline_evidence"].values()):
                    created.append(batch)
                    continue
            else:
                if not settings["enabled"] or not _clock(settings["start_time"]) <= at.time().replace(tzinfo=None) < _clock(settings["deadline_time"]):
                    continue
                universe = self.universe_snapshot()["universe"]
                symbols = sorted({h["symbol"] for h in account["holdings"]} | set(universe.get("candidates", [])))
                if not symbols:
                    continue
                try:
                    batch = self.create_forecast_batch(account["id"], at=at, symbols=symbols,
                        calendar_path=config.get("calendar_path"), expected_input_version=account["version"])
                except ValueError as exc:
                    self.set_meta("forecast_baseline_wait:" + account["id"], {"at": at.isoformat(), "date": at.date().isoformat(), "reason": str(exc), "symbols": symbols})
                    continue
                with self.connect() as db:
                    db.execute("UPDATE forecast_batches SET state='waiting_data' WHERE id=?", (batch["id"],))
                    db.execute("UPDATE forecast_samples SET state='waiting_data',reason='等待当日精确收盘基准，尚未启动模型研究' WHERE batch_id=? AND state='uncovered'", (batch["id"],))
            if provider is None:
                from .providers import MarketProvider
                provider = MarketProvider(config)
            baseline = {}
            for symbol in batch["symbols"]:
                try:
                    baseline[symbol] = await asyncio.wait_for(closing_pair(provider, symbol, batch["base_date"], batch["base_date"]),
                        timeout=min(60, max(10, int(config.get("network_timeout_seconds", 12)) * 3)))
                except Exception as exc:
                    baseline[symbol] = {"symbol": symbol, "base_date": batch["base_date"], "target_date": batch["base_date"],
                        "status": "waiting_data", "reason": "baseline_unavailable", "error": type(exc).__name__, "fetched_at": now()}
            ready = any(v["status"] == "ready" for v in baseline.values())
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._forecast_batch(db, batch["id"])
                if current["job_id"]:
                    continue
                if self.account(account["id"], db)["version"] != batch["input_version"]:
                    db.execute("UPDATE forecast_batches SET state='failed',baseline_evidence=? WHERE id=?", (_dump(baseline), batch["id"]))
                    db.execute("UPDATE forecast_samples SET state='failed',reason='等待基准期间账户输入已变化' WHERE batch_id=? AND state IN ('uncovered','waiting_data')", (batch["id"],))
                    continue
                db.execute("UPDATE forecast_batches SET state=?,baseline_evidence=? WHERE id=?", ("open" if ready else "waiting_data", _dump(baseline), batch["id"]))
                if ready:
                    db.execute("UPDATE forecast_samples SET state='uncovered',reason=NULL WHERE batch_id=? AND state='waiting_data'", (batch["id"],))
                    created.append(self._forecast_batch(db, batch["id"]))
        return {"evaluated": evaluated, "batches": created}
