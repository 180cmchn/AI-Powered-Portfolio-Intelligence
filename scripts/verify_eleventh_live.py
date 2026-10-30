#!/usr/bin/env python3
"""Two bounded real research rounds after manual simulated trades in an isolated DB.

Reads only the authorized daily account's model matrix and safe credential refs.
No daily writes, endpoint/effort changes, business retries or formal clock shifts.
Finite pre-connection/HTTP-rejection retries follow the frozen application policy.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.calendar import market_state
from app.config import defaults, migrate_timeout_policy
from app.domain import now
from app.runtime_version import source_digest, env_file_stamp
from app.store import Store
from scripts.verify_live_chain import all_events, scrubbed


def configure(source, target, account_id):
    store = Store(target, 'live')
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin, store.connect() as dest:
        origin.row_factory = sqlite3.Row
        if not origin.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            raise ValueError('Reference account not present; no inferred model configuration')
        raw = origin.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        cfg = {**defaults(), **migrate_timeout_policy(json.loads(raw[0]) if raw else {})}
        cfg.update(scheduled_research_enabled=False, market_refresh_enabled=False,
            forecast_evaluation_enabled=False, news_queries=[], debounce_seconds=600, max_job_attempts=1)
        for table in ('model_providers', 'provider_connections', 'provider_catalogs', 'agent_definitions'):
            dest.execute('DELETE FROM ' + table)
            for row in origin.execute('SELECT * FROM ' + table):
                dest.execute('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', tuple(row))
        overrides = {r['role_id']: json.loads(r['body']) for r in origin.execute(
            'SELECT role_id,body FROM account_agent_overrides WHERE account_id=?', (account_id,))}
        for row in dest.execute('SELECT role_id,body FROM agent_definitions').fetchall():
            body = json.loads(row['body'])
            body['default_binding'].update(overrides.get(row['role_id'], {}))
            dest.execute('UPDATE agent_definitions SET body=? WHERE role_id=?',
                (json.dumps(body, ensure_ascii=False), row['role_id']))
    store.set_meta('settings', cfg)
    return store, cfg


async def run(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    store, cfg = configure(args.source, out / 'evaluation.sqlite3', args.reference_account)
    digest, stamp = source_digest(), env_file_stamp()
    summary = {'kind': 'real_provider_api_worker_with_simulated_ledger', 'started_at': now(),
        'daily_access': 'SQLite mode=ro only; no daily restart/migration/settings/account changes',
        'source_sha256': digest, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'market_state': market_state(), 'formal_prediction': 'No clock/window change; ordinary exploratory research only',
        'maximum_business_jobs': 2, 'automatic_business_retry': False,
        'preparation_gate': 'Isolated queue only: 600s debounce while recording the three HTTP buys; release only latest job after all inputs are committed, then restore 4s debounce. No market or forecast clock shift.',
        'binding_origin': 'Daily effective account overrides expanded into isolated role defaults; daily roles unchanged',
        'limits': {k: cfg.get(k) for k in ('model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds',
            'model_request_hard_timeout_seconds', 'synthesis_request_hard_timeout_seconds',
            'agent_timeout_seconds', 'analysis_timeout_seconds', 'max_model_calls', 'max_output_tokens')},
        'roles': [{k: r[k] for k in ('role_id', 'resolved', 'config_error')} for r in store.list_agent_roles()],
        'test_inputs': [{'symbol': 'SH:518880', 'quantity': '100', 'price': '10.000'},
            {'symbol': 'SZ:002354', 'quantity': '1000', 'price': '10.00'},
            {'symbol': 'SZ:159599', 'quantity': '1000', 'price': '2.500'}],
        'input_label': 'Explicit synthetic arithmetic prices/quantities; not market quotes, user trades or broker fills',
        'rounds': [], 'trades': []}
    def save():
        (out / 'summary.json').write_text(scrubbed(summary, cfg) + '\n')
        (out / 'summary.json').chmod(0o600)
    def request_evidence(task_id):
        events = all_events(store, task_id)
        return events, [{**r, 'event_at': e.get('timestamp', e.get('at')), 'agent_id': e.get('agent_id')}
            for e in events for r in (e.get('payload') or {}).get('requests', [])]
    def forecasts_for(job_id):
        with store.connect() as db:
            batches = [dict(row) for row in db.execute('SELECT id,kind,state,base_date,deadline,expected_count FROM forecast_batches WHERE job_id=?', (job_id,))]
            forecasts = [dict(row) for row in db.execute('SELECT id,symbol,horizon,direction,kind,published_at,target_date FROM forecasts WHERE job_id=?', (job_id,))]
            samples = [dict(row) for row in db.execute('SELECT s.symbol,s.horizon,s.state,s.reason,s.target_date FROM forecast_samples s JOIN forecast_batches b ON b.id=s.batch_id WHERE b.job_id=?', (job_id,))]
        return {'batches': batches, 'published': forecasts, 'samples': samples,
            'note': 'Structured persisted rows only; prose abstention is not a published forecast'}
    process = log = None
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        log = (out / 'service.log').open('w')
        process = subprocess.Popen([sys.executable, 'scripts/serve.py'], cwd=ROOT,
            env={**os.environ, 'APP_DB_PATH': str(store.path), 'APP_MODE': 'live', 'APP_HOST': '127.0.0.1', 'APP_PORT': str(port)},
            stdout=log, stderr=log)
        summary['instance'] = {'port': port, 'pid': process.pid}
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=45, trust_env=False) as api:
            async def request(method, path, **kwargs):
                res = await api.request(method, path, **kwargs)
                res.raise_for_status()
                return res.json()
            for _ in range(100):
                try:
                    summary['health'] = await request('GET', '/api/health'); break
                except httpx.HTTPError:
                    if process.poll() is not None: raise RuntimeError('Isolated process exited')
                    await asyncio.sleep(.2)
            else: raise RuntimeError('Isolated startup failed')
            assert summary['health']['runtime_version']['source_sha256_at_start'] == digest
            summary['symbol_resolution'] = await request('POST', '/api/securities/resolve', json={'symbols': ['518880', '002354', '159599']})
            if not summary['symbol_resolution']['ok']: raise RuntimeError('Actual security directory did not resolve all three securities')
            b = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'eleventh-create-b'}, json={'name': '第十一轮独立B（隔离检查）', 'cash': '20000', 'holdings': []})
            a = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'eleventh-create-a'}, json={
                'name': '第十一轮三证券模拟A', 'cash': '100000', 'holdings': [], 'preferences': {'risk': 'balanced',
                'horizon': '模拟成交验收，价格数量为算术测试输入。分析三证券趋势、现金与集中度、费用影响、建议及改变观点条件；成本未知不妨碍趋势分析。模型建议不是交易。'}})
            summary['accounts'] = {'a': a['id'], 'b': b['id']}
            before_b = {k: b[k] for k in ('version', 'cash', 'holdings', 'latest_job_id')}
            async def trade(values, operation):
                current = await request('GET', '/api/accounts/' + a['id'])
                plan = await request('GET', f"/api/accounts/{a['id']}/fee-plan")
                payload = {**values, 'operation_id': operation, 'expected_version': current['version'],
                    'fee_plan_version': plan['version'], 'traded_at': now(), 'mode': 'simulated',
                    'note': '第十一轮明确模拟算术输入，不是用户成交或当前行情'}
                preview = await request('POST', f"/api/accounts/{a['id']}/trades/preview", json=payload)
                receipt = await request('POST', f"/api/accounts/{a['id']}/trades", json=payload)
                summary['trades'].append({'preview': preview, 'receipt': receipt})
                save()
                return receipt
            for i, values in enumerate(summary['test_inputs']):
                receipt = await trade({**values, 'side': 'buy'}, f'eleventh-initial-buy-{i}')
            assert receipt['cash_after'] == '86484.90'
            cfg['debounce_seconds'] = 4
            store.set_meta('settings', cfg)
            with store.connect() as db:
                if db.execute('SELECT count(*) FROM jobs WHERE attempts>0').fetchone()[0]:
                    raise RuntimeError('Preparation exceeded isolated queue gate; audit all attempts without starting additional work')
                db.execute("UPDATE jobs SET due_at=? WHERE id=? AND state='queued'", (now(), receipt['research']['job_id']))
            summary['preparation_gate_released_at'] = now()
            async def collect(job_id, label):
                last = None
                async with asyncio.timeout(float(cfg['analysis_timeout_seconds']) + 90):
                    while True:
                        job = await request('GET', '/api/jobs/' + job_id)
                        state = (job['state'], job.get('stage'))
                        if state != last:
                            print(json.dumps({'label': label, 'job_id': job_id, 'state': state}, ensure_ascii=False), flush=True)
                            last = state
                        if job['state'] not in ('queued', 'running'): break
                        await asyncio.sleep(2)
                events, requests = request_evidence(job_id)
                snap = store.snapshot(a['id'], job['input_version'])
                result = job.get('result') or {}
                report = result.get('report')
                summary['rounds'].append({'label': label, 'job': job, 'request_count': len(requests), 'requests': requests,
                    'event_counts': dict(Counter(e['type'] for e in events)),
                    'report_published': job['state'] == 'completed' and job.get('report_status') == 'current' and bool(job.get('report_id')),
                    'frozen_account': snap, 'forecast_submission': forecasts_for(job_id),
                    'cost_note': 'Provider usage retained when returned; interrupted requests may have unknown cost; no invoice reconciliation',
                    'provider_log_comparison': 'Provider-side account/log access unavailable; compare returned request/response IDs only',
                    'source_unchanged': source_digest() == digest})
                if report: (out / (label + '-report.md')).write_text(report)
                save()
                return job
            await collect(receipt['research']['job_id'], 'initial')
            if source_digest() != digest: raise RuntimeError('Code changed after initial run; do not mix validation versions')
            sold = await trade({'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '10.500'}, 'eleventh-update-sell')
            assert sold['cash_after'] == '87529.90'
            await collect(sold['research']['job_id'], 'after_sell')
            after_b = await request('GET', '/api/accounts/' + b['id'])
            summary['second_account_unchanged'] = before_b == {k: after_b[k] for k in before_b}
            summary['final_ledger'] = await request('GET', f"/api/accounts/{a['id']}/ledger")
            summary['all_jobs'] = await request('GET', f"/api/accounts/{a['id']}/jobs")
            summary['completed_at'] = now()
            summary['source_unchanged'] = source_digest() == digest
            summary['environment_unchanged'] = env_file_stamp() == stamp
            summary['reports_completed'] = sum(r['report_published'] for r in summary['rounds'])
            save()
    except Exception as exc:
        summary['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        save()
        raise
    finally:
        if process:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        if log: log.close()
        # Include superseded, failed, cancelled and unexpected work, even if
        # the HTTP orchestration itself failed before collect() could finish.
        with store.connect() as db:
            tasks = [dict(row) for row in db.execute('SELECT id,account_id,state,stage,input_version,attempts,error FROM jobs ORDER BY created_at')]
            global_tasks = [dict(row) for row in db.execute('SELECT id,state,attempts,error FROM global_runs ORDER BY created_at')]
        attempts = []
        for task in [*tasks, *global_tasks]:
            events, requests = request_evidence(task['id'])
            attempts.append({'task': task, 'event_counts': dict(Counter(e['type'] for e in events)), 'requests': requests})
        summary['all_attempts_after_shutdown'] = attempts
        summary['total_http_attempts'] = sum(len(task['requests']) for task in attempts)
        summary['business_jobs_with_http'] = sum(bool(task['requests']) for task in attempts)
        summary['business_job_bound_observed'] = summary['business_jobs_with_http'] <= 2
        summary['final_source_unchanged'] = source_digest() == digest
        summary['final_environment_unchanged'] = env_file_stamp() == stamp
        summary['audit_completed_at'] = now()
        save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/live.sqlite3')
    parser.add_argument('--reference-account', default='96b91df38e854008b989206c63808c3a')
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
