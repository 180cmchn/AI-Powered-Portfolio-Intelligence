"""Durable visible research checkpoints, never opaque model continuation state."""
from datetime import datetime
import hashlib
import json

from .domain import TZ, now


class RecoveryStore:
    def checkpoint_material_guard(self, material_ids, account_id):
        result = {}
        with self.connect() as db:
            for mid in material_ids:
                if not isinstance(mid, str) or not self.material_accessible(mid, account_id) or not db.execute('SELECT 1 FROM materials WHERE id=?', (mid,)).fetchone():
                    continue  # Unknown references remain explicit gaps in the body.
                row = db.execute('''SELECT d.stream_key FROM material_representations r
                    JOIN material_document_versions d ON d.id=r.document_version_id WHERE r.material_id=?''', (mid,)).fetchone()
                latest = db.execute('''SELECT id FROM material_document_versions
                    WHERE stream_key=? AND version>0 ORDER BY version DESC LIMIT 1''', (row[0],)).fetchone() if row else None
                result[mid] = latest[0] if latest else None
        return result

    def research_guard(self, context):
        with self.connect() as db:
            events = [tuple(r) for r in db.execute(
                "SELECT material_id,created_at FROM material_notifications ORDER BY material_id")]
            demands = [tuple(r) for r in db.execute(
                "SELECT event_key FROM research_demands WHERE account_id=? ORDER BY event_key",
                ((context.get('account') or {}).get('id', ''),))]
        # Current valuations can be used in a recovered synthesis only when the
        # same source snapshot is still available; fetch timestamps are immaterial.
        values = context.get('metrics') or {}
        quotes = {p['symbol']: {k: (p.get('quote') or {}).get(k) for k in ('price', 'as_of', 'source', 'status')}
                  for p in values.get('positions', [])}
        for key in ('quotes', 'candidate_quotes'):
            for security_id, quote in (context.get(key) or {}).items():
                quotes[security_id] = {k: quote.get(k) for k in ('price', 'as_of', 'source', 'status')}
        target = context.get('target_security') or {}
        if target.get('symbol'):
            quotes[target['symbol']] = {k: (target.get('quote') or {}).get(k) for k in ('price', 'as_of', 'source', 'status')}
        raw = {'events': events, 'demands': demands, 'quotes': quotes,
               'target': (context.get('target_security') or {}).get('symbol')}
        return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()

    def save_synthesis_checkpoint(self, job, body, context):
        from .store import dumps
        body = {**body, 'verified_material_versions': self.checkpoint_material_guard(
            body.get('material_ids', []), job.get('account_id'))}
        with self.connect() as db:
            db.execute('''INSERT INTO synthesis_checkpoints
                (job_id,account_id,input_version,universe_version,body,guard,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                body=excluded.body,guard=excluded.guard,created_at=excluded.created_at''',
                (job['id'], job.get('account_id') or '', job.get('input_version'),
                 context['universe_version'], dumps(body), self.research_guard(context), now()))

    def reusable_synthesis_checkpoint(self, job, context):
        from .store import loads
        guard = self.research_guard(context)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT c.* FROM synthesis_checkpoints c
                WHERE account_id=? AND input_version IS ? AND universe_version=?
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=c.job_id AND j.state='completed')
                AND NOT EXISTS(SELECT 1 FROM global_runs g WHERE g.id=c.job_id AND g.state='completed')
                ORDER BY created_at DESC LIMIT 10''',
                (job.get('account_id') or '', job.get('input_version'), context['universe_version'])).fetchall()
            for row in rows:
                age = (datetime.now(TZ) - datetime.fromisoformat(row['created_at'])).total_seconds()
                if row['guard'] != guard or not 0 <= age <= self.settings()['synthesis_checkpoint_max_age_seconds']:
                    continue
                if row['resume_count'] >= 2:
                    continue
                body = loads(row['body'])
                verified = body.get('verified_material_versions', {})
                if self.checkpoint_material_guard(list(verified), job.get('account_id')) != verified:
                    continue
                db.execute('UPDATE synthesis_checkpoints SET resume_count=resume_count+1 WHERE job_id=?', (row['job_id'],))
                return {**body, 'checkpoint_job_id': row['job_id'],
                        'checkpoint_created_at': row['created_at'], 'resume_attempt': row['resume_count'] + 1}
        return None

    def checkpoint_status(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT created_at,resume_count FROM synthesis_checkpoints WHERE job_id=?', (job_id,)).fetchone()
        return {'available': bool(row), **(dict(row) if row else {}),
                'note': '重试前检查账户版本、公共范围、行情、事件与时效；不满足时重新研究'}
