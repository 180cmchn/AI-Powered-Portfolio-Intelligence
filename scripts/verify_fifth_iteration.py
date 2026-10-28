#!/usr/bin/env python3
"""Opt-in 159278 evaluation using real HTTP, scheduler, worker and isolated SQLite.

Never changes daily settings, supplies discovery URLs, switches models or invents
prices/reports. A failed first report prevents the dependent A holding edit.
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
from app.calendar import market_state
from app.config import db_path, defaults, model_config_summary
from app.domain import TZ, now, quote_display_identity
from app.store import Store
from scripts.verify_live_chain import configuration, public_config, observation, write_json

SYMBOL = 'SZ:159278'
IDENTITY = {'symbol':SYMBOL, 'canonical_name':'机器人ETF鹏华', 'name':'机器人ETF鹏华', 'kind':'etf',
    'full_name':'鹏华国证机器人产业交易型开放式指数证券投资基金', 'quantity_unit':'份',
    'identity_source':{'url':'https://www.szse.cn/disclosure/notice/t20250807_615378.html',
        'checked_at':'2026-10-28', 'kind':'official_listing_identity_checked_by_developer'}}
PURPOSE = ('仅研究 SZ:159278 机器人ETF鹏华（鹏华国证机器人产业交易型开放式指数证券投资基金）。'
 '从代码和问题自主原生搜索，不预给URL；核对基金/指数身份及实际可得的费用、披露持仓、行业敞口、流动性和跟踪表现，缺项说明。'
 '基金不是公司；基金持仓/指数权重各注时点。区分链接、可见片段、本地原文和推论，价格与NAV/IOPV分开。'
 '把必要的独立核对问题交子Agent并整合其报告；不为凑数追问。及时prepare_synthesis后交简洁中文报告，写反向因素和复查条件，无充分依据可不新增建议。')
TERMINAL = {'completed','failed','waiting_config','cancelled','superseded'}


def code_identity():
    files = sorted([*ROOT.joinpath('app').rglob('*.py'), *ROOT.joinpath('app/prompts').glob('*.txt'),
                    *ROOT.joinpath('app/static').glob('*'), Path(__file__), ROOT/'scripts/inspect_fourth_browser.mjs'])
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    return {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'worktree_status':subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True).strip(),
        'application_sha256':digest.hexdigest(),'python':sys.version,
        'dependencies':{n:importlib.metadata.version(n) for n in ['fastapi','uvicorn','httpx','pydantic','pypdf']}}


def input_identity(account):
    return {key:account.get(key) for key in ('id','name','version','cash','preferences','holdings')}


def sample_record(quote):
    state = market_state()
    local = datetime.now(TZ)
    session = ('lunch_break' if state['trading_day'] and (11,30) <= (local.hour,local.minute) < (13,0)
               else state['phase'])
    return {'received_at':now(), 'market':state, 'session':session, 'quote':quote,
        'intraday_fresh':state['phase'] in ('morning','afternoon') and quote.get('fresh_for_triggers') is True
                        and quote.get('cache_hit') is False}


def account_payload(label, quantity, cash, risk):
    """Only bounded preferences enter the account schema; discovery uses PURPOSE."""
    return {'name':'第五轮 ETF 验收 '+label, 'cash':cash,
        'preferences':{'risk':risk,'horizon':'长期；只研究159278机器人ETF鹏华，复用公共研究并核算本账户敞口。按实际需要补查，报告注明资料时点与可见范围、反向因素及条件，可不新增建议。'},
        'holdings':[{'symbol':SYMBOL,'quantity':quantity}] if quantity!='0' else []}


def evaluation_config(config, timeout):
    """Use settings-API-valid bounds without importing the app or changing model identity."""
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 3600:
        raise ValueError('evaluation timeout must be an integer from 10 to 3600 seconds')
    return {**config, 'debounce_seconds':0, 'model_retries':min(1,config.get('model_retries',1)),
        'max_model_calls':min(30,config.get('max_model_calls',40)), 'max_job_attempts':1,
        'analysis_timeout_seconds':min(timeout,config.get('analysis_timeout_seconds',600)),
        'scan_interval_minutes':240,
        'news_queries':[PURPOSE], 'news_interval_minutes':1440,'news_start_hour':0,'news_end_hour':24,
        'scheduled_research_enabled':True}


def safe_http_failure(response):
    """Retain routing/status and known validation fields, never echoed inputs or response prose."""
    result = {'method':response.request.method, 'path':response.request.url.path,
              'status_code':response.status_code, 'field_errors':[]}
    fields = set(defaults()) | {'body','query','path','name','cash','preferences','risk','horizon',
        'holdings','symbol','quantity','cost_price','expected_version','max_position_pct','max_sector_pct',
        'model_allow_no_key','max_context_chars','max_total_agents','chat_use_max_completion_tokens',
        'read_timeout_seconds','read_max_bytes','read_max_chars','read_max_pdf_pages','read_cache_max_bytes','read_search_max_pages'}
    error_types = {'missing','value_error','extra_forbidden','less_than_equal','greater_than_equal',
        'less_than','greater_than','string_too_long','string_too_short','string_type','list_type',
        'int_parsing','int_type','float_parsing','bool_parsing','literal_error','finite_number'}
    try:
        body = response.json()
        detail = body.get('detail') if isinstance(body,dict) else None
    except ValueError:
        detail = None
    if isinstance(detail,list):
        for item in detail[:20]:
            if not isinstance(item,dict) or not isinstance(item.get('loc'),(list,tuple)):
                continue
            path = [str(part) if isinstance(part,int) or (isinstance(part,str) and part in fields)
                    else 'unrecognized_field' for part in item['loc'][:8]]
            error_type = item.get('type')
            result['field_errors'].append({'field':'.'.join(path),
                'error':error_type if isinstance(error_type,str) and error_type in error_types else 'validation_error'})
    return result


def collect_executions(store, selected_rounds):
    """Include every started execution, even one outside the planned four."""
    labels = {item['job_id']: item['label'] for item in selected_rounds}
    with store.connect() as db:
        public_ids = [row[0] for row in db.execute('SELECT id FROM global_runs WHERE attempts>0 ORDER BY created_at,id')]
        account_ids = [row[0] for row in db.execute('SELECT id FROM jobs WHERE attempts>0 ORDER BY created_at,id')]
    result = []
    for public, ids in ((True, public_ids), (False, account_ids)):
        for job_id in ids:
            run = store.global_run(job_id) if public else store.job(job_id)
            result.append({'label': labels.get(job_id, 'additional-public' if public else 'additional-account'),
                           **observation(store, run, public)})
    return result


def summarize_acceptance(summary, cfg):
    """Summarize persisted evidence; HTTP acceptance never proves applied effort."""
    executions = summary.get('all_executions', summary['rounds'])
    requests=[r for row in executions for e in row['events'] if e['type'] in ('model_usage','model_error') for r in e.get('payload',{}).get('requests',[])]
    summary['request_diagnostics']=requests
    events=[e for row in executions for e in row['events']]
    native_events=[v for e in events if e['type']=='native_search' for v in e.get('payload',{}).get('events',[])]
    native=[v for v in native_events if v.get('status')=='completed']
    roles={r.get('agent_id') for r in requests}
    expected_effort=cfg.get('reasoning_effort','medium')
    summary['request_accounting']={'http_requests':len(requests),
        'native_search_event_items':len(native_events), 'completed_native_search_items':len(native),
        'provider_reported_usage':[r.get('usage') for r in requests],
        'retries':sum(r.get('attempt',1)>1 for r in requests),
        'duration_ms':sum(r.get('duration_ms',0) for r in requests), 'billing_amount':None}
    summary['execution_inventory']={'planned_rounds':4, 'started_executions':len(executions),
        'public_executions':sum(row.get('scope')=='public' for row in executions),
        'account_executions':sum(row.get('scope')=='account' for row in executions),
        'additional_executions':[{'job_id':row.get('job_id'),'state':row['state'],'label':row.get('label')}
            for row in executions if row.get('label','').startswith('additional-')]}
    summary['research_quality']='pending_evidence_review' if any(r['result'].get('report') for r in summary['rounds']) else 'not_assessable_no_report'
    quotes=[sample.get('quote',{}) for sample in summary['quote_samples']]
    summary['checks']={'fixed_code':summary['same_application_code'],
        'execution_finished_without_error':not summary.get('execution_failure'),
        'all_started_executions_completed':bool(executions) and all(row['state']=='completed' for row in executions),
        'all_four_reports':len(summary['rounds'])==4 and all(r['state']=='completed' and r['result'].get('report') for r in summary['rounds']),
        'quote_identity_matches_159278':bool(quotes) and all(q.get('symbol')==SYMBOL and ''.join(str(q.get('name') or '').split())=='机器人ETF鹏华' for q in quotes),
        'quote_price_precision':bool(quotes) and all(q.get('price') is not None and len(str(q['price']).partition('.')[2])==3 for q in quotes),
        'three_intraday_fresh_samples':len(summary['quote_samples'])>=3 and all(s['intraday_fresh'] for s in summary['quote_samples']),
        'b_inputs_isolated':bool(summary['steps'].get('holding_edit',{}).get('prior_success_stale')) and summary['steps'].get('b_input_unchanged_by_a_edit',False),
        'successful_old_A_report_stale':summary['steps'].get('holding_edit',{}).get('prior_success_stale',False),
        'normal_scheduler_worker':bool(summary['steps'].get('scheduler_news_demand')),
        'native_search_observed':bool(native),
        'visible_child_report':any(e['type']=='agent_completed' and e.get('agent_id') not in (None,'manager') and e.get('payload',{}).get('report') for e in events),
        'effort_sent_consistently':bool(requests) and all(r.get('reasoning_effort_sent')==expected_effort for r in requests),
        'effort_request_accepted':any(r.get('reasoning_effort_sent')==expected_effort and r.get('request_accepted') is True for r in requests),
        'manager_child_synthesis_requests': 'manager' in roles and any(role not in (None,'manager') for role in roles) and any(r.get('stage')=='synthesis' for r in requests),
        'A_snapshot_metrics_recomputable':all(r['observations'].get('metrics_recomputable') for r in summary['rounds'] if r.get('label') in ('A','edited-A')) and any(r.get('label')=='edited-A' for r in summary['rounds']),
        'A_edited_quantity_cash':summary['steps'].get('accounts',{}).get('a',{}).get('cash')=='10000' and any(h.get('symbol')==SYMBOL and h.get('quantity')=='20000' for h in summary['steps'].get('accounts',{}).get('a',{}).get('holdings',[])),
        'browser_matches_persisted_API':bool(summary.get('browser',{}).get('ok'))}
    summary['mechanical_acceptance']=all(summary['checks'].values())
    return summary


async def verify(args):
    cfg, origin = configuration(args.settings_db)
    # No effort/model override: explicit persisted > env > project medium.
    cfg = evaluation_config(cfg,args.timeout)
    output=args.output.resolve(); output.mkdir(parents=True,exist_ok=False); output.chmod(0o700)
    summary={'test_kind':'real_159278_native_model_http_scheduler_worker','started_at':now(),
        'code':code_identity(),'configuration':{**public_config(cfg,origin),**model_config_summary(cfg)},
        'security':IDENTITY,
        'budget':{'max_model_calls_per_round':cfg['max_model_calls'],'planned_rounds':4,
            'note':'计划四轮不是费用硬上限；所有实际启动执行另计。HTTP请求上限不限制一次请求内多次原生搜索；未返回计费金额保持未知。'},
        'account_ids':{},'steps':{},'rounds':[],'quote_samples':[],
        'natural_price_trigger':'not_observed','new_material_event':'not_established_by_this_run',
        'controlled_trigger_recovery':'separate_deterministic_tests_not_real_endpoint_fault',
        'output_directory':str(output)}
    def save(): write_json(output/'summary.json',summary,cfg)
    save()
    if not summary['configuration']['configured']:
        summary['blocked']='model_not_configured';save();return 78
    store=Store(output/'evaluation.sqlite3'); store.set_meta('settings',cfg)
    store.set_meta('universe',{'watchlist':[SYMBOL],'candidates':[SYMBOL]})
    store.put_cache('securities',SYMBOL,IDENTITY)
    store.set_meta('schedule:candidate_scan',int(time.time() // (cfg['scan_interval_minutes'] * 60)))
    with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    env={**os.environ,'APP_DB_PATH':str(store.path),'APP_MODE':'live','APP_HOST':'127.0.0.1','APP_PORT':str(port)}
    log=open(output/'server.log','w')
    process=subprocess.Popen([sys.executable,'scripts/serve.py'],cwd=ROOT,env=env,stdout=log,stderr=log)
    summary['server']={'pid':process.pid,'port':port,'normal_worker':True};save()
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=60,trust_env=False) as http:
        async def request(method,path,payload=None):
            response=await http.request(method,path,json=payload)
            if response.is_error:
                summary.setdefault('http_failures',[]).append(safe_http_failure(response));save()
            response.raise_for_status();return response.json()
        async def wait_account(aid):
            deadline=time.monotonic()+cfg['analysis_timeout_seconds']+90
            while time.monotonic()<deadline:
                account=await request('GET',f'/api/accounts/{aid}')
                job=await request('GET',f"/api/jobs/{account['latest_job_id']}")
                if job['state'] in TERMINAL:return job
                await asyncio.sleep(1)
            raise TimeoutError('account worker deadline')
        async def sampling():
            # Independent source sampling, two normal 60s refresh periods; never
            # force cached timestamps forward. Persist each sample immediately.
            from app.providers import MarketProvider
            for i in range(3):
                if i: await asyncio.sleep(max(60,cfg.get('quote_interval_seconds',60)))
                quote=quote_display_identity(await MarketProvider(cfg).quote(SYMBOL),IDENTITY)
                summary['quote_samples'].append(sample_record(quote));save()
                print(json.dumps({'stage':'quote','sample':i+1,'phase':summary['quote_samples'][-1]['session'],'fresh':quote.get('fresh_for_triggers')}),flush=True)
        async def record(job,label,public=False):
            obs=observation(store,store.global_run(job['id']) if public else store.job(job['id']),public)
            summary['rounds'].append({'label':label,**obs})
            write_json(output/f'round-{len(summary["rounds"])}-{label}.json',obs,cfg)
            if obs['result'].get('report'):
                (output/f'report-{label}.md').write_text(obs['result']['report'],encoding='utf-8')
            save();print(json.dumps({'stage':label,'state':obs['state'],'error':obs['error']},ensure_ascii=False),flush=True)
            return obs
        samples=None
        try:
            for _ in range(120):
                try:
                    status=await request('GET','/api/status')
                    if status['worker']['running']:break
                except (httpx.HTTPError,ValueError):pass
                await asyncio.sleep(.25)
            else:raise TimeoutError('worker startup')
            samples=asyncio.create_task(sampling())
            deadline=time.monotonic()+cfg['analysis_timeout_seconds']+90; run=None; scheduler_stopped=False
            while time.monotonic()<deadline:
                runs=await request('GET','/api/global-research')
                with store.connect() as db:
                    news_demands=[dict(r) for r in db.execute("SELECT * FROM research_demands WHERE kind='news_observation' AND job_id IS NOT NULL")]
                news_runs={d['job_id'] for d in news_demands}
                matches=[r for r in runs if r['id'] in news_runs]
                if matches:
                    run=next((r for r in matches if run and r['id']==run['id']),min(matches,key=lambda r:r['created_at']))
                    if not scheduler_stopped:
                        settings=await request('GET','/api/settings'); settings.update(scheduled_research_enabled=False,news_queries=[])
                        await request('PUT','/api/settings',settings)
                        summary['steps']['scheduler_stopped_after_news_dispatch']={'at':now(),'job_id':run['id']}
                        scheduler_stopped=True;save()
                    if run['state'] in TERMINAL:break
                await asyncio.sleep(1)
            if not run or run['state'] not in TERMINAL:raise TimeoutError('public worker deadline')
            with store.connect() as db:
                summary['steps']['scheduler_news_demand']=[dict(r) for r in db.execute("SELECT * FROM research_demands WHERE kind='news_observation' AND job_id=?",(run['id'],))]
            public=await record(run,'public',True)
            # Even an unusable upstream must leave independently functioning
            # accounts/quotes and visible failed tasks through the normal path.
            accounts=[]
            for label,quantity,cash,risk in [('A','10000','10000','conservative'),('B','0','20000','balanced')]:
                account=await request('POST','/api/accounts',account_payload(label, quantity, cash, risk))
                accounts.append(account);summary['account_ids'][label.lower()]=account['id'];save()
                await record(await wait_account(account['id']),label)
            a,b=accounts
            before=input_identity(await request('GET',f"/api/accounts/{b['id']}"))
            prior=store.analyses(a['id'])
            if prior and any(r['status']=='current' for r in prior):
                current=await request('GET',f"/api/accounts/{a['id']}")
                edited=await request('PUT',f"/api/accounts/{a['id']}",{'name':current['name'],'cash':'10000',
                    'preferences':current['preferences'],'holdings':[{'symbol':SYMBOL,'quantity':'20000'}],
                    'expected_version':current['version']})
                summary['steps']['holding_edit']={'previous_version':current['version'],'new_version':edited['version'],
                    'had_prior_success':True,'prior_success_stale':all(store.analysis(r['id'])['status']=='stale' for r in prior)}
                await record(await wait_account(a['id']),'edited-A')
            else:summary['steps']['holding_edit']={'state':'not_run','reason':'A_initial_report_not_successful'}
            summary['steps']['b_input_unchanged_by_a_edit']=before==input_identity(await request('GET',f"/api/accounts/{b['id']}"))
            if samples:await samples
            summary['steps']['accounts']={key:await request('GET',f'/api/accounts/{aid}') for key,aid in summary['account_ids'].items()}
            summary['steps']['securities']=store.cache('securities')
            summary['steps']['quote_records']=store.cache('quotes')
            save()
            browser=await asyncio.to_thread(subprocess.run,['node','scripts/inspect_fourth_browser.mjs',f'http://127.0.0.1:{port}',str(output)],cwd=ROOT,capture_output=True,text=True,timeout=180)
            try:summary['browser']=json.loads(browser.stdout)
            except ValueError:summary['browser']={'ok':False,'exit_code':browser.returncode,'error':browser.stderr[-2000:]}
        except Exception as exc:
            summary['execution_failure']={'type':type(exc).__name__}
        finally:
            if samples and not samples.done():
                samples.cancel();await asyncio.gather(samples,return_exceptions=True)
            # Stop the worker before the final inventory, so cancelled in-flight
            # requests and unexpected background executions are also accounted.
            if process.poll() is None:
                process.terminate()
                try:await asyncio.to_thread(process.wait,15)
                except subprocess.TimeoutExpired:
                    process.kill();await asyncio.to_thread(process.wait)
                    summary['execution_failure']={'type':'ForcedServerShutdown'}
            log.close()
            summary['finished_at']=now();summary['final_code']=code_identity()
            summary['same_application_code']=summary['code']['application_sha256']==summary['final_code']['application_sha256']
            summary['all_executions']=collect_executions(store,summary['rounds'])
            summarize_acceptance(summary, cfg)
            write_json(output/'materials.json',store.materials(),cfg);save()
            print(json.dumps({'output':str(output),'checks':summary['checks'],'failure':summary.get('execution_failure')},ensure_ascii=False),flush=True)
    return 0 if summary['mechanical_acceptance'] else 2

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings-db',type=Path,default=db_path('live'))
    parser.add_argument('--output',type=Path,default=ROOT/'data/evaluations'/('fifth-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--timeout',type=int,default=600)
    raise SystemExit(asyncio.run(verify(parser.parse_args())))
