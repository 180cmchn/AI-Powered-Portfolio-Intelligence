#!/usr/bin/env python3
"""Bounded real business matrix through manage.py and ordinary HTTP APIs.

Only model configuration is copied from the read-only reference database. No
transport injection, worker patch, task allowlist or time override is used.
All legitimate account/global jobs remain eligible under production policy.
Private reports and credentials references stay in ignored data/evaluations.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.calendar import market_state
from app.domain import now
from app.runtime_version import source_digest
from scripts.verify_fourteenth_live import configure_isolated
from scripts.verify_live_chain import all_events


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    path.chmod(0o600)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def observations(store):
    with store.connect() as db:
        jobs = [dict(r) for r in db.execute("SELECT id,account_id,input_version,state,reason,attempts,json_extract(result,'$.delivery.error_code') AS error_code,json_extract(task_metadata,'$.cancellation.code') AS cancellation_code,due_at FROM jobs ORDER BY created_at")]
        runs = [dict(r) for r in db.execute('SELECT id,state,kind,reason,attempts,error FROM global_runs ORDER BY created_at')]
        reports = [dict(r) for r in db.execute('SELECT id,account_id,job_id,input_version,status,report,result FROM analyses ORDER BY created_at')]
        forecasts = [dict(r) for r in db.execute('SELECT id,job_id,symbol,horizon,direction,kind,published_at,target_date FROM forecasts')]
        demands = [dict(r) for r in db.execute('SELECT id,account_id,kind,state,job_id,detail FROM research_demands')]
    requests = []
    for job in jobs + runs:
        for event in all_events(store, job['id']):
            for request in (event.get('payload') or {}).get('requests', []):
                requests.append({'job_id': job['id'], 'event_seq': event['seq'], **request})
    for report in reports:
        report['report_sha256'] = sha(report.pop('report'))
        result = json.loads(report.pop('result'))
        report['delivery'] = result.get('delivery')
    return {'jobs': jobs, 'global_runs': runs, 'reports': reports, 'forecasts': forecasts,
            'demands': demands, 'requests': requests,
            'http_attempts': len(requests),
            'completed_with_eof': sum(r.get('http_body_complete') is True and
                (r.get('response_status') == 'completed' or r.get('stream_terminal') == 'response.completed') for r in requests),
            'known_fee': None, 'fee_source': 'No invoice or supplier billing correlation performed'}


def pending_work(snapshot):
    """Report blocked work without failing it or swallowing ordinary new events."""
    jobs, runs = snapshot['jobs'], snapshot['global_runs']
    busy = [item for item in jobs + runs if item['state'] in ('queued', 'running')]
    blocked = [item for item in jobs + runs if item['state'] in ('waiting_config', 'waiting_scope')]
    blocked_accounts = {item['account_id'] for item in jobs if item in blocked}
    global_blocked = any(item in blocked for item in runs)
    pending, deferred = [], []
    for demand in snapshot['demands']:
        if demand['state'] != 'pending':
            continue
        target = demand.get('account_id')
        (deferred if target in blocked_accounts or (not target and global_blocked) else pending).append(demand)
    return {'busy': busy, 'pending': pending, 'blocked': blocked, 'deferred': deferred}


def event_outcomes(snapshot):
    """Account publication cannot hide failure of another legitimate event."""
    tasks = snapshot['jobs'] + snapshot['global_runs']
    failed = [item for item in tasks if item['state'] == 'failed']
    unresolved = [item for item in tasks if item['state'] not in ('completed', 'superseded', 'failed')]
    failed_demands = [item for item in snapshot['demands'] if item['state'] == 'failed']
    work = pending_work(snapshot)
    return {'status': 'fail' if failed or failed_demands else 'incomplete' if
            unresolved or work['pending'] or work['deferred'] else 'pass',
            'failed_tasks': failed, 'failed_demands': failed_demands, 'unresolved_tasks': unresolved,
            'superseded_tasks': [item for item in tasks if item['state'] == 'superseded'],
            'boundary': 'Superseded tasks remain visible with their real input/replacement reason; this is event processing, not uninterrupted request success.'}


async def run(args):
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / 'data/evaluations').resolve()):
        raise ValueError('Output must be a new private data/evaluations directory')
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    store, config = configure_isolated(args.source.resolve(), out / 'evaluation.sqlite3', args.reference_account)
    # Latest user authorization limits validation effort to medium/high/xhigh.
    # Preserve every other effective binding, and do not alter the daily source.
    with store.connect() as db:
        row = db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()
        role = json.loads(row[0])
        old_effort = role['default_binding']['reasoning_effort']
        role['default_binding']['reasoning_effort'] = args.manager_effort
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (json.dumps(role, ensure_ascii=False),))
    config.update(scheduled_research_enabled=False, market_refresh_enabled=True,
                  forecast_evaluation_enabled=True, debounce_seconds=30,
                  scan_interval_minutes=240, analysis_interval_minutes=1440,
                  news_queries=[], model_network_route=args.route)
    store.set_meta('settings', config)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'APP_DB_PATH': str(store.path), 'APP_RUNTIME_DIR': str(out),
           'APP_MODE': 'live', 'APP_HOST': '127.0.0.1', 'APP_PORT': str(port)}
    summary = {'kind': 'sixteenth_normal_start_real_matrix', 'started_at': now(),
        'source_sha256': source_digest(), 'baseline_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'market_state_at_start': market_state(), 'route': args.route,
        'manager_effort': {'source': old_effort, 'isolated': args.manager_effort},
        'per_job_call_limit': config['max_model_calls'], 'max_started_tasks': args.max_started_tasks,
        'execution_wall_limit_seconds': args.wall_seconds,
        'planned': ['A three production simulated buys', 'A one idempotent simulated sell',
                    'B cash plus 159278 and legitimate A/global universe-change jobs'],
        'attempt_policy': 'No automatic business retry. Production bounded explicit-rejection retries retained; accepted unknown outcomes not replayed.',
        'formal_natural_generation': {'status': 'not_executed',
            'reason': 'This business matrix leaves isolated formal settings disabled; actual market time is recorded separately.'},
        'natural_maturity': 'not_yet_observed',
        'stages': [], 'trades': [], 'port': port, 'daily_database_writes': 0}
    started = False
    deadline = time.monotonic() + args.wall_seconds

    def save():
        summary['observations'] = observations(store)
        summary['source_unchanged'] = source_digest() == summary['source_sha256']
        write(out / 'summary.json', summary)

    async def manage(action):
        proc = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'scripts/manage.py'), action,
            '--mode', 'live', '--port', str(port), cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await proc.communicate()
        with (out / 'lifecycle.log').open('a') as log:
            log.write(now() + ' ' + action + '\n' + stdout.decode())
        if proc.returncode:
            raise RuntimeError('manage.py ' + action + ' failed; see private lifecycle.log')

    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=90) as api:
        async def request(method, path, **kw):
            response = await api.request(method, path, **kw)
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                write(out / 'api-failure.json', {'method': method, 'path': path, 'status': response.status_code,
                    'body': body})
                raise RuntimeError(f'{method} {path}: {response.status_code}')
            return response.json()

        def check_budget():
            snap = observations(store)
            started_tasks = sum(j['attempts'] > 0 for j in snap['jobs'] + snap['global_runs'])
            if time.monotonic() >= deadline or started_tasks > args.max_started_tasks:
                raise RuntimeError('Declared wall/task observation budget reached; remaining work preserved by normal shutdown')
            if source_digest() != summary['source_sha256']:
                raise RuntimeError('Source changed during fixed-source run')
            return snap

        async def settle(label, account_id=None):
            begin = time.monotonic()
            quiet_since = None
            while True:
                snap = check_budget()
                work = pending_work(snap)
                if not work['busy'] and not work['pending']:
                    quiet_since = quiet_since or time.monotonic()
                    if time.monotonic() - quiet_since >= 5:
                        break
                else:
                    quiet_since = None
                save()
                await asyncio.sleep(2)
            stage = {'label': label, 'at': now(), 'duration_seconds': round(time.monotonic()-begin, 3),
                     'account_id': account_id, 'observations': snap,
                     'remaining_work': work,
                     'settled_status': 'blocked_or_deferred' if work['blocked'] or work['deferred'] else 'quiescent'}
            if account_id:
                stage['account'] = await request('GET', '/api/accounts/' + account_id)
                stage['ledger'] = await request('GET', f'/api/accounts/{account_id}/ledger')
                stage['reports'] = await request('GET', f'/api/accounts/{account_id}/analyses')
                current = [report for report in stage['reports'] if report['status'] == 'current'
                           and report['input_version'] == stage['account']['version']]
                stage['publication'] = {'status': 'pass' if len(current) == 1 and
                    current[0]['result'].get('delivery', {}).get('contract_verified') is True else 'fail',
                    'current_version': stage['account']['version'], 'current_report_ids': [r['id'] for r in current],
                    'content_assessment': 'not_evaluated'}
                for report in stage['reports']:
                    report_path = out / (label + '-' + report['id'] + '-original.md')
                    report_path.write_bytes(report['report'].encode('utf-8'))
                    report_path.chmod(0o600)
            summary['stages'].append(stage)
            save()
            print(json.dumps({'stage': label, 'http': snap['http_attempts'], 'reports': len(snap['reports']),
                              'jobs': [(j['state'], j['reason']) for j in snap['jobs']]}, ensure_ascii=False), flush=True)
            return stage

        async def trade(account_id, values, operation):
            base = f'/api/accounts/{account_id}'
            account = await request('GET', base)
            fees = await request('GET', base + '/fee-plan')
            payload = {**values, 'operation_id': operation, 'expected_version': account['version'],
                'fee_plan_version': fees['version'], 'mode': 'simulated', 'traded_at': now(),
                'note': '第十六轮独立验收模拟算术成交，不是市场报价或券商成交'}
            preview = await request('POST', base + '/trades/preview', json=payload)
            receipt = await request('POST', base + '/trades', json=payload)
            replay = await request('POST', base + '/trades', json=payload)
            assert replay.get('replayed') is True
            assert {k: v for k, v in receipt.items() if k != 'replayed'} == {k: v for k, v in replay.items() if k != 'replayed'}
            assert preview['cash_after'] == receipt['cash_after']
            summary['trades'].append({'input': payload, 'receipt': receipt, 'idempotent_replay_equal': True})
            save()

        try:
            await manage('start')
            started = True
            summary['health'] = await request('GET', '/api/health')
            assert summary['health']['runtime_version']['source_sha256_at_start'] == summary['source_sha256']
            summary['symbol_resolution'] = await request('POST', '/api/securities/resolve',
                json={'symbols': ['518880', '002354', '159599', '159278']})
            if not summary['symbol_resolution']['ok']:
                raise RuntimeError('Real security identity unresolved')
            # Public API choice is persisted/read back; no custom client transport.
            settings = await request('PUT', '/api/settings', json={'model_network_route': args.route})
            summary['network_setting'] = {k: settings.get(k) for k in ('model_network_route', 'model_network_revision', 'max_output_tokens')}
            a = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'sixteenth-create-a'}, json={
                'name': '第十六轮独立模拟A', 'cash': '100000', 'holdings': [],
                'preferences': {'horizon': '关注518880、002354、159599；结合真实资料分析仓位现金、逐项动作、反向因素和再评估条件。成交为独立模拟输入；建议不执行交易。'}})
            summary['a_id'] = a['id']
            for i, (symbol, quantity, price) in enumerate((('SH:518880', '200', '10'), ('SZ:002354', '1000', '10'), ('SZ:159599', '1000', '2.5'))):
                await trade(a['id'], {'symbol': symbol, 'side': 'buy', 'quantity': quantity, 'price': price}, f'sixteenth-buy-{i}')
            await request('PUT', '/api/settings', json={'debounce_seconds': 0, 'scheduled_research_enabled': True})
            await settle('a_initial', a['id'])
            await trade(a['id'], {'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '10'}, 'sixteenth-sell-once')
            await settle('a_after_sell', a['id'])
            # Build B before changing the shared universe: both accounts must
            # follow ordinary input invalidation and public-material routing.
            b = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'sixteenth-create-b'}, json={
                'name': '第十六轮独立现金B', 'cash': '100000', 'holdings': [],
                'preferences': {'horizon': '只考虑159278是否适配本账户；自主决定是否研究/派发。给出持币或投入观察条件、反向因素与再评估条件。不得借用其他账户结论。'}})
            summary['b_id'] = b['id']
            await request('PUT', '/api/universe', json={'watchlist': [], 'candidates': ['159278']})
            await settle('b_cash_and_universe_events', b['id'])
            # Pause new scheduled research through the same user-facing API,
            # keep due scoring enabled, then verify durable publication restart.
            await request('PUT', '/api/settings', json={'scheduled_research_enabled': False})
            # Saving runtime settings is itself a legitimate account demand.
            # Drain it before the restart baseline; never mask or reject it.
            await settle('after_pause_configuration_events')
            before = observations(store)
            await manage('stop')
            started = False
            await manage('start')
            started = True
            summary['restart_health'] = await request('GET', '/api/health')
            after = await settle('after_restart')
            summary['restart_preserved_reports'] = before['reports'] == after['observations']['reports']
            summary['restart_preserved_forecasts'] = before['forecasts'] == after['observations']['forecasts']
            summary['continuous_events'] = event_outcomes(after['observations'])
            required_stages = [stage for stage in summary['stages'] if stage.get('account_id')]
            summary['execution_result'] = ('matrix_published_content_review_pending' if
                len(required_stages) == 3 and summary['continuous_events']['status'] == 'pass' and
                all(stage.get('publication', {}).get('status') == 'pass' and stage['settled_status'] == 'quiescent'
                    for stage in required_stages) and summary['restart_preserved_reports'] and
                summary['restart_preserved_forecasts'] else 'matrix_incomplete_content_review_pending')
        except Exception as exc:
            summary['execution_result'] = 'stopped_with_failure'
            summary['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
            raise
        finally:
            if started:
                try:
                    await manage('stop')
                except Exception as exc:
                    summary['shutdown_failure'] = {'type': type(exc).__name__, 'message': str(exc)}
            summary['finished_at'] = now()
            save()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--reference-account', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--route', choices=['environment', 'direct'], default='environment')
    p.add_argument('--manager-effort', choices=['medium', 'high', 'xhigh'], default='xhigh')
    p.add_argument('--max-started-tasks', type=int, default=12)
    p.add_argument('--wall-seconds', type=int, default=14400)
    p.add_argument('--run-real', action='store_true')
    args = p.parse_args()
    if not args.run_real:
        p.error('Use --run-real only with authorization for the declared paid matrix')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
