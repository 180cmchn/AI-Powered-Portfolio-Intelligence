"""Real protocol payloads distinguish a bounded initial excerpt from later projections.

No shared effective_context helper: the regression was hidden by that helper's
same whole-wrapper skip. No external HTTP or production database is used.
"""
import copy
import hashlib
import json

import httpx
import pytest

from app import agents
from app.context import archive, bounded_result, compact, serialized
from app.model import ModelClient, ModelError, ModelSession, ToolCall
from app.recovery import report_identity
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, setup, output
from tests.test_seventeenth_child_final_evidence import add_tool
from tests.test_seventeenth_model_cache import response


def users(payload):
    return [json.loads(m['content']) for m in payload.get('input', payload.get('messages'))
            if m.get('role') == 'user']


def huge_context(context):
    # A previously visible report may be larger than the initial context budget.
    return {**context, 'previous_analysis': {'report': 'known prior finding; ' * 6000
                + 'UNREAD-ORIGINAL-TAIL', 'result': {}, 'input_version': 1}}


def expanded(seed):
    result=[]
    for item in seed.get('calculation_summaries', []):
        value=copy.deepcopy((seed.get('calculation_contexts') or {}).get(item.get('context_ref'), {}))
        value.update(copy.deepcopy(item));value.pop('context_ref',None)
        if value.pop('model_config_ref',None)=='seed.model_config':
            value['model_config']=copy.deepcopy(seed['model_config'])
        result.append(value)
    return result


def flood(client,session):
    for i in range(25):client.add_user(session,{'prior_visible_note':i,'text':'bounded context note ' * 500})


async def send(runtime,session,*,final=False):
    token=runtime.request_owner.set(('manager',final))
    try:return await runtime.client.complete(session,agents.RESEARCH_TOOLS)
    finally:runtime.request_owner.reset(token)


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_large_initial_wrapper_is_scoped_and_owned_calculations_survive_epochs(tmp_path,monkeypatch,protocol):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None,
               max_tool_result_chars=12000)
    store,tools,context=setup(tmp_path,cfg);payloads=[];events=[]
    def handler(request):
        payload=json.loads(request.content);payloads.append(payload)
        current=users(payload)[0]
        assert current['initial_context_excerpt']==wrapper
        assert 'truncated_for_context' not in current
        assert 'UNREAD-ORIGINAL-TAIL' not in json.dumps(payload)
        actual=expanded(current)
        assert actual==[item['summary'] for item in runtime.calculations]
        if actual:
            assert all(item['visibility']=='summary' for item in runtime.calculation_coverage[-1]['calculations'])
        return httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):events.append(event)
    runtime=agents.ResearchRuntime(huge_context(context),cfg,tools,emit);session=runtime.manager_session
    try:
        initial=json.loads(session.history[0]['content']);wrapper=copy.deepcopy(initial['initial_context_excerpt'])
        assert wrapper==bounded_result(ModelSession(system='controlled comparison'),runtime.context,80000)
        assert wrapper['truncated_for_context'] is True and len(wrapper['context_excerpt'])==40000
        assert initial['account']==context['account'] and initial['input_version']==context['input_version']
        archived=next(item for item in session.archive if item['record_id']==wrapper['record_id'])
        assert 'UNREAD-ORIGINAL-TAIL' in archived['payload']
        wrapper_hash=hashlib.sha256(serialized(wrapper).encode()).hexdigest()
        runtime._pin_runtime_status(session,'manager',None,False)
        await send(runtime,session)
        for epoch in range(3):
            values=[str(epoch+12), '5.67']
            await runtime._dispatch('manager','calculate',{'operation':'subtract','values':values})
            runtime._pin_runtime_status(session,'manager',None,False)
            flood(runtime.client,session)
            await send(runtime,session)
            assert session.compactions==epoch+1
            current=json.loads(session.history[0]['content'])
            assert hashlib.sha256(serialized(current['initial_context_excerpt']).encode()).hexdigest()==wrapper_hash
            runtime._pin_runtime_status(session,'manager',None,False)
            update=json.loads(session.history[-1]['content'])
            assert 'calculation_summaries' not in update  # No false omitted/repeated pin.
            before=copy.deepcopy(payloads[-1].get('input',payloads[-1].get('messages')))
            await send(runtime,session)
            after=payloads[-1].get('input',payloads[-1].get('messages'))
            assert after[:len(before)]==before
            assert session.compactions==epoch+1
        assert len(payloads)==7
        assert runtime._visible_calculations(session)[0].keys()=={r['calculation_id'] for r in runtime.calculations}
    finally:await runtime.close()


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_large_initial_owned_pending_source_receipt_matches_accepted_http(tmp_path,monkeypatch,protocol):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None,
               max_tool_result_chars=12000)
    store,tools,context=setup(tmp_path,cfg)
    material=store.save_material({'scope':'account','text':'Exact owned source with original period 2026Q3.',
        'url':'https://fixture.invalid/owned-source','access':'full_text'},context['job_id'],context['account']['id'])
    value=store.material_range(material['id']);payloads=[]
    async def handler(request):
        payload=json.loads(request.content);payloads.append(payload);current=users(payload)[0]
        assert current['initial_context_excerpt']==wrapper
        item,=[item for item in current['delivered_research_evidence'] if item['tool']=='get_material']
        assert item['result']==value and item['source_agent_id']=='manager'
        assert item['source_task_id']==context['job_id']
        assert not store.checkpoint_deliveries(context['job_id'])
        return httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):pass
    runtime=agents.ResearchRuntime(huge_context(context),cfg,tools,emit);session=runtime.manager_session
    try:
        wrapper=copy.deepcopy(json.loads(session.history[0]['content'])['initial_context_excerpt'])
        call=add_tool(runtime.client,session,'get_material',value,'owned-source',{'material_id':material['id']})
        proof=store.prepare_pending_tool_output(context['job_id'],context['account']['id'],'manager','get_material',value)
        session._pending_material_deliveries=[{'call':call,'message':copy.deepcopy(session.history[-1]),'result':copy.deepcopy(value),'proof':proof}]
        runtime._pin_runtime_status(session,'manager',None,False);flood(runtime.client,session)
        await send(runtime,session)
        receipts=store.checkpoint_deliveries(context['job_id']);assert len(receipts)==1
        assert receipts[0]['agent_id']=='manager' and receipts[0]['job_id']==context['job_id']
        assert receipts[0]['body']['visible_result']==value
        assert not session._pending_material_deliveries
        assert session.compactions==1
    finally:await runtime.close()


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_large_initial_owned_child_report_receipt_preserves_full_identity(tmp_path,monkeypatch,protocol):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None,
               max_tool_result_chars=12000)
    store,tools,context=setup(tmp_path,cfg)
    material=store.save_material({'scope':'public','text':'Child original period and units.',
        'url':'https://fixture.invalid/child-source','access':'full_text'})
    ids=store.record_material_delivery(context['job_id'],context['account']['id'],'child-fixture','get_material',store.material_range(material['id']))
    report={'report_id':'child-owned-report','agent_id':'child-fixture','research_round':1,'role_id':'researcher',
        'summary':'Owned completed report','findings':[{'claim':'Known original period and units'}],
        'material_ids':[material['id']],'material_delivery_ids':ids,'open_questions':[]}
    checkpoint={'phase':'synthesis','task_id':context['job_id'],'account_id':context['account']['id'],
        'input_version':context['input_version'],'reports':[report],'calculations':[],'recovery_mode':'continue_research'}
    def handler(request):
        p=json.loads(request.content);current=users(p)[0]
        assert current['initial_context_excerpt']==wrapper
        child=next(item for item in current['synthesis_checkpoint']['reports'] if item['report_id']==report['report_id'])
        assert child['summary']==report['summary'] and child['findings']==report['findings']
        assert not store.checkpoint_report_deliveries(context['job_id'])
        return httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):pass
    runtime=agents.ResearchRuntime(huge_context(context),cfg,tools,emit);session=runtime.manager_session
    try:
        wrapper=copy.deepcopy(json.loads(session.history[0]['content'])['initial_context_excerpt'])
        # Runtime-owned restored report authority and projection, added after
        # the original oversized seed; the source was read by the child only.
        runtime.restored_checkpoint=copy.deepcopy(checkpoint)
        current=json.loads(session.history[0]['content']);current['synthesis_checkpoint']=agents._checkpoint_context(checkpoint)
        session.history[0]['content']=json.dumps(current,ensure_ascii=False)
        runtime._pin_runtime_status(session,'manager',None,False);flood(runtime.client,session)
        await send(runtime,session)
        receipts=store.checkpoint_report_deliveries(context['job_id']);assert len(receipts)==1
        body=receipts[0]['body'];assert body['access']=='full_report'
        assert body['report_hash']==report_identity(report)
        assert [x['id'] for x in body['source_material_deliveries']]==ids
        assert body['source_task_id']==context['job_id'] and body['source_agent_id']=='child-fixture'
        assert not [r for r in store.checkpoint_deliveries(context['job_id']) if r['agent_id']=='manager']
    finally:await runtime.close()


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
@pytest.mark.parametrize('case',['rejected','altered','foreign'])
async def test_owned_envelope_never_invents_a_pending_source_receipt(tmp_path,monkeypatch,protocol,case):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None,
               max_tool_result_chars=12000)
    store,tools,context=setup(tmp_path,cfg)
    material=store.save_material({'scope':'account','text':'Exact source for acceptance negative control.',
        'url':'https://fixture.invalid/negative-source','access':'full_text'},context['job_id'],context['account']['id'])
    value=store.material_range(material['id']);captured=[]
    def handler(request):
        p=json.loads(request.content);captured.append(p);current=users(p)[0]
        assert current['initial_context_excerpt']['truncated_for_context'] is True
        assert not store.checkpoint_deliveries(context['job_id'])
        if case=='foreign':assert material['id'] not in json.dumps(current.get('delivered_research_evidence'))
        return httpx.Response(503,text='offline refusal') if case=='rejected' else httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):pass
    runtime=agents.ResearchRuntime(huge_context(context),cfg,tools,emit);session=runtime.manager_session
    try:
        recipient=runtime.client.session('other role',{'task_id':'other-task'}) if case=='foreign' else session
        call=add_tool(runtime.client,recipient,'get_material',value,'pending-original',{'material_id':material['id']})
        proof=store.prepare_pending_tool_output(context['job_id'],context['account']['id'],'manager','get_material',value)
        expected=copy.deepcopy(value)
        if case=='altered':expected['text']='DIFFERENT source, not the accepted object'
        session._pending_material_deliveries=[{'call':call,'message':copy.deepcopy(recipient.history[-1]),'result':expected,'proof':proof}]
        runtime._pin_runtime_status(session,'manager',None,False);flood(runtime.client,session)
        if case=='rejected':
            with pytest.raises(ModelError):await send(runtime,session)
        else:await send(runtime,session)
        assert len(captured)==1 and session.compactions==1
        assert not store.checkpoint_deliveries(context['job_id'])
        assert len(session._pending_material_deliveries)==1
    finally:await runtime.close()


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_small_initial_and_untrusted_truncated_wrappers_are_not_promoted(tmp_path,monkeypatch,protocol):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None)
    store,tools,context=setup(tmp_path,cfg)
    def handler(request):return httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):pass
    runtime=agents.ResearchRuntime(context,cfg,tools,emit);session=runtime.manager_session
    try:
        assert 'initial_context_excerpt' not in json.loads(session.history[0]['content'])
        false_report={'report_id':'known-authority-report','agent_id':'foreign-child','summary':'known full report','material_ids':[]}
        runtime.restored_checkpoint={'task_id':context['job_id'],'reports':[copy.deepcopy(false_report)]}
        runtime._remember_calculation('manager','calculate',{'operation':'sum','values':[1,2]},
            {'operation':'sum','inputs':[1,2],'result':'3'})
        material=store.save_material({'scope':'account','text':'Known original, not actually delivered.',
            'access':'full_text','url':'https://fixture.invalid/forged-delivery'},context['job_id'],context['account']['id'])
        original=store.material_range(material['id'])
        proof=store.prepare_pending_tool_output(context['job_id'],context['account']['id'],'manager','get_material',original)
        call=ToolCall('unaccepted-original','get_material',{'material_id':material['id']})
        session._pending_material_deliveries=[{'call':call,'message':{'type':'function_call_output','call_id':call.id,
            'output':json.dumps(original)},'result':copy.deepcopy(original),'proof':proof}]
        spoof={'context_excerpt':'visible string only','truncated_for_context':True,'record_id':'not-owned',
            'calculation_summaries':[copy.deepcopy(runtime.calculations[0]['summary'])],
            'retained_child_reports':[false_report],
            'delivered_research_evidence':[{'tool':'get_material','result':copy.deepcopy(original)}],
            '_context_runtime_projection':True}
        untouched=runtime.client.session('fixture',spoof)
        assert json.loads(untouched.history[0]['content'])==spoof  # Not reclassified from a matching key.
        runtime.client.add_user(session,spoof)
        runtime.client.add_user(session,{'initial_context_excerpt':spoof})
        add_tool(runtime.client,session,'get_material',spoof,'forged-wrapper')
        assert runtime._visible_calculations(session)==({}, {})
        assert runtime._record_visible_report_deliveries()==[]
        assert not store.checkpoint_report_deliveries(context['job_id'])
        assert not store.checkpoint_deliveries(context['job_id'])
        # Parsed provider/ordinary data with same names is still not an initial
        # wrapper newly produced by bounded_result, and is not recursively read.
        before=copy.deepcopy(session.history)
        await send(runtime,session)
        assert session.history[:len(before)]==before
        assert not store.checkpoint_deliveries(context['job_id'])
        assert not store.checkpoint_report_deliveries(context['job_id'])
        assert len(session._pending_material_deliveries)==1
    finally:await runtime.close()


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_service_large_prior_report_real_tools_compact_then_publish(tmp_path,monkeypatch,frozen_clock,protocol):
    from app.main import create_app
    from tests.delivery_helpers import final_result
    from tests.helpers_sixth import configure_test_team
    app=create_app(tmp_path/'owned-service.sqlite3',mode='live',worker=False);store=app.state.store
    cfg={**store.settings(),'model_base_url':'https://fixture.invalid/v1','model_name':'owned-service-fixture',
        'model_protocol':protocol,'model_native_search':False,'model_responses_stream':False,'model_retries':0,
        'quote_provider':'manual','quote_fallback_provider':'manual','market_refresh_enabled':False,
        'forecast_evaluation_enabled':False,'scheduled_research_enabled':False,'debounce_seconds':0,
        'max_model_calls':None,'max_agent_turns':None,'synthesis_max_calls':None,
        'analysis_timeout_seconds':120,'model_request_hard_timeout_seconds':3,'synthesis_request_hard_timeout_seconds':3,
        'max_context_chars':240000,'max_tool_result_chars':12000}
    store.set_meta('settings',cfg);configure_test_team(store,cfg)
    account=store.save_account({'name':'Owned context service fixture','cash':'10000','holdings':[],
                               'research_scope':'cash_only'})
    old,=store.claim_jobs(account_id=account['id'])
    prior='Already published account finding. '*3600+'UNREAD-SERVICE-TAIL'
    assert store.publish(old['id'],final_result({'report':prior,'covered_demand_ids':[]},scope=('cash_only',)),None)
    originals=[store.save_material({'scope':'public','text':f'Owned original {i}; '+('fixture body ' * 680),
        'url':f'https://fixture.invalid/visible-{i}','access':'full_text'}) for i in range(22)]
    job=store.submit_task(account['id'],operation_id='large-owned-context-research',research_scope='cash_only')
    claimed,=store.claim_jobs(account_id=account['id']);assert claimed['id']==job['id']
    captured=[];wrapper=None
    def handler(request):
        nonlocal wrapper
        p=json.loads(request.content);captured.append(p);current=users(p)[0]
        assert 'truncated_for_context' not in current
        assert current['initial_context_excerpt']['truncated_for_context'] is True
        if len(captured)==1:
            wrapper=copy.deepcopy(current['initial_context_excerpt'])
            return output(protocol,[('calculate',{'operation':'subtract','values':['12.34','5.67']}),
                ('calculate',{'operation':'percent','values':['0.63','37.72']}),
                *[('read_material_range',{'material_id':m['id'],'start':0,'length':10000}) for m in originals]])
        assert len(captured)==2
        assert current['initial_context_excerpt']==wrapper
        summaries=expanded(current)
        assert [(v['operation'],v['inputs']) for v in summaries]==[('subtract',['12.34','5.67']),('percent',['0.63','37.72'])]
        assert summaries[0]['result']=='6.67'
        assert current.get('delivered_research_evidence')
        assert 'UNREAD-SERVICE-TAIL' not in json.dumps(p)
        return output(protocol,[('finish_analysis',delivery_arguments(report='Controlled report adopts 12.34 minus 5.67 = 6.67.',
            scope=('cash_only',),covered_demand_ids=[]))])
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    await app.state.service.execute(claimed)
    result=store.job(claimed['id'],events=True)
    assert result['state']=='completed',result['error']
    assert len(captured)==2
    cov=[e for e in result['events'] if e['type']=='calculation_context_coverage']
    assert cov and [x['visibility'] for x in cov[-1]['payload']['calculations']]==['summary','summary']
    assert any(e['type']=='context_compacted' for e in result['events'])
    assert store.checkpoint_deliveries(claimed['id'])
    with store.connect() as db:
        assert db.execute('select report from analyses where job_id=?',(old['id'],)).fetchone()[0]==prior


@pytest.mark.parametrize('protocol',['responses','chat_completions'])
@pytest.mark.parametrize('truncated',[False,True],ids=['complete-child','summary-only-child'])
async def test_spawned_child_projection_remains_attributable_under_large_initial_seed(tmp_path,monkeypatch,protocol,truncated):
    cfg=config(monkeypatch,model_protocol=protocol,max_model_calls=None,max_agent_turns=None,synthesis_max_calls=None,
               max_tool_result_chars=12000)
    store,tools,context=setup(tmp_path,cfg);payloads=[];report={}
    def handler(request):
        p=json.loads(request.content);payloads.append(p);current=users(p)[0]
        assert current['initial_context_excerpt']['truncated_for_context'] is True
        item,=current['retained_child_reports']
        assert item['summary']==report['summary'] and item.get('summary_truncated')==report.get('summary_truncated')
        assert 'UNREAD-ORIGINAL-TAIL' not in json.dumps(p)
        return httpx.Response(200,json=response(protocol))
    monkeypatch.setattr(agents,'ModelClient',lambda c,before_request=None:ModelClient(c,before_request,httpx.MockTransport(handler)))
    async def emit(event):pass
    runtime=agents.ResearchRuntime(huge_context(context),cfg,tools,emit);session=runtime.manager_session
    monkeypatch.setattr(runtime,'_launch',lambda child:None)
    try:
        child_id=(await runtime._spawn({'task':'Controlled attribution of an actually completed child.'}))['agent_id']
        child=runtime.children[child_id];child.status='completed'
        report.update(report_id='spawned-controlled-report',agent_id=child_id,research_round=1,role_id=child.role_id,
            summary='Complete visible child finding.' if not truncated else 'Explicit child excerpt.',
            findings=[] if truncated else [{'claim':'Complete child fact'}],material_ids=[],open_questions=[])
        if truncated:report['summary_truncated']=True
        child.reports.append(report)
        runtime._pin_runtime_status(session,'manager',None,False);flood(runtime.client,session)
        await send(runtime,session)
        receipts=store.checkpoint_report_deliveries(context['job_id']);assert receipts
        for receipt in receipts:
            body=receipt['body'];assert body['source_agent_id']==child_id and body['source_task_id']==context['job_id']
            assert body['report_hash']==report_identity(report)
            if truncated:assert body['access']=='report_summary'
        if not truncated:assert any(r['body']['access']=='full_report' for r in receipts)
        assert len(payloads)==1 and session.compactions==1
    finally:await runtime.close()
