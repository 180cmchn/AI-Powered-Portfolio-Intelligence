#!/usr/bin/env python3
"""Bounded authorized real research; read daily configuration, write isolated DB only.

Three representative jobs, at most 16 logical model calls each. Current per-role
models/efforts and request/child/round deadlines are retained. No clock, proxy,
daily process, keychain, daily account, or historical report is modified.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import hashlib
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
from app.config import db_path
from app.domain import now, AccountInput
from app.service import Service
from app.symbols import SymbolResolver
from app.runtime_version import source_digest, env_file_stamp
from app.store import Store
from scripts.verify_live_chain import configuration, scrubbed, all_events


def isolated_configuration(source, destination, account_id):
    cfg, origin = configuration(source)
    cfg.update(scheduled_research_enabled=False, market_refresh_enabled=False,
               forecast_evaluation_enabled=False, news_queries=[], debounce_seconds=0,
               max_model_calls=min(16, cfg['max_model_calls']),
               max_total_agents=min(3, cfg.get('max_total_agents', 16)),
               max_concurrent_agents=min(2, cfg['max_concurrent_agents']))
    store = Store(destination, 'live')
    store.set_meta('settings', cfg)
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as source_db, store.connect() as target:
        source_db.row_factory = sqlite3.Row
        if not source_db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            raise ValueError('Reference account unavailable; do not infer configuration')
        for table in ('model_providers', 'provider_connections', 'provider_catalogs', 'agent_definitions'):
            target.execute(f'DELETE FROM {table}')
            for row in source_db.execute(f'SELECT * FROM {table}'):
                target.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in row)})', tuple(row))
        overrides = {row['role_id']: json.loads(row['body']) for row in source_db.execute(
            'SELECT role_id,body FROM account_agent_overrides WHERE account_id=?', (account_id,))}
        # Use the selected account's complete effective matrix as this isolated
        # environment's defaults, so initial automatic jobs bind correctly.
        for row in target.execute('SELECT role_id,body FROM agent_definitions').fetchall():
            body = json.loads(row['body'])
            body['default_binding'].update(overrides.get(row['role_id'], {}))
            target.execute('UPDATE agent_definitions SET body=? WHERE role_id=?',
                           (json.dumps(body, ensure_ascii=False), row['role_id']))
        target.execute("DELETE FROM meta WHERE key='tenth_role_migration'")
        store.migrate_tenth_roles(target)
    matrix = store.list_agent_roles()
    manager = next(r for r in matrix if r['role_id'] == 'manager')
    if manager['resolved']['reasoning_effort'] != 'max':
        raise ValueError('Current reference account is no longer max; do not silently replace requested configuration')
    return store, cfg, matrix


async def run(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    store, cfg, matrix = isolated_configuration(db_path('live'), out/'evaluation.sqlite3', args.reference_account)
    digest, env_stamp = source_digest(), env_file_stamp()
    summary = {'kind':'real_tenth_api_worker', 'started_at':now(), 'source_sha256':digest,
               'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
               'market_state':market_state(), 'roles':[{k:r[k] for k in ('role_id','resolved','config_error')} for r in matrix],
               'budget':{k:cfg.get(k, {'model_retries':1}.get(k)) for k in ('max_model_calls','max_total_agents','max_concurrent_agents',
                    'model_request_timeout_seconds','agent_timeout_seconds','analysis_timeout_seconds','model_retries','max_output_tokens')},
               'maximum_business_jobs':3, 'daily_database_access':'read_only', 'rounds':[]}
    def save():
        (out/'summary.json').write_text(scrubbed(summary,cfg))
        (out/'summary.json').chmod(0o600)
    process = log = None
    try:
        # Confirm the actual provider catalog without a paid inference probe.
        summary['catalogs'] = []
        for pid in dict.fromkeys(r['resolved']['provider_id'] for r in matrix):
            catalog = await store.discover_models(pid, force=True)
            summary['catalogs'].append({'provider_id':pid,'catalog':catalog})
        resolver = SymbolResolver(store)
        setup = await resolver.resolve(['518880','688825','562500','159278','000001'])
        summary['identity_resolution'] = setup
        if not setup['ok']: raise RuntimeError('Actual public security directory did not resolve scope')
        store.save_universe({'watchlist':['SH:518880','SH:688825','SH:562500'],'candidates':[]})
        with store.connect() as db:
            db.execute("UPDATE research_demands SET state='cancelled',detail='isolated setup: no public research requested' WHERE account_id='' AND state='pending'")
        preference = {'risk':'balanced','horizon':'研究真实账户风险、价格趋势及可执行观察条件。成本未知不妨碍集中度或走势分析。先用实际日线计算再总结；给出维持、调整或观察建议及条件，方向不足可弃权。'}
        # Seed before the worker starts so a lawful formal scheduler preparation
        # cannot supersede an already paid manual run.
        a = store.save_account(AccountInput(name='第十轮虚拟 A', cash='10000',
            holdings=[{'symbol':'SZ:159278','quantity':'10000'}], preferences=preference))
        from datetime import datetime
        from app.domain import TZ
        stamp = datetime.now(TZ)
        if market_state()['trading_day'] is True and '15:30' <= stamp.strftime('%H:%M') < '18:00':
            store.save_forecast_settings(a['id'], {'enabled':True})
            store.set_meta('settings', {**cfg,'scheduled_research_enabled':True})
            scheduler = Service(store)
            await scheduler.scheduler_tick()
            if scheduler.forecast_task:
                await scheduler.forecast_task
            summary['formal_scheduler'] = {'at':now(),'natural_window':True,
                'batches':store.forecast_batches(), 'note':'Normal scheduler preparation before starting worker; no clock/window change'}
            store.set_meta('settings',cfg)
        else:
            summary['formal_scheduler'] = {'at':now(),'natural_window':False,'note':'Outside lawful window; no clock or deadline change'}
        a = store.account(a['id'])
        summary['accounts'] = {'a':a['id']}
        save()
        sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]; sock.close()
        log = (out/'service.log').open('w')
        process = subprocess.Popen([sys.executable,'scripts/serve.py'],cwd=ROOT,
            env={**os.environ,'APP_MODE':'live','APP_DB_PATH':str(store.path),'APP_PORT':str(port),'APP_HOST':'127.0.0.1'},
            stdout=log,stderr=log)
        summary['instance'] = {'port':port,'pid':process.pid}
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=40, trust_env=False) as api:
            async def request(method,path,**kwargs):
                response = await api.request(method,path,**kwargs)
                response.raise_for_status()
                return response.json()
            for _ in range(100):
                try:
                    summary['health'] = await request('GET','/api/health'); break
                except httpx.HTTPError:
                    if process.poll() is not None: raise RuntimeError('Isolated server exited')
                    await asyncio.sleep(.2)
            else: raise RuntimeError('Isolated startup failed')
            if summary['health']['runtime_version']['source_sha256_at_start'] != digest:
                raise RuntimeError('Source changed before isolated process start')
            resolved = await request('POST','/api/securities/resolve',json={'symbols':['518880','688825','562500','159278','000001']})
            summary['identity_resolution'] = resolved
            if not resolved['ok']: raise RuntimeError('Actual public security directory did not resolve scope')
            summary['universe'] = await request('GET','/api/universe')
            summary['a_setup'] = 'Seeded virtual holding before worker; UI creation/save path separately tested on actual FastAPI'
            save()
            async def wait_job(job_id):
                previous = None
                async with asyncio.timeout(cfg['analysis_timeout_seconds']+45):
                    while True:
                        job = await request('GET','/api/jobs/'+job_id)
                        sig = (job['state'],job.get('stage'))
                        if sig != previous:
                            print(json.dumps({'job_id':job_id,'state':sig[0],'stage':sig[1]},ensure_ascii=False),flush=True)
                            previous = sig
                        if job['state'] not in ('queued','running'): return job
                        await asyncio.sleep(1)
            def collect(job,label):
                events = all_events(store,job['id'])
                safe_events = [e for e in events if e['type'] in ('agent_started','model_usage','model_error','tool_result','agent_completed',
                    'agent_failed','agent_timeout','published','failed','synthesis_calculation_coverage',
                    'final_calculation_coverage','calculation_context_coverage','synthesis_context_prepared','model_configuration_bound')]
                summary['rounds'].append({'label':label,'job':job,'event_counts':dict(Counter(e['type'] for e in events)),
                    'events':safe_events, 'report_published':bool(job.get('report_id')),
                    'review':'pending independent content review'})
                report = (job.get('result') or {}).get('report')
                if report: (out/(label+'-original.md')).write_text(report)
                save()
            first = await wait_job(a['latest_job_id']); collect(first,'a_initial')
            if source_digest() != digest: raise RuntimeError('Source changed during acceptance')
            changed = await request('PUT',f"/api/accounts/{a['id']}",headers={'X-Operation-ID':'tenth-a-update'},json={
                'name':a['name'],'cash':'10000','expected_version':a['version'],
                'holdings':[{'symbol':'159278','quantity':'20000'}],'preferences':preference})
            summary['position_update'] = {'quantity':'20000','cash':changed['cash'],
                'previous_reports':await request('GET',f"/api/accounts/{a['id']}/analyses?summary=true")}
            collect(await wait_job(changed['latest_job_id']),'a_position_update')
            b = await request('POST','/api/accounts',headers={'X-Operation-ID':'tenth-b-create'},json={
                'name':'第十轮虚拟 B','cash':'20000','holdings':[], 'preferences':{
                    'risk':'balanced','horizon':'研究518880黄金ETF是否适合作为候选：给出暂不买入、观察或配置建议及明确条件。未提供拟投入金额，只能算明确假设情景，不等于满仓决定。'}})
            summary['accounts']['b'] = b['id']; summary['b_before_explicit_target'] = b['analysis_state']
            target = await request('POST',f"/api/accounts/{b['id']}/analyze",json={
                'symbol':'518880','operation_id':'tenth-b-target','origin':{'page':'market','action':'research_security'}})
            collect(await wait_job(target['id']),'b_security')
            summary['forecasts'] = store.forecasts_page(kind='exploratory')
            summary['formal_forecasts'] = store.forecasts_page(kind='formal')
            summary['completed_at'] = now()
            summary['source_unchanged'] = source_digest() == digest
            summary['env_unchanged'] = env_file_stamp() == env_stamp
            summary['natural_mature_scoring'] = 'not observed; no accuracy claim'
            save()
    except Exception as exc:
        summary['acceptance_error'] = {'type':type(exc).__name__,'message':str(exc)}
        save()
        raise
    finally:
        if process:
            process.terminate()
            try: process.wait(timeout=12)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        if log: log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-account', default='96b91df38e854008b989206c63808c3a')
    asyncio.run(run(parser.parse_args()))
