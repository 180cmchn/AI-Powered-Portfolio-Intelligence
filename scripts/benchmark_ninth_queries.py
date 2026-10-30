#!/usr/bin/env python3
"""Synthetic mixed-attribution SQL benchmark; never opens the user's database."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.calendar import SHANGHAI
from app.store import Store
from benchmark_seventh_queries import measure


def benchmark(batches=5000):
    with tempfile.TemporaryDirectory(prefix='guanlan-ninth-query-') as directory:
        store = Store(Path(directory) / 'synthetic.sqlite3')
        account = store.save_account({'name': 'synthetic benchmark only', 'holdings': [
            {'symbol': s, 'quantity': '1'} for s in ('SZ:159278', 'SZ:159279')]})
        original = store.create_forecast_batch(account['id'], kind='exploratory', at=datetime(2026, 10, 28, 16, tzinfo=SHANGHAI))
        stamp = '2026-10-28T16:30:00+08:00'
        with store.connect() as db:
            template = dict(db.execute('SELECT * FROM forecast_batches WHERE id=?', (original['id'],)).fetchone())
            samples = [dict(row) for row in db.execute('SELECT * FROM forecast_samples WHERE batch_id=? ORDER BY symbol,horizon', (original['id'],))]
            projection = dict(db.execute('SELECT * FROM forecast_rule_projections WHERE batch_id=?', (original['id'],)).fetchone())
            def insert(table, row):
                db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', list(row.values()))
            for index in range(batches):
                bid = original['id'] if index == 0 else f'fixture-batch-{index}'
                if index:
                    insert('forecast_batches', {**template, 'id': bid})
                    insert('forecast_rule_projections', {**projection, 'batch_id': bid})
                attempt_ids = []
                for model in ('A', 'B'):
                    cur = db.execute("""INSERT INTO forecast_execution_attempts(job_id,attempt,batch_id,account_id,team_id,
                        manager_model,manager_effort,team_snapshot,planned_at,published_at,state)
                        VALUES(?,1,?,?,?,?,'medium','{}',?,?,?)""", (bid + model, bid, account['id'], 'team-' + model,
                        'fixture-model-' + model, stamp, stamp if model == 'A' else None, 'published' if model == 'A' else 'failed'))
                    attempt_ids.append(cur.lastrowid)
                for number, sample in enumerate(samples):
                    sid = sample['id'] if index == 0 else f'{bid}-sample-{number}'
                    if index:
                        insert('forecast_samples', {**sample, 'id': sid, 'batch_id': bid})
                    db.execute('INSERT INTO forecast_sample_attempts VALUES(?,?,?)', (sid, attempt_ids[0], stamp))
                    if sample['symbol'] == 'SZ:159279':
                        db.execute('INSERT INTO forecast_sample_attempts VALUES(?,?,?)', (sid, attempt_ids[1], stamp))
                        db.execute("UPDATE forecast_samples SET state='failed' WHERE id=?", (sid,))
                    else:
                        insert('forecasts', {'id': sid + '-forecast', 'sample_id': sid, 'batch_id': bid, 'account_id': account['id'],
                            'job_id': bid + 'A', 'report_id': bid + '-report', 'role_id': 'manager', 'kind': 'exploratory',
                            'symbol': sample['symbol'], 'base_date': template['base_date'], 'horizon': sample['horizon'],
                            'target_date': sample['target_date'], 'direction': 'up', 'reason': 'synthetic benchmark', 'citations': '[]',
                            'rule_version': template['rule_version'], 'rule': template['rule'], 'team_id': 'team-A', 'team_snapshot': '{}',
                            'manager_model': 'fixture-model-A', 'manager_effort': 'medium', 'published_at': stamp,
                            'information_cutoff': stamp, 'report': 'synthetic; not a real forecast', 'revises_id': None})
                        db.execute("UPDATE forecast_samples SET state='predicted' WHERE id=?", (sid,))
            query, args = store._forecast_query({'kind': 'exploratory', 'manager_model': 'fixture-model-B'})
            plan = [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + query + ' LIMIT 30 OFFSET 60', args)]
        def full_then_slice():
            with store.connect() as db:
                rows = store._forecast_rows(db, {'kind': 'exploratory'})
            return len(rows[60:90])
        def page(model=None):
            result = store.forecasts_page(kind='exploratory', manager_model=model, limit=30, offset=60)
            assert result['total'] == batches * (3 if model else 6)
            return len(result['items'])
        def aggregate():
            result = store.forecast_statistics(kind='exploratory', manager_model='fixture-model-B')
            assert result['expected'] == batches * 3
            return len(result['groups'])
        return {'scope': 'Synthetic mixed published A / failed frozen B, exact sample-attempt mappings; no network or user database',
            'batches': batches, 'samples': batches * 6, 'attempts': batches * 2, 'sample_attempt_links': batches * 9,
            'hardware': {'machine': platform.machine(), 'system': platform.platform(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
            'full_materialization_control': measure(full_then_slice), 'sql_page': measure(page),
            'sql_filtered_page': measure(lambda: page('fixture-model-B')), 'sql_filtered_aggregate': measure(aggregate),
            'query_plan': plan, 'limitations': 'Controlled local microbenchmark, not a production SLA. Python memory excludes SQLite native allocation and OS caches.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batches', type=int, default=5000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = json.dumps(benchmark(args.batches), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + '\n')
    print(result)
