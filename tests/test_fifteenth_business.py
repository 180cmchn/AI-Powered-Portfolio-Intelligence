"""Offline business harness evidence; no paid/provider/quote/search network."""
import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from scripts import verify_fifteenth_business as verifier

LONG_TASK_TIMEOUTS = {
    'model_first_event_timeout_seconds':90, 'model_stream_idle_timeout_seconds':120,
    'model_request_hard_timeout_seconds':900, 'synthesis_request_hard_timeout_seconds':900,
    'agent_timeout_seconds':1800, 'analysis_timeout_seconds':3600,
}


def approved_fixture(tmp_path, accepted=True):
    root=tmp_path/'approved';root.mkdir()
    body='Explicit controlled prior report, no real model or investment opinion.'
    from hashlib import sha256
    summary={'job_id':'approved-fixture-job','published':True,'contract_accepted':True}
    (root/'summary.json').write_text(json.dumps(summary))
    (root/'model-result.json').write_text(json.dumps({'report':body}))
    with sqlite3.connect(root/'evaluation.sqlite3') as db:
        db.execute('CREATE TABLE jobs(id TEXT,state TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?)',('approved-fixture-job','completed'))
        db.execute('CREATE TABLE analyses(job_id TEXT,report TEXT)')
        db.execute('INSERT INTO analyses VALUES(?,?)',('approved-fixture-job',body))
    review=root/'review.json'
    review.write_text(json.dumps({'kind':'controlled_review_fixture','accepted':accepted,
        'overall':'accepted' if accepted else 'qualified','reviewer':'controlled-test-only','reviewed_at':'2026-11-02T20:00:00+08:00',
        'identity':{'job_id':summary['job_id'],'report_sha256':sha256(body.encode()).hexdigest()}}))
    return root/'summary.json',review


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    from app import runtime_version
    from app.agent_config import AgentConfigStore
    from app.domain import AccountInput,now
    from app.store import Store
    from tests.helpers_sixth import configure_test_team
    monkeypatch.setattr(verifier,'EVALUATION_ROOT',tmp_path)
    monkeypatch.setattr(verifier,'source_digest',lambda:'controlled-business-source')
    monkeypatch.setattr(runtime_version,'source_digest',lambda:'controlled-business-source')
    source=Store(tmp_path/'source.sqlite3','live')
    cfg={**source.settings(),'quote_provider':'manual','model_name':'business-fixture',
        'model_base_url':'https://fixture.invalid/v1','model_responses_stream':False,
        'model_native_search':False,'model_retries':0,'max_agent_turns':4,
        'model_request_hard_timeout_seconds':2,'synthesis_request_hard_timeout_seconds':2,
        'analysis_timeout_seconds':10,'agent_timeout_seconds':5}
    source.set_meta('settings',cfg);configure_test_team(source,cfg)
    account=source.save_account(AccountInput(name='Controlled configuration account',cash='1',holdings=[]))
    monkeypatch.setattr(AgentConfigStore,'_vault',lambda self:source._credential_store)
    configure=verifier.configure_isolated
    def fixture_config(origin,target,account_id):
        assert Path(origin)==source.path
        store,cfg=configure(origin,target,account_id)
        for symbol,kind in [('SH:518880','etf'),('SZ:002354','stock'),('SZ:159599','etf'),('SZ:159278','etf')]:
            store.put_cache('securities',symbol,{'symbol':symbol,'name':symbol+' controlled fixture',
                'kind':kind,'status':'ok','identity_status':'confirmed','fetched_at':now(),'source':'controlled-fixture'})
            store.put_cache('quotes',symbol,{'symbol':symbol,'price':'10.000','status':'closed',
                'fresh_for_triggers':False,'as_of':'2026-10-30T15:00:00+08:00','source':'controlled-fixture'})
        return store,cfg
    monkeypatch.setattr(verifier,'configure_isolated',fixture_config)
    summary,review=approved_fixture(tmp_path)
    args=SimpleNamespace(stage='a_initial',max_http_attempts=16,source=source.path,reference_account=account['id'],
        output=tmp_path/'prepared',approved_summary=summary,content_review=review,previous_stage=None,
        manager_reasoning_effort='xhigh',max_output_tokens=6000)
    return source,args


def run_args(prepared_args, **changes):
    return SimpleNamespace(**{'prepared':prepared_args.output,'run_real':True,'authorize_stage':'a_initial',
        'authorized_http_attempts':16,'network_route':getattr(prepared_args,'network_route','environment'),
        'authorization_note':'Controlled fixture authorization; zero external requests',**changes})


def direct_fixture_transport(monkeypatch, handler):
    """Keep the real direct TLS/pool construction; replace only fixture I/O."""
    factory=httpx.AsyncHTTPTransport
    controlled=httpx.MockTransport(handler)
    created=[]
    def transport(*args,**kwargs):
        result=factory(*args,**kwargs)
        result.handle_async_request=controlled.handle_async_request
        created.append(result)
        return result
    monkeypatch.setattr(httpx,'AsyncHTTPTransport',transport)
    return created


def test_qualified_review_and_changed_body_cannot_unlock_business(tmp_path):
    summary,review=approved_fixture(tmp_path,accepted=False)
    with pytest.raises(ValueError,match='accepted=true'):
        verifier.approved_report(summary,review)
    data=json.loads(review.read_text());data['accepted']=True;review.write_text(json.dumps(data))
    (summary.parent/'model-result.json').write_text(json.dumps({'report':'different controlled body'}))
    with pytest.raises(ValueError,match='another report'):
        verifier.approved_report(summary,review)


def test_prepare_is_offline_and_exposes_exact_reviewable_contract(prepared,monkeypatch):
    source,args=prepared
    before=verifier.db_digest(source.path)
    def forbidden(*a,**k):raise AssertionError('Preparation must not construct/start an HTTP client or server')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    monkeypatch.setattr(verifier,'Instance',forbidden)
    manifest=verifier.prepare(args)
    assert verifier.db_digest(source.path)==before
    assert manifest['network_started'] is False and manifest['model_requests']==0
    assert manifest['maximum_http_attempts']==16 and manifest['final_submission_reserved_calls']==2
    assert manifest['listener']=='http://127.0.0.1:18765'
    assert manifest['database']==str(args.output/'execution/evaluation.sqlite3')
    assert manifest['isolated_overrides']=={'manager_reasoning_effort':'xhigh','max_output_tokens':6000}
    assert manifest['role_matrix'][0]['resolved']['reasoning_effort']=='xhigh'
    assert 'research_records' in manifest['production_final_schema']['parameters']['properties']
    assert manifest['expected_cash']=='86484.90' and manifest['expected_fees']=='15.10'
    assert not (args.output/'execution').exists()


@pytest.mark.parametrize('configured', [None, 4000])
@pytest.mark.parametrize('omit', [False, True])
def test_output_limit_preparation_preserves_source_or_explicitly_omits(prepared, configured, omit):
    from app.store import Store
    source,args=prepared
    source.set_meta('settings',{**source.settings(),'max_output_tokens':configured})
    before=verifier.db_digest(source.path)
    args.max_output_tokens=None;args.omit_output_limit=omit
    manifest=verifier.prepare(args)
    expected=None if omit else configured
    assert manifest['execution_policy']['max_output_tokens'] == expected
    assert Store(args.output/'prepared.sqlite3','live').settings()['max_output_tokens'] == expected
    assert ('max_output_tokens' in manifest['isolated_overrides']) is omit
    if omit:assert manifest['isolated_overrides']['max_output_tokens'] is None
    assert verifier.db_digest(source.path)==before and source.settings()['max_output_tokens'] == configured


def test_omission_and_numeric_override_are_mutually_exclusive(prepared,monkeypatch):
    import sys
    source,args=prepared
    args.omit_output_limit=True
    with pytest.raises(ValueError,match='mutually exclusive'):verifier.prepare(args)
    assert not args.output.exists()
    monkeypatch.setattr(sys,'argv',['verify_fifteenth_business.py','--omit-output-limit','--max-output-tokens','6000'])
    with pytest.raises(SystemExit) as caught:verifier.main()
    assert caught.value.code==2


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['omitted_direct','unexpected_direct','private_route','manifest_override','unknown'])
async def test_network_route_is_frozen_before_any_business_instance(prepared,monkeypatch,change):
    _,args=prepared
    args.network_route='environment' if change=='unexpected_direct' else 'direct'
    manifest=verifier.prepare(args)
    assert manifest['network_route']==args.network_route
    run=run_args(args)
    if change=='omitted_direct':del run.network_route
    elif change=='unexpected_direct':run.network_route='direct'
    elif change=='unknown':run.network_route='unknown'
    elif change=='private_route':
        path=args.output/'private-source.json';value=json.loads(path.read_text())
        value['network_route']='environment';verifier.write(path,value)
    else:
        path=args.output/'preparation.json';value=json.loads(path.read_text())
        value['isolated_overrides'].pop('network_route');verifier.write(path,value)
        path=args.output/'private-source.json';value=json.loads(path.read_text())
        value['preparation_sha256']=verifier.file_digest(args.output/'preparation.json');verifier.write(path,value)
    monkeypatch.setattr(verifier,'Instance',lambda *a,**k:pytest.fail('No instance on route drift'))
    with pytest.raises(ValueError,match='Network route'):await verifier.run_prepared(run)
    assert not (args.output/'execution').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['unchanged','route_claim','proxy_mount','tls','transport','retry_mount'])
async def test_direct_route_is_checked_at_every_actual_model_http_boundary(prepared,monkeypatch,drift):
    import os
    from app.model import ModelClient
    from app.store import Store
    source,args=prepared
    args.network_route='direct'
    source.set_meta('settings',{**source.settings(),'model_retries':1})
    manifest=verifier.prepare(args);store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    cfg=store.runtime_role_config(team,'manager',store.settings())
    frozen={'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':6000,
        'network_route':'direct','retry_policy':manifest['retry_policy'],
        'roles':{'manager':{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']}}}
    budget=verifier.StageBudget.create(args.output/'guard.sqlite3','a_initial',16,'fixture-only',
        verifier.file_digest(args.output/'preparation.json'),frozen);budget.admit('direct-fixture')
    sent=[]
    def handler(request):
        sent.append(request)
        if drift=='retry_mount':return httpx.Response(503,json={'error':'controlled retry'})
        return httpx.Response(200,json={'status':'completed','output':[]})
    direct_fixture_transport(monkeypatch,handler)
    restore=verifier.install_budget(budget.path)
    environment=dict(os.environ)
    async def original_guard():
        if drift=='route_claim':client._business_network_route='environment'
        elif drift=='proxy_mount' or drift=='retry_mount' and sent:
            client.http._mounts['controlled-invalid-mount']=httpx.MockTransport(handler)
        elif drift=='tls':
            import ssl
            context=client.http._transport._pool._ssl_context
            context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
        elif drift=='transport':client.http._transport=httpx.MockTransport(handler)
    client=ModelClient(cfg,before_request=original_guard,transport=httpx.MockTransport(handler))
    previous=client.http
    unrelated=httpx.AsyncClient(transport=httpx.MockTransport(handler));unrelated_transport=unrelated._transport
    session=client.session('controlled direct transport',{'task_id':'direct-fixture'})
    try:
        payload={'model':cfg['model_name'],'reasoning':{'effort':cfg['reasoning_effort']},'max_output_tokens':6000}
        if drift=='unchanged':await client._post('responses',payload,session)
        else:
            with pytest.raises(ValueError,match='network route|Direct model route'):
                await client._post('responses',payload,session)
        expected=1 if drift in {'unchanged','retry_mount'} else 0
        assert len(sent)==budget.status()['reserved_attempts']==expected
        assert previous.is_closed and client.http is not previous
        assert dict(os.environ)==environment and unrelated._transport is unrelated_transport
        assert all(item['isolated_network_route']=='direct' for item in client.requests)
    finally:
        await client.aclose();await unrelated.aclose();restore()


@pytest.mark.parametrize('value',[0,-1,float('inf'),float('nan'),True])
def test_invalid_timeout_override_rejects_before_preparation(prepared,value):
    source,args=prepared
    before=verifier.db_digest(source.path)
    args.model_request_hard_timeout_seconds=value
    with pytest.raises(ValueError,match='model_request_hard_timeout_seconds must be a finite positive'):
        verifier.prepare(args)
    assert not args.output.exists() and verifier.db_digest(source.path)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['missing_note','wrong_stage','wrong_cap','changed_source','changed_prepared','changed_matrix','changed_manifest','run_timeout'])
async def test_preflight_rejects_before_any_instance_or_attempt(prepared,monkeypatch,change):
    source,args=prepared;verifier.prepare(args);run=run_args(args)
    if change=='missing_note':run.authorization_note=''
    elif change=='run_timeout':run.model_request_hard_timeout_seconds=900
    elif change=='wrong_stage':run.authorize_stage='b_cash'
    elif change=='wrong_cap':run.authorized_http_attempts=17
    elif change=='changed_source':monkeypatch.setattr(verifier,'source_digest',lambda:'another-source')
    elif change=='changed_manifest':
        path=args.output/'preparation.json';body=json.loads(path.read_text());body['maximum_http_attempts']=40;path.write_text(json.dumps(body))
    elif change=='changed_prepared':
        with sqlite3.connect(args.output/'prepared.sqlite3') as db:db.execute("INSERT INTO meta VALUES('controlled-change','true')")
    else:source.update_agent_definition('manager',{'default_binding':{'reasoning_effort':'high'}})
    monkeypatch.setattr(verifier,'Instance',lambda *a,**k:pytest.fail('No instance before authorization/configuration guards'))
    with pytest.raises(ValueError):await verifier.run_prepared(run)
    assert not (args.output/'execution/business-attempts.sqlite3').exists()


def test_stage_ledger_rejects_old_pools_and_honors_atomic_stop(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.model import ModelBudgetExceeded
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    old=tmp_path/'old.sqlite3';SharedAttemptBudget.initialize(old,maximum=10,final_maximum=10,full_maximum=0)
    with pytest.raises(ValueError,match='cannot authorize'):verifier.StageBudget(old)
    path=tmp_path/'stage.sqlite3';budget=verifier.StageBudget.create(path,'a_initial',3,'controlled stage note','fixture-manifest')
    budget.admit('one-job')
    def reserve(_):
        try:verifier.StageBudget(path).reserve();return True
        except ModelBudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(reserve,range(15)))==3
    assert verifier.StageBudget(path).status()['reserved_attempts']==3
    with pytest.raises(ModelBudgetExceeded):budget.check_job('unapproved-auto-job')
    budget.halt('unknown-outcome')
    with pytest.raises(ModelBudgetExceeded):budget.check_job('one-job')
    with pytest.raises(FileExistsError):verifier.StageBudget.create(path,'a_initial',40,'cannot reset','x')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,long_timeouts,omit_output_limit,route',[
    ('valid',False,False,'environment'),('valid',True,False,'environment'),('unknown_transport',False,False,'environment'),
    ('retry_503',False,False,'environment'),('valid',False,True,'environment'),('valid',False,True,'direct')])
async def test_actual_api_ledger_service_agent_model_publication_and_restart(prepared,monkeypatch,outcome,long_timeouts,omit_output_limit,route):
    from app import agents
    from app.main import create_app
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    source,args=prepared
    args.network_route=route
    if outcome=='retry_503':
        source.set_meta('settings',{key:value for key,value in source.settings().items() if key!='model_retries'})
    original_settings=source.settings()
    if omit_output_limit:
        args.max_output_tokens=None;args.omit_output_limit=True
    if long_timeouts:
        for key,value in LONG_TASK_TIMEOUTS.items():setattr(args,key,value)
        args.max_http_attempts=40
    # The fixture proposes shorter polling before the manifest is sealed.
    monkeypatch.setattr(verifier,'OBSERVATION_SECONDS',.02)
    manifest=verifier.prepare(args)
    source_before=verifier.db_digest(source.path);sent=[];clients=[]
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"controlled-broken",'
            raise httpx.RemoteProtocolError('peer closed connection without complete body')
    def handler(request):
        payload=json.loads(request.content);sent.append(payload)
        assert payload['reasoning']['effort']=='xhigh'
        assert {k:payload[k] for k in ('max_output_tokens','max_completion_tokens','max_tokens') if k in payload} == (
            {} if omit_output_limit else {'max_output_tokens':6000})
        seed=json.loads(next(m['content'] for m in payload['input'] if m['role']=='user'))
        assert seed['account']['cash']=='86484.90' and len(seed['account']['holdings'])==3
        if outcome=='unknown_transport':return httpx.Response(200,stream=Broken())
        if outcome=='retry_503' and len(sent)==1:return httpx.Response(503,json={'error':'controlled temporary overload'})
        return response(call('finish_analysis',{'report':'Controlled fixture report; no investment claim.',
            'completion':'limited','scope':['SH:518880','SZ:002354','SZ:159599'],
            'decision_summary':'Controlled fixture only.','limitations':['No real network evidence']}))
    def model_client(cfg,before_request=None):
        client=ModelClient(cfg,before_request,httpx.MockTransport(handler));clients.append(client)
        return client
    monkeypatch.setattr(agents,'ModelClient',model_client)
    if route=='direct':direct_fixture_transport(monkeypatch,handler)
    install_inprocess_instance(monkeypatch)
    code=await verifier.run_prepared(run_args(args,authorized_http_attempts=args.max_http_attempts))
    summary=json.loads((args.output/'execution/summary.json').read_text())
    assert verifier.db_digest(source.path)==source_before
    assert source.settings()==original_settings
    if long_timeouts:
        assert manifest['baseline_configuration']['policy']['model_request_hard_timeout_seconds']==2
        assert summary['budget']['maximum']==40 and summary['final_submission_reserved_calls']==2
        assert {k:summary['execution_policy'][k] for k in LONG_TASK_TIMEOUTS}==LONG_TASK_TIMEOUTS
        assert clients and all({k:c.config[k] for k in LONG_TASK_TIMEOUTS}==LONG_TASK_TIMEOUTS for c in clients)
        assert clients[0].requests[0]['timeout_policy']['request_hard_seconds']==900
        from app.store import Store
        store=Store(args.output/'execution/evaluation.sqlite3','live')
        started=next(e for e in store.events_since(summary['job_id'])['events'] if e['type']=='agent_started')
        limits=started['payload']['resource_limits']
        assert limits['timeout_policy']['request_hard_seconds']==900
        assert limits['synthesis_timeout_policy']['request_hard_seconds']==900
        assert limits['timeout_policy']['first_event_seconds']==90
        assert limits['timeout_policy']['stream_idle_seconds']==120
        assert limits['child_timeout_seconds']==1800 and limits['analysis_timeout_seconds']==3600
    assert len(sent)==summary['budget']['reserved_attempts']==(2 if outcome=='retry_503' else 1)
    assert summary['retry_policy']==manifest['retry_policy']
    assert summary['network_route']==manifest['network_route']==route
    assert all(item['isolated_network_route']==route and item['isolated_network_route_verified']
        for client in clients for item in client.requests)
    assert summary['execution_policy']['max_output_tokens']==(None if omit_output_limit else 6000)
    assert clients[0].requests[0]['output_limit_fields_present']==([] if omit_output_limit else ['max_output_tokens'])
    if outcome=='retry_503':
        assert manifest['retry_policy']['model_retries_source']=='ModelClient_default'
        assert manifest['retry_policy']['effective_model_retries']==1
        assert clients and all('model_retries' not in client.config for client in clients)
    assert summary['prepared_ledger']['cash']=='86484.90' and len(summary['trades'])==3
    assert summary['uncovered_stages']==['a_after_buy','a_after_sell','b_cash']
    assert summary['overall_business_acceptance_passed'] is False
    if outcome in {'valid','retry_503'}:
        assert code==0,summary.get('error')
        assert summary['mechanical_passed'] and summary['published'] and summary['contract_accepted']
        assert len(summary['instances'])==3 and len(summary['observations'])==4
        assert summary['observations'][0]['view']['reports']==summary['observations'][-1]['view']['reports']
        assert summary['content_assessment']=='not_evaluated'
    else:
        assert code==2 and not summary['published']
        assert summary['budget']['stops'][0]['reason']=='accepted_or_sent_request_outcome_unknown'
        assert not (args.output/'execution/model-result.json').exists()


def install_inprocess_instance(monkeypatch):
    from app.main import create_app
    class InProcessInstance:
        def __init__(self,database,output,name,worker):
            self.database,self.output,self.name,self.worker=database,output,name,worker
            self.api=None;self.restore=None
        async def start(self):
            self.restore=verifier.install_budget(self.output/'business-attempts.sqlite3')
            self.app=create_app(self.database,mode='live',worker=self.worker)
            async def quotes(*a,**k):return list(self.app.state.store.cache('quotes').values())
            self.app.state.service.refresh_quotes=quotes
            self.lifespan=self.app.router.lifespan_context(self.app);await self.lifespan.__aenter__()
            self.api=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://127.0.0.1:18765')
            return {'name':self.name,'worker':self.worker,'pid':None,'url':'http://controlled.test',
                'test_double':'ASGI transport/fixture quote cache/vault/MockTransport; actual Service and Agent',
                'health':await self.request('GET','/api/health')}
        async def request(self,method,url,**kwargs):
            r=await self.api.request(method,url,**kwargs);r.raise_for_status();return r.json()
        async def stop(self):
            if self.api:
                await self.api.aclose();self.api=None
                await self.lifespan.__aexit__(None,None,None)
            if self.restore:self.restore();self.restore=None
    monkeypatch.setattr(verifier,'Instance',InProcessInstance)


@pytest.mark.asyncio
@pytest.mark.parametrize('child_failure',['idle_timeout','unknown_200'])
async def test_failed_child_preserves_completed_evidence_for_limited_manager_publication(prepared,monkeypatch,child_failure):
    from collections import Counter
    from app import agents
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    source,args=prepared
    source.set_meta('settings',{**source.settings(),'max_agent_turns':12,'analysis_timeout_seconds':20,
        'agent_timeout_seconds':10,'model_responses_stream':True,'model_retries':2,
        'model_first_event_timeout_seconds':1,'model_stream_idle_timeout_seconds':.06})
    monkeypatch.setattr(verifier,'OBSERVATION_SECONDS',.02)
    manifest=verifier.prepare(args)
    source_before=verifier.db_digest(source.path)
    incomplete='UNFINISHED_CHILD_TEXT_MUST_NOT_BECOME_REPORT'
    completed='COMPLETED_CHILD_FACT'
    sent=[];clients=[];stream_closed=asyncio.Event()
    class IncompleteChild(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield ('data: '+json.dumps({'type':'response.output_text.delta','delta':incomplete})+'\n\n').encode()
                if child_failure=='unknown_200':
                    raise httpx.RemoteProtocolError('controlled child body interrupted')
                await asyncio.Event().wait()
            finally:
                stream_closed.set()
    async def handler(request,role):
        payload=json.loads(request.content);sent.append((role,payload))
        if role=='filing_researcher':
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=IncompleteChild())
        if role=='market_researcher':
            return response(call('submit_report',{'summary':completed,
                'findings':[{'claim':'Controlled completed finding; adverse scenario retained.'}],
                'open_questions':['Controlled report does not resolve the filing topic.']}))
        assert role=='manager'
        seed=json.loads(next(item['content'] for item in payload['input'] if item.get('role')=='user'))
        children=seed['runtime_status']['children']
        if not children:
            return response(call('calculate',{'operation':'sum','values':[17,20]},'manager-calculation'),
                call('spawn_research',{'role_id':'market_researcher','task':'Controlled independent completed topic.'},'good-child'),
                call('spawn_research',{'role_id':'filing_researcher','task':'Controlled independent unavailable filing topic.'},'failed-child'))
        if any(child['status'] not in {'completed','failed'} for child in children):
            await asyncio.wait_for(stream_closed.wait(),timeout=1)
            return response(call('wait_agents',{'timeout_seconds':.1},'wait-children'))
        assert {child['status'] for child in children}=={'completed','failed'}
        if 'synthesis_transition' not in seed:
            return response(call('prepare_synthesis',{'summary':'Combine completed evidence; filing topic failed.'},'prepare'))
        assert incomplete not in json.dumps(payload)
        reports=seed['synthesis_checkpoint']['reports']
        assert len(reports)==1 and reports[0]['summary']==completed
        assert any(item['result']=='37' for item in seed['calculation_summaries'])
        return response(call('finish_analysis',{'report':'Controlled limited summary of completed evidence and calculation 37.',
            'completion':'limited','scope':['SH:518880','SZ:002354','SZ:159599'],
            'decision_summary':'Controlled existing evidence supports only this limited fixture judgment.',
            'limitations':['Filing child failed; its unfinished text is unavailable as a completed report.']},'manager-final'))
    def model_client(cfg,before_request=None):
        client=ModelClient(cfg,before_request,httpx.MockTransport(lambda request:handler(request,cfg['role_id'])))
        clients.append(client);return client
    monkeypatch.setattr(agents,'ModelClient',model_client)
    install_inprocess_instance(monkeypatch)
    assert await verifier.run_prepared(run_args(args))==0
    summary=json.loads((args.output/'execution/summary.json').read_text())
    result=json.loads((args.output/'execution/model-result.json').read_text())
    counts=Counter(role for role,_ in sent)
    assert counts['filing_researcher']==counts['market_researcher']==1
    assert summary['published'] and summary['contract_accepted'] and summary['mechanical_passed']
    assert result['completion']=='limited' and incomplete not in result['report']
    failed=next(child for child in result['agents'] if child['role_id']=='filing_researcher')
    assert failed['status']=='failed' and failed['reports']==[]
    good=next(child for child in result['agents'] if child['role_id']=='market_researcher')
    assert good['reports'][0]['summary']==completed
    child_requests=[request for client in clients if client.config.get('role_id')=='filing_researcher' for request in client.requests]
    assert len(child_requests)==1
    observation=child_requests[0]
    assert observation['status_code']==200 and observation['request_accepted'] is True
    if child_failure=='idle_timeout':assert observation['error_code']=='stream_idle_timeout'
    else:assert observation['retry_suppressed']=='accepted_or_sent_request_outcome_unknown'
    assert summary['budget']['reserved_attempts']==len(sent)==sum(len(client.requests) for client in clients)
    assert summary['budget']['stops']==[{'reason':'first_valid_publication_stop','at':summary['budget']['stops'][0]['at']}]
    assert len(summary['after_publish_baseline']['reports'])==1 and len(summary['observations'])==4
    assert summary['observations'][0]['view']['reports']==summary['observations'][-1]['view']['reports']
    assert manifest['retry_policy']['effective_model_retries']==2 and verifier.db_digest(source.path)==source_before


def test_unimplemented_matrix_stages_are_not_claimed_or_released(prepared):
    _,args=prepared
    args.stage='a_after_buy'
    with pytest.raises(ValueError,match='not implemented'):verifier.prepare(args)
    for stage in ('a_after_sell','b_cash'):
        args.stage=stage
        with pytest.raises(ValueError,match='previous-stage'):verifier.prepare(args)
    assert not args.output.exists()


def accept_business_fixture(output):
    """Independent controlled review of an actual fixture-chain publication."""
    execution=output/'execution'
    summary=json.loads((execution/'summary.json').read_text())
    review=output/'controlled-content-review.json'
    verifier.write(review,{'accepted':True,'reviewer':'offline independent fixture reviewer','reviewed_at':verifier.now(),
        'identity':{'job_id':summary['job_id'],'report_sha256':summary['report_sha256']}})
    return execution/'summary.json',review


@pytest_asyncio.fixture
async def failed_sale(prepared,monkeypatch):
    from app import agents
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    source,args=prepared
    source.set_meta('settings',{**source.settings(),'max_agent_turns':12})
    monkeypatch.setattr(verifier,'OBSERVATION_SECONDS',.01)
    install_inprocess_instance(monkeypatch)
    stage={'value':'a_initial'}
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"failed-sale",'
            raise httpx.RemoteProtocolError('Controlled failed sale research, committed trade remains intact')
    def handler(request,role):
        payload=json.loads(request.content)
        seed=json.loads(next(item['content'] for item in payload['input'] if item.get('role')=='user'))
        if stage['value']=='a_initial':
            return response(call('finish_analysis',{'report':'controlled-original-A-body','completion':'limited',
                'scope':['SH:518880','SZ:002354','SZ:159599'],'decision_summary':'Controlled original A judgment.',
                'limitations':['Controlled fixture only.']}))
        if role!='manager':
            return response(call('submit_report',{'summary':'Completed sale child with explicit missing source.',
                'material_ids':['controlled-unavailable-source'], 'open_questions':['Source remains unavailable.']}))
        children=seed['runtime_status']['children']
        if not children:
            return response(call('calculate',{'operation':'sum','values':[17,20]},'sale-calc'),
                call('spawn_research',{'role_id':'market_researcher','task':'Controlled completed sale topic.'},'sale-child'))
        if any(child['status']!='completed' for child in children):
            return response(call('wait_agents',{'timeout_seconds':.1},'sale-wait'))
        return httpx.Response(200,stream=Broken())
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:
        ModelClient(cfg,before_request,httpx.MockTransport(lambda request:handler(request,cfg['role_id']))))
    verifier.prepare(args)
    assert await verifier.run_prepared(run_args(args))==0
    accepted,review=accept_business_fixture(args.output)
    sale_args=SimpleNamespace(**{**vars(args),'stage':'a_after_sell','max_http_attempts':8,
        'output':args.output.parent/'failed-sale','approved_summary':accepted,'content_review':review,'previous_stage':accepted})
    stage['value']='a_after_sell'
    verifier.prepare(sale_args)
    assert await verifier.run_prepared(run_args(sale_args,authorize_stage='a_after_sell',authorized_http_attempts=8))==2
    execution=sale_args.output/'execution'
    summary=json.loads((execution/'summary.json').read_text())
    identity=verifier.retry_parent_identity(execution,'a_after_sell',summary['previous_stage'])
    retry_args=SimpleNamespace(**{**vars(sale_args),'output':args.output.parent/'retry-sale',
        'max_http_attempts':None,'retry_execution':execution})
    return SimpleNamespace(source=source,args=retry_args,summary=summary,identity=identity,accepted=accepted,review=review)


@pytest.mark.asyncio
@pytest.mark.parametrize('expired',[False,True])
async def test_sale_retry_uses_normal_service_without_another_trade_and_allows_B(failed_sale,monkeypatch,expired):
    from app import agents
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    fixture=failed_sale;args=fixture.args
    if expired:
        # This controlled source is expired before preparation; never extend it.
        with sqlite3.connect(args.retry_execution/'evaluation.sqlite3') as db:
            db.execute("UPDATE synthesis_checkpoints SET created_at='2000-01-01T00:00:00+08:00' WHERE job_id=?",(fixture.identity['job_id'],))
    parent_before=verifier.db_digest(args.retry_execution/'evaluation.sqlite3')
    manifest=verifier.prepare(args)
    assert manifest['maximum_http_attempts']==40 and manifest['simulated_trades']==[]
    assert manifest['retry_parent']['checkpoint']['calculation_count']==1
    assert manifest['retry_parent']['checkpoint']['report_count']==1
    sent=[]
    def handler(request):
        payload=json.loads(request.content);sent.append(payload)
        seed=json.loads(next(item['content'] for item in payload['input'] if item.get('role')=='user'))
        assert seed['account']['version']==5 and seed['account']['cash']=='87479.90'
        if expired:
            assert 'synthesis_checkpoint' not in seed
        else:
            checkpoint=seed['synthesis_checkpoint']
            assert checkpoint['recovery_mode']=='continue_research'
            assert checkpoint['checkpoint_job_id']==fixture.identity['job_id']
            assert checkpoint['reports'][0]['summary']=='Completed sale child with explicit missing source.'
            assert any(item['result']=='37' for item in seed['calculation_summaries'])
            assert 'spawn_research' in {tool.get('name') for tool in payload['tools']}
        return response(call('finish_analysis',{'report':'Controlled retry sale report.','completion':'limited',
            'scope':['SZ:002354','SZ:159599'],'decision_summary':'Controlled post-sale judgment.',
            'limitations':['Missing source remains unresolved; no prior half-response is a report.']}))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    assert await verifier.run_prepared(run_args(args,authorize_stage='a_after_sell',authorized_http_attempts=40))==0
    summary=json.loads((args.output/'execution/summary.json').read_text())
    assert len(sent)==summary['budget']['reserved_attempts']==1
    assert summary['kind']=='fifteenth_business_stage' and summary['stage']=='a_after_sell'
    assert summary['account_id']==fixture.identity['account_id'] and summary['trades']==[]
    assert summary['retry_receipt']['merged'] is False and summary['job_id']!=fixture.identity['job_id']
    assert summary['retry_receipt']['parent_job_id']==fixture.identity['job_id']
    assert summary['retry_execution']['actual_recovery_mode']==('fresh_research' if expired else 'continue_research')
    assert summary['retry_parent_unchanged'] and summary['inherited_reports_preserved'] and summary['inherited_forecasts_preserved']
    assert verifier.ledger_facts(summary['prepared_ledger'])==verifier.ledger_facts(fixture.summary['prepared_ledger'])
    assert len([entry for entry in summary['final_ledger']['entries'] if entry.get('side')=='sell'])==1
    assert verifier.db_digest(args.retry_execution/'evaluation.sqlite3')==parent_before
    store=verifier.ReadOnlyStore(args.output/'execution/evaluation.sqlite3')
    assert store.job(fixture.summary['previous_stage']['job_id'])['result']['report']=='controlled-original-A-body'
    accepted,review=accept_business_fixture(args.output)
    b_args=SimpleNamespace(**{**vars(args),'stage':'b_cash','max_http_attempts':8,'retry_execution':None,
        'output':args.output.parent/'B-after-retry','approved_summary':accepted,'content_review':review,'previous_stage':accepted})
    b_manifest=verifier.prepare(b_args)
    assert b_manifest['previous_stage']['job_id']==summary['job_id'] and b_manifest['account_a_id']==summary['account_id']


@pytest_asyncio.fixture
async def failed_retry(failed_sale,monkeypatch):
    from app import agents
    from app.model import ModelClient
    fixture=failed_sale;args=fixture.args
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"controlled-failed-retry",'
            raise httpx.RemoteProtocolError('Controlled incomplete retry response')
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:
        ModelClient(cfg,before_request,httpx.MockTransport(lambda request:httpx.Response(200,stream=Broken()))))
    verifier.prepare(args)
    assert await verifier.run_prepared(run_args(args,authorize_stage='a_after_sell',authorized_http_attempts=40))==2
    execution=args.output/'execution';summary=json.loads((execution/'summary.json').read_text())
    assert summary['trades']==[] and summary['retry_parent_unchanged']
    next_args=SimpleNamespace(**{**vars(args),'output':args.output.parent/'retry-sale-again',
        'retry_execution':execution,'network_route':'direct'})
    return SimpleNamespace(args=next_args,summary=summary,original=fixture)


@pytest.mark.asyncio
async def test_direct_retry_of_failed_retry_keeps_one_sale_and_original_report(failed_retry,monkeypatch):
    from app import agents
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    fixture=failed_retry;args=fixture.args
    source_paths=[args.retry_execution/'evaluation.sqlite3',fixture.original.args.retry_execution/'evaluation.sqlite3']
    before={str(path):verifier.db_digest(path) for path in source_paths}
    manifest=verifier.prepare(args)
    assert manifest['retry_parent']['job_id']==fixture.summary['job_id']
    assert manifest['retry_parent']['retry_parent']==fixture.summary['retry_parent']
    assert manifest['retry_parent']['original_sale']==fixture.original.identity['original_sale']
    sent=[]
    def handler(request):
        sent.append(json.loads(request.content))
        return response(call('finish_analysis',{'report':'Controlled chained retry report.','completion':'limited',
            'scope':['SZ:002354','SZ:159599'],'decision_summary':'Controlled post-sale judgment.',
            'limitations':['Retained missing source remains unknown.']}))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    direct_fixture_transport(monkeypatch,handler)
    assert await verifier.run_prepared(run_args(args,authorize_stage='a_after_sell',authorized_http_attempts=40))==0
    summary=json.loads((args.output/'execution/summary.json').read_text())
    assert summary['trades']==[] and summary['original_sale']==fixture.original.identity['original_sale']
    assert len(sent)==summary['budget']['reserved_attempts']==1 and summary['network_route']=='direct'
    assert summary['retry_execution']['parent_job_id']==fixture.summary['job_id']
    assert summary['retry_execution']['actual_recovery_mode']=='continue_research'
    assert summary['inherited_reports_preserved'] and summary['inherited_forecasts_preserved'] and summary['retry_parent_unchanged']
    assert len([entry for entry in summary['final_ledger']['entries'] if entry.get('side')=='sell'])==1
    assert verifier.ledger_facts(summary['final_ledger'])==verifier.ledger_facts(fixture.original.summary['prepared_ledger'])
    assert all(verifier.db_digest(Path(path))==value for path,value in before.items())
    accepted,review=accept_business_fixture(args.output)
    b_args=SimpleNamespace(**{**vars(args),'stage':'b_cash','max_http_attempts':8,'retry_execution':None,
        'output':args.output.parent/'B-after-chained-retry','approved_summary':accepted,'content_review':review,'previous_stage':accepted})
    assert verifier.prepare(b_args)['previous_stage']['job_id']==summary['job_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['cycle','ancestor','merged','same_id','receipt_account','receipt_version','completed'])
async def test_failed_retry_lineage_rejects_corrupt_ancestor_or_receipt(failed_retry,drift):
    args=failed_retry.args;path=args.retry_execution/'summary.json';summary=json.loads(path.read_text())
    if drift=='cycle':summary['retry_parent']['execution']=str(args.retry_execution)
    elif drift=='ancestor':
        ancestor=Path(summary['retry_parent']['execution'])/'summary.json';value=json.loads(ancestor.read_text())
        value['controlled_mutation']=True;verifier.write(ancestor,value)
    elif drift=='merged':summary['retry_receipt']['merged']=True
    elif drift=='same_id':summary['retry_receipt']['id']=summary['retry_parent']['job_id']
    elif drift=='receipt_account':summary['retry_receipt']['account_id']='different-account'
    elif drift=='receipt_version':summary['retry_receipt']['input_version']=4
    elif drift=='completed':
        with sqlite3.connect(args.retry_execution/'evaluation.sqlite3') as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(summary['job_id'],))
    verifier.write(path,summary)
    with pytest.raises(ValueError):verifier.prepare(args)
    assert not args.output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['published','account','stage','version','latest','sale_receipt'])
async def test_sale_retry_rejects_incompatible_failed_parent_before_preparation(failed_sale,drift):
    args=failed_sale.args;path=args.retry_execution/'summary.json'
    summary=json.loads(path.read_text())
    if drift=='published':summary['published']=True
    elif drift=='account':summary['account_id']='another-account'
    elif drift=='stage':summary['stage']='a_initial'
    elif drift=='sale_receipt':summary['trades'][0]['input']['price']='999'
    else:
        with sqlite3.connect(args.retry_execution/'evaluation.sqlite3') as db:
            if drift=='version':db.execute('UPDATE accounts SET version=6 WHERE id=?',(failed_sale.identity['account_id'],))
            else:db.execute('UPDATE accounts SET latest_job_id=? WHERE id=?',
                (summary['previous_stage']['job_id'],failed_sale.identity['account_id']))
    if drift in {'published','account','stage','sale_receipt'}:path.write_text(json.dumps(summary))
    with pytest.raises(ValueError):verifier.prepare(args)
    assert not args.output.exists()


@pytest.mark.asyncio
async def test_sale_retry_parent_drift_blocks_preflight_and_each_http(failed_sale,monkeypatch):
    from app.model import ModelClient,ModelBudgetExceeded
    from app.store import Store
    fixture=failed_sale;args=fixture.args;manifest=verifier.prepare(args)
    store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    cfg=store.runtime_role_config(team,'manager',store.settings())
    frozen={'stage':'a_after_sell','source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(fixture.source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':6000,
        'previous_stage':{'summary':str(args.previous_stage),'review':str(args.content_review),'identity':manifest['previous_stage']},
        'retry_parent':manifest['retry_parent'],
        'roles':{'manager':{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']}}}
    budget=verifier.StageBudget.create(args.output/'guard.sqlite3','a_after_sell',40,'controlled fixture',
        verifier.file_digest(args.output/'preparation.json'),frozen)
    budget.admit('controlled-retry');sent=[]
    restore=verifier.install_budget(budget.path)
    client=ModelClient(cfg,transport=httpx.MockTransport(lambda request:sent.append(request) or httpx.Response(200,json={'output':[]})))
    with sqlite3.connect(args.retry_execution/'evaluation.sqlite3') as db:
        db.execute("INSERT INTO meta VALUES('controlled-parent-drift','true')")
    try:
        with pytest.raises(ValueError,match='retry-parent evidence changed'):
            await verifier.run_prepared(run_args(args,authorize_stage='a_after_sell',authorized_http_attempts=40))
        assert not (args.output/'execution').exists()
        with pytest.raises(ModelBudgetExceeded,match='retry-parent evidence changed'):
            await client._post('responses',{'model':cfg['model_name'],'reasoning':{'effort':'xhigh'},'max_output_tokens':6000},
                client.session('controlled retry',{'task_id':'controlled-retry'}))
        assert not sent and budget.status()['reserved_attempts']==0
    finally:
        await client.aclose();restore()


@pytest.mark.asyncio
async def test_sale_retry_rejects_an_api_merge_without_starting_model(failed_sale,monkeypatch):
    from app import agents
    fixture=failed_sale;args=fixture.args;verifier.prepare(args)
    original=verifier.Instance.request
    async def concurrent_task(self,method,url,**kwargs):
        if method=='POST' and url=='/api/jobs/'+fixture.identity['job_id']+'/retry':
            await original(self,'POST','/api/accounts/'+fixture.identity['account_id']+'/analyze',
                json={'operation_id':'controlled-concurrent-research'})
        return await original(self,method,url,**kwargs)
    monkeypatch.setattr(verifier.Instance,'request',concurrent_task)
    monkeypatch.setattr(agents,'ModelClient',lambda *a,**k:pytest.fail('A merged task must not reach the model'))
    assert await verifier.run_prepared(run_args(args,authorize_stage='a_after_sell',authorized_http_attempts=40))==2
    summary=json.loads((args.output/'execution/summary.json').read_text())
    assert summary['budget']['reserved_attempts']==0 and summary['trades']==[]
    assert 'new child job' in summary['error']['message']
    assert summary['retry_parent_unchanged'] and not summary['published']


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown_stage',[None,'a_after_sell','b_cash'])
async def test_followup_matrix_uses_actual_ledger_scope_separate_accounts_and_new_budgets(prepared,monkeypatch,unknown_stage):
    from app import agents
    from app.model import ModelClient
    from app.store import Store
    from tests.test_fourteenth_chain import response,call
    source,args=prepared
    monkeypatch.setattr(verifier,'OBSERVATION_SECONDS',.01)
    install_inprocess_instance(monkeypatch)
    current={'stage':'a_initial'};sent=[];outputs=[];parent_hashes={};materials={}
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"controlled-followup-broken",'
            raise httpx.RemoteProtocolError('controlled accepted unknown outcome')
    def handler(request):
        payload=json.loads(request.content);stage=current['stage']
        seed=json.loads(next(m['content'] for m in payload['input'] if m['role']=='user'))
        sent.append((stage,seed))
        assert payload['reasoning']['effort']=='xhigh' and payload['max_output_tokens']==6000
        assert 'research_records' in next(t for t in payload['tools'] if t['name']=='finish_analysis')['parameters']['properties']
        if stage=='a_initial':
            assert seed['account']['cash']=='86484.90'
            scope=['SH:518880','SZ:002354','SZ:159599']
            material_store=Store(args.output/'execution/evaluation.sqlite3','live')
            materials['public']=material_store.save_material({'scope':'public','symbol':'SZ:159278',
                'url':'https://fixture.invalid/shared-source','text':'Controlled shared original fact for 159278.'})['id']
            materials['private']=material_store.save_material({'scope':'account','url':'https://fixture.invalid/private-a',
                'text':'A private unshared fact must stay inaccessible.'},account_id=seed['account']['id'])['id']
        elif stage=='a_after_sell':
            assert seed['account']['cash']=='87479.90' and seed['account']['version']==5
            scope=['SZ:002354','SZ:159599']
        else:
            assert seed['account']['cash']=='100000' and seed['account']['holdings']==[]
            assert seed['account']['id']!=outputs[0]['account_id']
            assert 'controlled-a_initial-body' not in json.dumps(seed) and 'controlled-a_after_sell-body' not in json.dumps(seed)
            scope=['SZ:159278']
        if stage==unknown_stage:return httpx.Response(200,stream=Broken())
        if stage=='b_cash':
            if sum(item[0]=='b_cash' for item in sent)==1:
                return response(call('get_material',{'material_id':materials['public']},'public-read'),
                    call('get_material',{'material_id':materials['private']},'private-denied'))
            wire=json.dumps(payload)
            assert 'Controlled shared original fact for 159278.' in wire
            assert 'A private unshared fact must stay inaccessible.' not in wire
        return response(call('finish_analysis',{'report':'controlled-'+stage+'-body', 'completion':'limited',
            'scope':scope,'decision_summary':'Controlled independent stage conclusion.', 'limitations':['Offline fixture only']}))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    daily_before=verifier.db_digest(source.path)
    for index,stage in enumerate(('a_initial','a_after_sell','b_cash')):
        current['stage']=stage
        if index:
            parent=args.output
            summary_path,review=accept_business_fixture(parent)
            parent_hashes[str(parent/'execution/evaluation.sqlite3')]=verifier.db_digest(parent/'execution/evaluation.sqlite3')
            args=SimpleNamespace(**{**vars(args),'stage':stage,'max_http_attempts':8,
                'output':args.output.parent/stage,'approved_summary':summary_path,'content_review':review,'previous_stage':summary_path})
            if index==1 and unknown_stage is None:
                # A failed publication, a qualified review and skipping the A
                # change are not interchangeable with an accepted predecessor.
                original_summary=summary_path.read_text();original_review=review.read_text()
                failed=json.loads(original_summary);failed['published']=False
                summary_path.write_text(json.dumps(failed))
                with pytest.raises(ValueError,match='published report'):verifier.prepare(args)
                summary_path.write_text(original_summary)
                qualified=json.loads(original_review);qualified['accepted']=False
                review.write_text(json.dumps(qualified))
                with pytest.raises(ValueError,match='accepted=true'):verifier.prepare(args)
                review.write_text(original_review)
                with pytest.raises(ValueError,match='preceding business stage'):
                    verifier.prepare(SimpleNamespace(**{**vars(args),'stage':'b_cash'}))
                assert not args.output.exists()
        manifest=verifier.prepare(args)
        assert manifest['model_requests']==0 and not (args.output/'execution').exists()
        assert manifest['required_matrix_stages']==['a_initial','a_after_sell','b_cash']
        run=SimpleNamespace(prepared=args.output,run_real=True,authorize_stage=stage,authorized_http_attempts=16 if not index else 8,
            authorization_note='Explicit separate controlled '+stage+' fixture authorization')
        if index==1 and unknown_stage is None:
            from app.model import ModelBudgetExceeded
            parent_database=args.previous_stage.parent/'evaluation.sqlite3'
            frozen=dict(verifier.StageBudget(parent_database.parent/'business-attempts.sqlite3').metadata['frozen'])
            frozen.update(stage=stage,previous_stage={'summary':str(args.previous_stage),'review':str(args.content_review),
                'identity':manifest['previous_stage']})
            guard=verifier.StageBudget.create(args.output/'controlled-guard.sqlite3',stage,8,'offline guard only','fixture',frozen)
            guard.admit('controlled-next-job')
            store=Store(args.output/'prepared.sqlite3','live')
            cfg=store.runtime_role_config(store.freeze_agent_team(),'manager',store.settings())
            restore=verifier.install_budget(guard.path)
            transport_sent=[]
            client=ModelClient(cfg,transport=httpx.MockTransport(lambda request:transport_sent.append(request) or httpx.Response(200,json={'output':[]})))
            with sqlite3.connect(parent_database) as db:db.execute("INSERT INTO meta VALUES('controlled-parent-drift','true')")
            try:
                with pytest.raises(ValueError,match='Preceding stage evidence changed'):await verifier.run_prepared(run)
                assert not (args.output/'execution').exists()
                with pytest.raises(ModelBudgetExceeded,match='Preceding stage evidence changed'):
                    await client._post('responses',{'model':cfg['model_name'],'reasoning':{'effort':'xhigh'},'max_output_tokens':6000},
                        client.session('controlled boundary',{'task_id':'controlled-next-job'}))
                assert not transport_sent and guard.status()['reserved_attempts']==0
            finally:
                with sqlite3.connect(parent_database) as db:db.execute("DELETE FROM meta WHERE key='controlled-parent-drift'")
                await client.http.aclose();restore()
        code=await verifier.run_prepared(run)
        summary=json.loads((args.output/'execution/summary.json').read_text());outputs.append(summary)
        expected_calls=2 if stage=='b_cash' and stage!=unknown_stage else 1
        assert summary['budget']['stage']==stage and summary['budget']['reserved_attempts']==expected_calls
        assert summary['budget']['maximum']==run.authorized_http_attempts
        assert sum(len(item['requests']) for item in summary['all_attempts_after_shutdown'])==expected_calls
        for path,identity in parent_hashes.items():assert verifier.db_digest(path)==identity
        if stage==unknown_stage:
            assert code==2 and not summary['published']
            assert summary['budget']['stops'][0]['reason']=='accepted_or_sent_request_outcome_unknown'
            break
        assert code==0,summary.get('error')
        assert summary['inherited_reports_preserved'] and summary['mechanical_passed']
        assert len(summary['observations'])==4
        if stage=='a_after_sell':
            assert len(summary['trades'])==1 and summary['trades'][0]['input']['side']=='sell'
            assert summary['trades'][0]['input']['traded_at'][:10]==verifier.now()[:10]
            assert summary['prepared_ledger']['input_version']==summary['before_change_ledger']['input_version']+1
            assert summary['prepared_ledger']['summary']['total_fees']=='20.10'
            old=Store(args.output/'execution/evaluation.sqlite3','live').job(outputs[0]['job_id'])
            assert old['state']=='completed' and old['result']['report']=='controlled-a_initial-body'
        if stage=='b_cash':
            assert summary['trades']==[] and summary['prepared_ledger']['positions']==[]
            assert summary['universe_change']['candidates']==['SZ:159278']
            assert summary['account_b_id']!=summary['account_a_id']
    assert verifier.db_digest(source.path)==daily_before
    assert len(sent)==sum(item['budget']['reserved_attempts'] for item in outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['source','environment','binding','wire_effort','wire_output','wire_null','wire_zero','wire_extra','unchanged_child',
    *LONG_TASK_TIMEOUTS,'timeout_after_guard','retry_policy','retry_after_guard'])
async def test_every_http_boundary_checks_frozen_source_and_actual_matrix(prepared,monkeypatch,drift):
    from app.model import ModelClient, ModelBudgetExceeded
    from app.store import Store
    source,args=prepared
    for key,value in LONG_TASK_TIMEOUTS.items():setattr(args,key,value)
    manifest=verifier.prepare(args)
    store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    role=next(r for r in team['roles'] if r!='manager')
    cfg=store.runtime_role_config(team,role,store.settings())
    frozen={'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':6000,
        'timeout_policy':LONG_TASK_TIMEOUTS,
        'retry_policy':manifest['retry_policy'],
        'roles':{role:{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']}}}
    budget=verifier.StageBudget.create(args.output/'guard.sqlite3','a_initial',16,'fixture-only',
        verifier.file_digest(args.output/'preparation.json'),frozen)
    budget.admit('admitted-manager-and-children')
    sent=[]
    restore=verifier.install_budget(budget.path)
    async def original_guard():
        if drift=='timeout_after_guard':client.config['model_request_hard_timeout_seconds']=300
        if drift=='retry_after_guard':client.config['model_retries']=2
    client=ModelClient(cfg,before_request=original_guard,
        transport=httpx.MockTransport(lambda r:sent.append(r) or httpx.Response(200,json={'output':[]})))
    session=client.session('Controlled child fixture',{'task_id':'admitted-manager-and-children'})
    payload={'model':cfg['model_name'],'reasoning':{'effort':cfg['reasoning_effort']},'max_output_tokens':6000}
    if drift=='source':monkeypatch.setattr(verifier,'source_digest',lambda:'changed-after-preflight')
    elif drift=='environment':monkeypatch.setattr(verifier,'env_file_stamp',lambda:{'changed':True})
    elif drift=='binding':source.update_agent_definition(role,{'default_binding':{'reasoning_effort':'low'}})
    elif drift=='wire_effort':payload['reasoning']['effort']='low'
    elif drift=='wire_output':payload['max_output_tokens']=4000
    elif drift=='wire_null':payload['max_output_tokens']=None
    elif drift=='wire_zero':payload['max_output_tokens']=0
    elif drift=='wire_extra':payload['max_tokens']=6000
    elif drift=='retry_policy':client.config['model_retries']=2
    elif drift in LONG_TASK_TIMEOUTS:client.config[drift]+=1
    try:
        if drift=='unchanged_child':
            await client._post('responses',payload,session)
            assert len(sent)==budget.status()['reserved_attempts']==1
            assert {k:client.config[k] for k in LONG_TASK_TIMEOUTS}==LONG_TASK_TIMEOUTS
            assert client.requests[0]['timeout_policy']['request_hard_seconds']==900
        else:
            with pytest.raises((ValueError,ModelBudgetExceeded)):await client._post('responses',payload,session)
            assert sent==[] and budget.status()['reserved_attempts']==0
    finally:
        await client.http.aclose();restore()


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', [{}, *[{field:value}
    for field in ('max_output_tokens','max_completion_tokens','max_tokens') for value in (None,0)]])
async def test_omitted_output_limit_guard_requires_all_three_wire_fields_absent(prepared, wire):
    from app.model import ModelClient
    from app.store import Store
    source,args=prepared
    args.max_output_tokens=None;args.omit_output_limit=True
    manifest=verifier.prepare(args)
    store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    cfg=store.runtime_role_config(team,'manager',store.settings())
    frozen={'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':None,
        'retry_policy':manifest['retry_policy'],
        'roles':{'manager':{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']}}}
    budget=verifier.StageBudget.create(args.output/'guard.sqlite3','a_initial',16,'fixture-only',
        verifier.file_digest(args.output/'preparation.json'),frozen)
    budget.admit('controlled-omission-job')
    sent=[];restore=verifier.install_budget(budget.path)
    client=ModelClient(cfg,transport=httpx.MockTransport(lambda r:
        sent.append(json.loads(r.content)) or httpx.Response(200,json={'status':'completed','output':[
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'controlled fixture'}]}]})))
    session=client.session('controlled omission',{'task_id':'controlled-omission-job'})
    try:
        if wire:
            with pytest.raises(ValueError,match='output configuration'):
                await client._post('responses',{'model':cfg['model_name'],
                    'reasoning':{'effort':cfg['reasoning_effort']},**wire},session)
            assert sent==[] and budget.status()['reserved_attempts']==0
        else:
            await client.complete(session,[])
            assert len(sent)==budget.status()['reserved_attempts']==1
            assert not any(field in sent[0] for field in ('max_output_tokens','max_completion_tokens','max_tokens'))
            assert client.requests[0]['output_limit_fields_present']==[]
    finally:
        await client.aclose();restore()


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,outcome,rejections,expected_attempts',[
    (None,'http_503',1,2), (0,'http_503',1,1), (2,'http_503',2,3),
    (None,'unknown_200',0,1), (2,'unknown_200',0,1), (2,'retry_policy_drift',1,1),
])
async def test_source_retry_policy_is_frozen_and_every_http_retry_is_counted(
        prepared,monkeypatch,configured,outcome,rejections,expected_attempts):
    from app.agents import MANAGER_TOOLS
    from app.model import ModelClient,ModelError
    from app.store import Store
    from tests.test_fourteenth_chain import response,call,final
    source,args=prepared
    settings=source.settings()
    if configured is None:settings.pop('model_retries',None)
    else:settings['model_retries']=configured
    source.set_meta('settings',settings)
    original_source=verifier.db_digest(source.path)
    manifest=verifier.prepare(args)
    store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    cfg=store.runtime_role_config(team,'manager',store.settings())
    expected_policy={'model_retries_source':'ModelClient_default' if configured is None else 'source_settings',
        'configured_model_retries':configured,'effective_model_retries':1 if configured is None else configured,
        'maximum_http_attempts_per_model_call':2 if configured is None else configured+1,
        'retryable_http_statuses':[429,500,502,503,504],
        'accepted_unknown_response_retry':False,'max_job_attempts':1}
    assert manifest['retry_policy']==expected_policy
    assert cfg.get('model_retries')==configured and ('model_retries' in cfg)==(configured is not None)
    assert cfg['max_job_attempts']==1
    frozen={'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':6000,
        'retry_policy':manifest['retry_policy'],
        'roles':{'manager':{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']}}}
    budget=verifier.StageBudget.create(args.output/'retry-policy.sqlite3','a_initial',4,
        'controlled fixture only',verifier.file_digest(args.output/'preparation.json'),frozen)
    budget.admit('controlled-retry-job')
    sent=[]
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"controlled-accepted-response",'
            raise httpx.RemoteProtocolError('controlled accepted HTTP 200 ended without complete body')
    def handler(request):
        sent.append(request)
        if outcome=='unknown_200':return httpx.Response(200,stream=Broken())
        if len(sent)<=rejections:
            if outcome=='retry_policy_drift':client.config['model_retries']=0
            return httpx.Response(503,json={'error':'controlled temporary overload'})
        return response(call('finish_analysis',final('Controlled successful retry.')))
    restore=verifier.install_budget(budget.path)
    client=ModelClient(cfg,transport=httpx.MockTransport(handler))
    session=client.session('Controlled retry fixture.',{'task_id':'controlled-retry-job'})
    tool=next(item for item in MANAGER_TOOLS if item['name']=='finish_analysis')
    try:
        succeeds=outcome=='http_503' and configured!=0
        if succeeds:
            result=await client.complete(session,[tool],tool_choice='finish_analysis')
            assert result.calls[0].name=='finish_analysis'
        else:
            with pytest.raises((ModelError,ValueError)):
                await client.complete(session,[tool],tool_choice='finish_analysis')
        status=verifier.StageBudget(budget.path).status()
        assert len(sent)==len(client.requests)==status['reserved_attempts']==expected_attempts
        if outcome=='unknown_200':
            assert client.requests[0]['status_code']==200 and client.requests[0]['request_accepted'] is True
            assert status['stops'][0]['reason']=='accepted_or_sent_request_outcome_unknown'
        elif outcome=='retry_policy_drift':
            assert status['stops'][0]['reason']=='actual_http_retry_policy_differs_from_preparation'
        else:
            assert not status['stops']
            assert [item['status_code'] for item in client.requests]==([503]*rejections+[200] if succeeds else [503])
        assert verifier.db_digest(source.path)==original_source
    finally:
        await client.aclose();restore()


@pytest.mark.asyncio
async def test_occupied_reviewed_port_rejects_before_child_launch(tmp_path,monkeypatch):
    import socket
    import subprocess
    attempted=[]
    class Occupied:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def setsockopt(self,level,option,value):
            assert (level,option,value)==(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        def bind(self,address):
            attempted.append(address);raise OSError('controlled port occupancy')
    monkeypatch.setattr(socket,'socket',Occupied)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('No child on an occupied reviewed port'))
    instance=verifier.Instance(tmp_path/'isolated.sqlite3',tmp_path,'occupied-fixture',False)
    try:
        with pytest.raises(OSError,match='occupancy'):await instance.start()
        assert attempted==[('127.0.0.1',18765)] and instance.process is None
    finally:
        await instance.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',[
    'accepted_external_cancel','accepted_child_deadline','before_send','http_rejected','completed_terminal_tail',
    'accepted_eof_no_terminal','accepted_invalid_terminal','sent_before_headers_cancel',
    'incomplete_terminal_tail','failed_terminal_tail','cancelled_terminal_tail',
])
async def test_child_failure_does_not_block_independent_manager_submission(prepared,monkeypatch,boundary):
    from app.agents import MANAGER_TOOLS,CHILD_TOOLS
    from app.model import ModelClient,ModelBudgetExceeded,ModelProtocolError
    from app.store import Store
    from tests.test_fourteenth_chain import response,call,final
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock,wait_for_stage
    source,args=prepared;manifest=verifier.prepare(args)
    store=Store(args.output/'prepared.sqlite3','live');team=store.freeze_agent_team()
    child_role=next(role for role in team['roles'] if role!='manager')
    configs={role:store.runtime_role_config(team,role,store.settings()) for role in ('manager',child_role)}
    for cfg in configs.values():
        cfg.update(model_responses_stream=True,model_request_hard_timeout_seconds=30,
            synthesis_request_hard_timeout_seconds=30,model_first_event_timeout_seconds=30,
            model_stream_idle_timeout_seconds=30)
    entered,release=asyncio.Event(),asyncio.Event();sent=[]
    server=None;peers=set()
    async def hold_without_headers(reader,writer):
        peer=asyncio.current_task();peers.add(peer)
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            length=next(int(line.split(b':',1)[1]) for line in headers.split(b'\r\n')
                        if line.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            sent.append('child');entered.set()
            await release.wait()
        finally:
            writer.close();await writer.wait_closed();peers.discard(peer)
    if boundary=='sent_before_headers_cancel':
        server=await asyncio.start_server(hold_without_headers,'127.0.0.1',0)
        configs[child_role]['model_base_url']='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/v1'
    frozen={'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
        'configuration_source':str(source.path),'reference_account':args.reference_account,
        'configuration_sha256':manifest['source_configuration_sha256'],'max_output_tokens':6000,
        'roles':{role:{'model':cfg['model_name'],'base_url':cfg['model_base_url'],
            'protocol':cfg['model_protocol'],'reasoning_effort':cfg['reasoning_effort']} for role,cfg in configs.items()}}
    budget=verifier.StageBudget.create(args.output/'cancel-guard.sqlite3','a_initial',4,'controlled fixture only',
        verifier.file_digest(args.output/'preparation.json'),frozen)
    job='admitted-manager-and-child';budget.admit(job)
    timeline=ControlledDeadlineClock(monkeypatch,60) if boundary=='accepted_child_deadline' else None
    terminal_status=boundary.removesuffix('_terminal_tail') if boundary.endswith('_terminal_tail') else 'completed'
    terminal={'type':'response.'+terminal_status,'response':{'id':'controlled-child','status':terminal_status,
        'output':[{'type':'function_call','status':'completed','call_id':'child-call','name':'submit_report',
                   'arguments':'{"summary":"Controlled complete child result."}'}]}}
    if boundary=='accepted_invalid_terminal':terminal['response']['output']='not-an-output-list'
    class HeldBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            if boundary=='http_rejected':yield b'{"error":'
            else:
                event=terminal if boundary.endswith('_terminal_tail') or boundary=='accepted_invalid_terminal' else {'type':'response.output_text.delta','delta':'Controlled visible progress.'}
                yield ('data: '+json.dumps(event)+'\n\n').encode()
                if boundary=='accepted_eof_no_terminal':return
            # Reaching here proves HTTP headers and the first body/event were
            # consumed; for completed, the production tail drain is now active.
            entered.set()
            await release.wait()
    async def before_child():
        if boundary=='before_send':
            entered.set()
            await release.wait()
    async def child_http(request):
        sent.append('child')
        return httpx.Response(401 if boundary=='http_rejected' else 200,
            headers={'content-type':'application/json' if boundary=='http_rejected' else 'text/event-stream'},stream=HeldBody())
    def manager_http(request):
        sent.append('manager')
        return response(call('finish_analysis',final('Controlled manager result.')))
    restore=verifier.install_budget(budget.path)
    child=ModelClient(configs[child_role],before_request=before_child,
        transport=httpx.AsyncHTTPTransport() if server else httpx.MockTransport(child_http))
    manager=ModelClient(configs['manager'],transport=httpx.MockTransport(manager_http))
    child_session=child.session('Controlled child.',{'task_id':job});child_session.agent_id='controlled-child'
    manager_session=manager.session('Controlled manager.',{'task_id':job})
    child_tool=next(tool for tool in CHILD_TOOLS if tool['name']=='submit_report')
    manager_tool=next(tool for tool in MANAGER_TOOLS if tool['name']=='finish_analysis')
    async def run_child():
        if timeline:
            deadline=timeline.monotonic()+20
            child_session.deadlines={'child_budget_exhausted':deadline}
            async with asyncio.timeout_at(deadline):
                return await child.complete(child_session,[child_tool],tool_choice='submit_report')
        return await child.complete(child_session,[child_tool],tool_choice='submit_report')
    task=asyncio.create_task(run_child())
    try:
        protocol_failure=boundary in {'accepted_eof_no_terminal','accepted_invalid_terminal'}
        if protocol_failure:
            with pytest.raises(ModelProtocolError):await task
        else:
            await wait_for_stage(task,entered)
            if timeline:timeline.advance(21)
            else:task.cancel('manager_cancelled_child')
            with pytest.raises(TimeoutError if timeline else asyncio.CancelledError):await task
        if boundary!='before_send':
            observation=child.requests[-1]
            if protocol_failure:
                assert observation['error_code']==('stream_missing_terminal' if boundary=='accepted_eof_no_terminal' else 'invalid_stream_terminal')
                assert not observation.get('retry_suppressed')
            else:
                assert observation['retry_suppressed']=='cancelled'
                assert observation['error_code']==('child_budget_exhausted' if timeline else 'cancelled')
            assert observation['request_accepted'] is (None if boundary=='sent_before_headers_cancel' else boundary!='http_rejected')
            expected_terminal='response.'+terminal_status if boundary.endswith('_terminal_tail') or boundary=='accepted_invalid_terminal' else None
            assert observation.get('stream_terminal')==expected_terminal
            if server:
                assert any(event['scope']=='origin' and event['event'].endswith('send_request_body.complete')
                           for event in observation['transport_events'])
        await manager.complete(manager_session,[manager_tool],tool_choice='finish_analysis')
        reopened=verifier.StageBudget(budget.path).status()
        assert not reopened['stops']
        assert sent==(['manager'] if boundary=='before_send' else ['child','manager'])
        assert reopened['reserved_attempts']==len(sent)
    finally:
        if not task.done():
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        await child.aclose();await manager.aclose();restore()
        release.set()
        if server:
            server.close();await server.wait_closed()
            await asyncio.gather(*peers)
