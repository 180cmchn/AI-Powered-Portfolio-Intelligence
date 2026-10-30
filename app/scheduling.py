"""Durable business demands, separate from replaceable account execution jobs."""
from __future__ import annotations

from .domain import now
import re


def information_impact(value, material_id):
    """Malformed relevance output is uncertainty, never an instruction or a lost event."""
    fallback = {'material_id': material_id, 'status': 'uncertain', 'symbols': [], 'sectors': [],
                'market_wide': False, 'explanation': '关联判断缺失或格式不完整，保留待查，不自动广播'}
    if not isinstance(value, dict) or value.get('status') not in ('relevant', 'irrelevant', 'uncertain'):
        return fallback
    if not isinstance(value.get('explanation'), str) or not value['explanation'].strip():
        return fallback
    symbols, sectors = value.get('symbols', []), value.get('sectors', [])
    if not isinstance(symbols, list) or not all(isinstance(s, str) and re.fullmatch(r'(SH|SZ|BJ):[0-9]{6}', s) for s in symbols):
        return fallback
    if not isinstance(sectors, list) or not all(isinstance(s, str) and s.strip() for s in sectors):
        return fallback
    if not isinstance(value.get('market_wide', False), bool):
        return fallback
    return {**fallback, 'status': value['status'], 'symbols': sorted(set(symbols)), 'sectors': sorted(set(sectors)),
            'market_wide': value.get('market_wide', False), 'explanation': value['explanation'],
            'event_key': value.get('event_key')[:300] if isinstance(value.get('event_key'), str) else None}


SCHEMA = """
CREATE TABLE IF NOT EXISTS research_demands(
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
 account_id TEXT NOT NULL, input_version INTEGER, kind TEXT NOT NULL, reason TEXT NOT NULL,
 state TEXT NOT NULL, job_id TEXT, detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS demands_pending ON research_demands(state,account_id,id);
CREATE TABLE IF NOT EXISTS research_demand_scopes(
 job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, max_demand_id INTEGER NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_demand_reads(
 job_id TEXT NOT NULL, demand_id INTEGER NOT NULL, PRIMARY KEY(job_id,demand_id));
CREATE TABLE IF NOT EXISTS research_demand_scope_items(
 job_id TEXT NOT NULL, demand_id INTEGER NOT NULL, PRIMARY KEY(job_id,demand_id));
CREATE TABLE IF NOT EXISTS global_runs(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
 result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


class SchedulingStore:
    def migrate_research_versions(self, db):
        # Historical scope was never saved: NULL means unknown, not current v1.
        additions = {
            'global_runs': {'universe_version': 'INTEGER', 'input_snapshot': 'TEXT',
                            'applicability': "TEXT NOT NULL DEFAULT 'legacy_unknown'"},
            'research_records': {'universe_version': 'INTEGER', 'logical_identity': 'TEXT',
                                 'applicability': "TEXT NOT NULL DEFAULT 'legacy_unknown'"},
        }
        for table, fields in additions.items():
            existing = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
            for field, declaration in fields.items():
                if field not in existing:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {field} {declaration}')
        db.execute("INSERT OR IGNORE INTO meta VALUES('universe_version','1')")

    def universe_snapshot(self, db=None):
        from .store import loads
        if db is None:
            with self.connect() as conn:
                return self.universe_snapshot(conn)
        rows = {r['key']: loads(r['value']) for r in db.execute("SELECT * FROM meta WHERE key IN ('universe','universe_version')")}
        return {'universe': rows.get('universe', {'watchlist': [], 'candidates': []}),
                'universe_version': rows.get('universe_version', 1)}

    def save_universe(self, universe):
        from .store import dumps
        universe = universe.model_dump() if hasattr(universe, 'model_dump') else universe
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = self.universe_snapshot(db)
            if prior['universe'] == universe:
                return prior
            version, stamp = prior['universe_version'] + 1, now()
            for key, value in [('universe', universe), ('universe_version', version)]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, dumps(value)))
            db.execute("UPDATE research_records SET applicability='stale' WHERE account_id='' AND applicability='current'")
            db.execute("UPDATE global_runs SET applicability='stale' WHERE applicability='current'")
            # Old waiting work cannot absorb the new scope's demands indefinitely.
            db.execute("UPDATE global_runs SET state='superseded' WHERE state='waiting_config'")
            db.execute("UPDATE research_demands SET state='pending',job_id=NULL WHERE account_id='' AND state='assigned' AND job_id IN (SELECT id FROM global_runs WHERE state='superseded')")
            for row in db.execute('SELECT id FROM accounts').fetchall():
                self.save_account(self.account(row['id'], db), row['id'], 'universe_changed', db)
            db.execute("""INSERT OR IGNORE INTO research_demands
                (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
                VALUES(?,'',?,'universe_changed',?,'pending',?,?)""",
                (f'universe:{version}', version, f'universe_changed:v{version}', stamp, stamp))
            return self.universe_snapshot(db)

    def request_analysis(self, account_id, kind, event_key=None, reason=None, delay=0):
        """Never interrupts running work. Only unstarted work can absorb new demands."""
        from .store import uid, dumps
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self.account(account_id, db) if account_id else None
            stamp = now()
            key = event_key or f"{kind}:{account_id}:{uid()}"
            db.execute("""INSERT OR IGNORE INTO research_demands
                (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
                VALUES(?,?,?,?,?,'pending',?,?)""",
                (key, account_id or "", account["version"] if account else None, kind, reason or kind, stamp, stamp))
            if account:
                self._dispatch_account(db, account, delay)
            return dict(db.execute("SELECT * FROM research_demands WHERE event_key=?", (key,)).fetchone())

    def _dispatch_account(self, db, account, delay=0):
        rows = db.execute("SELECT * FROM research_demands WHERE account_id=? AND state='pending' ORDER BY id", (account["id"],)).fetchall()
        if not rows:
            return
        job = db.execute("SELECT * FROM jobs WHERE id=?", (account["latest_job_id"],)).fetchone()
        if job and job["state"] == "running":
            db.execute("UPDATE research_demands SET detail='deferred: current research running' WHERE account_id=? AND state='pending'", (account["id"],))
            return
        reasons = list(dict.fromkeys(r["reason"] for r in rows))
        # The authoritative reasons remain in demands; job labels are bounded.
        label = "; ".join(reasons)[:1500]
        if job and job["state"] in ("queued", "waiting_config") and job["input_version"] == account["version"]:
            job_id = job["id"]
            if label not in job["reason"]:
                combined = label if job["reason"] == "account_saved" else job["reason"] + "; " + label
                db.execute("UPDATE jobs SET reason=?,updated_at=? WHERE id=?", (combined[:2000], now(), job_id))
            detail = "merged before execution" if job["state"] == "queued" else "merged: waiting for model configuration"
        else:
            job_id = self._enqueue(db, account, label, delay=delay)["id"]
            detail = "scheduled after current research"
        db.execute("""UPDATE research_demands SET state='assigned',job_id=?,input_version=?,detail=?,updated_at=?
                    WHERE account_id=? AND state='pending'""", (job_id, account["version"], detail, now(), account["id"]))

    def dispatch_demands(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in db.execute("SELECT account_id FROM research_demands WHERE state='pending' AND account_id<>'' GROUP BY account_id ORDER BY min(id)")]
            for account_id in ids:
                self._dispatch_account(db, self.account(account_id, db))

    def demands(self, account_id=None, limit=100, offset=0, job_id=None):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM research_demands WHERE (? IS NULL OR account_id=?) AND (? IS NULL OR job_id=?) ORDER BY id DESC LIMIT ? OFFSET ?",
                              (account_id, account_id, job_id, job_id, min(max(limit, 1), 1000), max(offset, 0))).fetchall()
        return [dict(r) for r in rows]

    def task_demands(self, job_id, account_id, limit=25, offset=0):
        """A stable, task-scoped page. Concurrent account demand arrivals cannot hide it."""
        limit, offset = min(max(int(limit), 1), 50), max(int(offset), 0)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT account_id FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not owner or owner[0] != account_id:
                raise KeyError('研究需求不属于当前账户任务')
            frozen = db.execute('SELECT 1 FROM research_demand_scopes WHERE job_id=?', (job_id,)).fetchone()
            db.execute('''INSERT OR IGNORE INTO research_demand_scopes
                SELECT ?,?,coalesce(max(id),0),? FROM research_demands WHERE job_id=? AND account_id=?''',
                (job_id, account_id, now(), job_id, account_id))
            if not frozen:
                db.execute('''INSERT OR IGNORE INTO research_demand_scope_items
                    SELECT ?,id FROM research_demands WHERE job_id=? AND account_id=?''', (job_id, job_id, account_id))
            maximum = db.execute('SELECT max_demand_id FROM research_demand_scopes WHERE job_id=?', (job_id,)).fetchone()[0]
            args = (job_id, account_id)
            total = db.execute('''SELECT count(*) FROM research_demands d JOIN research_demand_scope_items s ON s.demand_id=d.id
                WHERE s.job_id=? AND d.account_id=?''', args).fetchone()[0]
            rows = [dict(row) for row in db.execute('''SELECT d.* FROM research_demands d
                JOIN research_demand_scope_items s ON s.demand_id=d.id
                WHERE s.job_id=? AND d.account_id=? ORDER BY d.id LIMIT ? OFFSET ?''', (*args, limit, offset))]
            db.executemany('INSERT OR IGNORE INTO research_demand_reads VALUES(?,?)', [(job_id, row['id']) for row in rows])
        return {'items': rows, 'total': total, 'offset': offset, 'limit': limit,
                'has_more': offset + len(rows) < total,
                'next_offset': offset + len(rows) if offset + len(rows) < total else None,
                'scope_max_demand_id': maximum,
                'coverage_policy': 'Only explicitly reported covered_demand_ids are completed; use list_business_demands for all pages.'}

    def complete_task_demands(self, db, job_id, result):
        """Publishing a report does not prove the entire assigned backlog was researched."""
        claimed = result.get('covered_demand_ids', [])
        claimed = {value for value in claimed if type(value) is int} if isinstance(claimed, list) else set()
        readable = {row[0] for row in db.execute('SELECT demand_id FROM research_demand_reads WHERE job_id=?', (job_id,))}
        allowed = claimed & readable
        for demand_id in allowed:
            db.execute("""UPDATE research_demands SET state='completed',detail='explicitly covered in published report',updated_at=?
                WHERE id=? AND job_id=? AND state='assigned'""", (now(), demand_id, job_id))
        db.execute("""UPDATE research_demands SET state='uncovered',
            detail='报告未明确覆盖此项；保留待处理，手动重试时重新安排',updated_at=?
            WHERE job_id=? AND state='assigned'""", (now(), job_id))
        rows = db.execute('''SELECT id,state FROM research_demands WHERE job_id=? OR id IN
            (SELECT demand_id FROM research_demand_scope_items WHERE job_id=?) ORDER BY id''', (job_id, job_id)).fetchall()
        return {'expected': len(rows), 'completed': sum(row['state'] == 'completed' for row in rows),
                'uncovered': sum(row['state'] == 'uncovered' for row in rows),
                'unaccepted_claims': sorted(claimed - allowed),
                'items': [{'id': row['id'], 'state': row['state']} for row in rows]}

    def bind_public_information(self, run_id, material_ids):
        """Own discovered inputs in this run without creating another execution.

        The outbox stays pending until a completed relevance plan is routed.
        Failed work may be picked up by a later explicit/new-event run, while
        ordinary ticks cannot turn the run's own discoveries into a retry loop.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT universe_version FROM global_runs WHERE id=? AND state='running'", (run_id,)).fetchone()
            if not run or run['universe_version'] != self.universe_snapshot(db)['universe_version']:
                return
            stamp = now()
            for mid in dict.fromkeys(material_ids):
                pending = db.execute("""SELECT 1 FROM material_notifications n
                    LEFT JOIN information_routes r ON r.material_id=n.material_id
                    WHERE n.material_id=? AND n.state='pending' AND
                    (r.state IS NULL OR r.state NOT IN ('ready','routed'))""", (mid,)).fetchone()
                if not pending:
                    continue
                db.execute("""INSERT INTO information_routes VALUES(?,?,'researching',NULL,?,?)
                    ON CONFLICT(material_id) DO UPDATE SET public_run_id=excluded.public_run_id,
                    state='researching',updated_at=excluded.updated_at""", (mid, run_id, stamp, stamp))
                db.execute("""INSERT INTO research_demands
                    (event_key,account_id,input_version,kind,reason,state,job_id,detail,created_at,updated_at)
                    VALUES(?,'',NULL,'public_information',?,'assigned',?,'included in current public research',?,?)
                    ON CONFLICT(event_key) DO UPDATE SET state='assigned',job_id=excluded.job_id,
                    detail=excluded.detail,updated_at=excluded.updated_at""",
                    ('public_information:' + mid, '待判断关联的公开材料：' + mid, run_id, stamp, stamp))

    def public_information_for_run(self, run_id, limit=100):
        # A backlog from a failed run must not hide the new external event that
        # caused this execution behind the first page of old notifications.
        with self.connect() as db:
            rows = db.execute("""SELECT n.material_id FROM material_notifications n
                LEFT JOIN research_demands d ON d.event_key='public_information:' || n.material_id
                LEFT JOIN information_routes r ON r.material_id=n.material_id
                WHERE n.state='pending' AND (r.state IS NULL OR r.state NOT IN ('ready','routed'))
                ORDER BY CASE WHEN d.job_id=? THEN 0 ELSE 1 END,n.created_at,n.material_id LIMIT ?""",
                (run_id, limit)).fetchall()
        return [row[0] for row in rows]

    def claim_global(self):
        from .store import uid, dumps
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = {**self.universe_snapshot(db), 'config': self.task_config_snapshot(db)}
            version = snapshot['universe_version']
            if db.execute("SELECT 1 FROM global_runs WHERE state IN ('running','waiting_config') AND universe_version=?", (version,)).fetchone():
                # A missing configuration must not create a new failed job every scan.
                waiting = db.execute("SELECT id FROM global_runs WHERE state='waiting_config' AND universe_version=? ORDER BY created_at LIMIT 1", (version,)).fetchone()
                if waiting:
                    db.execute("UPDATE research_demands SET state='assigned',job_id=?,detail='merged: waiting for configuration' WHERE account_id='' AND state='pending'", (waiting[0],))
                return None
            pending = db.execute("SELECT * FROM research_demands WHERE account_id='' AND state='pending' ORDER BY id").fetchall()
            if not pending:
                return None
            run_id, stamp = uid(), now()
            reasons = "; ".join(dict.fromkeys(r["reason"] for r in pending))[:2000]
            prior_attempts = [db.execute("SELECT attempts FROM global_runs WHERE id=? AND universe_version=?", (r["job_id"], version)).fetchone() for r in pending if r["job_id"]]
            attempts = 1 + max((r[0] for r in prior_attempts if r), default=0)
            exhausted = attempts > self.settings()["max_job_attempts"]
            db.execute("""INSERT INTO global_runs
                (id,kind,state,reason,result,error,attempts,created_at,updated_at,universe_version,input_snapshot,applicability)
                VALUES(?,?,?,?,NULL,?,?,?,?,?,?,'current')""", (run_id, "candidate_scan", "failed" if exhausted else "running", reasons,
                       "中断后重试次数已达上限；等待新的研究窗口" if exhausted else None, attempts, stamp, stamp, version, dumps(snapshot)))
            db.execute("UPDATE research_demands SET state='assigned',job_id=?,detail='shared global research; coalesced pending windows',updated_at=? WHERE account_id='' AND state='pending'", (run_id, stamp))
            if exhausted:
                db.execute("UPDATE research_demands SET state='failed',detail='interrupted global retry limit reached' WHERE job_id=?", (run_id,))
                return None
            return self._global_body(db.execute("SELECT * FROM global_runs WHERE id=?", (run_id,)).fetchone())

    def finish_global(self, run_id, state, result=None, error=None):
        from .store import dumps
        prior = self.global_run(run_id)
        if prior['state'] not in ('running', 'waiting_config'):
            return False
        if result:
            result = self.resolve_result_references(result)
            result.update(universe_version=prior['universe_version'], input_snapshot=prior['input_snapshot'])
        if state == "completed" and result and result.get("report") and not result.get("demo"):
            report = self.save_material({"content": result["report"], "scope": "public", "classification": "agent_analysis",
                                        "access": "generated_report", "source_title": "公共研究主 Agent 综合报告",
                                        "material_ids": result.get("material_ids", []), "global_run_id": run_id})
            result = {**result, "report_material_id": report["id"]}
            if not result.get("research_records"):
                result["research_records"] = [{"kind": "market", "title": "公共市场研究", "reason": "公共主 Agent 研究报告",
                   "findings": [{"classification": "agent_analysis", "summary": result["report"][:4000], "material_id": report["id"]}],
                   "material_ids": [report["id"], *(result.get("material_ids") or [])], "status": "reviewed"}]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute('SELECT * FROM global_runs WHERE id=?', (run_id,)).fetchone()
            if run['state'] not in ('running', 'waiting_config'):
                return False
            applicable = run['universe_version'] == self.universe_snapshot(db)['universe_version']
            applicability = 'current' if applicable else 'stale'
            terminal = state if applicable or state != 'completed' else 'superseded'
            if result:
                result['applicability'] = applicability
            db.execute("UPDATE global_runs SET state=?,result=?,error=?,updated_at=?,applicability=? WHERE id=?", (terminal, dumps(result) if result else None, error, now(), applicability, run_id))
            if state == "completed" and result:
                self.save_research_records(result.get("research_records", []), run_id, db=db)
                if applicable:
                    db.execute("UPDATE synthesis_checkpoints SET resume_count=2 WHERE job_id=? AND account_id='' AND universe_version=?",
                               (result.get('synthesis_checkpoint_job_id'), run['universe_version']))
                    impacts = {d.get('material_id'): d for d in (result.get('information_impacts') or [])
                               if isinstance(d, dict) and isinstance(d.get('material_id'), str)} if isinstance(result.get('information_impacts', []), list) else {}
                    for mid in result.get('_information_material_ids', []):
                        decision = information_impact(impacts.get(mid), mid)
                        # Commit the relevance plan with the final public report.
                        # A crash during fanout resumes this plan without another model call.
                        db.execute("""INSERT INTO information_routes VALUES(?,?,'ready',?,?,?)
                            ON CONFLICT(material_id) DO UPDATE SET public_run_id=excluded.public_run_id,
                            state='ready',decision=excluded.decision,updated_at=excluded.updated_at
                            WHERE information_routes.state<>'routed'""", (mid, run_id, dumps(decision), now(), now()))
            if state != "waiting_config":
                db.execute("UPDATE research_demands SET state=?,detail=?,updated_at=? WHERE job_id=? AND state='assigned'",
                           ("completed" if state == "completed" else "failed", error or "global research completed", now(), run_id))
                db.execute("""UPDATE information_routes SET state='awaiting_retry',updated_at=?
                    WHERE public_run_id=? AND state='researching'""", (now(), run_id))
            return applicable

    @staticmethod
    def _global_body(row):
        from .store import loads
        return {**dict(row), 'result': loads(row['result']), 'input_snapshot': loads(row['input_snapshot'])}

    def global_runs(self, limit=20, summary=False):
        from .store import loads
        with self.connect() as db:
            rows = db.execute("SELECT * FROM global_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            values = [self._global_body(r) for r in rows]
            if summary:
                return [{k: v for k, v in value.items() if k != 'result'} for value in values]
            return [{**value, 'result': self.project_report_references(value['result'], db=db)} for value in values]

    def global_run(self, run_id):
        from .store import loads
        with self.connect() as db:
            row = db.execute("SELECT * FROM global_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("公共研究不存在")
            value = self._global_body(row)
            value['result'] = self.project_report_references(value['result'], db=db)
            return value

    def recover_demands(self, db, config_ready=False):
        db.execute("UPDATE research_demands SET state='pending',detail='recovered interrupted global work' WHERE job_id IN (SELECT id FROM global_runs WHERE state='running')")
        db.execute("UPDATE global_runs SET state='interrupted',updated_at=? WHERE state='running'", (now(),))
        if config_ready:
            self.resume_configuration_waiters(db)
