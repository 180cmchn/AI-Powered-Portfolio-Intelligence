#!/usr/bin/env python3
"""Opt-in ONE real research round after three isolated simulated buys.

Writing/importing/--help never calls a model. --run-real is mandatory for execution.
The daily database is opened mode=ro only. The preparation server has no worker.
The only manually released task is the latest account version. Production
automatic follow-ups are recorded, never hidden or retried to manufacture success.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from decimal import Decimal
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
from app.domain import now
from app.runtime_version import source_digest, env_file_stamp
from scripts.verify_eleventh_live import configure
from scripts.verify_live_chain import all_events, scrubbed

ACTIVE = {'queued', 'running', 'pending'}
INPUTS = [
    {'symbol': 'SH:518880', 'quantity': '100', 'price': '10.000'},
    {'symbol': 'SZ:002354', 'quantity': '1000', 'price': '10.00'},
    {'symbol': 'SZ:159599', 'quantity': '1000', 'price': '2.500'},
]


def serve_child(database: Path, descriptor: int, worker: bool):
    """Use a parent-reserved random socket, avoiding any 8765 bind or port race."""
    with socket.socket(fileno=descriptor) as listener:
        os.environ.update(APP_DB_PATH=str(database), APP_MODE='live', APP_HOST='127.0.0.1',
                          APP_PORT=str(listener.getsockname()[1]))
        from app.main import create_app
        import uvicorn
        application = create_app(database, mode='live', worker=worker)
        uvicorn.Server(uvicorn.Config(application, access_log=False, log_level='info')).run(sockets=[listener])


class Instance:
    def __init__(self, database, output, name, worker):
        self.database, self.output, self.name, self.worker = database, output, name, worker
        self.process = self.log = self.api = None

    async def start(self):
        self.log = (self.output / (self.name + '.log')).open('w')
        os.chmod(self.log.name, 0o600)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            if port == 8765:
                raise RuntimeError('Random port unexpectedly matched daily port; refuse to launch')
            args = [sys.executable, str(getattr(self, 'child_script', Path(__file__).resolve())), '--serve-child', str(self.database),
                    '--listen-fd', str(listener.fileno())]
            if self.worker:
                args.append('--worker')
            self.process = subprocess.Popen(args, cwd=ROOT, pass_fds=(listener.fileno(),),
                env={**os.environ, 'APP_DB_PATH': str(self.database), 'APP_MODE': 'live',
                     'APP_HOST': '127.0.0.1', 'APP_PORT': str(port)}, stdout=self.log, stderr=self.log)
        self.base = f'http://127.0.0.1:{port}'
        self.api = httpx.AsyncClient(base_url=self.base, timeout=45, trust_env=False)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError(f'{self.name} process exited before readiness')
            try:
                health = await self.request('GET', '/api/health')
                if health['ready']:
                    return {'name': self.name, 'url': self.base, 'pid': self.process.pid,
                            'worker': self.worker, 'health': health}
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        raise RuntimeError(f'{self.name} process did not become ready')

    async def request(self, method, path, **kwargs):
        result = await self.api.request(method, path, **kwargs)
        result.raise_for_status()
        return result.json()

    async def stop(self):
        if self.api:
            await self.api.aclose()
            self.api = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)
        if self.log:
            self.log.close()
            self.log = None


def requests_for(store, task_id):
    events = all_events(store, task_id)
    requests = [{**item, 'event_id': event.get('id'), 'event_at': event.get('timestamp', event.get('at')),
                 'event_type': event.get('type')}
                for event in events for item in (event.get('payload') or {}).get('requests', [])]
    return events, requests


def task_audit(store):
    with store.connect() as db:
        tasks = [{**dict(row), 'scope': 'account'} for row in db.execute(
            'SELECT id,account_id,state,stage,input_version,attempts,error,reason,created_at,completed_at FROM jobs ORDER BY created_at')]
        tasks += [{**dict(row), 'scope': 'global'} for row in db.execute(
            'SELECT id,state,attempts,error,kind,created_at FROM global_runs ORDER BY created_at')]
    audits = []
    for task in tasks:
        events, requests = requests_for(store, task['id'])
        # Tool/network diagnostics are already safe production event payloads;
        # hidden reasoning and raw HTTP request/response bodies are never read.
        network = [{'id': event.get('id'), 'type': event.get('type'), 'payload': event.get('payload')}
                   for event in events if event.get('type') in {
                       'tool_started', 'tool_completed', 'tool_failed', 'model_error', 'model_timeout'}]
        audits.append({'task': task, 'requests': requests,
                       'event_counts': dict(Counter(event['type'] for event in events)),
                       'tool_network_events': network})
    return audits


async def run(args):
    output = args.output.resolve()
    allowed = (ROOT / 'data/evaluations').resolve()
    if not output.is_relative_to(allowed) or output == allowed:
        raise ValueError('--output must be a NEW child directory of this repository data/evaluations')
    if not args.source.is_file():
        raise ValueError('Source database does not exist')
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    summary = {'kind': 'twelfth_single_real_research_with_isolated_simulated_ledger', 'started_at': now(),
        'planned_business_jobs': 1, 'manual_business_retries': 0, 'second_sell_round': False,
        'model_probe_calls': 0, 'daily_access': 'SQLite mode=ro configuration only; no daily service or data writes',
        'preparation_gate': 'worker=False until all three preview/confirm API operations are reconciled; one latest task released',
        'input_label': 'Explicit virtual arithmetic quantities/prices, not live quotes or real broker fills',
        'test_inputs': INPUTS, 'initial_cash': '100000', 'expected_cash_after_buys': '86484.90',
        'market_state': market_state(), 'clock': 'Actual wall clock; no historical or market-window shift',
        'prediction_note': 'No forced forecasts, directions or participation; ordinary production rules apply',
        'instances': [], 'trades': [], 'observations': [], 'preflight': {},
        'fees': 'Provider invoice costs unknown; returned usage is not invoice reconciliation',
        'network_evidence_boundary': 'All production model attempt diagnostics and available tool events; no claim of packet-level accounting for uninstrumented public HTTP subrequests'}
    store = None
    cfg = {}
    instance = None
    expected_bindings = {}
    digest, environment = source_digest(), env_file_stamp()
    summary.update(source_sha256=digest, commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), timezone='Asia/Shanghai')

    def save():
        path = output / 'summary.json'
        path.write_text(scrubbed(summary, cfg) + '\n', encoding='utf-8')
        path.chmod(0o600)

    def same_source():
        if source_digest() != digest:
            raise RuntimeError('Source changed during acceptance; stop instead of validating mixed code')

    async def start(name, worker):
        nonlocal instance
        same_source()
        instance = Instance(store.path, output, name, worker)
        evidence = await instance.start()
        summary['instances'].append(evidence)
        if evidence['health']['runtime_version']['source_sha256_at_start'] != digest:
            raise RuntimeError('Isolated service startup digest differs from frozen code')
        save()
        print(json.dumps({'instance': name, 'url': instance.base, 'worker': worker,
                          'account_id': summary.get('account_id')}, ensure_ascii=False), flush=True)

    def collect_observation(phase, index):
        account_id, task_id = summary['account_id'], summary['released_job_id']
        job = store.job(task_id)
        recommendations = store.recommendations_page(account_id, view='active', limit=100)['items']
        audit = task_audit(store)
        extra = [entry['task'] for entry in audit if entry['task']['id'] != task_id and
                 (entry['task'].get('attempts', 0) > 0 or entry['requests'] or entry['task']['state'] in ACTIVE)]
        observation = {'at': now(), 'phase': phase, 'index': index,
            'job_state': job['state'], 'report_id': job.get('report_id'), 'report_status': job.get('report_status'),
            'account_version': store.account(account_id)['version'],
            'recommendations': [{key: item.get(key) for key in
                ('id', 'security_id', 'price_state', 'review_status', 'review_reason', 'price_conditions', 'review_followup')}
                for item in recommendations],
            'quotes': store.cache('quotes'), 'demands': store.demands(account_id, limit=100),
            'additional_tasks': extra}
        summary['observations'].append(observation)
        save()
        if extra:
            raise RuntimeError('Additional automatic task observed; preserve it in audit and stop this single-round acceptance')
        return observation

    try:
        store, cfg = configure(args.source, output / 'evaluation.sqlite3', args.reference_account)
        # configure reuses the authorized credential references. Keep the source
        # revocation markers too; copying references must never undo revocation.
        with sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro', uri=True) as origin, store.connect() as dest:
            for key, value in origin.execute("SELECT key,value FROM meta WHERE key LIKE 'revoked_provider_credentials:%'"):
                dest.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key,value))
        team = store.freeze_agent_team()
        expected_bindings = {role_id: {key: role['resolved'].get(key) for key in
            ('provider_id', 'model_id', 'reasoning_effort')} for role_id, role in team['roles'].items()}
        summary['preflight']['bindings'] = expected_bindings
        summary['preflight']['role_errors'] = {key: role.get('config_error') for key, role in team['roles'].items()}
        # This resolves only this project's authorized reference; it performs no
        # model capability probe, external login or credential discovery.
        runtime_config = store.runtime_role_config(team, 'manager', cfg)
        if not runtime_config.get('model_name'):
            raise RuntimeError('Current manager configuration has no model; no fallback model is selected')
        runtime_config.pop('_model_api_key', None)
        del runtime_config
        summary['preflight']['manager_configured'] = True
        summary['limits'] = {key: cfg.get(key) for key in (
            'model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds',
            'model_request_hard_timeout_seconds', 'synthesis_request_hard_timeout_seconds',
            'agent_timeout_seconds', 'analysis_timeout_seconds', 'max_model_calls', 'model_retries')}
        summary['isolation_settings'] = {key: cfg.get(key) for key in (
            'scheduled_research_enabled', 'market_refresh_enabled', 'forecast_evaluation_enabled',
            'news_queries', 'max_job_attempts')}
        await start('prepare-no-worker', False)
        summary['symbol_resolution'] = await instance.request('POST', '/api/securities/resolve',
            json={'symbols': ['518880', '002354', '159599'], 'context': 'stock_etf'})
        if not summary['symbol_resolution']['ok']:
            raise RuntimeError('Actual directory could not resolve all securities; no fabricated identity fallback')
        account = await instance.request('POST', '/api/accounts', headers={'X-Operation-ID': getattr(args, 'operation_prefix', 'twelfth') + '-create-a'}, json={
            'name': getattr(args, 'iteration_label', '第十二轮') + '单次真实研究（隔离虚拟账户）', 'cash': '100000', 'holdings': [],
            'preferences': {'risk': 'balanced', 'horizon': '三证券数量价格为明确的虚拟算术输入。研究趋势、现金与集中度、费用影响、已知事实及改变观点的条件。允许证据不足或弃权，不要求强行发布涨跌预测；模型建议不是交易。'}})
        summary['account_id'] = account['id']
        print(json.dumps({'prepared_url': instance.base+'/#holdings', 'account_id': account['id']}, ensure_ascii=False), flush=True)
        for index, values in enumerate(INPUTS):
            current = await instance.request('GET', '/api/accounts/'+account['id'])
            plan = await instance.request('GET', f"/api/accounts/{account['id']}/fee-plan")
            payload = {**values, 'side': 'buy', 'operation_id': getattr(args, 'operation_prefix', 'twelfth') + f'-initial-buy-{index}',
                'expected_version': current['version'], 'fee_plan_version': plan['version'], 'traded_at': now(),
                'mode': 'simulated', 'note': getattr(args, 'iteration_label', '第十二轮') + '隔离虚拟算术输入；不是当前行情或实际券商成交'}
            preview = await instance.request('POST', f"/api/accounts/{account['id']}/trades/preview", json=payload)
            receipt = await instance.request('POST', f"/api/accounts/{account['id']}/trades", json=payload)
            summary['trades'].append({'input': payload, 'preview': preview, 'receipt': receipt})
            if preview['cash_after'] != receipt['cash_after']:
                raise RuntimeError('Preview and receipt cash differ')
            save()
        ledger = await instance.request('GET', f"/api/accounts/{account['id']}/ledger")
        if ledger['cash'] != '86484.90':
            raise RuntimeError('Three-buy ledger did not reconcile to 86484.90; do not start model')
        if {row['symbol']: row['quantity'] for row in ledger['positions']} != {row['symbol']: row['quantity'] for row in INPUTS}:
            raise RuntimeError('Ledger quantities do not match the explicit virtual inputs')
        expected_costs = {'SH:518880': Decimal('1005.00'), 'SZ:002354': Decimal('10005.10'), 'SZ:159599': Decimal('2505.00')}
        if {row['symbol']: Decimal(row['total_cost']) for row in ledger['positions']} != expected_costs:
            raise RuntimeError('Exact position costs do not match gross plus recorded buy fees')
        if sum(Decimal(row['total_cost']) for row in ledger['positions']) != Decimal('13515.10') or Decimal(ledger['summary']['total_fees']) != Decimal('15.10') or Decimal(ledger['summary']['realized_pnl']) != 0:
            raise RuntimeError('Exact total cost, cumulative fees or realized P&L did not reconcile')
        entries = {row['id']: row for row in ledger['entries']}
        if len(entries) != 4 or len(ledger['cash_entries']) != 4:
            raise RuntimeError('Expected exactly one opening plus three trades and cash entries')
        for trade in summary['trades']:
            receipt, values = trade['receipt'], trade['input']
            if entries.get(receipt['entry_id']) != receipt['entry']:
                raise RuntimeError('Persisted entry differs from the actual API receipt')
            position = next(row for row in ledger['positions'] if row['symbol'] == values['symbol'])
            recorded = receipt['entry']['after_positions'][values['symbol']]
            if position['lots'] != recorded['lots'] or len(position['lots']) != 1 or position['lots'][0]['source'] != 'trade' or position['lots'][0]['quantity'] != values['quantity'] or not position['lots'][0]['sellable_on'] or not position['fees_included_in_cost']:
                raise RuntimeError('Original trade lot, sellable date or included-fee basis differs')
        summary['accounting_preflight'] = {'cash': '86484.90', 'total_cost': '13515.10', 'fees': '15.10',
            'realized_pnl': '0', 'ledger_entries': 4, 'cash_entries': 4, 'receipts_and_lots_exact': True}
        summary['prepared_ledger'] = ledger
        latest_id = summary['trades'][-1]['receipt']['research']['job_id']
        with store.connect() as db:
            if db.execute('SELECT count(*) FROM jobs WHERE attempts>0').fetchone()[0]:
                raise RuntimeError('Unexpected research attempt during worker-free preparation')
            active = [row['id'] for row in db.execute("SELECT id FROM jobs WHERE state IN ('queued','running','pending')")]
            if active != [latest_id]:
                raise RuntimeError('Preparation has unexpected runnable tasks; refuse to release more than the latest version')
        await instance.stop()
        cfg['debounce_seconds'] = 4
        store.set_meta('settings', cfg)
        with store.connect() as db:
            db.execute("UPDATE jobs SET due_at=? WHERE id=? AND state='queued'", (now(), latest_id))
        summary.update(released_job_id=latest_id, preparation_gate_released_at=now())
        await start('research-worker', True)
        last = None
        async with asyncio.timeout(float(cfg['analysis_timeout_seconds'])+120):
            while True:
                same_source()
                job = await instance.request('GET', '/api/jobs/'+latest_id)
                status = (job['state'], job.get('stage'))
                if status != last:
                    print(json.dumps({'job_id': latest_id, 'state': status, 'at': now()}, ensure_ascii=False), flush=True)
                    last = status
                if job['state'] not in ACTIVE:
                    break
                await asyncio.sleep(2)
        summary['initial_job'] = job
        report = (job.get('result') or {}).get('report')
        summary['report_published'] = job['state']=='completed' and bool(job.get('report_id')) and bool(report)
        if report:
            report_path = output / 'initial-report.md'
            report_path.write_text(report, encoding='utf-8')
            report_path.chmod(0o600)
        save()
        if not summary['report_published']:
            raise RuntimeError('Single real research did not publish a complete report; no success retry will be made')
        for index in range(10):
            await asyncio.sleep(1.1)
            same_source()
            await instance.request('GET', '/api/status')
            collect_observation('after_publish', index+1)
        # Same final code, same persisted database, a genuinely new process and
        # normal Service.start/tick. Do not mutate report/recommendation history.
        await instance.stop()
        await start('restart-observer-worker', True)
        for index in range(10):
            await asyncio.sleep(1.1)
            same_source()
            await instance.request('GET', '/api/status')
            collect_observation('after_restart', index+1)
        summary['final_ledger'] = await instance.request('GET', f"/api/accounts/{account['id']}/ledger")
        summary['report_current_after_observation'] = all(item['report_status']=='current' for item in summary['observations'])
        summary['old_quote_baseline_observed'] = any(item['price_state']=='stale' and item['price_conditions']
            for snapshot in summary['observations'] for item in snapshot['recommendations'])
        summary['old_quote_evidence_note'] = ('Natural real recommendations observed with stale price conditions; inspect persisted demands and before/after report status'
            if summary['old_quote_baseline_observed'] else 'Not exercised by this real report; controlled production-path regression remains separate')
        summary['completed_at'] = now()
    except Exception as exc:
        summary['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        print(json.dumps({'error': summary['error'], 'no_business_retry': True}, ensure_ascii=False), flush=True)
    finally:
        if instance:
            await instance.stop()
        audits = task_audit(store) if store else []
        summary['all_attempts_after_shutdown'] = audits
        requests = [request for task in audits for request in task['requests']]
        summary['total_http_attempts'] = len(requests)
        summary['business_jobs_with_http'] = sum(bool(task['requests']) for task in audits)
        summary['automatic_tasks'] = [task['task'] for task in audits if task['task']['id']!=summary.get('released_job_id') and
                                      (task['requests'] or task['task'].get('attempts',0)>0 or task['task']['state'] in ACTIVE)]
        summary['complete_stream_terminals'] = sum(request.get('stream_terminal')=='response.completed' and not request.get('error') for request in requests)
        summary['terminal_counts'] = dict(Counter(request.get('stream_terminal') or 'no_stream_terminal_recorded' for request in requests))
        summary['failure_counts'] = dict(Counter(request.get('error_code') or request.get('error') or 'no_error_recorded' for request in requests))
        summary['usage_reported_requests'] = sum(request.get('usage') is not None for request in requests)
        if store and summary.get('released_job_id'):
            final_job = store.job(summary['released_job_id'])
            summary['final_released_job'] = final_job
            summary['final_reports'] = store.analyses(summary['account_id'], limit=100)
            summary['final_report_current'] = final_job['state']=='completed' and final_job.get('report_status')=='current'
        summary['binding_mismatches'] = [{key: request.get(key) for key in ('role_id','model','reasoning_effort_sent','provider_id')}
            for request in requests if request.get('role_id') not in expected_bindings or
            any(request.get(observed)!=expected_bindings.get(request.get('role_id'),{}).get(expected)
                for observed,expected in [('model','model_id'),('reasoning_effort_sent','reasoning_effort'),('provider_id','provider_id')])]
        summary['final_source_unchanged'] = source_digest()==digest
        summary['final_environment_unchanged'] = env_file_stamp()==environment
        summary['audit_completed_at'] = now()
        summary['single_report_acceptance_passed'] = bool(not summary.get('error') and summary.get('report_published') and
            summary.get('report_current_after_observation') and summary.get('final_report_current') and len(summary['observations'])==20 and
            summary['business_jobs_with_http']==1 and not summary['automatic_tasks'] and not summary['binding_mismatches'] and
            summary['final_source_unchanged'] and summary['final_environment_unchanged'])
        save()
    return 0 if summary['single_report_acceptance_passed'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'data/live.sqlite3')
    parser.add_argument('--reference-account', help='Existing authorized account whose effective role bindings are copied read-only')
    parser.add_argument('--output', type=Path, help='New directory below data/evaluations')
    parser.add_argument('--run-real', action='store_true', help='Explicitly enable this one optional real research; may incur provider charges')
    parser.add_argument('--serve-child', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--listen-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_child:
        if args.listen_fd is None:
            parser.error('Internal child mode needs a reserved listener descriptor')
        serve_child(args.serve_child, args.listen_fd, args.worker)
    else:
        if not args.run_real or not args.reference_account or not args.output:
            parser.error('Use --run-real --reference-account ID --output data/evaluations/NEW-DIRECTORY only after engineering regressions pass')
        raise SystemExit(asyncio.run(run(args)))
