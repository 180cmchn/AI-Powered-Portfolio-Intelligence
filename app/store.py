from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import sqlite3
import uuid

from .config import defaults
from .domain import TZ, now
from .materials import MaterialStore, SCHEMA as MATERIAL_SCHEMA
from .recommendations import RecommendationStore, SCHEMA as RECOMMENDATION_SCHEMA
from .scheduling import SchedulingStore, SCHEMA as SCHEDULING_SCHEMA
from .references import ReferenceStore
from .recovery import RecoveryStore
from .agent_config import AgentConfigStore, SCHEMA as AGENT_CONFIG_SCHEMA
from .forecasts import ForecastStore, SCHEMA as FORECAST_SCHEMA
from .forecast_windows import ForecastWindowStore, SCHEMA as FORECAST_WINDOW_SCHEMA
from .task_tracking import TaskTrackingStore


def uid() -> str:
    return uuid.uuid4().hex


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value, fallback=None):
    return json.loads(value) if value else fallback


class Conflict(Exception):
    pass


class ReportPublicationError(ValueError):
    pass


class Store(MaterialStore, RecommendationStore, SchedulingStore, ReferenceStore, RecoveryStore, AgentConfigStore, ForecastStore, ForecastWindowStore, TaskTrackingStore):
    """SQLite is the durable queue, input snapshot store, and atomic publication fence."""

    def __init__(self, path: Path | str, mode="live"):
        self.path, self.mode = Path(path), mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Before any upgrade, retain an online SQLite snapshot (including WAL).
        if self.path.exists():
            with sqlite3.connect(self.path) as prior:
                table = prior.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                version = prior.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if table else None
                if version and int(loads(version[0])) > 10:
                    raise ValueError("数据库版本比程序更新，请升级程序")
                if version and int(loads(version[0])) == 1:
                    backup = self.path.with_name(self.path.name + ".before-v2.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 3:
                    backup = self.path.with_name(self.path.name + ".before-v3.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 4:
                    backup = self.path.with_name(self.path.name + ".before-v4.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 5:
                    backup = self.path.with_name(self.path.name + ".before-v5.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 6:
                    backup = self.path.with_name(self.path.name + ".before-v6.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 7:
                    backup = self.path.with_name(self.path.name + ".before-v7.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 8:
                    backup = self.path.with_name(self.path.name + ".before-v8.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 9:
                    backup = self.path.with_name(self.path.name + ".before-v9.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
                if version and int(loads(version[0])) < 10:
                    backup = self.path.with_name(self.path.name + ".before-v10.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as saved:
                            prior.backup(saved)
                        backup.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, cash TEXT, preferences TEXT NOT NULL,
                  holdings TEXT NOT NULL, version INTEGER NOT NULL, analysis_state TEXT NOT NULL,
                  latest_job_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(
                  account_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                  created_at TEXT NOT NULL, PRIMARY KEY(account_id,version));
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, input_version INTEGER NOT NULL,
                  reason TEXT NOT NULL, state TEXT NOT NULL, mode TEXT NOT NULL, due_at TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT, data_as_of TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, event_key TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,due_at);
                CREATE INDEX IF NOT EXISTS jobs_account_created ON jobs(account_id,created_at);
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id,seq);
                CREATE TABLE IF NOT EXISTS analyses(
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
                  input_version INTEGER NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
                  report TEXT NOT NULL, result TEXT NOT NULL, data_as_of TEXT, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS analyses_account_created ON analyses(account_id,created_at);
                CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS securities(symbol TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS materials(
                  id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS material_uses(
                  material_id TEXT NOT NULL, job_id TEXT NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(material_id,job_id));
                CREATE TABLE IF NOT EXISTS synthesis_checkpoints(
                  job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, input_version INTEGER,
                  universe_version INTEGER NOT NULL, body TEXT NOT NULL, guard TEXT NOT NULL,
                  created_at TEXT NOT NULL, resume_count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS information_routes(
                  material_id TEXT PRIMARY KEY, public_run_id TEXT, state TEXT NOT NULL,
                  decision TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            existing_mode = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            if existing_mode and loads(existing_mode[0]) != mode:
                raise ValueError("演示与真实运行必须使用不同数据库")
            db.execute("INSERT OR IGNORE INTO meta VALUES('mode',?)", (dumps(mode),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
            db.executescript(SCHEDULING_SCHEMA + MATERIAL_SCHEMA + RECOMMENDATION_SCHEMA + AGENT_CONFIG_SCHEMA + FORECAST_SCHEMA + FORECAST_WINDOW_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            self.migrate_material_catalog(db)
            self.migrate_research_versions(db)
            self.migrate_recovery_deliveries(db)
            self.migrate_recommendations(db)
            if "config_snapshot" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN config_snapshot TEXT")
            settings_row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            saved_settings = loads(settings_row[0], {}) if settings_row else {}
            # Independent settings: an old search-provider disable never disables
            # model-hosted search. Preserve an explicit new choice on every upgrade.
            if "model_native_search" not in saved_settings:
                saved_settings["model_native_search"] = defaults()["model_native_search"]
                db.execute("INSERT OR REPLACE INTO meta VALUES('settings',?)", (dumps(saved_settings),))
                db.execute("INSERT OR IGNORE INTO meta VALUES('native_search_migration',?)", (dumps({
                    "at": now(), "from_schema": 3, "legacy_search_provider": saved_settings.get("search_provider"),
                    "model_native_search": saved_settings["model_native_search"], "reason": "independent_model_tool_setting"}),))
            # Do not persist a reasoning_effort default here: saved explicit
            # values > environment > medium is resolved on each settings read.
            self.migrate_agent_config(db, {**defaults(), **saved_settings})
            self.migrate_eighth_roles(db)
            self.migrate_ninth_roles(db)
            self.migrate_tenth_roles(db)
            self.migrate_forecasts(db)
            self.migrate_forecast_windows(db)
            self.migrate_task_tracking(db)
            db.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key, default=None):
        with self.connect() as db:
            r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return loads(r[0], default) if r else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))
            if key in ('settings', 'universe'):
                self.record_window_configuration(db, source=key + '_saved')

    def settings(self):
        settings = {**defaults(), **self.meta("settings", {})}
        # Keep legacy values in the DB for traceability, out of the runtime/UI.
        for key in ("search_provider", "search_key_env", "search_max_results", "news_read_sources"):
            settings.pop(key, None)
        return settings

    def running_model_configs(self):
        from .config import model_config_summary
        with self.connect() as db:
            jobs = db.execute("SELECT id,account_id,config_snapshot FROM jobs WHERE state='running' ORDER BY created_at").fetchall()
            runs = db.execute("SELECT id,input_snapshot FROM global_runs WHERE state='running' ORDER BY created_at").fetchall()
        result = []
        for row in jobs:
            cfg = loads(row['config_snapshot'])
            result.append({'id': row['id'], 'scope': 'account', 'account_id': row['account_id'],
                           **(model_config_summary(cfg) if cfg else {'reasoning_effort': None, 'note': '历史任务未记录模型配置'})})
        for row in runs:
            cfg = (loads(row['input_snapshot']) or {}).get('config')
            result.append({'id': row['id'], 'scope': 'global', 'account_id': None,
                           **(model_config_summary(cfg) if cfg else {'reasoning_effort': None, 'note': '历史任务未记录模型配置'})})
        return result

    def task_config_snapshot(self, db, account_id=None):
        """Resolve settings once and record credential-sensitive evidence identity."""
        from .model import native_search_fingerprint
        saved = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        config = {**defaults(), **loads(saved[0] if saved else None, {})}
        config['_agent_team'] = self.freeze_agent_team(account_id, db=db)
        team = config['_agent_team']
        role = team['roles']['manager']
        binding = role['resolved']
        provider = team['connections'].get(binding.get('provider_id'), {})
        config.update(model_name=binding.get('model_id', ''), reasoning_effort=binding.get('reasoning_effort', 'medium'),
            model_base_url=provider.get('base_url', ''), model_protocol=provider.get('protocol', 'responses'),
            provider_id=provider.get('id'), provider_revision=provider.get('revision'),
            credential_revision=provider.get('credential_revision'), role_id='manager', role_version=role['version'],
            prompt_version=role['prompt_version'], tool_version=role['tool_version'], team_id=team['team_id'],
            account_id=account_id, _credential_fingerprint=(provider.get('credential_reference') or {}).get('fingerprint'))
        request_options = {k: config.get(k) for k in ('model_native_search', 'model_allow_no_key', 'reasoning',
            'chat_use_max_completion_tokens', 'max_output_tokens', 'model_request_timeout_seconds', 'model_responses_stream')}
        config['_model_configuration_fingerprint'] = hashlib.sha256(
            dumps({'team_id': team['team_id'], 'request_options': request_options}).encode()).hexdigest()
        return config

    def resume_configuration_waiters(self, db):
        """Do not automatically repeat a rejected request under identical settings."""
        current = self.task_config_snapshot(db)['_model_configuration_fingerprint']
        jobs = db.execute("""SELECT id,account_id,config_snapshot FROM jobs WHERE state='waiting_config'
            AND EXISTS(SELECT 1 FROM accounts a WHERE a.latest_job_id=jobs.id AND a.version=jobs.input_version)""").fetchall()
        for row in jobs:
            current = self.task_config_snapshot(db, row['account_id'])['_model_configuration_fingerprint']
            previous = (loads(row['config_snapshot']) or {}).get('_model_configuration_fingerprint')
            if previous != current:  # Legacy work without evidence can resume once.
                db.execute("UPDATE jobs SET state='queued',stage='waiting',error=NULL,due_at=?,updated_at=? WHERE id=?", (now(), now(), row['id']))
        current = self.task_config_snapshot(db)['_model_configuration_fingerprint']
        runs = db.execute("SELECT id,input_snapshot FROM global_runs WHERE state='waiting_config'").fetchall()
        for row in runs:
            previous = ((loads(row['input_snapshot']) or {}).get('config') or {}).get('_model_configuration_fingerprint')
            if previous != current:
                db.execute("UPDATE research_demands SET state='pending',job_id=NULL,detail='model configuration changed' WHERE job_id=? AND state='assigned'", (row['id'],))
                db.execute("UPDATE global_runs SET state='superseded',updated_at=? WHERE id=?", (now(), row['id']))

    def account(self, account_id, db=None):
        if db is None:
            with self.connect() as conn:
                return self.account(account_id, conn)
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError("账户不存在")
        result = dict(row)
        for key in ("holdings", "preferences"):
            result[key] = loads(result[key])
        return result

    def accounts(self):
        with self.connect() as db:
            return [self.account(r[0], db) for r in db.execute("SELECT id FROM accounts ORDER BY created_at")]

    def save_account(self, payload, account_id=None, reason="account_saved", db=None):
        if db is None:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                return self.save_account(payload, account_id, reason, conn)
        data = payload.model_dump() if hasattr(payload, "model_dump") else payload.copy()
        account_id, stamp = account_id or uid(), now()
        delay = self.settings()["debounce_seconds"]
        if db is not None:
            old = db.execute("SELECT version FROM accounts WHERE id=?", (account_id,)).fetchone()
            version = old[0] + 1 if old else 1
            if data.get("expected_version") is not None and (not old or data["expected_version"] != old[0]):
                raise Conflict("账户已被其他页面修改，请刷新后重试")
            db.execute("""INSERT INTO accounts VALUES(?,?,?,?,?,?,'pending',NULL,?,?)
                          ON CONFLICT(id) DO UPDATE SET name=excluded.name,cash=excluded.cash,
                          preferences=excluded.preferences,holdings=excluded.holdings,
                          version=excluded.version,analysis_state='pending',updated_at=excluded.updated_at""",
                       (account_id, data["name"], data.get("cash"), dumps(data.get("preferences", {})),
                        dumps(data.get("holdings", [])), version, stamp, stamp))
            db.execute("UPDATE analyses SET status='stale' WHERE account_id=? AND status='current'", (account_id,))
            db.execute("UPDATE research_records SET applicability='stale' WHERE account_id=? AND applicability='current'", (account_id,))
            self._invalidate_recommendations(db, account_id, "账户持仓或偏好版本已变更")
            db.execute("DELETE FROM condition_latches WHERE account_id=?", (account_id,))
            account = self.account(account_id, db)
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (account_id, version, dumps(account), stamp))
            self.record_window_configuration(db, account_id, at=stamp, source='account_saved')
            queued = self._enqueue(db, account, reason, delay=delay)
            if data.get('research_scope'):
                metadata = self.new_task_metadata(db, account, reason, research_scope=data['research_scope'])
                metadata['planned_at'] = queued['due_at']
                if data['research_scope'] == 'cash_only' and metadata['target']['symbols']:
                    raise ValueError('现金与偏好分析仅用于空证券范围')
                db.execute('UPDATE jobs SET task_metadata=? WHERE id=?', (dumps(metadata), queued['id']))
        return self.account(account_id, db)

    def _enqueue(self, db, account, reason, delay=0, event_key=None):
        if event_key:
            exists = db.execute("SELECT * FROM jobs WHERE event_key=?", (event_key,)).fetchone()
            if exists:
                return dict(exists)
        job_id, stamp = uid(), now()
        replaced = db.execute("SELECT id FROM jobs WHERE account_id=? AND state IN ('queued','running','waiting_config','waiting_scope')", (account['id'],)).fetchall()
        for previous in replaced:
            self.update_forecast_attempt_state(db, previous['id'], 'superseded', '已有较新的账户研究任务')
        db.execute("UPDATE jobs SET state='superseded',completed_at=?,updated_at=? WHERE account_id=? AND state IN ('queued','running','waiting_config','waiting_scope')", (stamp, stamp, account["id"]))
        # A replaced execution cannot erase the business work it was assigned.
        db.execute("UPDATE research_demands SET state='pending',job_id=NULL,input_version=?,detail='moved to latest account input',updated_at=? WHERE account_id=? AND state IN ('assigned','pending')", (account["version"], stamp, account["id"]))
        db.execute("UPDATE analyses SET status='stale' WHERE account_id=? AND status='current'", (account["id"],))
        due = (datetime.now(TZ) + timedelta(seconds=delay)).isoformat()
        db.execute("INSERT INTO jobs(id,account_id,input_version,reason,state,mode,due_at,created_at,updated_at,event_key) VALUES(?,?,?,?,'queued',?,?,?,?,?)",
                   (job_id, account["id"], account["version"], reason, self.mode, due, stamp, stamp, event_key))
        db.execute("UPDATE jobs SET task_metadata=?,stage='waiting' WHERE id=?", (dumps({**self.new_task_metadata(db, account, reason),'planned_at':due}), job_id))
        self.reconcile_forecast_jobs(db, account["id"], job_id, account["version"])
        db.execute("UPDATE accounts SET latest_job_id=?,analysis_state='pending' WHERE id=?", (job_id, account["id"]))
        db.execute("UPDATE research_demands SET state='assigned',job_id=?,detail='included in latest account execution',updated_at=? WHERE account_id=? AND state='pending'", (job_id, stamp, account["id"]))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue(self, account_id, reason="manual", event_key=None, delay=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if reason in ('manual', 'retry', 'manual_retry'):
                db.execute("""UPDATE research_demands SET state='pending',job_id=NULL,
                    detail='explicit user retry of unfinished research',updated_at=?
                    WHERE account_id=? AND state IN ('uncovered','failed')""", (now(), account_id))
            return self._enqueue(db, self.account(account_id, db), reason, delay, event_key)

    def delete_account(self, account_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.account(account_id, db)
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE account_id=?", (account_id,))]
            for j in ids:
                db.execute("DELETE FROM events WHERE job_id=?", (j,))
            db.execute("DELETE FROM recommendation_events WHERE recommendation_id IN (SELECT id FROM recommendations WHERE account_id=?)", (account_id,))
            for table in ("accounts", "snapshots", "jobs", "analyses", "material_uses", "recommendations", "research_records", "research_demands", "condition_latches", "synthesis_checkpoints", "task_operations", "account_operations"):
                col = "id" if table == "accounts" else "account_id"
                db.execute(f"DELETE FROM {table} WHERE {col}=?", (account_id,))

    def job(self, job_id, events=False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            r = dict(row)
            r["result"] = loads(r["result"])
            r["config_snapshot"] = loads(r["config_snapshot"])
            r["synthesis_checkpoint"] = self.checkpoint_status(job_id)
            if events:
                r["events"] = [loads(e[0]) for e in db.execute("SELECT body FROM events WHERE job_id=? ORDER BY seq", (job_id,))]
            return self.task_view(r, db)

    def jobs(self, account_id, limit=60, offset=0, events=False, summary=False):
        with self.connect() as db:
            rows = db.execute("""SELECT j.*, (SELECT count(*) FROM events e WHERE e.job_id=j.id) AS event_count,
                 (SELECT max(seq) FROM events e WHERE e.job_id=j.id) AS last_event_seq
                 FROM jobs j WHERE account_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?""", (account_id, min(max(limit, 1), 200), max(offset, 0))).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["config_snapshot"] = loads(item["config_snapshot"])
            item["synthesis_checkpoint"] = self.checkpoint_status(item['id'])
            if summary:
                item.pop("result", None)
            else:
                item["result"] = loads(item["result"])
            if events:
                item["events"] = self.events_since(item["id"])["events"]
            result.append(self.task_view(item))
        return result

    def events_since(self, job_id, after=0, limit=100):
        limit = min(max(limit, 1), 500)
        with self.connect() as db:
            rows = db.execute("SELECT seq,body FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, max(0, after), limit + 1)).fetchall()
        events = [{**loads(r["body"]), "seq": r["seq"]} for r in rows[:limit]]
        return {"events": events, "next_after": events[-1]["seq"] if events else after, "has_more": len(rows) > limit}

    def snapshot(self, account_id, version):
        with self.connect() as db:
            row = db.execute("SELECT body FROM snapshots WHERE account_id=? AND version=?", (account_id, version)).fetchone()
        if not row:
            raise KeyError("输入版本不存在")
        return loads(row[0])

    def claim_jobs(self, limit=2, account_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT j.* FROM jobs j JOIN accounts a ON a.latest_job_id=j.id WHERE j.state='queued' AND j.due_at<=? AND (? IS NULL OR j.account_id=?) ORDER BY j.created_at LIMIT ?", (now(), account_id, account_id, limit)).fetchall()
            # Queued work adopts the configuration at claim/start. This same
            # immutable value drives all HTTP retries and child sessions. A new
            # claim after restart explicitly starts a new attempt/configuration.
            for r in rows:
                cfg = self.task_config_snapshot(db, r["account_id"])
                from .config import model_config_summary
                previous = loads(r['config_snapshot'])
                before = model_config_summary(previous) if previous else None
                current = model_config_summary(cfg)
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1,started_at=COALESCE(started_at,?),attempt_started_at=?,completed_at=NULL,stage='preparing',updated_at=?,config_snapshot=? WHERE id=?", (now(), now(), now(), dumps(cfg), r["id"]))
                self.bind_forecast_attempt(db, r['id'], r['attempts'] + 1, cfg)
                db.execute("UPDATE accounts SET analysis_state='running' WHERE id=?", (r["account_id"],))
                # Each restart attempt keeps an audit relation, even when the
                # latest configuration snapshot on the job is replaced.
                db.execute("INSERT INTO events(job_id,body) VALUES(?,?)", (r['id'], dumps({
                    'type': 'model_configuration_bound', 'at': now(), 'agent': 'system',
                    'message': '已按任务领取时设置绑定本轮模型配置；尚不代表请求已发送',
                    'payload': {'attempt': r['attempts'] + 1, 'policy': 'claim_time',
                        'previous': before, 'current': current,
                        'changed': before != current if before else None}})))
        return [self.job(r["id"]) for r in rows]

    def event(self, job_id, event):
        payload = event.get("payload", {})
        msg = event.get("message") or (next((payload.get(k) for k in ("message", "summary", "error", "task", "tool") if payload.get(k)), None) if isinstance(payload, dict) else None) or event.get("type", "progress")
        body = {**event, "at": event.get("timestamp") or now(), "agent": event.get("agent_id", "system"), "message": msg}
        with self.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE id=? UNION SELECT 1 FROM global_runs WHERE id=?", (job_id, job_id)).fetchone():
                db.execute("INSERT INTO events(job_id,body) VALUES(?,?)", (job_id, dumps(body)))
                if event.get('type') in ('synthesis_resuming', 'synthesis_context_prepared', 'synthesis_context_projected'):
                    db.execute("UPDATE jobs SET stage='synthesizing' WHERE id=? AND state='running'", (job_id,))

    def set_job_state(self, job_id, state, error=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] in ("superseded", "cancelled", "completed"):
                return
            db.execute("UPDATE jobs SET state=?,error=?,completed_at=?,updated_at=? WHERE id=?", (state, error, now() if state in ('failed','cancelled','superseded') else None, now(), job_id))
            if state in ('waiting_config', 'waiting_scope'):
                db.execute('UPDATE jobs SET stage=? WHERE id=?', (state, job_id))
            self.update_forecast_attempt_state(db, job_id, state, error)
            db.execute("UPDATE accounts SET analysis_state=? WHERE id=? AND latest_job_id=?", (state, row["account_id"], job_id))
            if state in ("failed", "cancelled"):
                db.execute("""UPDATE forecast_samples SET state='failed',reason=? WHERE batch_id IN
                    (SELECT id FROM forecast_batches WHERE job_id=?) AND state IN ('uncovered','waiting_data')
                    AND id IN (SELECT sa.sample_id FROM forecast_sample_attempts sa JOIN forecast_execution_attempts a
                        ON a.id=sa.attempt_id WHERE a.job_id=?)""", (error or state, job_id, job_id))
                db.execute("UPDATE research_demands SET state=?,detail=?,updated_at=? WHERE job_id=? AND state='assigned'", (state, error or state, now(), job_id))

    def publish(self, job_id, result, data_as_of):
        if not isinstance(result.get('report'), str) or not result['report'].strip():
            raise ReportPublicationError('最终报告为空；子研究或成功请求不能代替最终报告发布')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return False
            already = db.execute("SELECT status FROM analyses WHERE job_id=?", (job_id,)).fetchone()
            if already:
                return already["status"] == "current"
            acc = db.execute("SELECT * FROM accounts WHERE id=?", (job["account_id"],)).fetchone()
            valid = bool(acc and acc["version"] == job["input_version"] and acc["latest_job_id"] == job_id and job["state"] == "running")
            result = self.resolve_result_references(result, job['account_id'], db)
            # Retain late results as explicitly stale history, never the current recommendation.
            analysis_id = uid()
            db.execute("INSERT OR IGNORE INTO analyses VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (analysis_id, job_id, job["account_id"], job["input_version"], "current" if valid else "stale", self.mode,
                        result.get("report", ""), dumps(result), data_as_of, now()))
            terminal = "completed" if valid else ("cancelled" if job["state"] == "cancelled" else "superseded")
            db.execute("UPDATE jobs SET state=?,result=?,data_as_of=?,stage='published',completed_at=?,updated_at=? WHERE id=?", (terminal, dumps(result), data_as_of, now(), now(), job_id))
            if valid:
                self.publish_forecasts(db, dict(job), result, analysis_id)
                db.execute('UPDATE synthesis_checkpoints SET resume_count=2 WHERE job_id=? AND account_id=? AND input_version=?',
                           (result.get('synthesis_checkpoint_job_id'), job['account_id'], job['input_version']))
                db.execute("UPDATE analyses SET status='stale' WHERE account_id=? AND job_id<>? AND status='current'", (job["account_id"], job_id))
                db.execute("UPDATE accounts SET analysis_state='current' WHERE id=?", (job["account_id"],))
                self._save_suggestions(db, job, result)
                self.save_research_records(result.get("research_records", []), job_id, job["account_id"], db)
                result['demand_coverage'] = self.complete_task_demands(db, job_id, result)
                db.execute('UPDATE analyses SET result=? WHERE job_id=?', (dumps(result), job_id))
                db.execute('UPDATE jobs SET result=? WHERE id=?', (dumps(result), job_id))
            return valid

    def analyses(self, account_id, limit=100, offset=0, summary=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM analyses WHERE account_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (account_id, min(max(limit, 1), 200), max(offset, 0))).fetchall()
            if summary:
                return [{k: v for k, v in dict(r).items() if k not in ("result", "report")} for r in rows]
            return [{**dict(r), "result": self.project_report_references(loads(r["result"]), account_id, db)} for r in rows]

    def analysis(self, analysis_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
            if not row:
                raise KeyError("分析不存在")
            return {**dict(row), "result": self.project_report_references(loads(row["result"]), row['account_id'], db)}

    def previous_analysis(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT a.* FROM analyses a JOIN jobs j ON j.id=a.job_id WHERE a.account_id=? AND j.state='completed' ORDER BY a.input_version DESC,a.created_at DESC LIMIT 1", (account_id,)).fetchone()
        return {**dict(row), "result": loads(row["result"])} if row else None

    def recover(self, config_ready=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            abandoned = db.execute("SELECT id FROM jobs WHERE state IN ('running','queued','waiting_config') AND NOT EXISTS(SELECT 1 FROM accounts a WHERE a.latest_job_id=jobs.id AND a.version=jobs.input_version)").fetchall()
            for previous in abandoned:
                self.update_forecast_attempt_state(db, previous['id'], 'superseded', '服务恢复时已有较新的账户输入或任务')
            db.execute("UPDATE jobs SET state='superseded' WHERE state IN ('running','queued','waiting_config') AND NOT EXISTS(SELECT 1 FROM accounts a WHERE a.latest_job_id=jobs.id AND a.version=jobs.input_version)")
            for interrupted in db.execute("SELECT id FROM jobs WHERE state='running'").fetchall():
                self.update_forecast_attempt_state(db, interrupted['id'], 'interrupted', '服务恢复：上次执行未完成')
                db.execute('UPDATE jobs SET recovery_attempts=recovery_attempts+1 WHERE id=?', (interrupted['id'],))
                db.execute('INSERT INTO events(job_id,body) VALUES(?,?)', (interrupted['id'], dumps({'type':'service_recovered','at':now(),'message':'服务重启恢复；与配置等待、供应商请求失败分别计数'})))
            db.execute("UPDATE jobs SET state='queued',stage='waiting',due_at=?,error='服务恢复：按当前输入重新运行',updated_at=? WHERE state='running'", (now(), now()))
            self.recover_demands(db, config_ready)
            db.execute("UPDATE accounts SET analysis_state='pending' WHERE latest_job_id IN (SELECT id FROM jobs WHERE state='queued')")

    def cache(self, table):
        assert table in ("quotes", "securities")
        with self.connect() as db:
            return {r[0]: loads(r[1]) for r in db.execute(f"SELECT symbol,body FROM {table}")}

    def put_cache(self, table, symbol, value):
        assert table in ("quotes", "securities")
        with self.connect() as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?)", (symbol, dumps(value)))

    def monitored(self):
        universe = self.meta("universe", {"watchlist": [], "candidates": []})
        symbols = set(universe["watchlist"]) | set(universe["candidates"])
        for a in self.accounts():
            symbols.update(h["symbol"] for h in a["holdings"])
        # Active job snapshots retain dependencies even when inputs have changed.
        with self.connect() as db:
            for r in db.execute("SELECT s.body FROM snapshots s JOIN jobs j ON s.account_id=j.account_id AND s.version=j.input_version WHERE j.state IN ('queued','running')"):
                symbols.update(h["symbol"] for h in loads(r[0])["holdings"])
            symbols.update(r[0] for r in db.execute("SELECT DISTINCT security_id FROM recommendations WHERE state IN ('watching','triggered','needs_review')"))
        return sorted(symbols)

    def materials(self, account_id=None, query=""):
        with self.connect() as db:
            if account_id:
                rows = db.execute("SELECT DISTINCT m.body FROM materials m JOIN material_uses u ON m.id=u.material_id WHERE u.account_id=? AND (?='' OR instr(lower(m.body),lower(?))>0) ORDER BY m.created_at DESC LIMIT 300", (account_id, query, query))
            else:
                rows = db.execute("SELECT body FROM materials WHERE ?='' OR instr(lower(body),lower(?))>0 ORDER BY created_at DESC LIMIT 300", (query, query))
            return [loads(r[0]) for r in rows]

    def material(self, material_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            raise KeyError("材料不存在")
        return loads(row[0])

    def backup(self, destination):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        target.chmod(0o600)
        return target
