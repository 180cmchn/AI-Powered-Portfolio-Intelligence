"""Stable scoring projections and execution provenance, independent of immutable predictions."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import json

from .domain import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_rule_projections(
 batch_id TEXT PRIMARY KEY, original_rule_version TEXT NOT NULL,
 scoring_rule_id TEXT, status TEXT NOT NULL, canonical_rule TEXT, projected_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forecast_rules_scoring ON forecast_rule_projections(scoring_rule_id,batch_id);
CREATE TABLE IF NOT EXISTS forecast_execution_attempts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 batch_id TEXT, account_id TEXT NOT NULL, role_id TEXT NOT NULL DEFAULT 'manager',
 team_id TEXT, manager_model TEXT, manager_effort TEXT, team_snapshot TEXT NOT NULL,
 planned_at TEXT NOT NULL, requested_at TEXT, published_at TEXT, state TEXT NOT NULL,
 error TEXT, requests TEXT NOT NULL DEFAULT '[]', UNIQUE(job_id,attempt));
CREATE INDEX IF NOT EXISTS forecast_attempts_batch ON forecast_execution_attempts(batch_id,id);
CREATE INDEX IF NOT EXISTS forecast_attempts_model ON forecast_execution_attempts(manager_model,team_id);
CREATE INDEX IF NOT EXISTS forecast_samples_batch_page ON forecast_samples(batch_id,symbol,horizon,id);
CREATE INDEX IF NOT EXISTS forecast_batches_page ON forecast_batches(kind,account_id,base_date DESC,created_at DESC,id);
CREATE TRIGGER IF NOT EXISTS forecast_attempt_identity_immutable
 BEFORE UPDATE OF job_id,attempt,account_id,role_id,team_id,manager_model,manager_effort,team_snapshot,planned_at
 ON forecast_execution_attempts BEGIN SELECT RAISE(ABORT,'attempt attribution is immutable'); END;
"""


# Executed under the Store migration transaction, never via executescript
# (which would commit DDL before the version/column migration can roll back).
SAMPLE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS forecast_research_scope_samples(
        job_id TEXT NOT NULL, sample_id TEXT NOT NULL, assigned_at TEXT NOT NULL,
        PRIMARY KEY(job_id,sample_id))""",
    """CREATE TABLE IF NOT EXISTS forecast_sample_attempts(
        sample_id TEXT NOT NULL, attempt_id INTEGER NOT NULL, assigned_at TEXT NOT NULL,
        PRIMARY KEY(sample_id,attempt_id))""",
    'CREATE INDEX IF NOT EXISTS forecast_sample_attempt_latest ON forecast_sample_attempts(sample_id,attempt_id DESC)',
    """CREATE TRIGGER IF NOT EXISTS forecast_sample_attempt_immutable BEFORE UPDATE ON forecast_sample_attempts
        BEGIN SELECT RAISE(ABORT,'sample attribution is immutable'); END""",
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def scoring_rule(rule):
    from .forecasts import DEFAULT_RULE, validate_rule
    if not isinstance(rule, dict) or not set(DEFAULT_RULE) <= set(rule):
        return None, None
    try:
        valid = validate_rule(rule)
    except (ValueError, TypeError):
        return None, None
    from decimal import Decimal
    canonical = {"method": "close_to_close_trading_days_v1", "timezone": "Asia/Shanghai",
        "horizons": sorted(valid["horizons"]), "threshold": str(Decimal(valid["threshold"]).normalize()),
        "price_policy": valid["price_policy"], "start_time": valid["start_time"],
        "deadline_time": valid["deadline_time"], "threshold_boundary": "inclusive_sideways"}
    return hashlib.sha256(dump(canonical).encode()).hexdigest(), canonical


class ForecastIdentityStore:
    def _register_scoring_rule(self, db, batch):
        try:
            rule = json.loads(batch["rule"]) if isinstance(batch["rule"], str) else batch["rule"]
        except (ValueError, TypeError):
            rule = None
        identity, canonical = scoring_rule(rule)
        db.execute("INSERT OR IGNORE INTO forecast_rule_projections VALUES(?,?,?,?,?,?)",
            (batch["id"], batch["rule_version"], identity, "known" if identity else "unknown",
             dump(canonical) if canonical else None, now()))
        return identity

    def migrate_forecast_identity(self, db):
        for statement in SAMPLE_SCHEMA:
            db.execute(statement)
        # Replace only the batch table's obsolete uniqueness constraint. The
        # prediction ledger and its immutable trigger are never touched.
        indexes = list(db.execute("PRAGMA index_list(forecast_batches)"))
        legacy = any([r[2] for r in db.execute(f"PRAGMA index_info('{idx[1]}')")] ==
            ["account_id", "kind", "base_date", "rule_version"] for idx in indexes if idx[2])
        if legacy:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='forecast_batches'").fetchone()[0]
            import re
            sql = re.sub(r",\s*UNIQUE\s*\(account_id,kind,base_date,rule_version\)", "", sql, flags=re.I)
            sql = sql.replace("forecast_batches", "forecast_batches_v7", 1)
            db.execute(sql)
            db.execute("INSERT INTO forecast_batches_v7 SELECT * FROM forecast_batches")
            db.execute("DROP TABLE forecast_batches")
            db.execute("ALTER TABLE forecast_batches_v7 RENAME TO forecast_batches")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS forecast_batches_daily ON forecast_batches(account_id,kind,base_date) WHERE kind IN ('formal','test')")
        db.execute("CREATE INDEX IF NOT EXISTS forecast_batches_page ON forecast_batches(kind,account_id,base_date DESC,created_at DESC,id)")
        for row in db.execute("SELECT b.id,b.rule,b.rule_version FROM forecast_batches b LEFT JOIN forecast_rule_projections p ON p.batch_id=b.id WHERE p.batch_id IS NULL"):
            self._register_scoring_rule(db, row)
        # Historical ownership comes only from its stored snapshot. Current
        # account settings are deliberately never read here.
        for job in list(db.execute("""SELECT DISTINCT j.* FROM jobs j JOIN forecast_batches b ON b.job_id=j.id
            WHERE j.config_snapshot IS NOT NULL AND j.attempts=1
            AND NOT EXISTS(SELECT 1 FROM forecast_execution_attempts a WHERE a.job_id=j.id)""")):
            self.bind_forecast_attempt(db, job["id"], 1, json.loads(job["config_snapshot"]), historical=True)

        if not db.execute("SELECT 1 FROM meta WHERE key='forecast_sample_identity_v9'").fetchone():
            # Only a single frozen attempt and a recorded scope prove historical
            # assignment. Never infer missing history from batch/current settings.
            for scope in list(db.execute("""SELECT r.*,a.id AS attempt_id FROM forecast_research_scopes r
                JOIN forecast_execution_attempts a ON a.job_id=r.job_id
                WHERE (SELECT count(*) FROM forecast_execution_attempts x WHERE x.job_id=r.job_id)=1""")):
                proven_ids = [row[0] for row in db.execute('''SELECT s.id FROM forecast_samples s WHERE s.batch_id=?
                    AND (NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id AND f.published_at<=?)
                         OR EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id AND f.job_id=?))''',
                    (scope['batch_id'], scope['assigned_at'], scope['job_id']))]
                ids = self.assign_forecast_scope(db, scope['job_id'], scope['batch_id'],
                    symbols=json.loads(scope['ready_symbols']), sample_ids=proven_ids,
                    assigned_at=scope['assigned_at'], include_published=True)
                db.executemany("INSERT OR IGNORE INTO forecast_sample_attempts VALUES(?,?,?)",
                    [(sid, scope['attempt_id'], scope['assigned_at']) for sid in ids])
            db.execute("INSERT INTO meta VALUES('forecast_sample_identity_v9',?)", (dump({'at': now(), 'unknown_history_preserved': True}),))

    def assign_forecast_scope(self, db, job_id, batch_id, *, symbols=None, horizons=None, sample_ids=None, assigned_at=None, include_published=False):
        clauses, args = ['s.batch_id=?'], [batch_id]
        for column, values in (('symbol', symbols), ('horizon', horizons), ('id', sample_ids)):
            if values is not None:
                values = list(values)
                if not values:
                    return []
                clauses.append('s.' + column + ' IN (' + ','.join('?' for _ in values) + ')')
                args.extend(values)
        if not include_published:
            clauses.append('NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=s.id)')
        ids = [r[0] for r in db.execute('SELECT s.id FROM forecast_samples s WHERE ' + ' AND '.join(clauses), args)]
        stamp = assigned_at or now()
        db.executemany('INSERT OR IGNORE INTO forecast_research_scope_samples VALUES(?,?,?)', [(job_id, sid, stamp) for sid in ids])
        return ids

    def bind_sample_attempt(self, db, job_id, attempt_id):
        db.execute("""INSERT OR IGNORE INTO forecast_sample_attempts(sample_id,attempt_id,assigned_at)
            SELECT r.sample_id,?,? FROM forecast_research_scope_samples r WHERE r.job_id=?
            AND NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.sample_id=r.sample_id)""", (attempt_id, now(), job_id))

    def bind_forecast_attempt(self, db, job_id, attempt, config, historical=False):
        from .forecasts import _safe_team, _manager_identity, _digest
        job = db.execute("SELECT account_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or not job["account_id"]:
            return
        batch = db.execute("SELECT id FROM forecast_batches WHERE job_id=?", (job_id,)).fetchone()
        team = _safe_team(config)
        model, effort = _manager_identity(team)
        team["manager_model"], team["manager_effort"] = model, effort
        team["team_id"] = team.get("team_id") or _digest(team)
        db.execute("""INSERT OR IGNORE INTO forecast_execution_attempts
            (job_id,attempt,batch_id,account_id,team_id,manager_model,manager_effort,team_snapshot,planned_at,state)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (job_id, max(1, attempt), batch[0] if batch else None,
            job["account_id"], team["team_id"] if model else None, model, effort if model else None, dump(team),
            now(), "historical_request_unknown" if historical else "planned"))
        if batch:
            if not historical:
                scope = db.execute('SELECT ready_symbols,purpose FROM forecast_research_scopes WHERE job_id=?', (job_id,)).fetchone()
                if scope and scope['purpose'] != 'explicit_subset':
                    baseline = json.loads(db.execute('SELECT baseline_evidence FROM forecast_batches WHERE id=?', (batch[0],)).fetchone()[0])
                    ready = {symbol for symbol, evidence in baseline.items() if evidence.get('status') == 'ready'}
                    self.assign_forecast_scope(db, job_id, batch[0], symbols=ready)
                    db.execute('UPDATE forecast_research_scopes SET ready_symbols=? WHERE job_id=?',
                        (dump(sorted(set(json.loads(scope['ready_symbols'])) | ready)), job_id))
            db.execute("UPDATE forecast_execution_attempts SET batch_id=? WHERE job_id=? AND batch_id IS NULL", (batch[0], job_id))
            frozen = db.execute("SELECT id,team_snapshot FROM forecast_execution_attempts WHERE job_id=? AND attempt=?", (job_id, max(1, attempt))).fetchone()
            self.bind_sample_attempt(db, job_id, frozen['id'])
            if not historical:
                db.execute("""UPDATE forecast_samples SET state='uncovered',reason=NULL WHERE id IN
                    (SELECT sample_id FROM forecast_sample_attempts WHERE attempt_id=?)
                    AND state IN ('waiting_config','failed')""", (frozen['id'],))
            if not db.execute("SELECT 1 FROM forecasts WHERE batch_id=? LIMIT 1", (batch[0],)).fetchone():
                db.execute("UPDATE forecast_batches SET team_snapshot=? WHERE id=?", (frozen["team_snapshot"], batch[0]))

    def ensure_forecast_attempt(self, job_id):
        with self.connect() as db:
            job = db.execute("SELECT attempts,config_snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job and job["config_snapshot"]:
                self.bind_forecast_attempt(db, job_id, job["attempts"], json.loads(job["config_snapshot"]))

    def update_forecast_attempt_state(self, db, job_id, state, error=None):
        db.execute("""UPDATE forecast_execution_attempts SET state=?,error=?
            WHERE job_id=? AND attempt=(SELECT max(attempt) FROM forecast_execution_attempts WHERE job_id=?)
            AND published_at IS NULL""", (state, str(error)[:1000] if error else None, job_id, job_id))

        if state == 'waiting_config':
            db.execute("""UPDATE forecast_samples SET state='waiting_config',reason=? WHERE batch_id IN
                (SELECT id FROM forecast_batches WHERE job_id=?) AND state IN ('uncovered','waiting_data')
                AND id IN (SELECT sa.sample_id FROM forecast_sample_attempts sa JOIN forecast_execution_attempts a
                    ON a.id=sa.attempt_id WHERE a.job_id=?)""", (str(error)[:1000] if error else '等待配置', job_id, job_id))

    def observe_forecast_model_event(self, job_id, event):
        payload = event.get("payload") or {}
        requests = payload.get("requests") or []
        if not requests:
            return
        allowed = {"model", "protocol", "reasoning_effort", "reasoning_effort_sent", "agent_id", "role_id",
            "provider_id", "provider_revision", "credential_revision", "prompt_version", "team_id",
            "stage", "status_code", "request_id", "error", "duration_ms", "input_chars", "attempt",
            "stream_requested", "response_format", "stream_first_byte_ms", "stream_first_event_ms",
            "stream_event_count", "stream_terminal", "transport_phase", "response_headers_received",
            "response_body_received"}
        clean = [{k: v for k, v in r.items() if k in allowed} for r in requests if isinstance(r, dict)]
        with self.connect() as db:
            row = db.execute("SELECT id,requests FROM forecast_execution_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1", (job_id,)).fetchone()
            if row:
                db.execute("UPDATE forecast_execution_attempts SET requested_at=COALESCE(requested_at,?),requests=? WHERE id=?",
                    (now(), dump(json.loads(row["requests"]) + clean), row["id"]))

    def forecast_attempts(self, batch_id=None, job_id=None, **filters):
        clauses, args = [], []
        for key, value in {"batch_id": batch_id, "job_id": job_id, **filters}.items():
            if key not in {"batch_id", "job_id", "account_id", "team_id", "manager_model", "manager_effort"}:
                raise ValueError("不支持的尝试筛选")
            if value not in (None, ""):
                clauses.append(f"{key}=?"); args.append(value)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM forecast_execution_attempts" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id", args)
            return [{**dict(r), "team_snapshot": json.loads(r["team_snapshot"]), "requests": json.loads(r["requests"]),
                "attribution_status": "known" if r["manager_model"] else "configuration_unknown",
                "request_observed": bool(r["requested_at"]), "publication_succeeded": bool(r["published_at"])} for r in rows]

    def _forecast_query(self, filters):
        allowed = {"account_id", "kind", "team_id", "manager_model", "manager_effort", "symbol", "start_date", "end_date", "rule_version", "batch_id", "horizon"}
        if set(filters) - allowed:
            raise ValueError("不支持的预测筛选条件")
        clauses, args = [], []
        identity = {key: f"CASE WHEN f.id IS NOT NULL THEN f.{key} ELSE a.{key} END" for key in ("team_id", "manager_model", "manager_effort")}
        for key, value in filters.items():
            if value in (None, ""):
                continue
            if key in ("start_date", "end_date"):
                date.fromisoformat(value)
                clauses.append("b.base_date" + (">=?" if key == "start_date" else "<=?")); args.append(value)
            elif key == "rule_version":
                clauses.append("(p.scoring_rule_id=? OR b.rule_version=?)"); args.extend([value, value])
            elif key in identity:
                if value == "__unknown__":
                    clauses.append(identity[key] + " IS NULL")
                else:
                    clauses.append(identity[key] + "=?"); args.append(value)
            else:
                alias = "b" if key in ("account_id", "kind") else "s"
                clauses.append(f"{alias}.{key}=?"); args.append(value)
        sql = """SELECT s.*,b.kind,b.base_date,b.rule_version AS original_rule_version,
            COALESCE(p.scoring_rule_id,'unknown:'||b.id) AS rule_version,
            p.scoring_rule_id,p.status AS rule_identity_status,b.rule,b.deadline,b.expected_count,b.created_at AS batch_created_at,
            f.id AS forecast_id,f.direction,f.published_at,f.report_id,f.reason AS forecast_reason,
            a.id AS execution_attempt_id,a.job_id AS assigned_job_id,a.attempt AS assigned_attempt,
            a.planned_at AS configuration_frozen_at,
            %s AS team_id,%s AS manager_model,%s AS manager_effort,
            CASE WHEN %s IS NULL THEN 'configuration_unknown' WHEN f.id IS NOT NULL THEN 'published' ELSE 'planned' END AS attribution_status,
            CASE WHEN t.last_error='calendar_revalidation_pending' THEN 'waiting_data' ELSE e.status END AS evaluation_status,
            CASE WHEN t.last_error='calendar_revalidation_pending' THEN 'calendar_revalidation_pending' ELSE e.reason END AS evaluation_reason,
            CASE WHEN t.last_error='calendar_revalidation_pending' THEN NULL ELSE e.actual_direction END AS actual_direction,
            CASE WHEN t.last_error='calendar_revalidation_pending' THEN NULL ELSE e.return_ratio END AS return_ratio,
            CASE WHEN t.last_error='calendar_revalidation_pending' THEN NULL ELSE e.hit END AS hit,
            e.revision AS evaluation_revision,t.last_error AS waiting_reason
            FROM forecast_samples s JOIN forecast_batches b ON b.id=s.batch_id
            LEFT JOIN forecast_rule_projections p ON p.batch_id=b.id
            LEFT JOIN forecasts f ON f.id=(SELECT f1.id FROM forecasts f1 WHERE f1.sample_id=s.id ORDER BY f1.published_at,f1.id LIMIT 1)
            LEFT JOIN forecast_execution_attempts a ON a.id=(SELECT sa.attempt_id
                FROM forecast_sample_attempts sa JOIN forecast_execution_attempts assigned ON assigned.id=sa.attempt_id
                WHERE sa.sample_id=s.id AND (f.id IS NULL OR (assigned.job_id=f.job_id AND assigned.team_id=f.team_id))
                ORDER BY sa.attempt_id DESC LIMIT 1)
            LEFT JOIN forecast_evaluations e ON e.forecast_id=f.id AND e.revision=(SELECT max(x.revision) FROM forecast_evaluations x WHERE x.forecast_id=f.id)
            LEFT JOIN forecast_evaluation_tasks t ON t.forecast_id=f.id""" % (identity["team_id"], identity["manager_model"], identity["manager_effort"], identity["manager_model"])
        return sql + (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def forecast_sample_attribution(self, db, batch_id):
        query, args = self._forecast_query({'batch_id': batch_id})
        row = db.execute("""SELECT count(DISTINCT COALESCE(team_id,'__unknown__')) AS identities,
            sum(manager_model IS NULL) AS unknown_samples,min(manager_model) AS model,min(team_id) AS team,
            min(manager_effort) AS effort FROM (""" + query + ')', args).fetchone()
        uniform = row['identities'] == 1 and not row['unknown_samples']
        return {'identity_count': row['identities'], 'unknown_samples': row['unknown_samples'] or 0,
            'attribution_status': 'known' if uniform else 'mixed' if row['identities'] > 1 else 'configuration_unknown',
            'manager_model': row['model'] if uniform else None, 'team_id': row['team'] if uniform else None,
            'manager_effort': row['effort'] if uniform else None,
            'identity_semantics': '已发布样本保留发布署名；未发布样本按实际分配的最近冻结尝试；批次可含多个执行配置'}

    def _forecast_rows(self, db, filters, limit=None, offset=0):
        query, args = self._forecast_query(filters)
        query += " ORDER BY b.base_date DESC,b.created_at DESC,s.batch_id,s.symbol,s.horizon,s.id"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"; args += [limit, offset]
        return db.execute(query, args).fetchall()

    def forecasts_page(self, limit=50, offset=0, **filters):
        limit, offset = min(200, max(1, limit)), max(0, offset)
        with self.connect() as db:
            query, args = self._forecast_query(filters)
            total = db.execute("SELECT count(*) FROM (" + query + ")", args).fetchone()[0]
            rows = self._forecast_rows(db, filters, limit, offset)
        items = []
        for row in rows:
            item = dict(row)
            item["rule"] = json.loads(item["rule"])
            item["calendar_evidence"] = json.loads(item["calendar_evidence"])
            item["display_state"] = self._forecast_display_state(item)
            items.append(item)
        return {"items": items, "total": total, "has_more": offset + len(items) < total,
                "next_offset": offset + len(items) if offset + len(items) < total else None}

    def forecast_comparison(self, account_ids, **filters):
        if not isinstance(account_ids, list) or not 2 <= len(set(account_ids)) <= 10:
            raise ValueError("请选择 2 至 10 个不同账户")
        filters = {"kind": "formal", **filters}
        if "account_id" in filters:
            raise ValueError("比较使用 account_ids")
        accounts = {a: self.account(a)["name"] for a in account_ids}
        buckets = {a: defaultdict(list) for a in accounts}
        with self.connect() as db:
            for a in accounts:
                for r in self._forecast_rows(db, {**filters, "account_id": a}):
                    key = (r["base_date"], r["symbol"], r["horizon"], r["rule_version"])
                    buckets[a][key].append(dict(r))
        # Exploration has no predeclared common information window. Even a
        # single coincident close date must not become a post-hoc matched trial.
        comparable = filters["kind"] != "exploratory"
        shared = set.intersection(*(set(v) for v in buckets.values())) if comparable else set()
        shared = {k for k in shared if all(len(buckets[a][k]) == 1 and buckets[a][k][0]["scoring_rule_id"] for a in accounts)}
        evaluated = {k for k in shared if all(buckets[a][k][0]["evaluation_status"] == "evaluated" for a in accounts)}
        groups = []
        for horizon, rule_id in sorted({(k[2], k[3]) for k in shared}):
            keys = {k for k in shared if k[2:] == (horizon, rule_id)}
            matured = keys & evaluated
            groups.append({"horizon": horizon, "rule_version": rule_id, "overlap_expected": len(keys),
                "overlap_evaluated": len(matured), "accounts": [{"account_id": a, "name": accounts[a],
                    "hits": sum(buckets[a][k][0]["hit"] or 0 for k in matured), "evaluated": len(matured),
                    "accuracy": sum(buckets[a][k][0]["hit"] or 0 for k in matured) / len(matured) if matured else None} for a in accounts]})
        return {"matching_dimensions": ["base_date", "symbol", "horizon", "rule_version"], "rule_version_semantics": "canonical_scoring_rule_id", "groups": groups,
            "pairing_status": "fixed_formal_batch" if comparable else "not_directly_pairable",
            "pairing_rule": "固定正式批次一对一" if comparable else "探索没有预先锁定共同信息窗口，累计展示全部记录但不作跨账户配对",
            "overlap_expected": len(shared), "overlap_evaluated": len(evaluated),
            "unmatched_counts": {a: sum(map(len, buckets[a].values())) - len(shared) for a in accounts},
            "note": "累计探索次数会改变样本数量；正式比较的实际发布时间、材料与附加搜索也可能不同，不是严格对照实验。"}

    def forecast_statistics(self, **filters):
        filters = {"kind": "formal", **filters}
        query, args = self._forecast_query(filters)
        state = "CASE WHEN state!='predicted' THEN state WHEN evaluation_status IS NOT NULL THEN evaluation_status WHEN waiting_reason IS NOT NULL OR target_date IS NULL THEN 'waiting_data' ELSE 'pending_maturity' END"
        cte = "WITH q AS (" + query + ") "
        with self.connect() as db:
            summaries = list(db.execute(cte + """SELECT horizon,rule_version,min(label) AS label,min(rule) AS rule,
                count(*) AS expected,sum(state='predicted') AS predicted,sum(state IN ('predicted','abstain')) AS addressed,
                sum(evaluation_status='evaluated') AS evaluated,sum(CASE WHEN evaluation_status='evaluated' THEN hit ELSE 0 END) AS hits
                FROM q GROUP BY horizon,rule_version ORDER BY horizon,rule_version""", args))
            states = list(db.execute(cte + f"SELECT horizon,rule_version,{state} AS display_state,count(*) AS n FROM q GROUP BY horizon,rule_version,display_state", args))
            matrix_rows = list(db.execute(cte + "SELECT horizon,rule_version,direction,actual_direction,evaluation_status,count(*) AS n FROM q WHERE direction IS NOT NULL GROUP BY horizon,rule_version,direction,actual_direction,evaluation_status", args))
            days = list(db.execute(cte + "SELECT horizon,rule_version,base_date,sum(hit) AS hits,count(*) AS evaluated FROM q WHERE evaluation_status='evaluated' GROUP BY horizon,rule_version,base_date ORDER BY base_date", args))
            # Attempts are counted separately; EXISTS prevents sample/revision
            # joins from duplicating either attempts or official denominators.
            identity_keys = {'team_id', 'manager_model', 'manager_effort'}
            sample_query, sample_args = self._forecast_query({k: v for k, v in filters.items() if k not in identity_keys})
            ac, av = ["a.batch_id IN (SELECT batch_id FROM (" + sample_query + "))"], list(sample_args)
            if filters.get('symbol') or filters.get('horizon'):
                ac.append('EXISTS(SELECT 1 FROM forecast_sample_attempts sa WHERE sa.attempt_id=a.id AND sa.sample_id IN (SELECT id FROM (' + sample_query + ')))')
                av.extend(sample_args)
            for k in identity_keys:
                v = filters.get(k)
                if v in (None, ''):
                    continue
                if v == '__unknown__':
                    ac.append('a.' + k + ' IS NULL')
                else:
                    ac.append('a.' + k + '=?'); av.append(v)
            attempts = [dict(r) for r in db.execute("SELECT a.state,count(*) AS count,sum(a.requested_at IS NOT NULL) AS requested,sum(a.published_at IS NOT NULL) AS published FROM forecast_execution_attempts a WHERE " + ' AND '.join(ac) + ' GROUP BY a.state', av)]
        output = []
        for g in summaries:
            key = (g['horizon'], g['rule_version'])
            matching = [r for r in matrix_rows if (r['horizon'],r['rule_version']) == key]
            matrix = {p: {a: 0 for a in ('up','sideways','down')} for p in ('up','sideways','down')}
            predicted_classes, actual_classes = defaultdict(int), defaultdict(int)
            for r in matching:
                predicted_classes[r['direction']] += r['n']
                if r['evaluation_status'] == 'evaluated':
                    matrix[r['direction']][r['actual_direction']] += r['n']; actual_classes[r['actual_direction']] += r['n']
            evaluated, hits = g['evaluated'] or 0, g['hits'] or 0
            output.append({'horizon': g['horizon'], 'label': g['label'], 'rule_version': g['rule_version'],
                'threshold': json.loads(g['rule']).get('threshold'), 'expected': g['expected'], 'predicted': g['predicted'],
                'hits': hits, 'evaluated': evaluated, 'accuracy': hits / evaluated if evaluated else None,
                'coverage': g['addressed'] / g['expected'], 'directional_coverage': g['predicted'] / g['expected'],
                'states': {r['display_state']: r['n'] for r in states if (r['horizon'],r['rule_version']) == key},
                'confusion_matrix': matrix, 'predicted_classes': dict(predicted_classes), 'actual_classes': dict(actual_classes),
                'by_day': [{'date': r['base_date'], 'hits': r['hits'], 'evaluated': r['evaluated'], 'accuracy': r['hits'] / r['evaluated']} for r in days if (r['horizon'],r['rule_version']) == key]})
        return {'groups': output, 'expected': sum(g['expected'] for g in output), 'filters': filters,
            'attempt_statistics': {'total': sum(r['count'] for r in attempts), 'states': attempts},
            'comparison_note': '正式样本按发布团队归属；未发布按最近已冻结尝试归属，旧失败另计尝试。探索累计次数会影响数量，不直接配对。',
            'method': 'D 收盘为基准，D 后第 N 个交易日收盘为终点。首次成功正式预测锁定，阈值边界计震荡；未成熟及缺失不进入准确率分母。'}
