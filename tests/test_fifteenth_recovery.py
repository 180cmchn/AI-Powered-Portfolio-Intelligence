"""Normal retry API/Service/Agent/ModelClient recovery; no provider network."""
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app import agents
from app.main import create_app
from app.model import ModelClient
from scripts import verify_fifteenth_business as business
from scripts import verify_fifteenth_recovery as recovery
from tests.test_fifteenth_business import prepared
from tests.test_fourteenth_chain import response,call


class InProcessInstance:
    """ASGI only replaces process/socket; actual startup, worker and APIs run."""
    quote_refresh = None
    def __init__(self,database,output,name,worker):
        self.database,self.output,self.name,self.worker=database,output,name,worker
        self.api=None;self.restore=None
    async def start(self):
        hook=recovery.install_recovery_budget if self.name.startswith('recovery-') else business.install_budget
        self.restore=hook(self.output/'business-attempts.sqlite3')
        self.app=create_app(self.database,mode='live',worker=self.worker)
        async def quotes(*args,**kwargs):
            if type(self).quote_refresh:
                return await type(self).quote_refresh(self.app.state.store,*args,**kwargs)
            return list(self.app.state.store.cache('quotes').values())
        self.app.state.service.refresh_quotes=quotes
        self.lifespan=self.app.router.lifespan_context(self.app);await self.lifespan.__aenter__()
        self.api=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://127.0.0.1:18765')
        return {'name':self.name,'worker':self.worker,'pid':None,'health':await self.request('GET','/api/health'),
            'test_boundary':'ASGI lifecycle and MockTransport; production Service/Agent/ModelClient'}
    async def request(self,method,url,**kwargs):
        result=await self.api.request(method,url,**kwargs);result.raise_for_status();return result.json()
    async def stop(self):
        if self.api:
            await self.api.aclose();self.api=None
            await self.lifespan.__aexit__(None,None,None)
        if self.restore:self.restore();self.restore=None


@pytest_asyncio.fixture
async def parent(prepared,monkeypatch,tmp_path,request):
    source,args=prepared
    maximum,used=getattr(request,'param',(16,10))
    args.max_http_attempts=maximum
    source.set_meta('settings',{**source.settings(),'max_agent_turns':24,'analysis_timeout_seconds':60,
        'agent_timeout_seconds':30,'synthesis_request_hard_timeout_seconds':10,'model_request_hard_timeout_seconds':10})
    monkeypatch.setattr(recovery,'EVALUATION_ROOT',tmp_path)
    monkeypatch.setattr(recovery,'source_digest',lambda:'controlled-business-source')
    monkeypatch.setattr(business,'OBSERVATION_SECONDS',.02)
    monkeypatch.setattr(business,'Instance',InProcessInstance)
    monkeypatch.setattr(recovery,'Instance',InProcessInstance)
    sent=[]
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"interrupted-parent",'
            raise httpx.RemoteProtocolError('controlled accepted missing body terminator')
    def handler(request):
        sent.append(json.loads(request.content))
        if len(sent)==used:return httpx.Response(200,stream=Broken())
        return response(call('calculate',{'operation':'sum','values':[1,len(sent)]},identity='calc-'+str(len(sent))))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    business.prepare(args)
    code=await business.run_prepared(SimpleNamespace(prepared=args.output,run_real=True,authorize_stage='a_initial',
        authorized_http_attempts=maximum,authorization_note='Offline parent fixture only'))
    summary=json.loads((args.output/'execution/summary.json').read_text())
    assert code==2 and len(sent)==used and summary['budget']['reserved_attempts']==used,summary
    parent=args.output/'execution'
    identity=recovery.parent_identity(parent)
    checkpoint=recovery.eligible_checkpoint(parent/'evaluation.sqlite3',identity['job_id'])
    assert checkpoint['partial_research']==(used==10) and checkpoint['calculation_count']==used-1
    return SimpleNamespace(source=source,path=parent,output=tmp_path/'recovery',sent=sent,identity=identity)


def prep(parent,cap=None):
    return recovery.prepare(SimpleNamespace(parent_execution=parent.path,output=parent.output,max_http_attempts=cap))


def runargs(parent,**changes):
    return SimpleNamespace(**{'prepared':parent.output,'run_real':True,'authorize_recovery':True,
        'authorized_http_attempts':2,'authorization_note':'Controlled fixture; no external authorization',**changes})


def install_model(monkeypatch,handler):
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_offline_preparation_has_no_client_and_preserves_failed_parent(parent,monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('Offline prepare cannot construct a client or service')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden);monkeypatch.setattr(recovery,'Instance',forbidden)
    m=prep(parent)
    assert m['model_requests']==0 and not m['authorized'] and not m['network_started']
    assert m['maximum_http_attempts']==2 and m['combined_maximum_used']==12 and m['remaining_after_maximum']==4
    assert recovery.parent_identity(parent.path)==parent.identity
    assert not (parent.output/'execution').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('parent',[(16,10),(6,5)],indirect=True)
@pytest.mark.parametrize('outcome',['valid','invalid','unknown'])
async def test_explicit_single_call_uses_parent_balance_and_never_corrects_or_retries(parent,monkeypatch,outcome):
    if parent.identity['maximum']==6:
        with pytest.raises(ValueError,match='remaining allowance'):prep(parent)
        assert not parent.output.exists()
    manifest=prep(parent,1);sent=[]
    assert manifest['maximum_http_attempts']==1
    assert manifest['combined_maximum_used']==parent.identity['used']+1
    with recovery.ReadOnlyStore(parent.output/'prepared.sqlite3').connect() as db:
        cfg=json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
    assert [cfg[k] for k in ('max_model_calls','synthesis_max_calls','final_submission_reserved_calls')]==[1,1,1]
    with pytest.raises(ValueError,match='exact frozen'):await recovery.run_prepared(runargs(parent))
    assert not (parent.output/'execution').exists()
    def handler(request):
        sent.append(json.loads(request.content))
        if outcome=='unknown':
            class Broken(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"id":"single-unknown",'
                    raise httpx.RemoteProtocolError('accepted unknown fixture')
            return httpx.Response(200,stream=Broken())
        args={'report':'Controlled single-call limited recovery.','completion':'limited',
            'scope':['SH:518880','SZ:002354','SZ:159599'],'decision_summary':'Controlled fixture.',
            'limitations':['Incomplete prior research remains a gap.']}
        if outcome=='invalid':args['suggestions']=[{'action':'watch','basis':'invalid missing security'}]
        return response(call('finish_analysis',args))
    install_model(monkeypatch,handler)
    code=await recovery.run_prepared(runargs(parent,authorized_http_attempts=1,max_http_attempts=1))
    summary=json.loads((parent.output/'execution/summary.json').read_text())
    assert len(sent)==summary['budget']['reserved_attempts']==summary['budget']['maximum']==1
    assert summary['combined_reserved_attempts']==parent.identity['used']+1<=parent.identity['maximum']
    assert recovery.read(Path(summary['authorization_claim']['path']))['maximum']==1
    assert recovery.parent_identity(parent.path)==parent.identity
    assert summary['execution_stopped'] and bool(summary['published'])==(outcome=='valid')
    assert code==(0 if outcome=='valid' else 2)
    if outcome=='unknown':assert any(s['reason']=='accepted_or_sent_request_outcome_unknown' for s in summary['budget']['stops'])


@pytest.mark.asyncio
@pytest.mark.parametrize('parent',[(6,5)],indirect=True)
async def test_service_quote_source_refresh_invalidates_recovery_before_any_http(parent,monkeypatch):
    manifest=prep(parent,1)
    original=parent.identity
    def forbidden(request):pytest.fail('Changed research inputs must prevent provider HTTP')
    install_model(monkeypatch,forbidden)
    refreshed=[]
    async def quote_refresh(store,symbols=None,**kwargs):
        for symbol,quote in store.cache('quotes').items():
            if symbols is not None and symbol not in symbols:continue
            store.put_cache('quotes',symbol,{**quote,'source':'sina:controlled-fallback',
                'as_of':'2026-10-30T15:01:00+08:00'})
            refreshed.append(symbol)
        return list(store.cache('quotes').values())
    monkeypatch.setattr(InProcessInstance,'quote_refresh',quote_refresh)
    assert await recovery.run_prepared(runargs(parent,authorized_http_attempts=1))==2
    summary=recovery.read(parent.output/'execution/summary.json')
    assert set(refreshed)=={'SH:518880','SZ:002354','SZ:159599'}
    assert summary['budget']['reserved_attempts']==0 and summary['execution_stopped'] and not summary['published']
    assert summary['budget']['stops'][0]['reason']=='recovery_preflight_rejected'
    assert recovery.parent_identity(parent.path)==original
    with recovery.ReadOnlyStore(parent.output/'execution/evaluation.sqlite3').connect() as db:
        cp=dict(db.execute('SELECT * FROM synthesis_checkpoints WHERE job_id=?',(original['job_id'],)).fetchone())
        events=[json.loads(row[0]) for row in db.execute('SELECT body FROM events WHERE job_id=?',(summary['job_id'],))]
    assert cp['created_at']==manifest['checkpoint']['created_at'] and cp['resume_count']==0
    assert business.digest(json.loads(cp['body']))==manifest['checkpoint']['body_sha256']
    assert cp['guard']==manifest['checkpoint']['guard']
    assert not any(e.get('type') in ('synthesis_resuming','research_resuming') for e in events)
    error=next(e['payload'] for e in events if e.get('type')=='model_error')
    assert 'parent checkpoint state=invalidated; reasons=research_inputs_changed' in error['error']
    assert error['requests']==[]
    claim=Path(summary['authorization_claim']['path'])
    assert claim.is_file() and business.file_digest(claim)==summary['authorization_claim']['sha256']
    with pytest.raises(FileExistsError):recovery.claim_recovery(manifest,parent.output/'preparation.json','Cannot renew failed attempt')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['valid','corrected','unknown','invalid_twice','fallback'])
async def test_actual_retry_service_synthesis_publication_and_stop(parent,monkeypatch,outcome):
    manifest=prep(parent);sent=[]
    def handler(request):
        payload=json.loads(request.content);sent.append(payload)
        seed=json.loads(next(m['content'] for m in payload['input'] if m.get('role')=='user'))
        cp=seed['synthesis_checkpoint']
        assert cp['recovery_mode']=='synthesis_only' and cp['checkpoint_job_id']==parent.identity['job_id']
        assert len(seed['calculation_summaries'])==9 and 'historical_evidence' not in seed
        assert payload['tool_choice']=={'type':'function','name':'finish_analysis'}
        assert [t['name'] for t in payload['tools']]==['finish_analysis']
        schema={k:v for k,v in payload['tools'][0].items() if k not in ('type','strict')}
        assert schema==manifest['production_final_schema']
        assert payload['reasoning']['effort']=='xhigh' and payload['max_output_tokens']==6000
        if outcome=='unknown':
            class Broken(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"id":"accepted-recovery",'
                    raise httpx.RemoteProtocolError('controlled unknown result')
            return httpx.Response(200,stream=Broken())
        args={'report':'Controlled limited normal-business checkpoint report.','completion':'limited',
            'scope':['SH:518880','SZ:002354','SZ:159599'],'decision_summary':'Controlled fixture only.',
            'limitations':['No completed child reports; remaining research unknown.']}
        if outcome=='invalid_twice' or (outcome=='corrected' and len(sent)==1):
            args['suggestions']=[{'action':'watch','basis':'missing security fixture'}]
        return response(call('finish_analysis',args,identity='final-'+str(len(sent))))
    install_model(monkeypatch,handler)
    if outcome=='fallback':
        from app.store import Store
        monkeypatch.setattr(Store,'reusable_synthesis_checkpoint',lambda *a,**k:None)
    code=await recovery.run_prepared(runargs(parent))
    summary=json.loads((parent.output/'execution/summary.json').read_text())
    assert recovery.parent_identity(parent.path)==parent.identity
    assert len(sent)=={'valid':1,'corrected':2,'unknown':1,'invalid_twice':2,'fallback':0}[outcome],summary
    assert summary['budget']['reserved_attempts']==len(sent)
    assert summary['combined_reserved_attempts']==10+len(sent)
    if outcome in ('valid','corrected'):
        assert code==0 and summary['mechanical_passed'] and summary['published'],summary
        result=json.loads((parent.output/'execution/model-result.json').read_text())
        assert result['synthesis_recovered'] and result['recovery_mode']=='synthesis_only'
        assert result['synthesis_checkpoint_job_id']==parent.identity['job_id']
        assert summary['report_sha256']==hashlib.sha256(result['report'].encode()).hexdigest()
        assert len(summary['observations'])==4 and len(summary['instances'])==3
        assert summary['budget']['stops'][0]['reason']=='first_valid_publication_stop'
    else:
        assert code==2 and not summary['published']
        reason='accepted_or_sent_request_outcome_unknown' if outcome=='unknown' else 'recovery_preflight_rejected' if outcome=='fallback' else 'recovery_execution_stopped'
        assert any(x['reason']==reason for x in summary['budget']['stops'])


@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['expired','parent_ledger','parent_db','parent_summary','missing_authorization','cap_three'])
async def test_recovery_preflight_refuses_drift_before_service_or_model(parent,monkeypatch,drift):
    prep(parent);args=runargs(parent)
    if drift=='expired':
        import app.recovery as module
        original=module.now_datetime
        monkeypatch.setattr(module,'now_datetime',lambda tz=None:original(tz)+timedelta(hours=2))
    elif drift=='parent_ledger':
        with sqlite3.connect(parent.path/'business-attempts.sqlite3') as db:
            db.execute("INSERT INTO attempts(reserved_at) VALUES('controlled drift')")
    elif drift=='parent_db':
        with sqlite3.connect(parent.path/'evaluation.sqlite3') as db:
            db.execute("INSERT INTO meta VALUES('controlled-drift','true')")
    elif drift=='parent_summary':
        p=parent.path/'summary.json';s=json.loads(p.read_text());s['controlled_drift']=True;p.write_text(json.dumps(s))
    elif drift=='missing_authorization':args.authorize_recovery=False
    else:args.authorized_http_attempts=3
    monkeypatch.setattr(recovery,'Instance',lambda *a,**k:pytest.fail('Must reject before service'))
    with pytest.raises(ValueError):await recovery.run_prepared(args)
    assert not (parent.output/'execution').exists()


@pytest.mark.asyncio
async def test_same_parent_copies_share_one_atomic_recovery_claim(parent,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import shutil
    first=prep(parent)
    copied=tmp_path/'parent-copy';shutil.copytree(parent.path,copied)
    second_path=tmp_path/'second-prepared'
    second=recovery.prepare(SimpleNamespace(parent_execution=copied,output=second_path))
    def claim(item):
        manifest,path=item
        try:return recovery.claim_recovery(manifest,path,'Controlled one recovery authorization')
        except FileExistsError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(claim,[(first,parent.output/'preparation.json'),(second,second_path/'preparation.json')]))
    assert sum(x is not None for x in claims)==1
    assert recovery.parent_identity(parent.path)==parent.identity
    with pytest.raises(FileExistsError):
        recovery.claim_recovery(first,parent.output/'preparation.json','Cannot renew the same parent authorization')


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['cancel','parent_drift','age_after_guard'])
async def test_inflight_cancel_and_last_boundary_guards_are_durable(parent,monkeypatch,boundary):
    prep(parent);entered=asyncio.Event();release=asyncio.Event();sent=[]
    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"accepted-awaiting-terminal",'
            entered.set();await release.wait()
    def handler(request):
        sent.append(request)
        return httpx.Response(200,stream=Held())
    install_model(monkeypatch,handler)
    if boundary!='cancel':
        original=agents.ResearchRuntime._use_budget
        async def crossing(self,*args,**kwargs):
            await original(self,*args,**kwargs)
            if boundary=='parent_drift':
                with sqlite3.connect(parent.path/'business-attempts.sqlite3') as db:
                    db.execute("INSERT INTO attempts(reserved_at) VALUES('controlled concurrent drift')")
            else:
                from app.domain import now
                future=(__import__('datetime').datetime.fromisoformat(now())+timedelta(hours=2)).isoformat()
                monkeypatch.setattr(recovery,'now',lambda:future)
        monkeypatch.setattr(agents.ResearchRuntime,'_use_budget',crossing)
    task=asyncio.create_task(recovery.run_prepared(runargs(parent)))
    try:
        if boundary=='cancel':
            from tests.test_fourteenth_deadlines import wait_for_stage
            await wait_for_stage(task,entered)
            task.cancel('explicit controlled recovery cancellation')
            with pytest.raises(asyncio.CancelledError):await task
        else:assert await task==2
        summary=json.loads((parent.output/'execution/summary.json').read_text())
        assert summary['completed_at'] and summary['execution_stopped'] and not summary['mechanical_passed']
        assert len(sent)==(1 if boundary=='cancel' else 0)
        assert summary['budget']['reserved_attempts']==len(sent)
        assert summary['budget']['stops']
        if boundary=='cancel':
            assert summary['error']['type']=='CancelledError'
            assert summary['budget']['stops'][0]['reason']=='recovery_execution_cancelled'
        elif boundary=='parent_drift':
            assert summary['parent_unchanged'] is False
            assert any(s['reason']=='recovery_preflight_rejected' for s in summary['budget']['stops'])
        else:
            assert recovery.parent_identity(parent.path)==parent.identity
    finally:
        release.set()
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
