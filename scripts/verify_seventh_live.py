#!/usr/bin/env python3
"""Opt-in real network verification in a NEW evaluation DB, with an exact authorized host.

Model IDs must first exist in the real directory. No clock/window changes, fake
prices, private daily accounts, credential writes, or automatic model fallbacks.
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
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.calendar import market_state
from app.config import db_path
from app.domain import TZ
from scripts.verify_live_chain import configuration, public_config, scrubbed, all_events
from scripts.verify_sixth_live import source_digest


def validate_provider_destination(configured, authorized):
    canonical = 'https://api.xxyapi.com/v1'
    if authorized.rstrip('/') != canonical or configured.rstrip('/') != canonical:
        raise ValueError('This evaluator is restricted to the explicitly authorized ' + canonical)
    return canonical


def validate_request_destination(request, host):
    if request.headers.get('authorization') and request.url.host != host:
        raise ValueError('credential destination outside the authorized provider')
    if request.url.host == host and (request.url.scheme != 'https' or request.url.port not in (None, 443)
                                   or not request.url.path.startswith('/v1/')):
        raise ValueError('request outside authorized HTTPS API prefix')


def select_directory_models(directory, model_a, model_b=None):
    ids = {row['id'] for row in directory.get('data', []) if isinstance(row, dict) and isinstance(row.get('id'), str)}
    selected = [model_a, model_b or model_a]
    if any(model not in ids for model in selected):
        raise ValueError('requested model absent from actual directory')
    return sorted(ids), selected


def placeholder_report(value):
    """Acceptance annotation only; never rejects or rewrites production reports."""
    return isinstance(value, str) and value.lstrip().startswith(('占位', '测试占位', '测试报告占位'))


def unchanged_files():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT / '.env', db_path('live')] if p.exists()}


async def run(args):
    cfg, origin = configuration(args.settings_db or db_path('live'))
    authorized = validate_provider_destination(cfg['model_base_url'], args.authorized_base_url)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    protected = unchanged_files()
    summary = {'iteration': 7, 'kind': 'real_network_preflight' if args.preflight_only else 'real_API_scheduler_worker',
        'started_at': datetime.now(TZ).isoformat(), 'commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'git_status': subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True).splitlines(),
        'source_sha256': source_digest(), 'configuration': public_config(cfg, origin),
        'dependencies': {n: importlib.metadata.version(n) for n in ('httpx','fastapi','uvicorn','pydantic')},
        'market_state': market_state(), 'wire_requests': [], 'rounds': [], 'accounts': [],
        'budget': {'model_calls_per_job': 16, 'max_total_agents': 3, 'job_timeout_seconds': args.timeout,
                   'initial_jobs': 2, 'position_update_jobs': 1, 'bounded_synthesis_retry_jobs': 1},
        'limitations': ['当前真实运行时钟决定正式或探索，绝不倒调时间或截止。',
                       '真实预测未到期保持待评估，工程历史夹具不进入前瞻表现。']}
    def checkpoint():
        (output/'summary.json').write_text(scrubbed(summary,cfg),encoding='utf-8')
        (output/'summary.json').chmod(0o600)
    original_send = httpx.AsyncClient.send
    host = urlsplit(authorized).hostname
    async def observed_send(client, request, *a, **kw):
        # A second runtime fence prevents credentials following an unapproved URL.
        validate_request_destination(request, host)
        item = None
        if request.url.host == host:
            item = {'method':request.method,'path':request.url.path,'started_at':datetime.now(TZ).isoformat()}
            if request.method == 'POST':
                body = json.loads(request.content)
                item.update(model=body.get('model'),reasoning=body.get('reasoning'),reasoning_effort=body.get('reasoning_effort'),
                    input_bytes=len(request.content),tool_names=[t.get('name') or t.get('function',{}).get('name') or t.get('type') for t in body.get('tools',[])])
            summary['wire_requests'].append(item)
        start = time.monotonic()
        try:
            result = await original_send(client,request,*a,**kw)
            if item is not None:
                item.update(http_status=result.status_code,request_id=result.headers.get('x-request-id'))
            return result
        except Exception as exc:
            if item is not None: item['error_type']=type(exc).__name__
            raise
        finally:
            if item is not None: item['duration_ms']=round((time.monotonic()-start)*1000,2)
    httpx.AsyncClient.send = observed_send
    checkpoint()
    try:
        key = os.getenv(cfg.get('model_key_env','MODEL_API_KEY'),'')
        if not key:
            summary['stopped_at']='existing credential unavailable';return 2
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            response=await client.get('https://api.xxyapi.com/v1/models',headers={'Authorization':'Bearer '+key})
            response.raise_for_status()
            try:
                ids, selected = select_directory_models(response.json(), args.model_a, args.model_b)
            except ValueError as exc:
                summary['stopped_at']=str(exc);return 2
        summary['directory']={'source':authorized+'/models','models':ids,'fetched_at':datetime.now(TZ).isoformat()}
        summary['selected_models']=selected
        cfg['model_name']=selected[0]
        summary['configuration']=public_config(cfg,origin)
        from app.providers import MarketProvider
        market=MarketProvider(cfg)
        summary['security']=await market.security('SZ:159278')
        summary['quote']=await market.quote('SZ:159278')
        summary['history']=await market.history('SZ:159278',start='2026-10-01',end=datetime.now(TZ).date().isoformat(),limit=30)
        checkpoint()
        if args.preflight_only:
            from app.model import ModelClient
            client=ModelClient({**cfg,'max_output_tokens':256})
            session=client.session('你是接口连通性验证助手。只回答“连接验证完成”。',{'task':'验证普通汇总响应的网络链路，不研究账户、不发布预测。'})
            session.agent_id='manager';session.phase='synthesis';session.native_search=False
            try:
                turn=await client.complete(session,[])
                summary['small_synthesis_probe']={'success':bool(turn.text),'visible_text':turn.text,'requests':client.requests}
            except Exception as exc:
                summary['small_synthesis_probe']={'success':False,'error_type':type(exc).__name__,'requests':client.requests}
            finally:await client.aclose()
            return 0 if summary['small_synthesis_probe']['success'] else 2
        if summary['security'].get('name') != '机器人ETF鹏华' or summary['security'].get('kind') != 'etf':
            summary['stopped_at']='security identity not confirmed';return 2
        os.environ['APP_DB_PATH']=str(output/'evaluation.sqlite3')
        os.environ['APP_MODE']='live'
        for env,key_name in [('MODEL_NAME','model_name'),('MODEL_PROTOCOL','model_protocol'),('MODEL_BASE_URL','model_base_url'),('MODEL_KEY_ENV','model_key_env'),('REASONING_EFFORT','reasoning_effort')]:
            os.environ[env]=str(cfg[key_name])
        from app.main import create_app
        app=create_app(output/'evaluation.sqlite3',mode='live',worker=True)
        store=app.state.store
        local=datetime.now(TZ)
        formal=market_state(local)['trading_day'] is True and '15:30' <= local.strftime('%H:%M') < '18:00'
        summary['validation_kind']='formal' if formal else 'exploratory'
        cfg.update(debounce_seconds=0,scheduled_research_enabled=formal,market_refresh_enabled=False,
            forecast_evaluation_enabled=True,max_model_calls=16,max_total_agents=3,max_concurrent_agents=2,
            max_agent_turns=12,max_job_attempts=1,analysis_timeout_seconds=args.timeout,agent_timeout_seconds=min(240,args.timeout),
            max_output_tokens=3000,max_tool_result_chars=6000,max_context_chars=100000,read_max_chars=6000,news_queries=[])
        store.set_meta('settings',cfg)
        provider=store.list_providers()[0]
        summary['provider_catalog']=await store.discover_models(provider['id'],force=True)
        if not summary['provider_catalog'].get('selectable'):
            summary['stopped_at']='production directory not selectable';return 2
        for role in store.list_agent_roles():
            store.update_agent_definition(role['role_id'],{'default_binding':{'provider_id':provider['id'],'model_id':selected[0],'reasoning_effort':cfg['reasoning_effort']}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as api:
            response=await api.put('/api/universe',json={'watchlist':[],'candidates':['SZ:159278']});response.raise_for_status()
            for name,quantity,cash in [('A','10000','10000'),('B','0','20000')]:
                response=await api.post('/api/accounts',json={'name':'第七轮独立真实验收 '+name,'cash':cash,
                    'holdings':[{'symbol':'SZ:159278','quantity':quantity}] if quantity!='0' else [],
                    'preferences':{'risk':'balanced','horizon':'研究159278机器人ETF鹏华、跟踪指数与敞口。自主选择必要的具名子角色核对行情或基金原始资料；资料不足可弃权。finish_analysis会立即发布最终结果，请仅在最终报告完成时调用，不要用占位或测试正文试调用。简明说明实际证据与缺口，完成报告及1/5/20交易日预测。'}})
                response.raise_for_status();summary['accounts'].append(response.json()['id'])
            a,b=summary['accounts']
            if selected[1]!=selected[0]:
                for role in store.list_agent_roles():
                    response=await api.put(f'/api/accounts/{b}/agent-models/{role["role_id"]}',json={'provider_id':provider['id'],'model_id':selected[1],'reasoning_effort':cfg['reasoning_effort']});response.raise_for_status()
            summary['roles']={aid:(await api.get(f'/api/accounts/{aid}/agent-models')).json() for aid in (a,b)}
            summary['same_complete_role_directory']=len({tuple(sorted(r['role_id'] for r in rows)) for rows in summary['roles'].values()})==1
            for aid in (a,b):
                response=await api.put(f'/api/accounts/{aid}/forecast-settings',json={'enabled':True});response.raise_for_status()
            async def collect(aid,label):
                job=store.job(store.jobs(aid)[0]['id']);events=all_events(store,job['id'])
                entry={'account_id':aid,'label':label,'job':job,'event_counts':dict(Counter(e['type'] for e in events)),'events':events}
                summary['rounds'].append(entry);checkpoint();return entry
            async def wait_accounts(accounts):
                end=asyncio.get_running_loop().time()+args.timeout+90;previous=None
                while asyncio.get_running_loop().time()<end:
                    jobs=[store.jobs(aid)[0] for aid in accounts]
                    states=[(j['id'],j['state']) for j in jobs]
                    if states!=previous:
                        print(json.dumps({'stage':'production_worker','states':states}),flush=True);previous=states
                    if all(j['state'] in ('completed','failed','waiting_config','cancelled') for j in jobs):return
                    await asyncio.sleep(1)
                summary.setdefault('timeouts',[]).append(accounts)
            if formal:
                # Use the production scheduler at the real current time before
                # starting its worker; queued account-save research must not race
                # ahead of the required exact closing-baseline preparation.
                await app.state.service.scheduler_tick()
                try:
                    if app.state.service.forecast_task:
                        await asyncio.wait_for(app.state.service.forecast_task,
                            timeout=cfg.get('forecast_cycle_timeout_seconds', 45) + 5)
                except TimeoutError:
                    summary['formal_preparation_timeout']=True
                batches=store.forecast_batches(limit=200)
                ready={batch['account_id'] for batch in batches if batch['kind']=='formal'
                       and batch['base_date']==local.date().isoformat() and batch.get('job_id')}
                summary['formal_preparation']={'ready_accounts':sorted(ready),
                    'blocked_accounts':[aid for aid in (a,b) if aid not in ready], 'batches':batches}
                if ready != {a,b}:
                    summary['stopped_at']='formal baseline or task binding unavailable; formal chain unverified'
                    summary['position_update']={'state':'not_triggered','reason':'A initial formal report not available'}
                    summary['batches']=batches
                    summary['windows']=store.forecast_window_runs(limit=200)
                    summary['formal_statistics']=store.forecast_statistics(kind='formal')
                    await app.state.service.stop()
                    return 2
            async with app.router.lifespan_context(app):
                # Normal scheduler/worker lifecycle; no timestamp override.
                await wait_accounts([a,b])
                initial_a=await collect(a,'initial');await collect(b,'initial')
                if initial_a['job']['state']=='failed' and initial_a['job'].get('synthesis_checkpoint',{}).get('available'):
                    response=await api.post(f'/api/jobs/{initial_a["job"]["id"]}/retry');response.raise_for_status()
                    await wait_accounts([a]);initial_a=await collect(a,'bounded_synthesis_retry')
                if initial_a['job']['state']=='completed':
                    before=store.account(b);team=store.freeze_agent_team(b);old=store.account(a)
                    with store.connect() as db:locked=[dict(r) for r in db.execute('SELECT * FROM forecasts WHERE account_id=? AND kind=\'formal\'',(a,))]
                    response=await api.put(f'/api/accounts/{a}',json={'name':old['name'],'cash':old['cash'],
                        'holdings':[{'symbol':'SZ:159278','quantity':'20000'}],'preferences':old['preferences'],'expected_version':old['version']});response.raise_for_status()
                    after=store.account(b)
                    summary['position_update']={'quantity':'20000','cash_unchanged':store.account(a)['cash']==old['cash'],
                        'b_input_unchanged':before==after,'b_team_unchanged':team['team_id']==store.freeze_agent_team(b)['team_id'],
                        'a_input_version':store.account(a)['version'],'old_report_status':store.analyses(a)[0]['status']}
                    await wait_accounts([a]);await collect(a,'position_update')
                    with store.connect() as db:summary['position_update']['formal_predictions_unchanged']=locked==[dict(r) for r in db.execute('SELECT * FROM forecasts WHERE account_id=? AND kind=\'formal\'',(a,))]
                else:summary['position_update']={'state':'not_triggered','reason':'A initial report did not succeed'}
            summary['batches']=store.forecast_batches(limit=200)
            summary['windows']=store.forecast_window_runs(limit=200)
            summary['forecast_page']=store.forecasts_page(kind=summary['validation_kind'],limit=200)
            summary['formal_statistics']=store.forecast_statistics(kind='formal')
            summary['exploratory_statistics']=store.forecast_statistics(kind='exploratory')
            summary['attempts']=store.forecast_attempts()
            summary['quotes']=store.cache('quotes')
        summary['final_job_states']={aid:store.jobs(aid)[0]['state'] for aid in summary['accounts']}
        placeholder = any(placeholder_report((r['job'].get('result') or {}).get('report','')) for r in summary['rounds'])
        return 0 if summary['final_job_states'] and all(state=='completed' for state in summary['final_job_states'].values()) and not placeholder else 2
    finally:
        httpx.AsyncClient.send=original_send
        summary['finished_at']=datetime.now(TZ).isoformat()
        summary['source_unchanged_during_run']=summary['source_sha256']==source_digest()
        summary['protected_file_observations']={k:{'unchanged':Path(ROOT/k).exists() and hashlib.sha256(Path(ROOT/k).read_bytes()).hexdigest()==v,
            'access_by_evaluator':'read_only_configuration' if k!='.env' else 'read_only_environment'} for k,v in protected.items()}
        summary['protected_files_unchanged']=all(v['unchanged'] for v in summary['protected_file_observations'].values())
        summary['research_delivery_review']=[{'account_id':r['account_id'],'label':r['label'],
            'report_chars':len((r['job'].get('result') or {}).get('report','')),
            'placeholder_observed':placeholder_report((r['job'].get('result') or {}).get('report','')),
            'direction_records':sum(f.get('direction') in ('up','down','sideways') for f in (r['job'].get('result') or {}).get('forecasts',[])),
            'abstentions':sum(f.get('direction')=='abstain' for f in (r['job'].get('result') or {}).get('forecasts',[])),
            'note':'记录发布状态与研究实质分别审阅；占位正文不视为研究完成。'} for r in summary['rounds']]
        checkpoint()
        print(json.dumps({'summary':str(output/'summary.json'),'states':[r['job']['state'] for r in summary['rounds']]}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--authorized-base-url',required=True)
    parser.add_argument('--model-a',required=True)
    parser.add_argument('--model-b')
    parser.add_argument('--settings-db',type=Path)
    parser.add_argument('--timeout',type=int,default=420)
    parser.add_argument('--preflight-only',action='store_true')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
