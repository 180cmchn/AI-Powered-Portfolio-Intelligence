#!/usr/bin/env python3
"""Opt-in real provider/API/worker evaluation in a NEW, disposable database.

Uses only the operator's existing selected model/protocol/effort. Does not pick
another discovered model or alter the daily database, .env, or Keychain. No fake
model or price transport is used. A failed prerequisite remains an observation.
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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_live_chain import configuration, public_config, scrubbed
from app.calendar import market_state
from app.config import db_path
from app.domain import TZ
import httpx


def source_digest():
    digest = hashlib.sha256()
    for folder in ('app', 'scripts'):
        for path in sorted((ROOT / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


async def run(args):
    cfg, origin = configuration(args.settings_db or db_path('live'))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    os.environ['APP_DB_PATH'] = str(output / 'evaluation.sqlite3')
    os.environ['APP_MODE'] = 'live'
    # Constructor migration sees exactly the already-selected connection.
    for name, key in [('MODEL_NAME', 'model_name'), ('MODEL_PROTOCOL', 'model_protocol'),
                      ('MODEL_BASE_URL', 'model_base_url'), ('MODEL_KEY_ENV', 'model_key_env'),
                      ('REASONING_EFFORT', 'reasoning_effort')]:
        os.environ[name] = str(cfg[key])
    from app.main import create_app
    from app.providers import MarketProvider
    app = create_app(output / 'evaluation.sqlite3', mode='live', worker=True)
    store, service = app.state.store, app.state.service
    cfg.update(debounce_seconds=0, scheduled_research_enabled=False, market_refresh_enabled=False,
               forecast_evaluation_enabled=False, max_model_calls=16, max_total_agents=3,
               max_concurrent_agents=2, max_agent_turns=12, max_job_attempts=1,
               analysis_timeout_seconds=args.timeout, agent_timeout_seconds=min(240, args.timeout),
               max_output_tokens=3000, max_tool_result_chars=6000, max_context_chars=100000, read_max_chars=6000, news_queries=[])
    store.set_meta('settings', cfg)
    summary = {'kind': 'real_HTTP_models_and_production_API_worker', 'started_at': datetime.now(TZ).isoformat(),
               'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'source_sha256': source_digest(), 'configuration': public_config(cfg, origin),
               'market_state': market_state(), 'database': str(store.path),
               'dependencies': {name: importlib.metadata.version(name) for name in ('httpx', 'fastapi', 'uvicorn', 'pydantic')},
               'budget': {'accounts': 2, 'model_calls_per_account': 16, 'seconds_per_account': args.timeout},
               'limitations': ['Only the existing operator-selected model/effort is authorized; no second real combination is invented.',
                              'Exploratory predictions use the latest completed close and never enter formal accuracy.',
                              'No newly issued 5/20-day prediction has matured during this run.'],
               'wire_requests': [], 'accounts': [], 'rounds': []}
    def checkpoint():
        (output / 'summary.json').write_text(scrubbed(summary, cfg), encoding='utf-8')
        (output / 'summary.json').chmod(0o600)
    original_send = httpx.AsyncClient.send
    expected_host = urlsplit(cfg['model_base_url']).hostname
    async def observed_send(client, request, *a, **kw):
        item = None
        if request.url.host == expected_host:
            item = {'method': request.method, 'url': str(request.url), 'has_authorization': bool(request.headers.get('authorization'))}
            if request.method == 'POST':
                try:
                    body = json.loads(request.content)
                    item.update(model=body.get('model'), reasoning=body.get('reasoning'), reasoning_effort=body.get('reasoning_effort'),
                                tool_names=[t.get('name') or t.get('function', {}).get('name') or t.get('type') for t in body.get('tools', [])])
                except (ValueError, AttributeError):
                    pass
            summary['wire_requests'].append(item)
        try:
            result = await original_send(client, request, *a, **kw)
            if item is not None:
                item['http_status'] = result.status_code
            return result
        except Exception as exc:
            if item is not None:
                item['error_type'] = type(exc).__name__
            raise
    httpx.AsyncClient.send = observed_send
    checkpoint()
    try:
        provider = store.list_providers()[0]
        catalog = await store.discover_models(provider['id'], force=True)
        summary['discovery'] = catalog
        ids = {row['id'] for row in catalog.get('models', [])}
        summary['selected_model_discovered'] = cfg['model_name'] in ids
        checkpoint()
        if not catalog.get('selectable') or cfg['model_name'] not in ids:
            summary['stopped_at'] = 'Existing model was not selectable in the actual directory; no manual fallback.'
            return 2
        binding = {'provider_id': provider['id'], 'model_id': cfg['model_name'], 'reasoning_effort': cfg['reasoning_effort']}
        for role in store.list_agent_roles():
            store.update_agent_definition(role['role_id'], {'default_binding': binding})
        market = MarketProvider(cfg)
        summary['security'] = await market.security('SZ:159278')
        summary['history'] = await market.history('SZ:159278', start='2026-10-01', end=datetime.now(TZ).date().isoformat(), limit=30)
        checkpoint()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as api:
            for name, quantity, cash in [('A', '10000', '10000'), ('B', '5000', '20000')]:
                response = await api.post('/api/accounts', json={'name': '第六轮独立真实验收 ' + name,
                    'cash': cash, 'holdings': [{'symbol': 'SZ:159278', 'quantity': quantity}],
                    'preferences': {'risk': 'balanced', 'horizon': '本次联调仅研究159278最近收盘与集中度；请自主选一个具名子角色核对已有日线或基金资料，获取其独立回报，不要求全员。仅需简短结论，缺资料可弃权，及时通过finish_analysis发布1/5/20交易日预测。'}})
                response.raise_for_status()
                summary['accounts'].append(response.json()['id'])
            summary['roles'] = {aid: (await api.get(f'/api/accounts/{aid}/agent-models')).json() for aid in summary['accounts']}
            summary['same_complete_role_directory'] = len({tuple(sorted(r['role_id'] for r in rows)) for rows in summary['roles'].values()}) == 1
            async with app.router.lifespan_context(app):
                deadline = asyncio.get_running_loop().time() + args.timeout + 90
                previous = None
                while asyncio.get_running_loop().time() < deadline:
                    jobs = [store.jobs(aid)[0] for aid in summary['accounts']]
                    states = [(j['id'], j['state']) for j in jobs]
                    if states != previous:
                        print(json.dumps({'stage': 'production_worker', 'states': states}), flush=True)
                        previous = states
                    if all(j['state'] in ('completed', 'failed', 'waiting_config', 'cancelled') for j in jobs):
                        break
                    await asyncio.sleep(1)
                for aid in summary['accounts']:
                    jobs = store.jobs(aid)
                    job = store.job(jobs[0]['id'])
                    events = store.events_since(job['id'], 0, 500)['events']
                    summary['rounds'].append({'account_id': aid, 'job': job, 'event_counts': dict(Counter(e['type'] for e in events)), 'events': events})
                summary['quotes'] = store.cache('quotes')
                summary['forecast_page'] = (await api.get('/api/forecasts', params={'kind': 'exploratory'})).json()
                summary['formal_statistics'] = store.forecast_statistics(kind='formal')
                summary['forecast_batches'] = store.forecast_batches()
            a, b = summary['accounts']
            b_before = (await api.get(f'/api/accounts/{b}')).json()
            old_team = store.freeze_agent_team(b)
            original = store.account(a)
            changed = await api.put(f'/api/accounts/{a}', json={'name': original['name'], 'cash': original['cash'],
                'holdings': [{'symbol': 'SZ:159278', 'quantity': '12000'}], 'preferences': original['preferences'], 'expected_version': original['version']})
            changed.raise_for_status()
            b_after = (await api.get(f'/api/accounts/{b}')).json()
            summary['a_edit_b_isolation'] = {'a_new_version': changed.json()['version'], 'b_input_unchanged': all(b_before.get(k) == b_after.get(k) for k in ('id','name','cash','holdings','preferences','version','latest_job_id')),
                'b_team_unchanged': old_team['team_id'] == store.freeze_agent_team(b)['team_id'], 'worker_stopped_before_edit': True}
        return 0 if all(r['job']['state'] == 'completed' for r in summary['rounds']) else 2
    finally:
        httpx.AsyncClient.send = original_send
        summary['finished_at'] = datetime.now(TZ).isoformat()
        summary['source_unchanged_during_run'] = summary['source_sha256'] == source_digest()
        checkpoint()
        print(json.dumps({'summary': str(output / 'summary.json'), 'states': [r['job']['state'] for r in summary['rounds']]}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--settings-db', type=Path)
    parser.add_argument('--timeout', type=int, default=420)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
