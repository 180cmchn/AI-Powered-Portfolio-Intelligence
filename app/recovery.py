"""Durable visible research checkpoints, never opaque model continuation state."""
from datetime import datetime
import hashlib
import json

from .domain import TZ, now


class RecoveryStore:
    def _checkpoint_config(self, job, context):
        # Global tasks already retain their full configuration in input_snapshot.
        return (job.get('config_snapshot') or (job.get('input_snapshot') or {}).get('config')
                or (context.get('input_snapshot') or {}).get('config') or self.settings())

    def checkpoint_material_guard(self, material_ids, account_id):
        """Bind actual immutable reads (and their parents), never label the head as read."""
        from .materials import parent_material_ids
        from .store import loads
        result = {}
        with self.connect() as db:
            pending = list(material_ids)
            while pending:
                mid = pending.pop()
                if mid in result:
                    continue
                if not isinstance(mid, str) or not self.material_accessible(mid, account_id) or not db.execute('SELECT 1 FROM materials WHERE id=?', (mid,)).fetchone():
                    continue  # Unknown references remain explicit gaps in the body.
                row = db.execute('''SELECT d.*,r.representation_hash,r.access_metadata FROM material_representations r
                    JOIN material_document_versions d ON d.id=r.document_version_id WHERE r.material_id=?''', (mid,)).fetchone()
                if not row:
                    continue
                head = db.execute('SELECT document_version_id FROM material_stream_heads WHERE stream_key=?', (row['stream_key'],)).fetchone()
                latest = head or db.execute('''SELECT id FROM material_document_versions
                    WHERE stream_key=? AND version>0 ORDER BY version DESC LIMIT 1''', (row['stream_key'],)).fetchone()
                body = loads(db.execute('SELECT body FROM materials WHERE id=?', (mid,)).fetchone()[0])
                parents = parent_material_ids(body)
                result[mid] = {'source_stream': row['stream_key'], 'source_key': row['source_key'],
                    'read_document_version_id': row['id'], 'read_document_version': row['version'],
                    'representation_hash': row['representation_hash'], 'read_range': loads(row['access_metadata']),
                    'representation_kind': row['representation_kind'], 'parent_material_ids': parents,
                    'latest_available_document_version_id': latest[0] if latest else None}
                pending.extend(parent for parent in parents if parent not in result)
        return result

    def record_material_delivery(self, job_id, account_id, agent_id, tool, result, *, authored=None):
        """Record the bounded result actually put into this actor's conversation.

        An index, model-authored ID, or context archive pointer is not a read.
        A native search citation can prove only its explicitly visible excerpt/link.
        """
        from .store import dumps
        if not isinstance(result, dict) or result.get('failed') or result.get('truncated_for_context'):
            return []
        if tool not in {'read', 'get_material', 'read_material_range', 'search_pdf', 'history',
                        'sectors', 'sector_members', 'sector_history', 'benchmark_history', 'fundamentals',
                        'native_search', 'save_material'}:
            return []
        mid = result.get('material_id') or result.get('id')
        if not mid or not self.material_accessible(mid, account_id):
            return []
        material = self.material(mid)
        guard = self.checkpoint_material_guard([mid], account_id).get(mid)
        if not guard:
            return []
        text = result.get('text') or result.get('content') or result.get('visible_excerpt')
        from .materials import material_text
        authored_content = (tool == 'save_material' and material.get('representation_kind') == 'agent_extraction'
                            and isinstance(authored, dict) and bool(material_text(authored))
                            and material_text(authored) == material_text(material))
        metadata_only = tool == 'native_search' and material.get('access') == 'source_link'
        structured = any(key in result for key in ('items', 'rows', 'matches', 'data'))
        if not text and not structured and not metadata_only and not authored_content:
            return []
        proof = {**guard, 'task_id': job_id, 'agent_id': agent_id,
            'delivered_content': bool(text or structured), 'metadata_only': metadata_only, 'authored_content': authored_content,
            'delivered_range': {key: result[key] for key in ('start', 'end', 'total_chars', 'returned_range',
                'page_range', 'pages_read', 'truncated', 'source_truncated') if key in result},
            'visible_result_hash': hashlib.sha256(dumps(result).encode()).hexdigest()}
        # The source's available range and this delivery's range are separate.
        with self.connect() as db:
            row = db.execute('INSERT INTO research_material_deliveries(job_id,account_id,agent_id,material_id,tool,body,delivered_at) VALUES(?,?,?,?,?,?,?)',
                (job_id, account_id or '', agent_id, mid, tool, dumps(proof), now()))
            return [row.lastrowid]

    def checkpoint_deliveries(self, job_id):
        from .store import loads
        with self.connect() as db:
            return [{**dict(row), 'body': loads(row['body'])} for row in db.execute(
                'SELECT * FROM research_material_deliveries WHERE job_id=? ORDER BY id', (job_id,))]

    def checkpoint_source_coverage(self, body, verified, deliveries):
        """Describe visible treatment of corrections, without judging research truth."""
        receipts = {item['id']: item for item in deliveries}
        reports = body.get('reports') or []
        # Keep old rounds for provenance; only the latest visible round is operative.
        latest_reports = {}
        for index, report in enumerate(reports):
            latest_reports[report.get('agent_id') or 'legacy-' + str(index)] = report
        manager = body.get('manager_summary') or {}
        documents = list(latest_reports.items()) + [('manager', manager)]
        streams = {}
        for mid, value in verified.items():
            streams.setdefault(value['source_stream'], []).append((mid, value))
        coverage = {}
        for stream, entries in streams.items():
            head = entries[0][1]['latest_available_document_version_id']
            current = {mid for mid, value in entries if value['read_document_version_id'] == head}
            old = {mid for mid, value in entries if value['read_document_version_id'] != head}
            received = [item for item in deliveries if item['body'].get('source_stream') == stream
                        and item['body'].get('read_document_version_id') == head
                        and (item['body'].get('delivered_content') or item['body'].get('authored_content') or
                             (not old and item['body'].get('metadata_only')))]
            status, reason = 'covered', None
            handled_by, unresolved = [], []
            if not received:
                status, reason = 'needs_research', 'latest_content_not_delivered'
            # An extraction's parent chain is verified but must have actual source
            # delivery too; merely naming a parent never establishes coverage.
            for actor, document in documents:
                refs = {mid for mid in document.get('material_ids', []) if mid in verified}
                pending = list(refs)
                while pending:
                    for parent in verified[pending.pop()]['parent_material_ids']:
                        if parent in verified and parent not in refs:
                            refs.add(parent)
                            pending.append(parent)
                related = {mid for mid in refs if verified[mid]['source_stream'] == stream}
                if not related:
                    continue
                own = {rid for rid in document.get('material_delivery_ids', []) if rid in receipts
                       and receipts[rid]['agent_id'] == actor}
                newest_read = any(item['id'] in own for item in received)
                if related & old:
                    resolved = any(update.get('status') == 'handled' and update.get('summary')
                        and update.get('latest_material_id') in current
                        and set(update.get('prior_material_ids') or []) >= (related & old)
                        for update in document.get('material_updates', []))
                    if not (newest_read and resolved):
                        unresolved.append(actor)
                    else:
                        handled_by.append(actor)
                elif newest_read:
                    handled_by.append(actor)
                else:
                    unresolved.append(actor)
            # The main agent may visibly supersede an old child conclusion, only
            # after receiving the correction itself. Preserve the child's attribution.
            manager_ids = set(manager.get('material_delivery_ids') or [])
            manager_read = any(item['id'] in manager_ids and item['agent_id'] == 'manager' for item in received)
            if manager_read:
                for update in manager.get('material_updates', []):
                    if (update.get('status') == 'handled' and update.get('summary')
                            and update.get('latest_material_id') in current
                            and set(update.get('prior_material_ids') or []) >= old):
                        unresolved = [actor for actor in unresolved if actor not in update.get('supersedes_agent_ids', [])]
                        handled_by.append('manager')
            if unresolved:
                status, reason = 'needs_research', 'visible_report_has_unprocessed_correction'
            if old and not handled_by:
                status, reason = 'needs_research', 'historical_comparison_not_processed'
            if any(update.get('status') == 'needs_review' and update.get('latest_material_id') in current
                   for _, document in documents for update in document.get('material_updates', [])):
                status, reason = 'needs_research', 'correction_review_pending'
            coverage[stream] = {'status': status, 'reason': reason, 'latest_document_version_id': head,
                'latest_material_ids': sorted(current), 'historical_material_ids': sorted(old),
                'delivery_ids': [item['id'] for item in received], 'handled_by': sorted(set(handled_by)),
                'unresolved_report_agents': sorted(set(unresolved))}
        return coverage

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
        from .config import model_config_summary
        deliveries = self.checkpoint_deliveries(job['id'])
        verified = self.checkpoint_material_guard(body.get('material_ids', []), job.get('account_id'))
        coverage = self.checkpoint_source_coverage(body, verified, deliveries)
        missing = sorted((set(body.get('material_ids', [])) |
            {mid for entry in verified.values() for mid in entry['parent_material_ids']}) - set(verified))
        body = {**body, 'verified_material_versions': verified,
            'material_guard_version': 3, 'material_deliveries': deliveries,
            'source_coverage': coverage, 'unknown_material_ids': missing,
            'recovery_state': 'needs_research' if missing or any(item['status'] != 'covered' for item in coverage.values()) else 'synthesis_ready',
            'covered_event_scope': context.get('_research_input_guard') or self.research_guard(context),
            'latest_event_scope_at_save': self.research_guard(context),
            'team_snapshot': body.get('team_snapshot') or context.get('team_snapshot'),
            'model_config': body.get('model_config') or context.get('model_config') or model_config_summary(self._checkpoint_config(job, context))}
        with self.connect() as db:
            db.execute('''INSERT INTO synthesis_checkpoints
                (job_id,account_id,input_version,universe_version,body,guard,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                body=excluded.body,guard=excluded.guard,created_at=excluded.created_at''',
                (job['id'], job.get('account_id') or '', job.get('input_version'),
                 context['universe_version'], dumps(body), body['covered_event_scope'], now()))

    def reusable_synthesis_checkpoint(self, job, context):
        from .store import loads
        guard = self.research_guard(context)
        config = self._checkpoint_config(job, context)
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
                if row['guard'] != guard or not 0 <= age <= config.get('synthesis_checkpoint_max_age_seconds', 1800):
                    continue
                if row['resume_count'] >= 2:
                    continue
                body = loads(row['body'])
                verified = body.get('verified_material_versions', {})
                # Older checkpoints lack per-actor delivery and processing proof.
                # Their evidence is preserved, but cannot establish it retroactively.
                if body.get('material_guard_version') != 3 or body.get('recovery_state') != 'synthesis_ready':
                    continue
                current_materials = self.checkpoint_material_guard(list(verified), job.get('account_id'))
                if current_materials != verified:
                    continue
                db.execute('UPDATE synthesis_checkpoints SET resume_count=resume_count+1 WHERE job_id=?', (row['job_id'],))
                from .config import model_config_summary
                current = context.get('model_config') or model_config_summary(config)
                previous = body.get('model_config')
                return {**body, 'checkpoint_job_id': row['job_id'],
                        'configuration_relation': {'strategy': 'reuse_visible_research_current_task_config',
                            'previous': previous, 'current': current,
                            'previous_team_snapshot': body.get('team_snapshot'),
                            'current_team_snapshot': context.get('team_snapshot') or config.get('_agent_team'),
                            'changed': previous != current if previous else None,
                            'note': '复用仍有效的可见研究；汇总使用本次任务配置，旧任务能力观测不作本次证明。'},
                        'checkpoint_created_at': row['created_at'], 'resume_attempt': row['resume_count'] + 1}
        return None

    def checkpoint_status(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT created_at,resume_count,body FROM synthesis_checkpoints WHERE job_id=?', (job_id,)).fetchone()
        from .store import loads
        saved = loads(row['body']) if row else {}
        return {'available': bool(row), **({key: row[key] for key in ('created_at', 'resume_count')} if row else {}),
                'recovery_state': saved.get('recovery_state', 'legacy_unknown'), 'source_coverage': saved.get('source_coverage', {}),
                'note': '重试前检查账户版本、公共范围、行情、事件与时效；不满足时重新研究'}
