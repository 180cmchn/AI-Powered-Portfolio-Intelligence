"""Durable visible research checkpoints, never opaque model continuation state."""
from datetime import datetime
import hashlib
import json

from .domain import TZ, now


REPORT_FIELDS = ('report_id', 'agent_id', 'role_id', 'model_config', 'research_round',
                 'summary', 'findings', 'material_ids', 'open_questions', 'material_updates',
                 'material_delivery_ids', 'summary_truncated')


def report_identity(report):
    """Immutable visible child report identity; never a model-provided receipt."""
    return hashlib.sha256(json.dumps({key: report[key] for key in REPORT_FIELDS if key in report},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class RecoveryStore:
    def migrate_recovery_deliveries(self, db):
        # Keep Store's surrounding migration transaction intact: executescript
        # would commit pending material/version migrations before this DDL.
        db.execute("""CREATE TABLE IF NOT EXISTS research_report_deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            account_id TEXT NOT NULL, recipient TEXT NOT NULL,
            source_job_id TEXT NOT NULL, report_id TEXT NOT NULL,
            visible_hash TEXT NOT NULL, body TEXT NOT NULL, delivered_at TEXT NOT NULL,
            UNIQUE(job_id,recipient,report_id,visible_hash))""")
        db.execute('CREATE INDEX IF NOT EXISTS report_deliveries_job ON research_report_deliveries(job_id,id)')

    def record_report_delivery(self, job_id, account_id, report, visible, *, channel, source_job_id=None):
        """Called only by Runtime after placing a report in a model-visible message.

        A full report is indirect research, never a direct original-material read.
        Source receipts and the original instance/configuration remain unchanged.
        """
        from .store import dumps, loads
        source_job_id = source_job_id or job_id
        if not report.get('report_id') or report.get('agent_id') in {None, 'manager'}:
            return None
        with self.connect() as db:
            # A resumed task may receive the old report only in the same frozen
            # account input scope. Global work uses its own task scope.
            jobs = {row['id']: dict(row) for row in db.execute(
                'SELECT id,account_id,input_version FROM jobs WHERE id IN (?,?)', (job_id, source_job_id))}
            if account_id:
                if set(jobs) != {job_id, source_job_id} or any(row['account_id'] != account_id for row in jobs.values()):
                    return None
                if jobs[job_id]['input_version'] != jobs[source_job_id]['input_version']:
                    return None
            elif source_job_id != job_id:
                runs = db.execute('SELECT input_snapshot FROM global_runs WHERE id IN (?,?)', (job_id, source_job_id)).fetchall()
                if len(runs) != 2 or len({loads(row[0]).get('universe_version') for row in runs}) != 1:
                    return None
            ids = set(report.get('material_delivery_ids') or [])
            originals = [{**dict(row), 'body': loads(row['body'])} for row in db.execute(
                'SELECT * FROM research_material_deliveries WHERE job_id=? AND account_id=? AND agent_id=?',
                (source_job_id, account_id or '', report['agent_id'])) if row['id'] in ids]
            fields = ('summary', 'findings', 'material_ids', 'open_questions', 'material_updates')
            full = (not visible.get('summary_truncated') and not report.get('summary_truncated')
                    and all(visible.get(key) == report.get(key) for key in fields if key in report))
            delivered = {key: visible[key] for key in fields if key in visible}
            visible_hash = hashlib.sha256(dumps({'visible': delivered, 'full_report': full}).encode()).hexdigest()
            proof = {'source_task_id': source_job_id, 'recipient_task_id': job_id,
                'source_agent_id': report['agent_id'], 'report_id': report['report_id'],
                'research_round': report.get('research_round'), 'role_id': report.get('role_id'),
                'model_config': report.get('model_config'), 'report_hash': report_identity(report),
                'access': 'full_report' if full else 'report_summary', 'channel': channel,
                'visible_report': delivered, 'summary_truncated': not full,
                'source_material_deliveries': originals,
                'note': '子研究成果交付；不是主 Agent 亲读原文，来源范围沿用子实例实际读取。'}
            db.execute('INSERT OR IGNORE INTO research_report_deliveries(job_id,account_id,recipient,source_job_id,report_id,visible_hash,body,delivered_at) VALUES(?,?,?,?,?,?,?,?)',
                (job_id, account_id or '', 'manager', source_job_id, report['report_id'], visible_hash, dumps(proof), now()))
            return db.execute('SELECT id FROM research_report_deliveries WHERE job_id=? AND recipient=? AND report_id=? AND visible_hash=?',
                (job_id, 'manager', report['report_id'], visible_hash)).fetchone()[0]

    def checkpoint_report_deliveries(self, job_id):
        from .store import loads
        with self.connect() as db:
            return [{**dict(row), 'body': loads(row['body'])} for row in db.execute(
                'SELECT * FROM research_report_deliveries WHERE job_id=? ORDER BY id', (job_id,))]

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
                        'native_search', 'save_material', 'query_market_rows', 'market_indicators'}:
            return []
        if tool == 'market_indicators' and not result.get('_input_receipt'):
            # The calculation output exposes a bounded set of input closes.
            # Record exactly those fields/dates, not every row in either source.
            visible_hash = hashlib.sha256(dumps(result).encode()).hexdigest()
            receipts = []
            scopes = [(result, result.get('input_closes') or [], 'asset')]
            benchmark = result.get('benchmark')
            if isinstance(benchmark, dict) and result.get('comparison_input_closes'):
                scopes.append((benchmark, [[row[0], row[2]] for row in result['comparison_input_closes']], 'benchmark_aligned'))
            for metadata, values, role in scopes:
                if not values:
                    continue
                rows = [{'time': row[0], 'close': row[1]} for row in values]
                receipts.extend(self.record_material_delivery(job_id, account_id, agent_id, tool, {
                    'material_id': metadata.get('material_id'), 'rows': rows, 'fields': ['time', 'close'],
                    'row_count': len(rows), 'returned_range': {'start': rows[0]['time'], 'end': rows[-1]['time']},
                    'requested_range': result.get('requested_range'), 'order': 'asc',
                    'delivery_kind': 'calculation_inputs', 'calculation_input_role': role,
                    'parent_visible_result_hash': visible_hash, '_input_receipt': True}))
            return receipts
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
        structured = any(bool(result.get(key)) for key in ('items', 'rows', 'matches', 'data'))
        if not text and not structured and not metadata_only and not authored_content:
            return []
        proof = {**guard, 'task_id': job_id, 'agent_id': agent_id,
            'delivery_kind': result.get('delivery_kind', 'direct_source'),
            'calculation_input_role': result.get('calculation_input_role'),
            'parent_visible_result_hash': result.get('parent_visible_result_hash'),
            'delivered_content': bool(text or structured), 'metadata_only': metadata_only, 'authored_content': authored_content,
            'delivered_range': {key: result[key] for key in ('start', 'end', 'total_chars', 'returned_range',
                'page_range', 'pages_read', 'truncated', 'source_truncated', 'requested_range', 'actual_range',
                'returned_range', 'returned_count', 'row_count', 'next_offset', 'order', 'fields', 'adjust') if key in result},
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

    def checkpoint_source_coverage(self, body, verified, deliveries, report_deliveries=()):
        """Describe visible treatment of corrections, without judging research truth."""
        receipts = {item['id']: item for item in deliveries}
        reports = body.get('reports') or []
        # Keep old rounds for provenance; only the latest visible round is operative.
        latest_reports = {}
        for index, report in enumerate(reports):
            latest_reports[report.get('agent_id') or 'legacy-' + str(index)] = report
        manager = body.get('manager_summary') or {}
        documents = list(latest_reports.items()) + [('manager', manager)]
        pending_actors = {item.get('agent_id') for item in body.get('unresolved_tasks', [])
            if item.get('pending_messages') or item.get('status') in {'queued', 'running'}
            or item.get('round', 0) > (latest_reports.get(item.get('agent_id'), {}).get('research_round') or 0)}
        delegated = {item['body']['report_id']: item for item in report_deliveries
            if item.get('recipient') == 'manager' and item['body'].get('access') == 'full_report'}
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
                        and (item['body'].get('delivered_content') or item['body'].get('authored_content'))]
            status, reason = 'covered', None
            handled_by, unresolved, delegated_via = [], [], []
            referenced = False
            if not received:
                status, reason = 'needs_research', 'latest_content_not_delivered'
            # An extraction's parent chain is verified but must have actual source
            # delivery too; merely naming a parent never establishes coverage.
            document_refs = {}
            for actor, document in documents:
                refs = {mid for mid in document.get('material_ids', []) if mid in verified}
                pending = list(refs)
                while pending:
                    for parent in verified[pending.pop()]['parent_material_ids']:
                        if parent in verified and parent not in refs:
                            refs.add(parent)
                            pending.append(parent)
                document_refs[actor] = refs
                related = {mid for mid in refs if verified[mid]['source_stream'] == stream}
                if not related:
                    continue
                referenced = True
                own = {rid for rid in document.get('material_delivery_ids', []) if rid in receipts
                       and receipts[rid]['agent_id'] == actor}
                own_materials = {receipts[rid]['material_id'] for rid in own
                    if receipts[rid]['body'].get('delivered_content') or receipts[rid]['body'].get('authored_content')}
                delegated_materials, historical_delegated, via = set(), set(), []
                if actor == 'manager':
                    for child_actor, child_report in latest_reports.items():
                        receipt = delegated.get(child_report.get('report_id'))
                        if not (receipt and child_actor not in pending_actors
                                and receipt['body'].get('report_hash') == report_identity(child_report)):
                            continue
                        child_refs = document_refs.get(child_actor, set())
                        if child_actor in handled_by:
                            delegated_materials.update(child_refs)
                            if related & child_refs:
                                via.append(receipt['id'])
                        else:
                            # An actually delivered old report can supply the
                            # historical comparison, never the latest version.
                            child_reads = {item['material_id'] for item in deliveries
                                if item['agent_id'] == child_actor
                                and item['id'] in child_report.get('material_delivery_ids', [])
                                and (item['body'].get('delivered_content') or item['body'].get('authored_content'))}
                            historical = child_refs & child_reads & old
                            historical_delegated.update(historical)
                            if related & historical:
                                via.append(receipt['id'])
                available = own_materials | delegated_materials | historical_delegated
                # A receipt for page/representation A does not make unread B
                # available merely because both belong to document version v1.
                complete_access = related <= available
                if actor in pending_actors:
                    unresolved.append(actor)
                    continue
                if related & old:
                    resolved = any(update.get('status') == 'handled' and update.get('summary')
                        and update.get('latest_material_id') in current & available
                        and set(update.get('prior_material_ids') or []) >= (related & old)
                        for update in document.get('material_updates', []))
                    if not (complete_access and (resolved or related <= delegated_materials)):
                        unresolved.append(actor)
                    else:
                        handled_by.append(actor)
                        delegated_via.extend(via)
                elif complete_access:
                    handled_by.append(actor)
                    delegated_via.extend(via)
                else:
                    unresolved.append(actor)
            # The main agent may visibly supersede an old child conclusion, only
            # after receiving the correction itself. Preserve the child's attribution.
            manager_ids = set(manager.get('material_delivery_ids') or [])
            manager_current = {item['material_id'] for item in received
                if item['id'] in manager_ids and item['agent_id'] == 'manager'}
            manager_read = bool(manager_current)
            if manager_read and old:
                for update in manager.get('material_updates', []):
                    if (update.get('status') == 'handled' and update.get('summary')
                            and update.get('latest_material_id') in current & manager_current
                            and set(update.get('prior_material_ids') or []) >= old):
                        superseded = set()
                        for actor in update.get('supersedes_agent_ids', []):
                            if actor == 'manager' or actor not in latest_reports or actor in pending_actors:
                                continue
                            # Supersession addresses an actually researched old
                            # conclusion, not missing current reads or oneself.
                            historical_refs = document_refs.get(actor, set()) & (old | current)
                            actor_reads = {item['material_id'] for item in deliveries
                                if item['agent_id'] == actor
                                and item['id'] in latest_reports[actor].get('material_delivery_ids', [])
                                and (item['body'].get('delivered_content') or item['body'].get('authored_content'))}
                            if historical_refs and historical_refs <= old & actor_reads:
                                superseded.add(actor)
                        unresolved = [actor for actor in unresolved if actor not in superseded]
                        if 'manager' not in unresolved:
                            handled_by.append('manager')
            if unresolved:
                status, reason = 'needs_research', (
                    'research_round_pending' if set(unresolved) & pending_actors else
                    'visible_report_has_unprocessed_correction' if old else
                    'visible_report_source_not_received')
                if unresolved == ['manager'] and any(actor != 'manager' for actor in handled_by):
                    reason = 'delegated_report_not_delivered'
            if old and not handled_by:
                status, reason = 'needs_research', 'historical_comparison_not_processed'
            if any(update.get('status') == 'needs_review' and update.get('latest_material_id') in current
                   for _, document in documents for update in document.get('material_updates', [])):
                status, reason = 'needs_research', 'correction_review_pending'
            if not referenced and not old and any(item['body'].get('source_stream') == stream
                    and item['body'].get('metadata_only') for item in deliveries):
                # A discovered but unused URL is an explicit gap, not an unmet
                # promise to read every document before synthesizing other work.
                status, reason = 'index_only', 'source_index_not_claimed_as_read'
            elif not referenced and old:
                # An unused URL title update does not revise an observed claim.
                # No version may contain text, a content receipt or a report
                # declaration mentioning these IDs; otherwise normal guards apply.
                mids = {mid for mid, _ in entries}
                mentioned = any(any(mid in json.dumps(document, ensure_ascii=False, default=str) for mid in mids)
                                for _, document in documents)
                originals = [self.material(mid) for mid in mids]
                link_indexes = all(m.get('representation_kind') == 'native_search_source' and m.get('access') == 'source_link'
                                   and not (m.get('content') or m.get('text') or '').strip() for m in originals)
                content_received = any(item['body'].get('source_stream') == stream and
                    (item['body'].get('delivered_content') or item['body'].get('authored_content')) for item in deliveries)
                if link_indexes and not mentioned and not content_received:
                    status, reason = 'index_only', 'unused_source_link_metadata_changed'
            coverage[stream] = {'status': status, 'reason': reason, 'latest_document_version_id': head,
                'latest_material_ids': sorted(current), 'historical_material_ids': sorted(old),
                'delivery_ids': [item['id'] for item in received], 'report_delivery_ids': sorted(set(delegated_via)),
                'handled_by': sorted(set(handled_by)),
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
        batch = context.get('forecast_batch') or {}
        frozen_batch = {key: batch.get(key) for key in ('id', 'account_id', 'kind', 'base_date',
            'rule_version', 'rule', 'expected_count', 'created_at', 'deadline', 'input_version', 'scoring_rule_id')
            if key in batch and (batch.get('kind') == 'formal' or key not in {'id', 'created_at', 'deadline'})}
        raw = {'events': events, 'demands': demands, 'quotes': quotes, 'forecast_batch': frozen_batch,
               'target': (context.get('target_security') or {}).get('symbol')}
        return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()

    def save_synthesis_checkpoint(self, job, body, context):
        from .store import dumps
        from .config import model_config_summary
        deliveries = self.checkpoint_deliveries(job['id'])
        verified = self.checkpoint_material_guard(body.get('material_ids', []), job.get('account_id'))
        report_deliveries = self.checkpoint_report_deliveries(job['id'])
        coverage = self.checkpoint_source_coverage(body, verified, deliveries, report_deliveries)
        missing = sorted((set(body.get('material_ids', [])) |
            {mid for entry in verified.values() for mid in entry['parent_material_ids']}) - set(verified))
        body = {**body, 'verified_material_versions': verified,
            'material_guard_version': 3, 'material_deliveries': deliveries,
            'report_delivery_guard_version': 1, 'report_deliveries': report_deliveries,
            'source_coverage': coverage, 'unknown_material_ids': missing,
            'recovery_state': 'needs_research' if missing or any(item['status'] not in {'covered', 'index_only'} for item in coverage.values()) else 'synthesis_ready',
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
                if body.get('material_guard_version') != 3:
                    continue
                current_materials = self.checkpoint_material_guard(list(verified), job.get('account_id'))
                if current_materials != verified:
                    continue
                body = self.reassess_checkpoint_coverage(body)
                if body.get('recovery_state') != 'synthesis_ready':
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

    def reassess_checkpoint_coverage(self, body):
        """Read-only correction of unused-link metadata coverage; no new evidence."""
        if body.get('material_guard_version') != 3 or body.get('recovery_state') != 'needs_research' or body.get('unknown_material_ids'):
            return body
        verified = body.get('verified_material_versions', {})
        if self.checkpoint_material_guard(list(verified), body.get('account_id')) != verified:
            return body
        coverage = self.checkpoint_source_coverage(body, verified, body.get('material_deliveries', []), body.get('report_deliveries', []))
        if not all(item['status'] in {'covered', 'index_only'} for item in coverage.values()):
            return body
        return {**body, 'source_coverage': coverage, 'recovery_state': 'synthesis_ready',
            'recovery_assessment': {'stored_state': body['recovery_state'], 'basis': 'unused_link_metadata_only',
                'note': '只重算未用仅链接元数据的覆盖语义；原检查点、失败状态、材料与全部输入/时效guard未改写。'}}

    def checkpoint_status(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT created_at,resume_count,body FROM synthesis_checkpoints WHERE job_id=?', (job_id,)).fetchone()
        from .store import loads
        original = loads(row['body']) if row else {}
        saved = self.reassess_checkpoint_coverage(original) if row else {}
        return {'available': bool(row), **({key: row[key] for key in ('created_at', 'resume_count')} if row else {}),
                'saved_recovery_state': original.get('recovery_state', 'legacy_unknown'),
                'recovery_state': saved.get('recovery_state', 'legacy_unknown'), 'source_coverage': saved.get('source_coverage', {}),
                'report_access': [{key: item['body'].get(key) for key in ('report_id', 'source_agent_id',
                    'research_round', 'role_id', 'access', 'summary_truncated', 'note')}
                    for item in saved.get('report_deliveries', [])],
                'note': '重试前检查账户版本、公共范围、行情、事件与时效；不满足时重新研究'}
