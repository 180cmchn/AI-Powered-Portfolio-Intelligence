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


def uid() -> str:
    return uuid.uuid4().hex


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value, fallback=None):
    return json.loads(value) if value else fallback


class Conflict(Exception):
    pass


class Store:
    """SQLite is the durable queue, input snapshot store, and atomic publication fence."""

    def __init__(self, path: Path | str, mode="live"):
        self.path, self.mode = Path(path), mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analyses(
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
                  input_version INTEGER NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
                  report TEXT NOT NULL, result TEXT NOT NULL, data_as_of TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS securities(symbol TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS materials(
                  id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS material_uses(
                  material_id TEXT NOT NULL, job_id TEXT NOT NULL, account_id TEXT NOT NULL,
                  PRIMARY KEY(material_id,job_id));
            """)
            existing_mode = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            if existing_mode and loads(existing_mode[0]) != mode:
                raise ValueError("演示与真实运行必须使用不同数据库")
            db.execute("INSERT OR IGNORE INTO meta VALUES('mode',?)", (dumps(mode),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
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

    def settings(self):
        return {**defaults(), **self.meta("settings", {})}

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

    def save_account(self, payload, account_id=None, reason="account_saved"):
        data = payload.model_dump() if hasattr(payload, "model_dump") else payload.copy()
        account_id, stamp = account_id or uid(), now()
        delay = self.settings()["debounce_seconds"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
            account = self.account(account_id, db)
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (account_id, version, dumps(account), stamp))
            self._enqueue(db, account, reason, delay=delay)
        return self.account(account_id)

    def _enqueue(self, db, account, reason, delay=0, event_key=None):
        if event_key:
            exists = db.execute("SELECT * FROM jobs WHERE event_key=?", (event_key,)).fetchone()
            if exists:
                return dict(exists)
        job_id, stamp = uid(), now()
        db.execute("UPDATE jobs SET state='superseded',updated_at=? WHERE account_id=? AND state IN ('queued','running','waiting_config')", (stamp, account["id"]))
        db.execute("UPDATE analyses SET status='stale' WHERE account_id=? AND status='current'", (account["id"],))
        due = (datetime.now(TZ) + timedelta(seconds=delay)).isoformat()
        db.execute("INSERT INTO jobs(id,account_id,input_version,reason,state,mode,due_at,created_at,updated_at,event_key) VALUES(?,?,?,?,'queued',?,?,?,?,?)",
                   (job_id, account["id"], account["version"], reason, self.mode, due, stamp, stamp, event_key))
        db.execute("UPDATE accounts SET latest_job_id=?,analysis_state='pending' WHERE id=?", (job_id, account["id"]))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue(self, account_id, reason="manual", event_key=None, delay=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, self.account(account_id, db), reason, delay, event_key)

    def delete_account(self, account_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.account(account_id, db)
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE account_id=?", (account_id,))]
            for j in ids:
                db.execute("DELETE FROM events WHERE job_id=?", (j,))
            for table in ("accounts", "snapshots", "jobs", "analyses", "material_uses"):
                col = "id" if table == "accounts" else "account_id"
                db.execute(f"DELETE FROM {table} WHERE {col}=?", (account_id,))

    def job(self, job_id, events=False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            r = dict(row)
            r["result"] = loads(r["result"])
            if events:
                r["events"] = [loads(e[0]) for e in db.execute("SELECT body FROM events WHERE job_id=? ORDER BY seq", (job_id,))]
            return r

    def jobs(self, account_id):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE account_id=? ORDER BY created_at DESC LIMIT 60", (account_id,))]
        return [self.job(j, events=True) for j in ids]

    def snapshot(self, account_id, version):
        with self.connect() as db:
            row = db.execute("SELECT body FROM snapshots WHERE account_id=? AND version=?", (account_id, version)).fetchone()
        if not row:
            raise KeyError("输入版本不存在")
        return loads(row[0])

    def claim_jobs(self, limit=2):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT j.* FROM jobs j JOIN accounts a ON a.latest_job_id=j.id WHERE j.state='queued' AND j.due_at<=? ORDER BY j.created_at LIMIT ?", (now(), limit)).fetchall()
            for r in rows:
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), r["id"]))
                db.execute("UPDATE accounts SET analysis_state='running' WHERE id=?", (r["account_id"],))
        return [self.job(r["id"]) for r in rows]

    def event(self, job_id, event):
        payload = event.get("payload", {})
        msg = event.get("message") or (next((payload.get(k) for k in ("message", "summary", "error", "task", "tool") if payload.get(k)), None) if isinstance(payload, dict) else None) or event.get("type", "progress")
        body = {**event, "at": event.get("timestamp") or now(), "agent": event.get("agent_id", "system"), "message": msg}
        with self.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                db.execute("INSERT INTO events(job_id,body) VALUES(?,?)", (job_id, dumps(body)))

    def set_job_state(self, job_id, state, error=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] in ("superseded", "cancelled", "completed"):
                return
            db.execute("UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?", (state, error, now(), job_id))
            db.execute("UPDATE accounts SET analysis_state=? WHERE id=? AND latest_job_id=?", (state, row["account_id"], job_id))

    def publish(self, job_id, result, data_as_of):
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
            # Retain late results as explicitly stale history, never the current recommendation.
            db.execute("INSERT OR IGNORE INTO analyses VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (uid(), job_id, job["account_id"], job["input_version"], "current" if valid else "stale", self.mode,
                        result.get("report", ""), dumps(result), data_as_of, now()))
            terminal = "completed" if valid else ("cancelled" if job["state"] == "cancelled" else "superseded")
            db.execute("UPDATE jobs SET state=?,result=?,data_as_of=?,updated_at=? WHERE id=?", (terminal, dumps(result), data_as_of, now(), job_id))
            if valid:
                db.execute("UPDATE accounts SET analysis_state='current' WHERE id=?", (job["account_id"],))
            return valid

    def analyses(self, account_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM analyses WHERE account_id=? ORDER BY created_at DESC LIMIT 100", (account_id,)).fetchall()
        return [{**dict(r), "result": loads(r["result"])} for r in rows]

    def previous_analysis(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT a.* FROM analyses a JOIN jobs j ON j.id=a.job_id WHERE a.account_id=? AND j.state='completed' ORDER BY a.input_version DESC,a.created_at DESC LIMIT 1", (account_id,)).fetchone()
        return {**dict(row), "result": loads(row["result"])} if row else None

    def recover(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state='superseded' WHERE state IN ('running','queued') AND id NOT IN (SELECT latest_job_id FROM accounts WHERE latest_job_id IS NOT NULL)")
            db.execute("UPDATE jobs SET state='queued',due_at=?,error='服务恢复：按当前输入重新运行',updated_at=? WHERE state='running'", (now(), now()))
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
        return sorted(symbols)

    def save_material(self, material, job_id=None, account_id=None):
        # No source admission policy. Unknown fields and mixed material survive intact.
        body = dict(material)
        if "classification" not in body and "category" in body:
            body["classification"] = body["category"]
        if "content" not in body and "claim" in body:
            body["content"] = body["claim"]
        if "source_title" not in body and "title" in body:
            body["source_title"] = body["title"]
        body.setdefault("classification", "uncertain")
        body.setdefault("fetched_at", now())
        body.setdefault("published_at", None)
        identity = {k: body.get(k) for k in ("url", "content", "text", "excerpt", "classification", "event_period", "scope")}
        if body.get("scope") != "public":
            identity["account_id"] = account_id
        fingerprint = hashlib.sha256(dumps(identity).encode()).hexdigest()
        with self.connect() as db:
            old = db.execute("SELECT id,body FROM materials WHERE fingerprint=?", (fingerprint,)).fetchone()
            if old:
                body = loads(old["body"])
            else:
                body["id"] = uid()
                body["mode"] = self.mode
                db.execute("INSERT INTO materials VALUES(?,?,?,?)", (body["id"], fingerprint, dumps(body), now()))
            if job_id and account_id:
                db.execute("INSERT OR IGNORE INTO material_uses VALUES(?,?,?)", (body["id"], job_id, account_id))
        return body

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
