#!/usr/bin/env python3
"""Opt-in real native-search + HTTP/worker evaluation in a new temporary DB.

No live DB mutation, no pre-supplied report URL, no scripted model responses.
Four required rounds, bounded probe first. Nonzero exits preserve actual failures.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.config import db_path
from app.domain import TZ, now
from app.model import probe_native_search
from app.store import Store
from scripts.verify_live_chain import configuration, public_config, observation, write_json


def code_identity():
    files = sorted([*ROOT.joinpath('app').rglob('*.py'), *ROOT.joinpath('app/prompts').glob('*.txt'),
                    *ROOT.joinpath('app/static').glob('*'), Path(__file__), ROOT/'scripts/inspect_fourth_browser.mjs'])
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'worktree_status': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True).strip(),
            'application_sha256': digest.hexdigest(), 'python': sys.version,
            'dependencies': {name: importlib.metadata.version(name) for name in ['fastapi', 'uvicorn', 'httpx', 'pydantic', 'pypdf']}}


def scheduled_news_demands(store, run_id):
    # Source discovery can create more than one page of newer demands. Audit the
    # exact run's original scheduler demand rather than only the latest UI page.
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM research_demands WHERE kind='news_observation' AND job_id=? ORDER BY id", (run_id,))]


async def verify(args):
    config, origin = configuration(args.settings_db)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    summary = {'test_kind': 'real_native_model_network_http_background_worker', 'started_at': now(),
               'code': code_identity(), 'configuration': public_config(config, origin),
               'steps': {}, 'rounds': [], 'quality': 'pending_evidence_review', 'output_directory': str(output)}
    def save():
        write_json(output / 'summary.json', summary, config)
    save()
    # Fail before the expensive four-round chain when hosted search is not observed.
    probe = await probe_native_search(config)
    summary['native_probe'] = probe
    save()
    if probe['status'] != 'observed':
        summary['blocked'] = 'native_search_not_observed'; save(); return 1
    config = {**config, 'model_native_search': True, 'model_retries': 1,
              'max_model_calls': 30, 'max_agent_turns': 18, 'max_total_agents': 4,
              'max_concurrent_agents': 2, 'analysis_timeout_seconds': args.timeout,
              'agent_timeout_seconds': 240, 'max_output_tokens': 3000,
              'debounce_seconds': 0, 'max_job_attempts': 2,
              'quote_cache_seconds': 300, 'read_max_bytes': 15000000,
              'news_queries': ['贵州茅台600519和平安银行000001最近一期公开业绩：每家公司只核对营业收入、归母净利润两项，含单位和报告期。把一家公司的资料核对交给独立研究子Agent，主Agent研究另一家并整合，可自主补查。不预给URL，使用本模型原生搜索发现资料。不延伸全行业；证据足够后 prepare_synthesis，提交简洁公共报告与出处，如实说明链接或片段范围，并保留汇总预算。'],
              'news_interval_minutes': 1440, 'news_start_hour': 0, 'news_end_hour': 24,
              'scheduled_research_enabled': True}
    store = Store(output / 'evaluation.sqlite3')
    store.set_meta('settings', config)
    store.set_meta('native_search_capability', probe)
    # Establish initial scope without creating account-dependent work.
    store.set_meta('universe', {'watchlist': ['SH:600519', 'SZ:000001'], 'candidates': ['SH:600519', 'SZ:000001']})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    env = {**os.environ, 'APP_DB_PATH': str(store.path), 'APP_MODE': 'live', 'APP_HOST': '127.0.0.1', 'APP_PORT': str(port)}
    log = open(output / 'server.log', 'w')
    process = subprocess.Popen([sys.executable, 'scripts/serve.py'], cwd=ROOT, env=env, stdout=log, stderr=log)
    summary['server'] = {'pid': process.pid, 'port': port, 'normal_worker': True}
    save()
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=60, trust_env=False) as http:
        async def request(method, path, payload=None):
            response = await http.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()
        async def wait_account(account_id):
            deadline = time.monotonic() + args.timeout + 90
            while time.monotonic() < deadline:
                account = await request('GET', f'/api/accounts/{account_id}')
                job = await request('GET', f"/api/jobs/{account['latest_job_id']}")
                if job['state'] in ('completed', 'failed', 'waiting_config', 'cancelled', 'superseded'):
                    return job
                await asyncio.sleep(1)
            raise TimeoutError('account worker deadline')
        try:
            for _ in range(120):
                try:
                    status = await request('GET', '/api/status')
                    if status['worker']['running']:
                        break
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(.25)
            else:
                raise TimeoutError('worker startup')
            deadline = time.monotonic() + args.timeout + 60
            run = None
            while time.monotonic() < deadline:
                runs = await request('GET', '/api/global-research')
                if runs:
                    run = next((item for item in runs if item['id'] == run['id']), run) if run else min(runs, key=lambda item: item['created_at'])
                    if run['state'] in ('completed', 'failed', 'waiting_config', 'superseded'):
                        break
                await asyncio.sleep(1)
            if not run:
                raise TimeoutError('scheduled public research not started')
            summary['steps']['scheduler_news_demand'] = scheduled_news_demands(store, run['id'])
            obs = observation(store, store.global_run(run['id']), True)
            summary['rounds'].append(obs); write_json(output/'round-1-public.json', obs, config); save()
            print(json.dumps({'stage': 'public', 'state': obs['state'], 'output': str(output)}), flush=True)
            # Pause periodic studies only; normal durable worker & account saves stay active.
            cfg = await request('GET', '/api/settings')
            cfg.update(scheduled_research_enabled=False, news_queries=[])
            await request('PUT', '/api/settings', cfg)
            if obs['state'] != 'completed':
                summary['blocked'] = 'public_final_report_failed'; return 1
            accounts = []
            for label, security, quantity, cash, risk in [('A', 'SH:600519', '100', '10000', 'conservative'), ('B', 'SZ:000001', '500', '20000', 'balanced')]:
                account = await request('POST', '/api/accounts', {'name': '第四轮真实验收 '+label, 'cash': cash,
                    'preferences': {'risk': risk, 'horizon': '长期；复用已取得公共材料，重点解释本账户敞口、证据和条件；小规模研究及时汇总'},
                    'holdings': [{'symbol': security, 'quantity': quantity}]})
                accounts.append(account)
                job = await wait_account(account['id'])
                obs = observation(store, store.job(job['id']))
                summary['rounds'].append(obs); write_json(output/f'round-{len(summary["rounds"])}-{label}.json', obs, config); save()
                print(json.dumps({'stage': label, 'state': obs['state']}), flush=True)
            a, b = accounts
            summary['account_ids'] = {'a': a['id'], 'b': b['id']}
            b_before = {'account': store.account(b['id']), 'analyses': store.analyses(b['id'])}
            prior = store.analyses(a['id'])
            current = await request('GET', f"/api/accounts/{a['id']}")
            edited = await request('PUT', f"/api/accounts/{a['id']}", {'name': current['name'], 'cash': current['cash'],
                'preferences': current['preferences'], 'holdings': [{'symbol': 'SH:600519', 'quantity': '200'}], 'expected_version': current['version']})
            summary['steps']['holding_edit'] = {'previous_version': current['version'], 'new_version': edited['version'],
                'had_prior_success': bool(prior), 'prior_success_stale': bool(prior) and all(store.analysis(row['id'])['status'] == 'stale' for row in prior)}
            job = await wait_account(a['id'])
            obs = observation(store, store.job(job['id']))
            summary['rounds'].append(obs); write_json(output/'round-4-edited-A.json', obs, config)
            summary['steps']['b_unchanged'] = b_before == {'account': store.account(b['id']), 'analyses': store.analyses(b['id'])}
            summary['steps']['quote_records'] = store.cache('quotes')
            summary['steps']['accounts'] = {key: await request('GET', f'/api/accounts/{aid}') for key, aid in summary['account_ids'].items()}
            summary['steps']['recommendations'] = {key: await request('GET', f'/api/accounts/{aid}/recommendations?envelope=true') for key, aid in summary['account_ids'].items()}
            events = [event for round in summary['rounds'] for event in round['events']]
            native = [event for event in events if event['type'] == 'native_search' and any(e.get('status') == 'completed' for e in event.get('payload', {}).get('events', []))]
            quotes = summary['steps']['quote_records']
            checks = {'all_four_final_reports': len(summary['rounds']) == 4 and all(r['state'] == 'completed' and r['observations']['report_returned'] for r in summary['rounds']),
                'native_search_in_actual_research': bool(native), 'native_search_from_child': any(e.get('agent_id') != 'manager' for e in native),
                'completed_child_report': any(e['type'] == 'agent_completed' and e.get('agent_id') not in (None, 'manager') and e.get('payload', {}).get('report') for e in summary['rounds'][0]['events']),
                'two_symbols_validly_priced': all(quotes.get(s, {}).get('price') and quotes[s].get('status') in ('ok', 'closed') for s in ('SH:600519','SZ:000001')),
                'all_account_valuations_complete': all(r['observations'].get('all_holdings_priced') and r['observations'].get('metrics_recomputable') for r in summary['rounds'][1:]),
                'public_facts_reused': all(r['result'].get('shared_research_used') for r in summary['rounds'][1:]),
                'prior_success_stale': summary['steps']['holding_edit']['prior_success_stale'], 'b_unchanged': summary['steps']['b_unchanged'],
                'normal_scheduler_and_worker': bool(summary['steps']['scheduler_news_demand'])}
            summary['checks'] = checks
            save()
            browser = await asyncio.to_thread(subprocess.run,
                ['node', 'scripts/inspect_fourth_browser.mjs', f'http://127.0.0.1:{port}', str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=180)
            try:
                summary['browser'] = json.loads(browser.stdout)
            except ValueError:
                summary['browser'] = {'ok': False, 'exit_code': browser.returncode, 'error': browser.stderr[-2000:]}
            checks['real_browser_matches_api'] = bool(summary['browser'].get('ok'))
            snapshot_path = output/'browser-api-snapshot.json'
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
                def analysis_keys(rows):
                    return sorted((r['id'], r['input_version'], r['status']) for r in rows)
                checks['browser_api_matches_database'] = all(
                    analysis_keys(snapshot.get('accounts', {}).get(key, {}).get('analyses', [])) == analysis_keys(store.analyses(aid))
                    and snapshot.get('accounts', {}).get(key, {}).get('account', {}).get('version') == store.account(aid)['version']
                    for key, aid in summary['account_ids'].items())
                def recommendation_keys(rows):
                    return [(r['id'], r.get('price_state'), r.get('review_status')) for r in rows]
                checks['browser_recommendations_match_database'] = all(
                    recommendation_keys(snapshot['accounts'][key]['recommendations']['items']) == recommendation_keys(store.recommendations_page(aid, 10, 0, 'active')['items'])
                    and snapshot['accounts'][key]['recommendations']['total'] == store.recommendations_page(aid, 10, 0, 'active')['total']
                    for key, aid in summary['account_ids'].items())
            else:
                checks['browser_api_matches_database'] = False
                checks['browser_recommendations_match_database'] = False
            summary['mechanical_acceptance'] = all(checks.values())
            save()
            return 0 if all(checks.values()) else 2
        except Exception as exc:
            summary['execution_failure'] = {'type': type(exc).__name__}
            return 1
        finally:
            summary['finished_at'] = now()
            summary['final_code'] = code_identity()
            summary['same_application_code'] = summary['code']['application_sha256'] == summary['final_code']['application_sha256']
            write_json(output/'materials.json', store.materials(), config)
            save()
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 15)
            except subprocess.TimeoutExpired:
                process.kill(); await asyncio.to_thread(process.wait)
            log.close()
            print(json.dumps({'output': str(output), 'states': [r['state'] for r in summary['rounds']], 'checks': summary.get('checks'), 'failure': summary.get('execution_failure')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings-db', type=Path, default=db_path('live'))
    parser.add_argument('--output', type=Path, default=ROOT/'data/evaluations'/('fourth-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--timeout', type=int, default=600)
    raise SystemExit(asyncio.run(verify(parser.parse_args())))
