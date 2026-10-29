"""Restart final production API/worker on a copy of a completed evaluation DB.

Does not create new research or retry failed jobs. An optional bounded production
protocol probe validates the final transport instrumentation through the API.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def run(args):
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    output.chmod(0o700)
    target=output/'restarted.sqlite3'
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    os.environ['APP_DB_PATH']=str(target)
    os.environ['APP_MODE']='live'
    import httpx
    from app.main import create_app
    from app.domain import now
    from scripts.verify_seventh_live import validate_provider_destination,validate_request_destination
    from scripts.verify_live_chain import scrubbed
    from scripts.verify_sixth_live import source_digest
    app=create_app(target,mode='live',worker=True)
    store=app.state.store
    cfg=store.settings()
    validate_provider_destination(cfg['model_base_url'],'https://api.xxyapi.com/v1')
    summary={'kind':'post_patch_production_API_worker_restart','started_at':now(),'source_sha256':source_digest(),
        'input_database':str(args.database),'database':str(target),'jobs_before':{a['id']:store.jobs(a['id'])[0]['id'] for a in store.accounts()},
        'application_manifest':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'app').rglob('*')) if p.is_file() and '__pycache__' not in p.parts}}
    original=httpx.AsyncClient.send
    async def fenced(client,request,*a,**kw):
        validate_request_destination(request,'api.xxyapi.com')
        return await original(client,request,*a,**kw)
    httpx.AsyncClient.send=fenced
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',timeout=150) as api:
            async with app.router.lifespan_context(app):
                await asyncio.sleep(1)
                for name,url in [('status','/api/status'),('formal','/api/forecasts'),('exploratory','/api/forecasts?kind=exploratory'),('windows','/api/forecast-window-runs')]:
                    response=await api.get(url);response.raise_for_status();data=response.json()
                    summary[name]={'http_status':response.status_code,'page_total':data.get('page',{}).get('total'),'expected':data.get('statistics',{}).get('expected')}
                if args.probe:
                    response=await api.post('/api/settings/probe')
                    summary['protocol_probe']={'http_status':response.status_code,'result':response.json()}
                summary['jobs_after']={a['id']:store.jobs(a['id'])[0]['id'] for a in store.accounts()}
                summary['no_research_jobs_created']=summary['jobs_before']==summary['jobs_after']
    finally:
        httpx.AsyncClient.send=original
        summary['finished_at']=now()
        summary['source_unchanged']=summary['source_sha256']==source_digest()
        (output/'summary.json').write_text(scrubbed(summary,cfg),encoding='utf-8')
        (output/'summary.json').chmod(0o600)
        print(json.dumps({'summary':str(output/'summary.json'),'no_research_jobs_created':summary.get('no_research_jobs_created')}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--probe',action='store_true')
    asyncio.run(run(p.parse_args()))
