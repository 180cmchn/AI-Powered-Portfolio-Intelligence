#!/usr/bin/env python3
"""Bounded, authorized A-only real research on a NEW independent evaluation DB.

Uses the production API/scheduler/worker, actual clock and provider directory.
No credentials, daily accounts, research timestamps or published forecasts change.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.calendar import market_state
from app.config import db_path
from app.domain import TZ, now
from scripts.verify_live_chain import configuration, public_config, scrubbed, all_events
from scripts.verify_sixth_live import source_digest
from scripts.verify_seventh_live import (validate_provider_destination, validate_request_destination,
    select_directory_models, placeholder_report, unchanged_files)


def delivered_report(job):
    report = (job.get('result') or {}).get('report')
    return job['state'] == 'completed' and isinstance(report, str) and bool(report.strip()) and not placeholder_report(report)


async def run(args):
    cfg, origin = configuration(db_path('live'))
    validate_provider_destination(cfg['model_base_url'], 'https://api.xxyapi.com/v1')
    out = args.output.resolve()
    if out == ROOT or ROOT not in out.parents:
        raise ValueError('Evaluation output must be a new directory within this workspace')
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    protected = unchanged_files()
    summary = {'kind': 'real_A_research_followup', 'started_at': now(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_sha256': source_digest(), 'configuration': public_config(cfg, origin),
        'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True).splitlines(),
        'dependencies': {n: importlib.metadata.version(n) for n in ('httpx', 'fastapi', 'uvicorn', 'pydantic')},
        'rounds': [], 'wire_requests': [], 'market_state': market_state(),
        'budget': {'model_calls_per_job': 16, 'max_total_agents': 3, 'max_concurrent_agents': 2,
                   'job_timeout_seconds': 420, 'max_initial_and_update_jobs': 2, 'max_recovery_jobs': 1}}
    def save():
        (out / 'summary.json').write_text(scrubbed(summary, cfg), encoding='utf-8')
        (out / 'summary.json').chmod(0o600)
    original_send = httpx.AsyncClient.send
    async def observed(client, request, *a, **kw):
        validate_request_destination(request, 'api.xxyapi.com')
        record = None
        if request.url.host == 'api.xxyapi.com':
            record = {'method': request.method, 'path': request.url.path, 'started_at': now()}
            if request.method == 'POST':
                body = json.loads(request.content)
                record.update(model=body.get('model'), reasoning=body.get('reasoning'),
                    stream_requested=body.get('stream', False), input_bytes=len(request.content),
                    tool_names=[t.get('name') or t.get('function', {}).get('name') or t.get('type') for t in body.get('tools', [])])
            summary['wire_requests'].append(record)
        try:
            response = await original_send(client, request, *a, **kw)
            if record is not None:
                record.update(http_status=response.status_code, content_type=response.headers.get('content-type'))
            return response
        except Exception as exc:
            if record is not None:
                record['error_type'] = type(exc).__name__
            raise
    httpx.AsyncClient.send = observed
    save()
    app = None
    try:
        key = os.getenv(cfg.get('model_key_env', 'MODEL_API_KEY'), '')
        if not key:
            raise ValueError('Existing authorized credential unavailable')
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get('https://api.xxyapi.com/v1/models', headers={'Authorization': 'Bearer ' + key})
            response.raise_for_status()
            ids, _ = select_directory_models(response.json(), args.model)
            summary['directory'] = {'source': 'https://api.xxyapi.com/v1/models', 'models': ids, 'fetched_at': now()}
        cfg['model_name'] = args.model
        if cfg['model_protocol'] != 'responses':
            raise ValueError('This follow-up verifies the previously authorized Responses configuration')
        summary['configuration'] = public_config(cfg, origin)
        from app.providers import MarketProvider
        market = MarketProvider(cfg)
        summary['security'] = await market.security('SZ:159278')
        summary['quote'] = await market.quote('SZ:159278')
        summary['history'] = await market.history('SZ:159278', start='2026-10-01', end=datetime.now(TZ).date().isoformat(), limit=30)
        if summary['security'].get('name') != '机器人ETF鹏华' or summary['security'].get('kind') != 'etf':
            raise ValueError('Actual security identity not confirmed')
        os.environ['APP_DB_PATH'] = str(out / 'evaluation.sqlite3')
        os.environ['APP_MODE'] = 'live'
        for env, key_name in [('MODEL_NAME', 'model_name'), ('MODEL_PROTOCOL', 'model_protocol'), ('MODEL_BASE_URL', 'model_base_url'), ('MODEL_KEY_ENV', 'model_key_env'), ('REASONING_EFFORT', 'reasoning_effort')]:
            os.environ[env] = str(cfg[key_name])
        from app.main import create_app
        app = create_app(out / 'evaluation.sqlite3', mode='live', worker=True)
        store = app.state.store
        def assert_account_only():
            with store.connect() as db:
                counts = {'global_demands': db.execute("SELECT count(*) FROM research_demands WHERE account_id=''").fetchone()[0],
                          'global_runs': db.execute('SELECT count(*) FROM global_runs').fetchone()[0]}
            summary['account_only_scope'] = counts
            if any(counts.values()):
                raise ValueError('Unexpected public research outside the bounded A-only evaluation')
        local = datetime.now(TZ)
        formal = market_state(local)['trading_day'] is True and '15:30' <= local.strftime('%H:%M') < '18:00'
        summary['validation_kind'] = 'formal' if formal else 'exploratory'
        cfg.update(debounce_seconds=0, scheduled_research_enabled=formal, market_refresh_enabled=False,
            forecast_evaluation_enabled=True, max_model_calls=16, max_total_agents=3, max_concurrent_agents=2,
            max_agent_turns=12, max_job_attempts=1, analysis_timeout_seconds=420, agent_timeout_seconds=240,
            max_output_tokens=3000, max_tool_result_chars=6000, max_context_chars=100000, read_max_chars=6000,
            model_responses_stream=True, news_queries=[])
        store.set_meta('settings', cfg)
        summary['run_configuration'] = {k: cfg[k] for k in ('model_responses_stream', 'max_model_calls', 'max_total_agents',
            'max_concurrent_agents', 'max_agent_turns', 'max_job_attempts', 'analysis_timeout_seconds',
            'agent_timeout_seconds', 'max_output_tokens', 'max_context_chars', 'model_request_timeout_seconds',
            'synthesis_request_timeout_seconds')}
        provider = store.list_providers()[0]
        summary['provider_catalog'] = await store.discover_models(provider['id'], force=True)
        if not summary['provider_catalog'].get('selectable'):
            raise ValueError('Production model directory is not selectable')
        for role in store.list_agent_roles():
            store.update_agent_definition(role['role_id'], {'default_binding': {'provider_id': provider['id'], 'model_id': args.model, 'reasoning_effort': cfg['reasoning_effort']}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as api:
            # The holding itself is the forecast scope. Changing the universe
            # would also schedule a public research job outside this A-only run.
            response = await api.post('/api/accounts', json={'name': '第七轮 A 真实研究复验', 'cash': '10000',
                'holdings': [{'symbol': 'SZ:159278', 'quantity': '10000'}],
                'preferences': {'risk': 'balanced', 'horizon': '研究159278机器人ETF鹏华、跟踪指数与组合敞口。自主选择必要的具名子角色核对行情和基金原始资料。提交简明完整报告，解释实际证据、来源和资料缺口，并为1/5/20交易日目标给出方向判断或明确弃权。'}})
            response.raise_for_status(); aid = response.json()['id']; summary['account_id'] = aid
            summary['roles'] = (await api.get(f'/api/accounts/{aid}/agent-models')).json()
            response = await api.put(f'/api/accounts/{aid}/forecast-settings', json={'enabled': True}); response.raise_for_status()
            async def wait_job():
                previous = None
                async with asyncio.timeout(510):
                    while True:
                        assert_account_only()
                        job = store.job(store.jobs(aid)[0]['id'])
                        status = (job['id'], job['state'])
                        if previous != status:
                            print(json.dumps({'stage': 'production_worker', 'job_id': job['id'], 'state': job['state']}), flush=True)
                            previous = status
                        if job['state'] in ('completed', 'failed', 'waiting_config', 'cancelled'):
                            return job
                        await asyncio.sleep(1)
            def collect(job, label):
                events = all_events(store, job['id'])
                summary['rounds'].append({'label': label, 'job': job, 'event_counts': dict(Counter(e['type'] for e in events)),
                    'events': events, 'report_delivered': delivered_report(job),
                    'placeholder_observed': placeholder_report((job.get('result') or {}).get('report'))})
                save()
            if formal:
                await app.state.service.scheduler_tick()
                if app.state.service.forecast_task:
                    await asyncio.wait_for(app.state.service.forecast_task, timeout=50)
                ready = [b for b in store.forecast_batches() if b['kind'] == 'formal' and b.get('job_id') and b['account_id'] == aid]
                if not ready:
                    summary['stopped_at'] = 'Actual formal baseline unavailable; no substitute clock or prices'
                    return 2
            assert_account_only()
            async with app.router.lifespan_context(app):
                job = await wait_job(); collect(job, 'initial')
                if job['state'] == 'failed' and job.get('synthesis_checkpoint', {}).get('available'):
                    response = await api.post(f'/api/jobs/{job["id"]}/retry'); response.raise_for_status()
                    job = await wait_job(); collect(job, 'bounded_synthesis_retry')
                if delivered_report(job):
                    old = store.account(aid)
                    response = await api.put(f'/api/accounts/{aid}', json={'name': old['name'], 'cash': old['cash'],
                        'holdings': [{'symbol': 'SZ:159278', 'quantity': '20000'}], 'preferences': old['preferences'],
                        'expected_version': old['version']}); response.raise_for_status()
                    summary['position_update'] = {'cash_unchanged': store.account(aid)['cash'] == old['cash'],
                        'new_input_version': store.account(aid)['version'], 'old_report_status': store.analyses(aid)[0]['status']}
                    job = await wait_job(); collect(job, 'position_update')
                else:
                    summary['position_update'] = {'state': 'not_triggered', 'reason': 'Initial substantive report not delivered'}
            summary['batches'] = store.forecast_batches(limit=200)
            summary['forecast_page'] = store.forecasts_page(kind=summary['validation_kind'], limit=200)
            summary['formal_statistics'] = store.forecast_statistics(kind='formal')
            summary['exploratory_statistics'] = store.forecast_statistics(kind='exploratory')
            summary['attempts'] = store.forecast_attempts()
            summary['windows'] = store.forecast_window_runs(limit=200)
            summary['quotes'] = store.cache('quotes')
            assert_account_only()
        initial_rounds = [r for r in summary['rounds'] if r['label'] in ('initial', 'bounded_synthesis_retry')]
        summary['success'] = bool(initial_rounds) and initial_rounds[-1]['report_delivered'] and summary['rounds'][-1]['label'] == 'position_update' and summary['rounds'][-1]['report_delivered']
        return 0 if summary['success'] else 2
    except Exception as exc:
        summary['error_type'] = type(exc).__name__
        raise
    finally:
        if app is not None:
            await app.state.service.stop()
        httpx.AsyncClient.send = original_send
        summary['finished_at'] = now()
        summary['source_unchanged_during_run'] = source_digest() == summary['source_sha256']
        summary['protected_file_observations'] = {k: {'unchanged': (ROOT / k).exists() and hashlib.sha256((ROOT / k).read_bytes()).hexdigest() == v,
            'access_by_evaluator': 'read_only_environment' if k == '.env' else 'read_only_configuration'} for k, v in protected.items()}
        save()
        print(json.dumps({'summary': str(out / 'summary.json'), 'success': summary.get('success'), 'states': [r['job']['state'] for r in summary['rounds']]}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='grok-4.7')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
