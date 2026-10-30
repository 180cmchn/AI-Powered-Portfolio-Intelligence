#!/usr/bin/env python3
"""One bounded real retry on an already isolated tenth-round evaluation DB.

Never accepts the daily database. Counts all recorded HTTP attempts before
reserving a remaining part of the 48-attempt acceptance budget. No report edits,
clock changes, lowered effort, increased request timeout or credential writes.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from app.config import db_path
from app.domain import now
from app.store import Store
from app.runtime_version import source_digest
from scripts.verify_live_chain import all_events, scrubbed


def request_count(store):
    with store.connect() as db:
        events=[json.loads(row[0]) for row in db.execute('SELECT body FROM events')]
    return sum(len(event.get('payload',{}).get('requests',[])) for event in events
               if event.get('type') in ('model_usage','model_error'))


async def run(args):
    database=args.database.resolve()
    if database == db_path('live').resolve() or not database.is_relative_to(ROOT/'data/evaluations'):
        raise ValueError('Only the independent evaluations directory is allowed')
    out=database.parent
    output=out/(args.label+'.json')
    if output.exists(): raise ValueError('Keep earlier acceptance evidence; use another label')
    store=Store(database)
    with store.connect() as db:
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise ValueError('Finish or explicitly cancel earlier isolated work before reserving a retry budget')
    previous=store.job(args.job)
    if store.account(previous['account_id'])['version'] != previous['input_version']:
        raise ValueError('Do not retry an obsolete input in this bounded recovery check')
    used=request_count(store)
    if used+args.max_calls > 48: raise ValueError('48-request acceptance budget would be exceeded')
    cfg=store.settings()
    cfg.update(max_model_calls=args.max_calls,scheduled_research_enabled=False,
               market_refresh_enabled=False,forecast_evaluation_enabled=False)
    store.set_meta('settings',cfg)
    manager=next(r for r in store.list_agent_roles(previous['account_id']) if r['role_id']=='manager')
    if manager['resolved']['reasoning_effort']!='max': raise ValueError('Current max binding changed')
    # Store's exact built-in prompt migration preserves custom definitions.
    digest=source_digest()
    result={'kind':'real_bounded_retry','label':args.label,'started_at':now(),
            'source_sha256':digest,'original_job_id':args.job,'requests_before':used,
            'max_additional_requests':args.max_calls,'acceptance_total_cap':48,
            'request_timeout_seconds':cfg['model_request_timeout_seconds'],
            'synthesis_request_timeout_seconds':cfg['synthesis_request_timeout_seconds'],
            'agent_timeout_seconds':cfg['agent_timeout_seconds'],'analysis_timeout_seconds':cfg['analysis_timeout_seconds'],
            'original_checkpoint':store.checkpoint_status(args.job)}
    def save(): output.write_text(scrubbed(result,cfg));output.chmod(0o600)
    process=log=None
    try:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        log=(out/(args.label+'.log')).open('w')
        process=subprocess.Popen([sys.executable,'scripts/serve.py'],cwd=ROOT,env={**os.environ,
            'APP_MODE':'live','APP_DB_PATH':str(database),'APP_PORT':str(port),'APP_HOST':'127.0.0.1'},stdout=log,stderr=log)
        result['instance']={'port':port,'pid':process.pid};save()
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=40,trust_env=False) as api:
            async def request(method,path,**kw):
                response=await api.request(method,path,**kw);response.raise_for_status();return response.json()
            for _ in range(100):
                try: result['health']=await request('GET','/api/health');break
                except httpx.HTTPError:
                    if process.poll() is not None:raise RuntimeError('Independent process exited')
                    await asyncio.sleep(.2)
            else:raise RuntimeError('Independent startup failed')
            if result['health']['runtime_version']['source_sha256_at_start'] != digest:
                raise RuntimeError('Independent process source differs from frozen acceptance source')
            job=await request('POST','/api/jobs/'+args.job+'/retry',json={'operation_id':args.label,
                'origin':{'page':'agents','action':'bounded-retry-current-version'}})
            result['accepted_job']=job;save()
            last=None
            async with asyncio.timeout(cfg['analysis_timeout_seconds']+45):
                while True:
                    job=await request('GET','/api/jobs/'+job['id'])
                    sig=(job['state'],job.get('stage'))
                    if sig!=last:print(json.dumps({'job':job['id'],'state':sig[0],'stage':sig[1]},ensure_ascii=False),flush=True);last=sig
                    if job['state'] not in ('queued','running'):break
                    await asyncio.sleep(1)
            events=all_events(store,job['id'])
            result.update(job=job,events=events,event_counts=dict(Counter(e['type'] for e in events)),
                requests_after=request_count(store),completed_at=now(),source_unchanged=source_digest()==digest,
                reused_checkpoint=any(e['type']=='synthesis_resuming' for e in events),
                report_published=bool(job.get('report_id')))
            if (job.get('result') or {}).get('report'):
                (out/(args.label+'-original.md')).write_text(job['result']['report'])
            save()
    except Exception as exc:
        result['acceptance_error']={'type':type(exc).__name__,'message':str(exc)};save();raise
    finally:
        if process:
            process.terminate()
            try:process.wait(timeout=12)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        if log:log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--job',required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--max-calls',type=int,choices=range(2,11),required=True)
    asyncio.run(run(parser.parse_args()))
