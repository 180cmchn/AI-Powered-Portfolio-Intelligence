#!/usr/bin/env python3
"""Explicit one- or two-request business checkpoint recovery; offline by default."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
from contextlib import contextmanager, closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import metrics, now
from app.store import Store
from app.runtime_version import source_digest, env_file_stamp
from scripts import verify_fifteenth_business as business

EVALUATION_ROOT = ROOT / 'data/evaluations'
KIND = 'fifteenth_business_checkpoint_recovery_v1'
CAP = 2


def requested_cap(value=None):
    value = CAP if value is None else value
    if type(value) is not int or value not in (1, 2):
        raise ValueError('Recovery maximum must be exactly one or two HTTP attempts')
    return value


def check_allowance(parent, cap):
    if parent['used']+cap > parent['maximum']:
        raise ValueError('Requested recovery exceeds the unchanged parent remaining allowance')


class ReadOnlyStore(Store):
    """Existing read projections only; no Store constructor/migrations/recovery."""
    def __init__(self, path):
        self.path, self.mode = Path(path), 'live'

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db


def read(path):
    return json.loads(Path(path).read_text())


def parent_identity(parent):
    parent = Path(parent).resolve()
    summary = read(parent/'summary.json')
    if (summary.get('kind') != 'fifteenth_business_stage' or summary.get('stage') != 'a_initial'
            or not summary.get('execution_stopped') or summary.get('published')
            or summary.get('contract_accepted')):
        raise ValueError('Only the stopped failed A initial execution can be recovered')
    store = ReadOnlyStore(parent/'evaluation.sqlite3')
    job = store.job(summary['job_id'])
    if job['state'] != 'failed' or job['account_id'] != summary['account_id']:
        raise ValueError('Parent failed task identity differs')
    with ReadOnlyStore(parent/'business-attempts.sqlite3').connect() as db:
        maximum = db.execute('SELECT maximum FROM budget').fetchone()[0]
        used = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
        stops = [dict(row) for row in db.execute('SELECT * FROM stops')]
        authorization = json.loads(db.execute('SELECT body FROM business_authorization').fetchone()[0])
    if type(maximum) is not int or not 1 <= used <= maximum <= 40:
        raise ValueError('Parent must retain a supported positive stage cap and actual used attempts')
    if not any(item['reason']=='accepted_or_sent_request_outcome_unknown' for item in stops):
        raise ValueError('Original unknown-outcome stop must remain intact')
    frozen = authorization.get('frozen') or {}
    if (authorization.get('kind') != business.BUDGET_KIND or authorization.get('stage') != 'a_initial'
            or frozen.get('source_sha256') != summary.get('source_sha256')):
        raise ValueError('Parent source and independent authorization identities differ')
    account = store.account(job['account_id'])
    if account['version'] != job['input_version'] or account['version'] != 4:
        raise ValueError('Recovery requires unchanged A input version four')
    ledger = business.ledger_facts(store.ledger(account['id']))
    if ledger != business.ledger_facts(summary['prepared_ledger']):
        raise ValueError('Parent ledger changed after the committed three buys')
    return {'execution': str(parent), 'summary_sha256': business.file_digest(parent/'summary.json'),
        'database_sha256': business.db_digest(store.path),
        'ledger_database_sha256': business.db_digest(parent/'business-attempts.sqlite3'),
        'source_sha256': summary['source_sha256'], 'job_id': job['id'], 'account_id': account['id'],
        'input_version': account['version'], 'ledger_facts_sha256': business.digest(ledger),
        'maximum': maximum, 'used': used, 'stops': stops, 'frozen': frozen}


def checkpoint_context(store, job):
    from app.service import Service
    account = store.account(job['account_id'])
    universe = job.get('universe') or store.universe_snapshot()['universe']
    # Use the same read-only freshness projection as Service, not raw cache
    # status (which can become stale without a database write).
    quotes = Service.quotes(SimpleNamespace(store=store))
    target = job.get('target_security')
    return {'account': account, 'metrics': metrics(account, quotes, store.cache('securities')),
        'candidate_quotes': {key: value for key,value in quotes.items() if key in universe['candidates']},
        'target_security': {'symbol': target, 'quote': quotes.get(target)} if target else None,
        'universe_version': job.get('universe_version') or store.universe_snapshot()['universe_version'],
        'forecast_batch': store.forecast_job_context(job['id'])}


def eligible_checkpoint(database, parent_job_id):
    store = ReadOnlyStore(database)
    job = store.job(parent_job_id)
    diagnostic = store.checkpoint_recovery_diagnostic(job, checkpoint_context(store, job))
    if not diagnostic.get('eligible') or diagnostic['state'] not in ('synthesis_ready', 'partial_synthesis_ready'):
        raise ValueError('Synthesis-only checkpoint is unavailable: '+diagnostic['state'])
    with store.connect() as db:
        row = dict(db.execute('SELECT * FROM synthesis_checkpoints WHERE job_id=?',(parent_job_id,)).fetchone())
    body = json.loads(row['body'])
    return {'created_at': row['created_at'], 'resume_count': row['resume_count'],
        'body_sha256': business.digest(body), 'guard': row['guard'],
        'max_age_seconds': job['config_snapshot'].get('synthesis_checkpoint_max_age_seconds',1800),
        'diagnostic': diagnostic, 'partial_research': bool(body.get('partial_research')),
        'report_count': len(body.get('reports',[])), 'calculation_count': len(body.get('calculations',[])),
        'material_index_count': len(body.get('material_ids',[])),
        'material_delivery_count': len(body.get('material_deliveries',[])),
        'visibility_boundary': 'Only the production checkpoint projection is delivered. Saved original materials/receipts outside that projection are not newly read. No historical package or missing financial rows are injected.'}


def validate_parent(expected):
    actual = parent_identity(expected['execution'])
    if actual != expected:
        raise ValueError('Parent summary, database, source identity or stopped ledger changed')


def claim_recovery(manifest, preparation_path, note):
    """One independent recovery authorization across all prepared copies."""
    key=business.digest({key:manifest['parent'][key] for key in ('job_id','account_id','source_sha256')})
    directory=EVALUATION_ROOT/'.fifteenth-recovery-claims'
    directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    path=directory/(key+'.json')
    body={'kind':KIND,'parent_job_id':manifest['parent']['job_id'],
        'parent_source_sha256':manifest['parent']['source_sha256'],'source_sha256':manifest['source_sha256'],
        'preparation_sha256':business.file_digest(preparation_path),'maximum':requested_cap(manifest['maximum_http_attempts']),
        'authorization_note':note,'claimed_at':now()}
    # Exclusive creation is the shared atomic claim; never clear it on failure.
    with path.open('x') as stream:
        path.chmod(0o600);json.dump(body,stream,ensure_ascii=False,indent=2)
    return {'path':str(path),'sha256':business.file_digest(path)}


def prepare(args):
    from app.agents import MANAGER_TOOLS
    cap = requested_cap(getattr(args,'max_http_attempts',None))
    identity = parent_identity(args.parent_execution)
    check_allowance(identity, cap)
    checkpoint = eligible_checkpoint(Path(identity['execution'])/'evaluation.sqlite3', identity['job_id'])
    frozen = identity['frozen']
    if (env_file_stamp()!=frozen['environment_stamp'] or
            business.configuration_digest(frozen['configuration_source'],frozen['reference_account'])!=frozen['configuration_sha256']):
        raise ValueError('Source configuration or environment changed after A execution')
    output = args.output.resolve()
    if not output.is_relative_to(EVALUATION_ROOT.resolve()) or output==EVALUATION_ROOT.resolve():
        raise ValueError('Use a new isolated evaluation child directory')
    output.mkdir(parents=True,exist_ok=False);output.chmod(0o700)
    target=output/'prepared.sqlite3'
    business.copy_database(Path(identity['execution'])/'evaluation.sqlite3',target)
    # Only the copied run allowance changes; all generation/time/route settings stay.
    with sqlite3.connect(target) as db:
        cfg=json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        cfg.update(max_model_calls=cap,synthesis_max_calls=cap,final_submission_reserved_calls=cap,
                   model_retries=0,max_job_attempts=1,debounce_seconds=600,
                   scheduled_research_enabled=False,market_refresh_enabled=False,forecast_evaluation_enabled=False)
        db.execute("UPDATE meta SET value=? WHERE key='settings'",(json.dumps(cfg,ensure_ascii=False),))
    validate_parent(identity)
    final_schema=next(tool for tool in MANAGER_TOOLS if tool['name']=='finish_analysis')
    manifest={'kind':KIND,'prepared_at':now(),'source_sha256':source_digest(),'environment_stamp':env_file_stamp(),
        'parent':identity,'checkpoint':checkpoint,'prepared_database_sha256':business.db_digest(target),
        'maximum_http_attempts':cap,'parent_used':identity['used'],'combined_maximum_used':identity['used']+cap,
        'original_stage_maximum':identity['maximum'],'remaining_after_maximum':identity['maximum']-identity['used']-cap,
        'execution_policy':business.execution_policy(cfg),'network_route':'environment','listener':'http://127.0.0.1:18765',
        'production_final_schema':final_schema,'production_final_schema_sha256':business.digest(final_schema),
        'model_requests':0,'network_started':False,'authorized':False,'observation_seconds_each':business.OBSERVATION_SECONDS,
        'recovery_method':'POST original failed job retry; normal Service selection, actual synthesis_only before any model HTTP',
        'content_boundary':'Limited normal business recovery only; incomplete topics remain gaps, with independent content review required.'}
    business.write(output/'preparation.json',manifest)
    business.write(output/'private-source.json',{'preparation_sha256':business.file_digest(output/'preparation.json')})
    return manifest


def install_recovery_budget(path):
    from app.model import ModelClient,ModelBudgetExceeded
    original_init=ModelClient.__init__
    restore_business=business.install_budget(path)
    original=ModelClient._post
    budget=business.StageBudget(path)
    recovery=budget.metadata['frozen'].get('recovery')
    if not recovery or recovery.get('kind')!=KIND:
        restore_business();raise ValueError('Explicit recovery ledger identity required')
    def verify(self,payload,session):
        try:
            validate_parent(recovery['parent'])
            claim=recovery['claim']
            if business.file_digest(Path(claim['path']))!=claim['sha256']:
                raise ValueError('Unique parent recovery authorization claim changed')
            cap=requested_cap(recovery['maximum_http_attempts'])
            if (budget.status()['maximum']!=cap or any(self.config.get(key)!=cap for key in
                    ('max_model_calls','synthesis_max_calls','final_submission_reserved_calls'))):
                raise ValueError('Frozen recovery allowance differs from the actual ledger or runtime')
            if recovery['parent']['used']+budget.status()['reserved_attempts']>=recovery['parent']['maximum']:
                raise ValueError('Original stage ceiling reached')
            age=(datetime.fromisoformat(now())-datetime.fromisoformat(recovery['checkpoint']['created_at'])).total_seconds()
            if not 0<=age<=recovery['checkpoint']['max_age_seconds']:
                raise ValueError('Saved checkpoint expired; age is never extended')
            messages=payload.get('input',payload.get('messages',[]))
            seed=json.loads(next(m['content'] for m in messages if m.get('role')=='user'))
            checkpoint=seed.get('synthesis_checkpoint') or {}
            schema=payload.get('tools',[{}])[0]
            if self.protocol!='responses':schema=schema.get('function',{})
            schema={key:value for key,value in schema.items() if key not in ('type','strict')}
            if len(payload.get('tools',[]))!=1 or business.digest(schema)!=recovery['production_final_schema_sha256']:
                raise ValueError('Complete production final schema must remain intact')
            if 'historical_evidence' in seed:
                raise ValueError('Normal business recovery cannot inject historical evidence packages')
            if not checkpoint:
                store=ReadOnlyStore(recovery['database'])
                parent=store.job(recovery['parent']['job_id'])
                diagnostic=store.checkpoint_recovery_diagnostic(parent,checkpoint_context(store,parent))
                raise ValueError('Actual runtime did not select the bound synthesis-only checkpoint; '
                    'parent checkpoint state='+diagnostic['state']+'; reasons='+','.join(diagnostic.get('reasons',[])))
            if (session is None or session.phase!='synthesis' or session.native_search is not False
                    or self.config.get('role_id')!='manager' or checkpoint.get('recovery_mode')!='synthesis_only'
                    or checkpoint.get('checkpoint_job_id')!=recovery['parent']['job_id']
                    or seed.get('account',{}).get('id')!=recovery['parent']['account_id']
                    or seed.get('input_version')!=recovery['parent']['input_version']):
                raise ValueError('Actual runtime did not select the bound synthesis-only checkpoint')
            with ReadOnlyStore(recovery['database']).connect() as db:
                events=[json.loads(r[0]) for r in db.execute('SELECT body FROM events WHERE job_id=?',(session.task_id,))]
                row=db.execute('SELECT task_metadata FROM jobs WHERE id=?',(session.task_id,)).fetchone()
            metadata=json.loads(row[0]) if row else {}
            if metadata.get('parent_job_id')!=recovery['parent']['job_id'] or not any(
                    e.get('type')=='synthesis_resuming' and e.get('recovery_mode')=='synthesis_only'
                    and e.get('checkpoint_job_id')==recovery['parent']['job_id'] for e in events):
                raise ValueError('Production Service recovery event and parent link are required')
            if any(t.get('type')=='web_search' or t.get('name')=='spawn_research' for t in payload.get('tools',[])):
                raise ValueError('Recovery cannot start native search or child research')
            return checkpoint,seed
        except Exception as exc:
            budget.halt('recovery_preflight_rejected')
            raise ModelBudgetExceeded('Recovery request stopped before HTTP: '+str(exc)) from exc
    def initialize(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        original_guard=self.before_request
        async def reserve():
            if original_guard:await original_guard()
            # Recheck after any production budget lock/event await, immediately
            # before the durable reservation and transport entry.
            payload,session=self._recovery_pending
            verify(self,payload,session)
            budget.check_frozen();budget.reserve()
        self.before_request=reserve
    async def post(self,endpoint,payload,session=None):
        budget.check_job(session.task_id if session else None)
        checkpoint,seed=verify(self,payload,session)
        observations=Path(path).parent/'recovery-request-inputs.json'
        previous=read(observations) if observations.exists() else []
        business.write(observations,[*previous,{'at':now(),'synthesis_only':True,'service_event_verified':True,
            'parent_job_id':recovery['parent']['job_id'],'payload_sha256':business.digest(payload),
            'checkpoint_fields_visible':sorted(checkpoint),'calculation_summary_count_visible':len(seed.get('calculation_summaries',[])),
            'calculation_index_coverage':seed.get('calculation_indexes',{}),
            'report_count_visible':len(checkpoint.get('reports',[])),
            'material_delivery_receipts_visible':bool(checkpoint.get('material_deliveries')),
            'production_final_schema_sha256':recovery['production_final_schema_sha256'],
            'production_final_schema_matches':True,
            'historical_package_injected':'historical_evidence' in seed}])
        self._recovery_pending=(payload,session)
        try:return await original(self,endpoint,payload,session)
        finally:self._recovery_pending=None
    ModelClient.__init__,ModelClient._post=initialize,post
    def restore():
        ModelClient._post=original;restore_business()
    return restore


class Instance(business.Instance):
    child_script=Path(__file__).resolve()


async def run_prepared(args):
    root=args.prepared.resolve();manifest=read(root/'preparation.json');private=read(root/'private-source.json')
    cap=requested_cap(manifest['maximum_http_attempts'])
    cli_cap=getattr(args,'max_http_attempts',None)
    if not (getattr(args,'run_real',False) and args.authorization_note and args.authorization_note.strip()
            and type(args.authorized_http_attempts) is int and args.authorized_http_attempts==cap
            and getattr(args,'authorize_recovery',False) and (cli_cap is None or requested_cap(cli_cap)==cap)):
        raise ValueError('New explicit recovery authorization, note and exact frozen HTTP maximum required')
    if manifest.get('kind')!=KIND or business.file_digest(root/'preparation.json')!=private['preparation_sha256']:
        raise ValueError('Prepared recovery identity changed')
    if source_digest()!=manifest['source_sha256'] or env_file_stamp()!=manifest['environment_stamp']:
        raise ValueError('Source or environment changed after recovery preparation')
    validate_parent(manifest['parent'])
    check_allowance(manifest['parent'],cap)
    parent_frozen=manifest['parent']['frozen']
    if business.configuration_digest(parent_frozen['configuration_source'],parent_frozen['reference_account'])!=parent_frozen['configuration_sha256']:
        raise ValueError('Source configuration changed before recovery')
    eligible_checkpoint(root/'prepared.sqlite3',manifest['parent']['job_id'])
    if business.db_digest(root/'prepared.sqlite3')!=manifest['prepared_database_sha256']:
        raise ValueError('Prepared recovery database changed')
    claim=claim_recovery(manifest,root/'preparation.json',args.authorization_note)
    output=root/'execution';output.mkdir(exist_ok=False);output.chmod(0o700)
    target=output/'evaluation.sqlite3';business.copy_database(root/'prepared.sqlite3',target)
    store=Store(target,'live');cfg=store.settings()
    frozen={**manifest['parent']['frozen'],'source_sha256':manifest['source_sha256'],
        'recovery':{'kind':KIND,'parent':manifest['parent'],'checkpoint':manifest['checkpoint'],'database':str(target),'claim':claim,
                    'maximum_http_attempts':cap,'production_final_schema_sha256':manifest['production_final_schema_sha256']}}
    budget=business.StageBudget.create(output/'business-attempts.sqlite3','a_initial',cap,args.authorization_note,
        business.file_digest(root/'preparation.json'),frozen=frozen)
    summary={'kind':KIND,'started_at':now(),'source_sha256':manifest['source_sha256'],'parent':manifest['parent'],
        'published':False,'contract_accepted':False,'mechanical_passed':False,'content_assessment':'not_evaluated',
        'instances':[],'observations':[],'authorization_claim':claim,'authorization_note':args.authorization_note,'recovery_method':manifest['recovery_method']}
    previous_env=os.environ.get('FIFTEENTH_BUSINESS_BUDGET');os.environ['FIFTEENTH_BUSINESS_BUDGET']=str(budget.path)
    instance=None
    def save():
        summary['budget']=budget.status();summary['combined_reserved_attempts']=manifest['parent']['used']+summary['budget']['reserved_attempts']
        business.write(output/'summary.json',summary)
    async def start(name,worker):
        nonlocal instance
        instance=Instance(target,output,name,worker);state=await instance.start();summary['instances'].append(state)
        if state['health']['runtime_version']['source_sha256_at_start']!=manifest['source_sha256']:
            raise ValueError('Isolated process source differs')
        save()
    try:
        save();await start('recovery-api-no-worker',False)
        receipt=await instance.request('POST','/api/jobs/'+manifest['parent']['job_id']+'/retry',
            json={'operation_id':'fifteenth-recovery-'+uuid.uuid4().hex})
        new_id=receipt.get('job_id') or receipt.get('id')
        if not new_id or new_id==manifest['parent']['job_id']:
            raise ValueError('Retry must create a new version-bound child task')
        summary['job_id']=new_id;summary['retry_receipt']=receipt
        await instance.stop();instance=None
        with store.connect() as db:
            db.execute("UPDATE jobs SET due_at=? WHERE id=? AND state='queued'",(now(),new_id))
        budget.admit(new_id);await start('recovery-worker',True)
        async with asyncio.timeout(float(cfg['analysis_timeout_seconds'])+120):
            while True:
                validate_parent(manifest['parent'])
                job=await instance.request('GET','/api/jobs/'+new_id)
                if job['state'] not in business.base.ACTIVE:break
                await asyncio.sleep(.25)
        summary['job']=job;result=job.get('result') or {}
        summary['published']=job['state']=='completed' and bool(job.get('report_id'))
        summary['contract_accepted']=bool(result.get('delivery',{}).get('contract_verified'))
        if not summary['published'] or not summary['contract_accepted']:
            raise ValueError('Recovery did not publish; preserve this first terminal failure')
        budget.halt('first_valid_publication_stop');business.write(output/'model-result.json',result)
        summary['report_sha256']=hashlib.sha256(result['report'].encode()).hexdigest()
        baseline=business.persisted_view(store,new_id);summary['after_publish_baseline']=baseline
        if len(baseline['reports'])!=1:raise ValueError('Recovery publication is not unique')
        for phase in ('after_publish','after_restart'):
            if phase=='after_restart':
                await instance.stop();instance=None;await start('recovery-restart-observer',True)
            for i in range(2):
                await asyncio.sleep(manifest['observation_seconds_each']/2)
                view=business.persisted_view(store,new_id)
                if ([(r['id'],r['report_sha256'],r['input_version']) for r in view['reports']]!=[(r['id'],r['report_sha256'],r['input_version']) for r in baseline['reports']] or view['forecast_ids']!=baseline['forecast_ids']
                        or view['recommendation_ids']!=baseline['recommendation_ids']):
                    raise ValueError('Recovery publication changed or duplicated')
                summary['observations'].append({'phase':phase,'index':i+1,'at':now(),'view':view});save()
        if business.digest(business.ledger_facts(store.ledger(manifest['parent']['account_id'])))!=manifest['parent']['ledger_facts_sha256']:
            raise ValueError('Recovery changed committed ledger facts')
        summary['mechanical_passed']=True
    except asyncio.CancelledError:
        budget.halt('recovery_execution_cancelled')
        summary['error']={'type':'CancelledError','message':'Explicit recovery execution cancelled; no continuation'}
        raise
    except Exception as exc:
        budget.halt('recovery_execution_stopped')
        summary['error']={'type':type(exc).__name__,'message':str(exc)}
    finally:
        cleanup=[]
        if instance:
            try:await instance.stop()
            except BaseException as exc:cleanup.append({'stage':'stop','type':type(exc).__name__})
        if previous_env is None:os.environ.pop('FIFTEENTH_BUSINESS_BUDGET',None)
        else:os.environ['FIFTEENTH_BUSINESS_BUDGET']=previous_env
        try:summary['all_attempts_after_shutdown']=business.base.task_audit(store)
        except Exception as exc:cleanup.append({'stage':'audit','type':type(exc).__name__})
        try:summary['parent_unchanged']=parent_identity(manifest['parent']['execution'])==manifest['parent']
        except Exception as exc:
            summary['parent_unchanged']=False;cleanup.append({'stage':'parent_identity','type':type(exc).__name__})
        summary['source_unchanged']=source_digest()==manifest['source_sha256']
        summary['environment_unchanged']=env_file_stamp()==manifest['environment_stamp']
        summary.update(completed_at=now(),execution_stopped=not any(x['stage']=='stop' for x in cleanup),cleanup_errors=cleanup)
        if cleanup or not all(summary[k] for k in ('parent_unchanged','source_unchanged','environment_unchanged')):summary['mechanical_passed']=False
        try:save()
        except Exception as exc:
            summary['mechanical_passed']=False;summary['summary_budget_error']=type(exc).__name__
            business.write(output/'summary.json',summary)
    return 0 if summary['mechanical_passed'] else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-execution',type=Path);parser.add_argument('--output',type=Path)
    parser.add_argument('--max-http-attempts',type=int,choices=(1,2),help='Freeze one or two recovery attempts; offline default is two')
    parser.add_argument('--prepared',type=Path);parser.add_argument('--run-real',action='store_true')
    parser.add_argument('--authorize-recovery',action='store_true');parser.add_argument('--authorized-http-attempts',type=int)
    parser.add_argument('--authorization-note');parser.add_argument('--serve-child',type=Path)
    parser.add_argument('--listen-fd',type=int);parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    if args.serve_child:
        path=os.environ.get('FIFTEENTH_BUSINESS_BUDGET')
        if args.listen_fd is None or not path or Path(path).resolve()!=args.serve_child.resolve().parent/'business-attempts.sqlite3':
            parser.error('Reserved socket and this recovery ledger required')
        install_recovery_budget(path);business.base.serve_child(args.serve_child,args.listen_fd,args.worker)
    elif args.run_real:
        if not args.prepared:parser.error('Explicit prepared recovery required')
        raise SystemExit(asyncio.run(run_prepared(args)))
    else:
        if not args.parent_execution or not args.output:parser.error('Offline prepare requires parent execution and a new output')
        manifest=prepare(args)
        print(json.dumps({'kind':manifest['kind'],'source_sha256':manifest['source_sha256'],'maximum_http_attempts':manifest['maximum_http_attempts'],'authorized':False,'model_requests':0}))


if __name__=='__main__':main()
