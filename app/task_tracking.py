"""Small durable task provenance and submission receipts on the existing queue."""
from __future__ import annotations

import hashlib
import json
import re

from .domain import now


def task_identity(reason):
    if reason.startswith('retry:'):
        return 'manual_retry', 'user_click'
    if 'manual_security:' in reason:
        return 'security_research', 'user_click'
    if reason == 'manual':
        return 'account_analysis', 'user_click'
    if reason == 'account_saved':
        return 'holdings_update', 'save_holdings'
    if 'close_summary' in reason:
        return 'close_summary', 'timer'
    if 'forecast' in reason:
        return 'forecast_batch', 'timer'
    if 'price' in reason:
        return 'account_analysis', 'price_event'
    if 'material' in reason or 'information' in reason:
        return 'material_review', 'material_event'
    if 'resume' in reason or 'restart' in reason:
        return 'account_analysis', 'recovery'
    if reason == 'universe_changed':
        return 'account_analysis', 'save_universe'
    return 'account_analysis', 'timer' if 'scheduled' in reason else 'unknown'


class TaskTrackingStore:
    def migrate_task_tracking(self, db):
        columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
        for key, declaration in {'task_metadata': 'TEXT', 'started_at': 'TEXT', 'attempt_started_at':'TEXT',
                                  'completed_at': 'TEXT', 'stage': 'TEXT',
                                  'recovery_attempts': 'INTEGER NOT NULL DEFAULT 0'}.items():
            if key not in columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {key} {declaration}')
        db.execute('''CREATE TABLE IF NOT EXISTS task_operations(
            account_id TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            job_id TEXT NOT NULL, merged INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(account_id,operation_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS account_operations(
            operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, account_id TEXT NOT NULL,
            job_id TEXT NOT NULL, input_version INTEGER NOT NULL, created_at TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS demands_job ON research_demands(job_id,id)')
        db.execute('CREATE INDEX IF NOT EXISTS forecast_batches_job ON forecast_batches(job_id)')

    def new_task_metadata(self, db, account, reason, origin=None, parent_job_id=None, research_scope=None):
        scope = self.universe_snapshot(db)
        universe = scope['universe']
        target = reason.split('manual_security:', 1)[1].split(';', 1)[0] if 'manual_security:' in reason else None
        symbols = list(dict.fromkeys([*[h['symbol'] for h in account['holdings']],
                                     *universe['candidates'], *([target] if target else [])]))
        kind, trigger = task_identity(reason)
        return {'task_kind': kind, 'trigger_source': trigger,
                'origin': origin or {'page': None, 'action': reason},
                'sources': [{'trigger_source': trigger, 'origin': origin or {'action': reason},
                             'parent_job_id': parent_job_id, 'at': now()}],
                'parent_job_id': parent_job_id, 'research_scope': research_scope or 'securities',
                'target_security': target, 'universe_version': scope['universe_version'],
                'universe': universe,
                'target': {'account_id': account['id'], 'account_name': account['name'],
                           'input_version': account['version'], 'holdings': account['holdings'],
                           'candidates': universe['candidates'], 'watchlist': universe['watchlist'], 'symbols': symbols}}

    def task_view(self, item, db=None):
        if db is None:
            with self.connect() as conn:
                return self.task_view(item, conn)
        from .store import loads
        metadata = loads(item.get('task_metadata'), {})
        if not metadata:
            # We can recover a holding snapshot, never a historical page or universe.
            row = db.execute('SELECT body FROM snapshots WHERE account_id=? AND version=?',
                             (item['account_id'], item['input_version'])).fetchone()
            snapshot = loads(row[0], {}) if row else {}
            metadata = {'task_kind': task_identity(item['reason'])[0], 'trigger_source': 'unknown',
                        'origin': {'page': None, 'action': '历史来源未知'}, 'sources': [],
                        'target': {'account_id': item['account_id'], 'account_name': snapshot.get('name'),
                                   'input_version': item['input_version'], 'holdings': snapshot.get('holdings'),
                                   'candidates': None, 'symbols': [h['symbol'] for h in snapshot.get('holdings', [])],
                                   'scope_unknown': True}}
            if 'manual_security:' in item['reason']:
                metadata['target_security'] = item['reason'].split('manual_security:', 1)[1].split(';', 1)[0]
                metadata['target']['symbols'] = list(dict.fromkeys([*metadata['target']['symbols'], metadata['target_security']]))
        item.update(metadata)
        report = db.execute("SELECT id,status,substr(report,1,500) AS report, json_extract(result,'$.research_delivery') AS research_delivery,json_extract(result,'$.reference_summary') AS reference_summary FROM analyses WHERE job_id=?", (item['id'],)).fetchone()
        item.update(report_id=report['id'] if report and report['report'].strip() else None,
                    report_status=report['status'] if report and report['report'].strip() else 'not_published',
                    report_summary=report['report'][:500] if report else None,
                    scheduled_at=metadata.get('planned_at'), current_due_at=item['due_at'], trading_day=None)
        item['research_delivery'] = loads(report['research_delivery']) if report else None
        item['reference_summary'] = loads(report['reference_summary']) if report else None
        demands = [dict(r) for r in db.execute('SELECT id,kind,reason,event_key,created_at,state FROM research_demands WHERE job_id=? ORDER BY id', (item['id'],))]
        item['demands'] = demands
        for demand in demands:
            match = re.search(r'(\d{4}-\d{2}-\d{2})$', demand['event_key'])
            if demand['kind'] == 'close_summary' and match:
                demand['trading_day'] = match[1]
                item['trading_day'] = match[1]
        item['forecast_batch_ids'] = [r[0] for r in db.execute('SELECT id FROM forecast_batches WHERE job_id=?', (item['id'],))]
        item['stage'] = item.get('stage') or {'queued': 'waiting', 'waiting_config': 'waiting_config',
            'waiting_scope': 'waiting_scope', 'completed': 'published', 'failed': 'failed'}.get(item['state'], item['state'])
        item['failure_stage'] = item['stage'] if item['state'] == 'failed' else None
        item['is_current_input'] = bool(db.execute('SELECT 1 FROM accounts WHERE id=? AND version=?', (item['account_id'], item['input_version'])).fetchone())
        item.pop('task_metadata', None)
        return item

    def submit_task(self, account_id, *, operation_id=None, target=None, origin=None, parent_job_id=None, research_scope=None):
        from .store import Conflict, dumps, loads
        reason = f'manual_security:{target}' if target else 'manual'
        if parent_job_id:
            reason = 'retry:' + reason
        fingerprint = hashlib.sha256(dumps({'target': target, 'origin': origin, 'parent': parent_job_id,
                                            'research_scope': research_scope}).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            account = self.account(account_id, db)
            if operation_id:
                previous = db.execute('SELECT * FROM task_operations WHERE account_id=? AND operation_id=?', (account_id, operation_id)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise Conflict('此操作ID已用于其他输入，请核对原任务')
                    return self._operation_job(previous, db)
            if parent_job_id and not db.execute('SELECT 1 FROM jobs WHERE id=? AND account_id=?', (parent_job_id, account_id)).fetchone():
                raise KeyError('原任务不属于当前账户')
            metadata = self.new_task_metadata(db, account, reason, origin, parent_job_id, research_scope)
            if research_scope == 'cash_only' and metadata['target']['symbols']:
                raise ValueError('账户已有持仓、候选或明确目标，请提交证券研究；现金与偏好入口仅用于空证券范围')
            previous = db.execute('SELECT * FROM jobs WHERE id=?', (account['latest_job_id'],)).fetchone()
            old = loads(previous['task_metadata'], {}) if previous else {}
            same = (previous and previous['state'] in ('queued', 'running', 'waiting_config') and
                    previous['input_version'] == account['version'] and old.get('target') == metadata['target'] and
                    old.get('target_security') == target and old.get('research_scope') == metadata['research_scope'] and
                    old.get('universe_version') == metadata['universe_version'])
            if same:
                job_id, merged = previous['id'], True
                old['sources'] = [*old.get('sources', []), *metadata['sources']]
                db.execute('UPDATE jobs SET task_metadata=? WHERE id=?', (dumps(old), job_id))
            else:
                job = self._enqueue(db, account, reason)
                job_id, merged = job['id'], False
                metadata['planned_at'] = job['due_at']
                db.execute('UPDATE jobs SET task_metadata=? WHERE id=?', (dumps(metadata), job_id))
                if not target:
                    db.execute("UPDATE research_demands SET state='assigned',job_id=?,detail='explicit retry on current input',updated_at=? WHERE account_id=? AND state IN ('uncovered','failed')", (job_id, now(), account_id))
            if not metadata['target']['symbols'] and research_scope != 'cash_only':
                db.execute("UPDATE jobs SET state='waiting_scope',stage='waiting_scope' WHERE id=?", (job_id,))
                db.execute("UPDATE accounts SET analysis_state='waiting_scope' WHERE id=?", (account_id,))
            if operation_id:
                db.execute('INSERT INTO task_operations VALUES(?,?,?,?,?,?)', (account_id, operation_id, fingerprint, job_id, int(merged), now()))
            return self._operation_job({'job_id': job_id, 'merged': merged, 'operation_id': operation_id}, db)

    def _operation_job(self, receipt, db):
        from .store import loads
        row = db.execute('SELECT * FROM jobs WHERE id=?', (receipt['job_id'],)).fetchone()
        if not row:
            raise KeyError('任务不存在')
        item = dict(row)
        item['result'] = loads(item['result'])
        item['config_snapshot'] = loads(item['config_snapshot'])
        return {**self.task_view(item, db), 'merged': bool(receipt['merged']), 'operation_id': receipt['operation_id']}

    def task_operation(self, account_id, operation_id):
        with self.connect() as db:
            self.account(account_id, db)
            row = db.execute('SELECT * FROM task_operations WHERE account_id=? AND operation_id=?', (account_id, operation_id)).fetchone()
            if not row:
                raise KeyError('未找到操作记录；可继续只读核对，不要盲目重复提交')
            return self._operation_job(row, db)

    def task_stage(self, job_id, stage):
        with self.connect() as db:
            db.execute("UPDATE jobs SET stage=? WHERE id=? AND state='running'", (stage, job_id))

    def save_account_operation(self, payload, account_id=None, operation_id=None):
        """Account write and receipt share the snapshot/queue transaction."""
        from .store import Conflict, dumps
        data = payload.model_dump() if hasattr(payload, 'model_dump') else payload
        fingerprint = hashlib.sha256(dumps({'account_id': account_id, 'data': data}).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM account_operations WHERE operation_id=?', (operation_id,)).fetchone() if operation_id else None
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('保存操作ID已对应其他输入，请核对原保存结果')
                return {**self.account(old['account_id'], db), 'save_receipt': {
                    'operation_id':operation_id,'account_id':old['account_id'],
                    'job_id':old['job_id'],'input_version':old['input_version'],'replayed':True}}
            account = self.save_account(payload, account_id, db=db)
            job = db.execute('SELECT * FROM jobs WHERE id=?', (account['latest_job_id'],)).fetchone()
            metadata = json.loads(job['task_metadata'])
            if not metadata['target']['symbols'] and metadata['research_scope'] != 'cash_only':
                db.execute("UPDATE jobs SET state='waiting_scope',stage='waiting_scope' WHERE id=?", (job['id'],))
                db.execute("UPDATE accounts SET analysis_state='waiting_scope' WHERE id=?", (account['id'],))
            if operation_id:
                db.execute('INSERT INTO account_operations VALUES(?,?,?,?,?,?)',
                    (operation_id, fingerprint, account['id'], job['id'], account['version'], now()))
            return {**self.account(account['id'], db), 'save_receipt': {
                'operation_id':operation_id,'account_id':account['id'],
                'job_id':job['id'],'input_version':account['version'],'replayed':False}}

    def account_operation(self, operation_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM account_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise KeyError('未找到保存操作记录；请继续核对，勿重复提交')
            result = {k: row[k] for k in ('operation_id', 'account_id', 'job_id', 'input_version', 'created_at')}
        return {**result, 'account': self.account(row['account_id']), 'job': self.job(row['job_id'])}
