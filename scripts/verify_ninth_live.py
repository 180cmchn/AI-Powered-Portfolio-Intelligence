#!/usr/bin/env python3
"""Authorized, bounded current-configuration acceptance on an independent DB and TCP port.

Existing configured endpoint/key reference only; no protocol fallback, clock change,
public-model research, broker operation or daily-service restart. Reports are evidence,
not automatically an investment-quality pass. Stop this independent process at end.
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
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from app.calendar import market_state
from app.config import db_path,model_config_summary
from app.domain import TZ,now
from app.model import ModelClient
from app.providers import MarketProvider
from app.runtime_version import source_digest, env_file_stamp
from app.service import ResearchTools
from app.store import Store
from scripts.verify_live_chain import configuration,public_config,scrubbed,all_events

async def run(args):
    cfg,origin=configuration(db_path('live'))
    if cfg['model_protocol']!='responses':
        raise ValueError('Current authorized protocol differs; no automatic protocol changes')
    if not os.getenv(cfg['model_key_env']):
        raise ValueError('Current configured credential is unavailable')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
    if not cfg.get('model_name'):
        raise ValueError('No explicitly configured model; select one from the real directory first')
    cfg.update(debounce_seconds=0,
        scheduled_research_enabled=False,market_refresh_enabled=False,forecast_evaluation_enabled=True,
        news_queries=[],max_model_calls=16,max_total_agents=3,max_concurrent_agents=2,max_agent_turns=12,
        max_job_attempts=1,analysis_timeout_seconds=420,agent_timeout_seconds=240,max_output_tokens=3000,
        max_tool_result_chars=6000,max_context_chars=100000,read_max_chars=6000,model_responses_stream=True)
    os.environ['MODEL_NAME']=cfg['model_name'];os.environ['MODEL_PROTOCOL']=cfg['model_protocol']
    # Store's initial provider migration reads defaults before set_meta(settings).
    # Bind it to the same current configuration used by the authorized probe.
    os.environ['MODEL_BASE_URL']=cfg['model_base_url'];os.environ['MODEL_KEY_ENV']=cfg['model_key_env']
    os.environ['REASONING_EFFORT']=cfg.get('reasoning_effort','medium');os.environ['APP_DB_PATH']=str(out/'evaluation.sqlite3')
    source_at_start=source_digest()
    verification_paths = [Path(__file__).resolve(), ROOT/'scripts/serve.py', ROOT/'scripts/verify_live_chain.py',
                          ROOT/'requirements.txt', ROOT/'package-lock.json', ROOT/'uv.lock']
    verification_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in verification_paths}
    env_stamp=env_file_stamp()
    summary={'kind':'real_ninth_production_api_worker','started_at':now(),'market_state':market_state(),
             'source_sha256':source_at_start,'verification_file_sha256':verification_hashes,'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
             'git_status':subprocess.check_output(['git','status','--short'],text=True).splitlines(),
             'configuration':public_config(cfg,origin),'rounds':[],
             'dependencies':{n:importlib.metadata.version(n) for n in ('httpx','fastapi','uvicorn','pydantic','pypdf')},
             'budget':{'jobs':3,'max_model_calls_per_job':16,'max_additional_synthesis_retry_jobs':1,
                       'job_seconds':420,'max_total_agents':3,'native_probe_requests':1},
             'daily_service_restarted':False,'daily_database_migrated':False}
    def save():
        (out/'summary.json').write_text(scrubbed(summary,cfg));(out/'summary.json').chmod(0o600)
    process=None;store=None;log=None
    try:
        store=Store(out/'evaluation.sqlite3',mode='live');store.set_meta('settings',cfg)
        # Initial independent virtual setup. No public research demand is generated.
        store.set_meta('universe',{'watchlist':[],'candidates':['SZ:159278']})
        provider=store.list_providers()[0]
        if provider['base_url'].rstrip('/') != cfg['model_base_url'].rstrip('/') or provider['protocol'] != cfg['model_protocol']:
            raise ValueError('Independent provider identity differs from the current authorized connection')
        catalog=await store.discover_models(provider['id'],force=True)
        summary['catalog']={k:v for k,v in catalog.items() if k!='models'}
        model_ids=[v['id'] if isinstance(v,dict) else v for v in catalog.get('models',[])]
        summary['directory_models']=model_ids
        if not catalog.get('selectable') or cfg['model_name'] not in model_ids:
            raise ValueError('Authorized model absent from current selectable directory')
        for role in store.list_agent_roles():
            store.update_agent_definition(role['role_id'],{'default_binding':{'provider_id':provider['id'],'model_id':cfg['model_name'],'reasoning_effort':cfg.get('reasoning_effort','medium')}})
        market=MarketProvider(cfg);summary['security']=await market.security('SZ:159278')
        summary['quote_preflight']=await market.quote('SZ:159278')
        tool=ResearchTools(store,{'id':'isolated-tool-preflight','account_id':None},cfg)
        rowview=await tool.history('SZ:159278',start='2026-09-01',end=datetime.now(TZ).date().isoformat(),limit=1000)
        summary['structured_preflight']={'latest_rows':rowview}
        raw_history=json.loads(store.material(rowview['material_id'])['content'])
        summary['identity_verification']={'quote_name':summary['quote_preflight'].get('name') or summary['security'].get('name'),
            'quote_source':summary['quote_preflight'].get('source'),
            'history_name':raw_history.get('name'),'history_symbol':raw_history.get('symbol'),
            'history_source':raw_history.get('source'),'history_fetched_at':raw_history.get('fetched_at'),
            'basis':'exact requested SZ:159278 in actual provider metadata; observed canonical Eastmoney name or Sina abbreviation 机器人PH; quote timestamp retained'}
        quote_identity=(summary['security'].get('name')=='机器人ETF鹏华' and summary['security'].get('kind')=='etf') or (summary['quote_preflight'].get('name') in ('机器人ETF鹏华','机器人PH') and summary['quote_preflight'].get('symbol')=='SZ:159278' and summary['quote_preflight'].get('status') in ('ok','closed','stale','delayed') and summary['quote_preflight'].get('price') is not None)
        history_identity=raw_history.get('status')=='ok' and raw_history.get('symbol')=='SZ:159278' and raw_history.get('name')=='机器人ETF鹏华'
        if not (quote_identity or history_identity):
            summary['stopped_reason']='Actual identity unavailable from current security, quote/fallback and exact-symbol history; no substitute data.'
            raise ValueError('Actual identity unavailable; no fixed identity substitute')
        if rowview.get('row_count'):
            start=rowview['rows'][min(19,len(rowview['rows'])-1)]['time'][:10]
            summary['structured_preflight']['calculation']=await tool.market_indicators(rowview['material_id'],start=start,window=min(20,rowview['row_count']))
            summary['structured_preflight']['exact_day']=await tool.query_market_rows(rowview['material_id'],start='2026-10-28',end='2026-10-28',fields=['close'])
        # One true native search request, target limited to the authorized security.
        client=ModelClient({**cfg,'model_retries':0,'model_request_timeout_seconds':60,'max_output_tokens':1000})
        try:
            session=client.session('Use native web_search once to find an official Penghua source for SZ:159278 机器人ETF鹏华. Return source title and URL, visible facts and any uncertainty. A URL alone does not prove you read a document.', 'Small real-source capability check for SZ:159278 only.')
            session.agent_id='ninth-preflight';session.phase='native_search_probe'
            turn=await client.complete(session,[],tool_choice='required')
            summary['native_probe']={'text':turn.text,'events':turn.native_events,'sources':turn.sources,'requests':client.requests}
        except Exception as exc:
            summary['native_probe']={'error_type':type(exc).__name__,'requests':client.requests}
        finally:
            await client.aclose()
        if source_digest() != source_at_start:
            raise RuntimeError('Application changed during preflight; restart acceptance with fixed final code')
        s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
        env={**os.environ,'APP_MODE':'live','APP_DB_PATH':str(store.path),'APP_HOST':'127.0.0.1','APP_PORT':str(port)}
        log=(out/'service.log').open('w')
        process=subprocess.Popen([sys.executable,'scripts/serve.py'],cwd=ROOT,env=env,stdout=log,stderr=log)
        summary['independent_service']={'port':port,'pid':process.pid};save()
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=35,trust_env=False) as api:
            for _ in range(100):
                try:
                    response=await api.get('/api/status');response.raise_for_status();break
                except httpx.HTTPError:
                    if process.poll() is not None:raise RuntimeError('Independent server exited')
                    await asyncio.sleep(.2)
            else:raise TimeoutError('Independent service startup')
            summary['startup_status']=response.json()
            summary['basic_routes']={}
            for route in ('/api/health','/api/providers','/api/agent-roles','/api/accounts','/api/forecasts','/api/research','/api/environment-connection'):
                checked=await api.get(route);checked.raise_for_status();summary['basic_routes'][route]=checked.status_code
            loaded = summary['startup_status'].get('runtime_version', {})
            summary['startup_source_matches'] = loaded.get('source_sha256_at_start') == source_at_start
            if not summary['startup_source_matches']:
                raise RuntimeError('Independent service does not identify the frozen acceptance source')
            async def request(method,path,**kw):
                r=await api.request(method,path,**kw);r.raise_for_status();return r.json()
            preference={'risk':'balanced','horizon':'研究159278机器人ETF鹏华。用已取得事实说明价格趋势、相对基准和持仓集中度；可按需使用具名子角色。行情已保存可按行查询，按日期比较；公开原文访问失败时解释具体缺口与可用替代证据。成本未知保持未知。给出1/5/20交易日方向判断或明确弃权，说明支持和反向证据，不要为了验收强行预测。'}
            # A and B are created with the same complete global role catalog.
            a=await request('POST','/api/accounts',json={'name':'第九轮 A 独立验收','cash':'10000','holdings':[{'symbol':'SZ:159278','quantity':'10000'}],'preferences':preference})
            # Keep B idle until A completes by creating it only afterward; no hidden cancellation of paid work.
            summary['accounts']={'a':a['id']};save()
            async def wait_job(aid):
                last=None
                async with asyncio.timeout(510):
                    while True:
                        jobs=await request('GET',f'/api/accounts/{aid}/jobs')
                        job=await request('GET','/api/jobs/'+jobs[0]['id'])
                        sig=(job['id'],job['state'])
                        if sig!=last:
                            print(json.dumps({'stage':'worker','job_id':job['id'],'state':job['state']}),flush=True);last=sig
                        if job['state'] in ('completed','failed','waiting_config','cancelled','superseded'):return job
                        await asyncio.sleep(1)
            def collect(job,label):
                events=all_events(store,job['id'])
                summary['rounds'].append({'label':label,'sampled_market_state':market_state(),'job':job,'events':events,
                    'event_counts':dict(Counter(e['type'] for e in events)),
                    'report_delivery':bool((job.get('result') or {}).get('report')),'content_review':'pending_manual_review', 'calculation_visibility':[e for e in events if e.get('type') in ('synthesis_calculation_coverage','final_calculation_coverage','calculation_context_coverage','context_compacted','synthesis_context_prepared')]})
                save()
            first=await wait_job(a['id']);collect(first,'a_initial')
            if first['state']=='failed' and first.get('synthesis_checkpoint',{}).get('available'):
                await request('POST',f'/api/jobs/{first["id"]}/retry');first=await wait_job(a['id']);collect(first,'a_bounded_synthesis_retry')
            b=await request('POST','/api/accounts',json={'name':'第九轮 B 独立候选','cash':'20000','holdings':[],'preferences':preference})
            summary['accounts']['b']=b['id'];b_before=store.account(b['id'])
            summary['bindings']={aid:await request('GET',f'/api/accounts/{aid}/agent-models') for aid in (a['id'],b['id'])}
            if first['state']=='completed':
                old=store.account(a['id'])
                await request('PUT',f'/api/accounts/{a["id"]}',json={'name':old['name'],'cash':'10000','holdings':[{'symbol':'SZ:159278','quantity':'20000'}],'preferences':old['preferences'],'expected_version':old['version']})
                summary['position_update']={'old_report_status':store.analyses(a['id'])[0]['status'],
                    'version':store.account(a['id'])['version'],'cash':store.account(a['id'])['cash'],
                    'b_unchanged':store.account(b['id'])['holdings']==b_before['holdings'] and store.account(b['id'])['cash']==b_before['cash']}
                changed=await wait_job(a['id']);collect(changed,'a_position_update')
            candidate=await wait_job(b['id']);collect(candidate,'b_candidate')
            summary['final_status']=await request('GET','/api/status')
            summary['bindings_after']={aid:await request('GET',f'/api/accounts/{aid}/agent-models') for aid in (a['id'],b['id'])}
            summary['b_binding_unchanged']=summary['bindings_after'][b['id']]==summary['bindings'][b['id']]
            summary['b_input_version_unchanged']=store.account(b['id'])['version']==b_before['version']
            summary['batches']=store.forecast_batches(limit=200)
            summary['formal']=store.forecast_statistics(kind='formal');summary['exploratory']=store.forecast_statistics(kind='exploratory')
            summary['predictions']=store.forecasts_page(kind='exploratory',limit=200)
            summary['attempts']=store.forecast_attempts();summary['windows']=store.forecast_window_runs(limit=200)
            summary['quotes']=store.cache('quotes');summary['materials']=store.material_index(limit=200)
            with store.connect() as db:
                summary['public_research_count']=db.execute('SELECT count(*) FROM global_runs').fetchone()[0]
            summary['formal_window_verified']=False
            actual_window = market_state()
            summary['sampling_market_state'] = actual_window
            local_time = datetime.now(TZ).strftime('%H:%M')
            summary['default_formal_window_open_at_sampling'] = actual_window.get('trading_day') is True and '15:30' <= local_time < '18:00'
            summary['formal_window_note'] = ('Actual run outside default formal window; no clock/cutoff changes or formal backfill.'
                if not summary['default_formal_window_open_at_sampling'] else
                'This isolated run has scheduled research disabled; formal generation was not verified despite the current window being open.')
            summary['success']=all(r['job']['state']=='completed' for r in summary['rounds'] if r['label']!='a_initial' or first['state']!='completed') and any(r['label']=='a_position_update' for r in summary['rounds'])
            # Read-only page verification uses this process while it remains live.
            (out/'browser-ready.json').write_text(json.dumps({'url':f'http://127.0.0.1:{port}','account_id':a['id']}))
            if args.page_inspection_seconds:
                print(json.dumps({'stage':'page_ready','port':port,'seconds':args.page_inspection_seconds}),flush=True)
                await asyncio.sleep(args.page_inspection_seconds)
    except Exception as exc:
        summary['error_type']=type(exc).__name__;summary['success']=False
        print(json.dumps({'error_type':type(exc).__name__}),flush=True)
    finally:
        if process is not None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
            summary['independent_service_exit']=process.returncode
        if log:log.close()
        summary['finished_at']=now();summary['source_unchanged']=source_digest()==source_at_start
        summary['verification_files_unchanged'] = all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == digest
            for path, digest in verification_hashes.items())
        summary['env_file_metadata_unchanged']=env_file_stamp()==env_stamp
        save()
        print(json.dumps({'summary':str(out/'summary.json'),'success':summary.get('success'),'states':[r['job']['state'] for r in summary['rounds']]}),flush=True)
    return 0 if summary.get('success') else 2

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--page-inspection-seconds',type=int,default=0)
    raise SystemExit(asyncio.run(run(p.parse_args())))
