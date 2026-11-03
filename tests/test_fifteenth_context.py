"""Visible-data compaction and final-only execution; all model output is fixture."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.context import compact_calculation_metadata
from scripts import verify_fifteenth_aggregate as verifier


def test_shared_context_is_exact_and_keeps_attribution_values_and_counter_evidence():
    contexts = {'model_config': {'model_name': 'fixture', 'fingerprint': 'x'*64},
        'method': 'Exact arithmetic; supplied operands only. '*8,
        'security': 'not supplied by calculate', 'date_range': 'not supplied by calculate',
        'source_material_version': 'not supplied by calculate'}
    originals = [{**contexts, 'calculation_id':str(i), 'result':str(i), 'unit':'input-dependent',
        'agent_id':'manager', 'source_task_id':'historical', 'input_version':4,
        'full_inputs':{'record_id':f'context-{i}'}} for i in range(3)]
    before = copy.deepcopy(originals)
    summaries, table = compact_calculation_metadata(originals)
    assert originals == before and len(table) == 1
    assert len(json.dumps([summaries, table])) < len(json.dumps(originals))
    for summary, original in zip(summaries, originals):
        ref = summary.pop('context_ref')
        assert {**table[ref], **summary} == original
    # A distinct period or missing source must not silently inherit a sibling's.
    originals[-1]['date_range'] = '2025 annual; source unverified'
    summaries, table = compact_calculation_metadata(originals)
    assert summaries[-1]['date_range'] == originals[-1]['date_range']
    assert 'context_ref' not in summaries[-1]


def fixture_preparation(tmp_path, monkeypatch, *, old_builtin=False, manager_reasoning_effort=None, source_effort=None,
                        max_output_tokens=None, required_fields_only=False, network_route='environment'):
    from app import runtime_version
    from app.domain import AccountInput, now
    from app.store import Store
    from tests.helpers_sixth import configure_test_team
    from app.agent_config import AgentConfigStore
    monkeypatch.setattr(runtime_version, 'source_digest', lambda:'controlled-source-identity')
    store = Store(tmp_path/'historical.sqlite3', 'live')
    cfg = {**store.settings(), 'model_name':'controlled-only', 'model_base_url':'https://fixture.invalid/v1',
        'model_protocol':'responses', 'model_native_search':False, 'model_responses_stream':False,
        'model_retries':0, 'max_model_calls':4, 'max_agent_turns':4, 'analysis_timeout_seconds':20, 'max_output_tokens':4000,
        'synthesis_request_hard_timeout_seconds':2, 'model_request_hard_timeout_seconds':2}
    if source_effort is not None:
        cfg['reasoning_effort'] = source_effort
    store.set_meta('settings',cfg)
    configure_test_team(store,cfg)
    # All copied isolated stores read the same explicit fixture key vault.
    monkeypatch.setattr(AgentConfigStore, '_vault', lambda self:store._credential_store)
    account = store.save_account(AccountInput(name='Controlled historical three-security account', cash='10000',
        holdings=[{'symbol':s,'quantity':'100'} for s in ('SH:518880','SZ:002354','SZ:159599')]))
    job = store.job(account['latest_job_id'])
    snapshot = store.snapshot(account['id'], account['version'])
    with store.connect() as db:
        db.execute('INSERT INTO synthesis_checkpoints VALUES(?,?,?,?,?,?,?,0)',
            (job['id'], account['id'],account['version'],1,'{}','{}',now()))
    package = {'kind':'controlled_fixture', 'historical_task_id':job['id'], 'input_version':account['version'],
        'data_as_of':'2026-10-30T15:00:00+08:00',
        'account':{key:snapshot[key] for key in ('cash','holdings','preferences','version')},
        'metrics':{'cash':'10000','calculated_at':'2026-10-30T15:00:00+08:00'},
        'reports':[{'report_id':'child-1','summary':'Controlled full report, adverse factor intact. '+'CONTROLLED_VISIBLE_REPORT '*1600,
                    'findings':[{'counter_factor':'Not verified by source全文'}], 'open_questions':['Missing market trend']}],
        'calculations':[{'tool':'calculate', 'calculation_id':str(i), 'arguments':{'values':['1','2']},
            'summary':{'tool':'calculate','calculation_id':str(i),'result':'3',
                'method':'Arithmetic only. '*30, 'security':'not supplied by calculate'}} for i in range(2)],
        'materials':[], 'open_questions':['Missing original filing'], 'unresolved_tasks':[]}
    import sqlite3
    configuration_source=tmp_path/'configuration.sqlite3'
    with sqlite3.connect(store.path) as origin, sqlite3.connect(configuration_source) as dest:
        origin.backup(dest)
    if old_builtin:
        from pathlib import Path
        with sqlite3.connect(configuration_source) as db:
            role=json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
            role.update(system_prompt=Path('tests/fixtures/thirteenth-manager-prompt.txt').read_text(),
                        custom=False, version=2, prompt_version=2)
            db.execute("UPDATE agent_definitions SET body=?,version=2 WHERE role_id='manager'",(json.dumps(role),))
    evidence=tmp_path/'evidence.json'
    evidence.write_text(json.dumps(package))
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    budget_path=tmp_path/'authorized-aggregate.sqlite3'
    SharedAttemptBudget.initialize(budget_path,maximum=10,final_maximum=10,full_maximum=0)
    args=SimpleNamespace(global_budget=budget_path,source=configuration_source, reference_account=account['id'], historical_source=store.path,
        evidence=evidence, output=tmp_path/'prepared', manifest=None, manager_reasoning_effort=manager_reasoning_effort,
        max_output_tokens=max_output_tokens, required_fields_only=required_fields_only, network_route=network_route)
    verifier.prepare(args)
    return store, args


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['valid','corrected','unknown_transport'])
async def test_prepared_aggregate_uses_real_agent_preflight_and_bounded_publication(tmp_path,monkeypatch,mode):
    from app import agents
    from app.model import ModelClient
    from app.store import Store
    store, prepared = fixture_preparation(tmp_path,monkeypatch)
    source_before=verifier.source_identity(store.path)
    requests=[]
    def handler(request):
        payload=json.loads(request.content); requests.append(payload)
        assert payload['tool_choice']=={'type':'function','name':'finish_analysis'}
        assert [tool['name'] for tool in payload['tools']]==['finish_analysis']
        assert payload['max_output_tokens']==4000
        schema={k:v for k,v in payload['tools'][0].items() if k not in ('type','strict')}
        manifest=json.loads((prepared.output/'preparation.json').read_text())
        assert verifier.digest(schema)==manifest['production_final_schema_sha256']
        assert 42000 < manifest['input_chars'] < 65000
        seed=json.loads(next(m['content'] for m in payload['input'] if m.get('role')=='user'))
        assert '完整填写必填字段，可选结构仅在必要时填写，避免重复正文' in seed['runtime_status']['instruction']
        evidence=seed['historical_evidence']
        assert evidence['reports'][0]['findings'][0]['counter_factor']=='Not verified by source全文'
        assert len(evidence['calculations'])==2
        assert seed['data_as_of']=='2026-10-30T15:00:00+08:00'
        time_context=seed['runtime_status']['time_context']
        assert time_context['historical_replay'] is True
        assert time_context['data_as_of']==seed['data_as_of']
        assert time_context['execution_time']
        assert 'decision_as_of 和实际执行时间 execution_time' in seed['reason']
        if mode=='unknown_transport': raise httpx.RemoteProtocolError('controlled terminal missing')
        body={'report':'Controlled fixture report, not an investment finding.', 'completion':'limited',
            'scope':['SH:518880','SZ:002354','SZ:159599'], 'decision_summary':'Fixture decision only',
            'limitations':['Missing market trends'], 'suggestions':[]}
        if mode=='corrected' and len(requests)==1:
            body['suggestions']=[{'action':'watch','basis':'Controlled missing security'}]
        return httpx.Response(200,json={'id':'controlled-final','status':'completed','output':[
            {'type':'function_call','call_id':'final-'+str(len(requests)),'name':'finish_analysis','arguments':json.dumps(body)}]})
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,authorization_note='controlled fixture only; no real permission',authorized_http_attempts=2)
    code=await verifier.run_prepared(args)
    summary=json.loads((prepared.output/'execution/summary.json').read_text())
    assert verifier.source_identity(store.path)==source_before
    assert len(requests)==(2 if mode=='corrected' else 1)
    assert summary['attempt_budget']['reserved_attempts']==len(requests)
    assert all(item['full_historical_evidence_visible'] for item in summary['request_inputs'])
    assert all(item['production_schema_matches'] for item in summary['request_inputs'])
    assert all(item['time_context']['historical_replay'] is True for item in summary['request_inputs'])
    assert summary['content_assessment']=='not_evaluated'
    assert summary['published']==(mode!='unknown_transport')
    assert len(summary['visible_terminal_files'])==(0 if mode=='unknown_transport' else len(requests))
    assert code==(2 if mode=='unknown_transport' else 0)
    isolated=Store(prepared.output/'execution/evaluation.sqlite3','live')
    assert len(isolated.analyses(summary['account_id']))==(0 if mode=='unknown_transport' else 1)
    if mode=='corrected': assert summary['submission_outcome']=='recovered'
    # A second invocation cannot reset the cap or reuse the output directory.
    with pytest.raises(FileExistsError): await verifier.run_prepared(args)


@pytest.mark.asyncio
async def test_actual_runtime_request_contains_shared_calculation_explanations(tmp_path, monkeypatch):
    from app import agents
    from app.model import ModelClient
    from tests.delivery_helpers import delivery_arguments
    from tests.test_ninth_calculation_context import config, setup, output, seed
    cfg = config(monkeypatch, max_model_calls=4)
    store, tools, context = setup(tmp_path,cfg)
    requests=[]
    def handler(request):
        payload=json.loads(request.content); requests.append(payload)
        if len(requests)==1:
            return output('responses',[('calculate',{'operation':'sum','values':[i,2]}) for i in (1,3,5)])
        visible=seed(payload)
        summaries=visible['calculation_summaries']
        assert len(summaries)==3 and len(visible['calculation_contexts'])==1
        for item, expected in zip(summaries,('3','5','7')):
            shared=visible['calculation_contexts'][item['context_ref']]
            assert item['result']==expected and item['unit']=='input-dependent; not inferred'
            assert shared['security']=='not supplied by calculate'
            assert shared['date_range']=='not supplied by calculate'
            assert shared['source_material_version']=='not supplied by calculate'
            assert shared['model_config']['model_name']=='fixture'
            assert item['agent_id']=='manager' and item['source_task_id']==context['job_id']
        return output('responses',[('finish_analysis',delivery_arguments(report='Controlled fixture only.'))])
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    async def emit(event): pass
    result=await agents.run_research(context,cfg,tools,emit)
    assert len(requests)==2 and result['model_calls']==2


@pytest.mark.asyncio
@pytest.mark.parametrize('manager',[True,False])
@pytest.mark.parametrize('final_request',[True,False])
async def test_final_submission_guidance_only_changes_manager_final_request_and_allows_visible_text_with_tool(
        tmp_path,monkeypatch,manager,final_request):
    from app import agents
    from app.model import ModelClient
    from tests.delivery_helpers import delivery_arguments
    from tests.test_fourteenth_chain import response,call,prose
    from tests.test_ninth_calculation_context import config,setup,seed
    cfg=config(monkeypatch,max_model_calls=4,max_agent_turns=1 if final_request else 4)
    _,tools,context=setup(tmp_path,cfg)
    original_instruction='这是当前运行状态。已有子任务不要重复创建；旧报告不代表后续补查已完成，阶段进展不等于最终报告。'
    final_instruction=' 当前只提交已有证据支持的报告；明确列出未完成任务、资料缺口及其影响，未知数字或分类保持未知，不再扩展研究。'
    sent=[]
    def handler(request):
        payload=json.loads(request.content);sent.append(payload)
        instruction=seed(payload)['runtime_status']['instruction']
        if manager and final_request:
            assert instruction.startswith(original_instruction+final_instruction)
            assert '直接且仅调用一次 finish_analysis' in instruction
            assert '范围内每只证券的动作、依据、重要反向因素和改变看法的条件' in instruction
            assert '完整填写必填字段，可选结构仅在必要时填写，避免重复正文' in instruction
        else:
            assert instruction==original_instruction+(final_instruction if final_request else '')
        terminal='finish_analysis' if manager else 'submit_report'
        if final_request:
            assert payload['tool_choice']=={'type':'function','name':terminal}
            assert [tool['name'] for tool in payload['tools']]==[terminal]
        else:
            assert 'tool_choice' not in payload
        arguments=delivery_arguments(report='Controlled final report.') if manager else {'summary':'Controlled child report.'}
        # Output text alongside a valid explicit function remains legal. The
        # outgoing guidance is not a new rejection rule for stream/text events.
        return response(prose('Controlled visible text.'),call(terminal,arguments))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime=agents.ResearchRuntime(context,cfg,tools,emit)
    agent_id='manager' if manager else 'controlled-child'
    session=runtime.manager_session
    if not manager:
        session=runtime.client.session('Controlled child instructions.',{'task':'Controlled child task.'})
        session.agent_id=agent_id
        runtime.children[agent_id]=agents.AgentState(agent_id,'Controlled child','Controlled child task.',session)
    try:
        result=await runtime._loop(agent_id,session,manager=manager)
        assert len(sent)==1
        assert result['report' if manager else 'summary']==('Controlled final report.' if manager else 'Controlled child report.')
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed_field',['prompt','model','effort'])
async def test_prepared_role_change_fails_before_model_client_and_records_zero_attempts(tmp_path,monkeypatch,changed_field):
    from app import agents
    from app.store import Store
    _, prepared=fixture_preparation(tmp_path,monkeypatch)
    source=Store(prepared.source,'live')
    if changed_field=='prompt':
        source.update_agent_definition('manager',{'system_prompt':'Controlled changed custom prompt.'})
    elif changed_field=='effort':
        source.update_agent_definition('manager',{'default_binding':{'reasoning_effort':'max'}})
    else:
        provider=source.list_agent_roles()[0]['resolved']['provider_id']
        await source.discover_models(provider,force=True,transport=httpx.MockTransport(
            lambda request:httpx.Response(200,json={'data':[{'id':'controlled-other-model'}]})))
        source.update_agent_definition('manager',{'default_binding':{'provider_id':provider,'model_id':'controlled-other-model'}})
    def forbidden(*args,**kwargs): raise AssertionError('No model client may be created')
    monkeypatch.setattr(agents,'ModelClient',forbidden)
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,authorization_note='controlled fixture only',authorized_http_attempts=2)
    with pytest.raises(ValueError,match='Role definitions'):
        await verifier.run_prepared(args)
    rejection=json.loads(next((prepared.output/'preflight-rejections').glob('*.json')).read_text())
    assert rejection['paid_requests_sent']==0 and not rejection['model_client_created']
    assert not (prepared.output/'execution/attempt-budget.sqlite3').exists()


@pytest.mark.asyncio
async def test_actual_trimmed_input_is_rejected_before_transport_and_reservation_is_retained(tmp_path,monkeypatch):
    from app import agents
    from app.model import ModelClient
    _, prepared=fixture_preparation(tmp_path,monkeypatch)
    original=agents.ResearchRuntime._pin_runtime_status
    def trim(self,session,*args,**kwargs):
        original(self,session,*args,**kwargs)
        seed=json.loads(session.history[0]['content'])
        seed['historical_evidence']['reports']=[]
        session.history[0]['content']=json.dumps(seed)
    monkeypatch.setattr(agents.ResearchRuntime,'_pin_runtime_status',trim)
    sent=[]
    def forbidden(request):
        sent.append(request)
        raise AssertionError('Mutated evidence must never enter HTTP transport')
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(forbidden)))
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,authorization_note='controlled fixture only',authorized_http_attempts=2)
    assert await verifier.run_prepared(args)==2
    summary=json.loads((prepared.output/'execution/summary.json').read_text())
    assert not sent and summary['attempt_budget']['reserved_attempts']==1
    assert summary['request_inputs'][0]['full_historical_evidence_visible'] is False
    assert summary['request_inputs'][0]['transport_not_entered_reason']
    assert not summary['published']


@pytest.mark.asyncio
async def test_changed_execution_timeout_cannot_use_the_old_preparation(tmp_path,monkeypatch):
    from app import agents
    from app.store import Store
    _, prepared=fixture_preparation(tmp_path,monkeypatch)
    source=Store(prepared.source,'live')
    source.set_meta('settings',{**source.settings(),'analysis_timeout_seconds':999})
    def forbidden(*args,**kwargs): raise AssertionError('No model client may be created')
    monkeypatch.setattr(agents,'ModelClient',forbidden)
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,
        authorization_note='controlled fixture only',authorized_http_attempts=2)
    with pytest.raises(ValueError,match='Execution policy'):
        await verifier.run_prepared(args)
    rejection=json.loads(next((prepared.output/'preflight-rejections').glob('*.json')).read_text())
    assert rejection['paid_requests_sent']==0


def test_shared_aggregate_allowance_survives_reopen_and_stops_at_ten(tmp_path):
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    from app.model import ModelBudgetExceeded
    path=tmp_path/'shared.sqlite3'
    SharedAttemptBudget.initialize(path,maximum=10,final_maximum=10,full_maximum=0)
    for _ in range(10): SharedAttemptBudget(path).reserve('final')
    with pytest.raises(ModelBudgetExceeded): SharedAttemptBudget(path).reserve('final')
    with pytest.raises(ModelBudgetExceeded): SharedAttemptBudget(path).reserve('full')
    with pytest.raises(FileExistsError): SharedAttemptBudget.initialize(path,maximum=10,final_maximum=10,full_maximum=0)
    assert SharedAttemptBudget(path).status()['reserved_attempts']==10


@pytest.mark.asyncio
async def test_exact_old_builtin_migrations_with_different_times_keep_the_same_effective_team(tmp_path,monkeypatch):
    import sqlite3
    from app import agents,agent_config
    from app.agent_config import AgentConfigStore
    from app.model import ModelClient
    from tests.test_fourteenth_chain import response,call
    monkeypatch.setattr(agent_config,'now',lambda:'2026-11-02T17:00:00+08:00')
    _, prepared=fixture_preparation(tmp_path,monkeypatch,old_builtin=True)
    monkeypatch.setattr(agent_config,'now',lambda:'2026-11-02T17:01:00+08:00')
    requests=[]
    def handler(request):
        requests.append(request)
        return response(call('finish_analysis',{
            'report':'Controlled historical fixture only.', 'completion':'limited',
            'scope':['SH:518880','SZ:002354','SZ:159599'],'decision_summary':'Controlled fixture.',
            'limitations':['Original data gap retained']}))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,
        authorization_note='controlled fixture only',authorized_http_attempts=2)
    assert await verifier.run_prepared(args)==0
    assert len(requests)==1
    teams=[];timestamps=[]
    for path in (prepared.output/'prepared-config.sqlite3',prepared.output/'execution/evaluation.sqlite3'):
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            team=AgentConfigStore().freeze_agent_team(db=db)
            teams.append(team['team_id']);timestamps.append(team['roles']['manager']['updated_at'])
            assert team['roles']['manager']['system_prompt']==(verifier.ROOT/'app/prompts/manager.txt').read_text()
    assert teams[0]==teams[1]
    assert timestamps==['2026-11-02T17:00:00+08:00','2026-11-02T17:01:00+08:00']


@pytest.mark.asyncio
@pytest.mark.parametrize('output_limit',[None,6000])
@pytest.mark.parametrize('required_only',[False,True])
@pytest.mark.parametrize('network_route',['environment','direct'])
async def test_isolated_xhigh_binding_reaches_frozen_job_and_actual_request_without_other_changes(tmp_path,monkeypatch,output_limit,required_only,network_route):
    import sqlite3
    from app import agents
    from app.model import ModelClient
    from app.store import Store
    from tests.test_fourteenth_chain import response,call
    source, prepared=fixture_preparation(tmp_path,monkeypatch,manager_reasoning_effort='xhigh',source_effort='max',
        max_output_tokens=output_limit,required_fields_only=required_only,network_route=network_route)
    hashes={path:verifier.source_identity(path) for path in (source.path,prepared.source)}
    original_roles=Store(prepared.source,'live').list_agent_roles()
    isolated_roles=Store(prepared.output/'prepared-config.sqlite3','live').list_agent_roles()
    assert original_roles[0]['resolved']['reasoning_effort']=='max'
    for before, after in zip(original_roles,isolated_roles):
        expected={**before['resolved'],**({'reasoning_effort':'xhigh'} if before['role_id']=='manager' else {})}
        assert after['resolved']==expected
        assert after['system_prompt']==before['system_prompt'] and after['tools']==before['tools']
    manifest=json.loads((prepared.output/'preparation.json').read_text())
    private=json.loads((prepared.output/'private-source.json').read_text())
    assert manifest['requested_configuration']['reasoning_effort']=='xhigh'
    assert manifest['isolated_overrides']=={'manager_reasoning_effort':'xhigh',
        **({'max_output_tokens':output_limit} if output_limit is not None else {}),
        **({'required_fields_only':True} if required_only else {}),
        **({'network_route':'direct'} if network_route=='direct' else {})}
    assert manifest['request_policy']['network_route']==network_route
    assert manifest['request_policy']['maximum_http_attempts']==(1 if network_route=='direct' else 2)
    assert manifest['request_policy']['contract_correction_maximum']==(0 if network_route=='direct' else 1)
    assert manifest['baseline_execution_policy']['max_output_tokens']==4000
    assert manifest['execution_policy']['max_output_tokens']==(output_limit or 4000)
    assert Store(prepared.source,'live').settings()['max_output_tokens']==4000
    assert private['baseline_effective_team_id']!=private['effective_team_id']
    requests=[]
    def handler(request):
        payload=json.loads(request.content);requests.append(payload)
        assert payload['reasoning']['effort']=='xhigh' and payload['max_output_tokens']==(output_limit or 4000)
        actual=json.loads(next(m['content'] for m in payload['input'] if m.get('role')=='user'))
        original_reason=('历史隔离三证券账户证据汇总；沿包内价格时点、版本和成本/费用口径，复核比较的主语、方向与分母。不是今天行情或日常持仓。'
            '回答现金与敞口、每只证券倾向和账户动作、比较合理的替代方案、依据、重要反向因素及改变看法条件。'
            '正文明确 runtime_status.time_context 中的历史判断时点 decision_as_of 和实际执行时间 execution_time；'
            '下一交易日等相对时间须注明以历史判断时点为准，不将当时的未来说成本次执行时仍未发生。'
            '具体说明未完成专题限制哪部分；只汇总可见证据，不新增搜索。本任务没有预测批次，forecasts应省略或为空。')
        if required_only:
            assert actual['reason'].startswith(original_reason)
            assert '只填写 report、completion、scope、decision_summary、limitations 五个必填字段' in actual['reason']
            assert '完整三证券问题、依据、反向因素、改变看法条件' in actual['reason']
            assert '关键来源就近附输入已有 URL 或明确材料ID' in actual['reason']
            assert '可核对的算式与分母' in actual['reason']
            assert '其他可选附录本次省略，不逐条复制材料索引' in actual['reason']
        else:
            assert actual['reason']==original_reason
        schema={k:v for k,v in payload['tools'][0].items() if k not in ('type','strict')}
        assert verifier.digest(schema)==manifest['production_final_schema_sha256']
        assert 'research_records' in schema['parameters']['properties']
        assert actual['historical_evidence']['reports'][0]['findings'][0]['counter_factor']=='Not verified by source全文'
        return response(call('finish_analysis',{
            'report':'Controlled fixture only.', 'completion':'limited',
            'scope':['SH:518880','SZ:002354','SZ:159599'],'decision_summary':'Controlled fixture.',
            'limitations':['Original gap retained']}))
    original_clients=[]
    def client_factory(cfg,before_request=None):
        client=ModelClient(cfg,before_request,httpx.MockTransport(handler))
        original_clients.append(client.http)
        return client
    monkeypatch.setattr(agents,'ModelClient',client_factory)
    if network_route=='direct':
        # Real ModelClient and replacement-client path, controlled transport only.
        monkeypatch.setattr(httpx,'AsyncHTTPTransport',lambda:httpx.MockTransport(handler))
        from scripts.verify_fourteenth_budget import SharedAttemptBudget
        global_budget=SharedAttemptBudget(prepared.global_budget)
        for _ in range(9):global_budget.reserve('final')
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,manager_reasoning_effort='xhigh',
        authorization_note='controlled fixture only',authorized_http_attempts=1,max_output_tokens=output_limit,
        required_fields_only=required_only,network_route=network_route)
    assert await verifier.run_prepared(args)==0 and len(requests)==1
    summary=json.loads((prepared.output/'execution/summary.json').read_text())
    assert summary['network_route']==network_route
    assert summary['request_inputs'][0]['network_route']==network_route
    assert all(client.is_closed for client in original_clients)
    if network_route=='direct':
        assert summary['shared_attempt_budget']['reserved_attempts']==10
    assert summary['requests'][0]['reasoning_effort_sent']=='xhigh'
    assert summary['request_inputs'][0]['reasoning_effort_matches'] is True
    assert summary['request_inputs'][0]['output_limit_matches'] is True
    assert summary['request_inputs'][0]['max_output_tokens_sent']==(output_limit or 4000)
    assert summary['request_inputs'][0]['task_instruction_matches'] is True
    assert summary['request_inputs'][0]['task_instruction_sha256']==manifest['task_instruction_sha256']==private['task_instruction_sha256']
    with sqlite3.connect((prepared.output/'execution/evaluation.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
        snapshot=json.loads(db.execute('SELECT config_snapshot FROM jobs WHERE id=?',(summary['job_id'],)).fetchone()[0])
    assert snapshot['_agent_team']['roles']['manager']['resolved']['reasoning_effort']=='xhigh'
    assert snapshot['_agent_team']['team_id']==private['effective_team_id']
    assert verifier.execution_policy(snapshot)==manifest['execution_policy']
    assert {path:verifier.source_identity(path) for path in hashes}==hashes


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['omitted_override','different_override','source_effort','actual_payload'])
async def test_isolated_effort_guard_rejects_changed_intent_source_or_wire_before_http(tmp_path,monkeypatch,change):
    from app import agents
    from app.model import ModelClient
    from app.store import Store
    _,prepared=fixture_preparation(tmp_path,monkeypatch,manager_reasoning_effort='xhigh',source_effort='max')
    if change=='source_effort':
        Store(prepared.source,'live').update_agent_definition('manager',{'default_binding':{'reasoning_effort':'high'}})
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,
        manager_reasoning_effort=None if change=='omitted_override' else 'high' if change=='different_override' else 'xhigh',
        authorization_note='controlled fixture only',authorized_http_attempts=1)
    sent=[]
    def forbidden_request(request):
        sent.append(request)
        raise AssertionError('Changed effort must not enter transport')
    def factory(cfg,before_request=None):
        assert change=='actual_payload', 'No model client should be created for preflight rejection'
        client=ModelClient(cfg,before_request,httpx.MockTransport(forbidden_request))
        client.reasoning_effort='max'
        return client
    monkeypatch.setattr(agents,'ModelClient',factory)
    if change=='actual_payload':
        assert await verifier.run_prepared(args)==2
        summary=json.loads((prepared.output/'execution/summary.json').read_text())
        assert summary['attempt_budget']['reserved_attempts']==1
        assert summary['request_inputs'][0]['reasoning_effort_matches'] is False
        assert not summary['published'] and not summary['visible_terminal_files']
    else:
        with pytest.raises(ValueError,match='override differs|Role definitions'):
            await verifier.run_prepared(args)
        assert not (prepared.output/'execution/attempt-budget.sqlite3').exists()
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['omitted_flag','added_flag','frozen_instruction','actual_instruction'])
async def test_required_fields_only_flag_and_complete_instruction_are_frozen_before_transport(tmp_path,monkeypatch,change):
    from app import agents
    from app.model import ModelClient
    _,prepared=fixture_preparation(tmp_path,monkeypatch,manager_reasoning_effort='xhigh',source_effort='max',
        max_output_tokens=6000,required_fields_only=change!='added_flag')
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,manager_reasoning_effort='xhigh',
        max_output_tokens=6000,required_fields_only=change!='omitted_flag',
        authorization_note='controlled fixture only',authorized_http_attempts=1)
    if change=='frozen_instruction':
        path=prepared.output/'preparation.json'
        manifest=json.loads(path.read_text());manifest['task_instruction_sha256']='0'*64
        path.write_text(json.dumps(manifest))
    if change=='actual_instruction':
        original=agents.ResearchRuntime._pin_runtime_status
        def replace_reason(self,session,*args,**kwargs):
            original(self,session,*args,**kwargs)
            seed=json.loads(session.history[0]['content'])
            seed['reason']=verifier.task_instruction({})
            session.history[0]['content']=json.dumps(seed)
        monkeypatch.setattr(agents.ResearchRuntime,'_pin_runtime_status',replace_reason)
    sent=[]
    def forbidden_request(request):
        sent.append(request)
        raise AssertionError('Changed instruction must not enter transport')
    def factory(cfg,before_request=None):
        assert change=='actual_instruction', 'No model client should be created for preflight rejection'
        return ModelClient(cfg,before_request,httpx.MockTransport(forbidden_request))
    monkeypatch.setattr(agents,'ModelClient',factory)
    if change=='actual_instruction':
        assert await verifier.run_prepared(args)==2
        summary=json.loads((prepared.output/'execution/summary.json').read_text())
        assert summary['attempt_budget']['reserved_attempts']==1
        assert summary['request_inputs'][0]['task_instruction_matches'] is False
        assert summary['request_inputs'][0]['full_historical_evidence_visible'] is True
        assert not summary['published'] and not summary['visible_terminal_files']
    else:
        with pytest.raises(ValueError,match='override differs|task instruction differs'):
            await verifier.run_prepared(args)
        assert not (prepared.output/'execution/attempt-budget.sqlite3').exists()
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['omitted_override','different_override','source_output','actual_payload'])
async def test_isolated_output_guard_rejects_changed_intent_source_or_wire_before_http(tmp_path,monkeypatch,change):
    from app import agents
    from app.model import ModelClient
    from app.store import Store
    _,prepared=fixture_preparation(tmp_path,monkeypatch,manager_reasoning_effort='xhigh',source_effort='max',
        max_output_tokens=6000)
    if change=='source_output':
        source=Store(prepared.source,'live')
        source.set_meta('settings',{**source.settings(),'max_output_tokens':7000})
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,manager_reasoning_effort='xhigh',
        max_output_tokens=None if change=='omitted_override' else 5000 if change=='different_override' else 6000,
        authorization_note='controlled fixture only',authorized_http_attempts=1)
    sent=[]
    def forbidden_request(request):
        sent.append(request)
        raise AssertionError('Changed output limit must not enter transport')
    def factory(cfg,before_request=None):
        assert change=='actual_payload', 'No model client should be created for preflight rejection'
        client=ModelClient(cfg,before_request,httpx.MockTransport(forbidden_request))
        client.config['max_output_tokens']=4000
        return client
    monkeypatch.setattr(agents,'ModelClient',factory)
    if change=='actual_payload':
        assert await verifier.run_prepared(args)==2
        summary=json.loads((prepared.output/'execution/summary.json').read_text())
        assert summary['attempt_budget']['reserved_attempts']==1
        assert summary['request_inputs'][0]['output_limit_matches'] is False
        assert not summary['published'] and not summary['visible_terminal_files']
    else:
        with pytest.raises(ValueError,match='override differs|Execution policy changed'):
            await verifier.run_prepared(args)
        assert not (prepared.output/'execution/attempt-budget.sqlite3').exists()
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts',[1,2])
async def test_known_incomplete_retains_only_visible_terminal_without_accepting_or_publishing(tmp_path,monkeypatch,attempts):
    import sqlite3
    from app import agents
    from app.model import ModelClient
    _,prepared=fixture_preparation(tmp_path,monkeypatch,manager_reasoning_effort='xhigh',source_effort='max')
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        assert requests[-1]['reasoning']['effort']=='xhigh'
        return httpx.Response(200,json={'success':True,'data':{
            'id':f'controlled-incomplete-{len(requests)}','status':'incomplete',
            'incomplete_details':{'reason':'max_output_tokens'},
            'reasoning':{'summary':'HIDDEN_REASONING_MARKER'},'summary':'HIDDEN_REASONING_MARKER',
            'output':[
                {'type':'reasoning','summary':[{'text':'HIDDEN_REASONING_MARKER'}]},
                {'type':'message','summary':'HIDDEN_REASONING_MARKER','content':[
                    {'type':'output_text','text':'Visible unfinished report','annotations':[{'text':'HIDDEN_REASONING_MARKER'}]},
                    {'type':'text','text':'Visible compatible text'},
                    {'type':'reasoning_text','text':'HIDDEN_REASONING_MARKER'}]},
                {'type':'function_call','status':'completed','name':'finish_analysis','call_id':'visible-call',
                    'arguments':'{"report":"Visible complete argument string"}','summary':'HIDDEN_REASONING_MARKER'},
                {'type':'function_call','status':'in_progress','name':'finish_analysis','call_id':'partial-call',
                    'arguments':'HIDDEN_REASONING_MARKER'}]}})
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,manager_reasoning_effort='xhigh',
        authorization_note='controlled fixture only',authorized_http_attempts=attempts)
    assert await verifier.run_prepared(args)==2
    summary=json.loads((prepared.output/'execution/summary.json').read_text())
    assert len(requests)==attempts==summary['attempt_budget']['reserved_attempts']
    assert summary['error_code']=='delivery_contract_failed' and not summary['published']
    assert not (prepared.output/'execution/model-result.json').exists()
    assert len(summary['visible_terminal_files'])==attempts
    for index,record in enumerate(summary['visible_terminal_files'],1):
        path=prepared.output/'execution'/record['file']
        raw=path.read_text();visible=json.loads(raw)
        assert path.stat().st_mode & 0o777==0o600
        assert 'HIDDEN_REASONING_MARKER' not in raw
        assert visible['response_id']==f'controlled-incomplete-{index}'
        assert visible['status']=='incomplete' and visible['not_accepted'] is True
        assert visible['output']==[
            {'type':'message','content':[{'type':'output_text','text':'Visible unfinished report'},
                                        {'type':'text','text':'Visible compatible text'}]},
            {'type':'function_call','name':'finish_analysis','call_id':'visible-call',
             'arguments':'{"report":"Visible complete argument string"}'}]
    with sqlite3.connect((prepared.output/'execution/evaluation.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
        for table in ('analyses','recommendations','forecasts','forecast_batches'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_network_route_default_preserves_existing_override_identity():
    assert verifier.isolated_overrides(SimpleNamespace())=={}
    assert verifier.isolated_overrides(SimpleNamespace(network_route='environment'))=={}
    assert verifier.isolated_overrides(SimpleNamespace(network_route='direct'))=={'network_route':'direct'}
    with pytest.raises(ValueError,match='network_route'):
        verifier.isolated_overrides(SimpleNamespace(network_route='unknown'))


@pytest.mark.asyncio
@pytest.mark.parametrize('route',['environment','direct'])
@pytest.mark.parametrize('ca_source',['file','directory'])
async def test_isolated_direct_route_keeps_tls_and_environment_ca_without_proxy_mounts(tmp_path,monkeypatch,route,ca_source):
    import certifi
    import os
    import ssl
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY','http://127.0.0.1:1')
    monkeypatch.delenv('NO_PROXY',raising=False)
    if ca_source=='file':
        monkeypatch.setenv('SSL_CERT_FILE',certifi.where())
        expected={'cafile':certifi.where()}
    else:
        monkeypatch.delenv('SSL_CERT_FILE',raising=False)
        monkeypatch.setenv('SSL_CERT_DIR',str(tmp_path))
        expected={'capath':str(tmp_path)}
    environment=dict(os.environ)
    observed=[];original=ssl.create_default_context
    def ssl_context(*args,**kwargs):
        observed.append(kwargs)
        return original(*args,**kwargs)
    monkeypatch.setattr(ssl,'create_default_context',ssl_context)
    previous=httpx.AsyncClient(base_url='https://fixture.invalid/v1/',
        headers={'Authorization':'Bearer controlled-fixture-only'},
        timeout=httpx.Timeout(connect=15,pool=15,write=30,read=None),follow_redirects=False)
    client=SimpleNamespace(http=previous)
    previous_headers=previous.headers.copy();previous_timeout=previous.timeout.as_dict()
    assert any(mount is not None for mount in previous._mounts.values())
    observed.clear()
    try:
        await verifier.configure_network_route(client,route)
        assert dict(os.environ)==environment
        assert client.http.base_url==previous.base_url and client.http.headers==previous_headers
        assert client.http.timeout.as_dict()==previous_timeout and client.http.follow_redirects is False
        if route=='environment':
            assert client.http is previous and not previous.is_closed and observed==[]
        else:
            assert previous.is_closed and client.http is not previous
            assert client.http._mounts=={} and client.http.trust_env is True
            assert observed==[expected]
            context=client.http._transport._pool._ssl_context
            assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname is True
    finally:
        await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['omitted_direct','unexpected_direct','manifest_policy','private_override','authorized_two'])
async def test_network_route_intent_must_match_preparation_before_client_or_attempt(tmp_path,monkeypatch,change):
    from app import agents
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    prepared_route='environment' if change=='unexpected_direct' else 'direct'
    _,prepared=fixture_preparation(tmp_path,monkeypatch,network_route=prepared_route)
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,
        authorization_note='Controlled fixture only; no external authorization',authorized_http_attempts=1,
        network_route='environment' if change=='omitted_direct' else 'direct')
    if change=='authorized_two':args.authorized_http_attempts=2
    if change=='manifest_policy':
        path=prepared.output/'preparation.json';value=json.loads(path.read_text())
        value['request_policy']['network_route']='environment';path.write_text(json.dumps(value))
    elif change=='private_override':
        path=prepared.output/'private-source.json';value=json.loads(path.read_text())
        value['isolated_overrides'].pop('network_route');path.write_text(json.dumps(value))
    monkeypatch.setattr(agents,'ModelClient',lambda *a,**k:pytest.fail('Route mismatch must stop before client construction'))
    with pytest.raises(ValueError,match='override differs|Network route policy differs|exactly one authorized HTTP'):
        await verifier.run_prepared(args)
    assert SharedAttemptBudget(prepared.global_budget).status()['reserved_attempts']==0
    assert not (prepared.output/'execution/attempt-budget.sqlite3').exists()
    rejection=json.loads(next((prepared.output/'preflight-rejections').glob('*.json')).read_text())
    assert rejection['paid_requests_sent']==0 and rejection['model_client_created'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['invalid_submission','accepted_body_disconnect'])
async def test_direct_last_global_attempt_does_not_retry_or_correct_failed_delivery(tmp_path,monkeypatch,failure):
    from app import agents
    from app.model import ModelClient
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    from tests.test_fourteenth_chain import response,call
    _,prepared=fixture_preparation(tmp_path,monkeypatch,network_route='direct',manager_reasoning_effort='xhigh',
        source_effort='max',max_output_tokens=6000,required_fields_only=True)
    budget=SharedAttemptBudget(prepared.global_budget)
    for _ in range(9):budget.reserve('final')
    sent=[]
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":"controlled-unfinished",'
            raise httpx.RemoteProtocolError('Controlled body disconnect after HTTP200')
    def handler(request):
        sent.append(request)
        if failure=='accepted_body_disconnect':return httpx.Response(200,stream=BrokenBody())
        return response(call('finish_analysis',{'report':'Controlled invalid completion fixture.',
            'completion':'invalid_fixture_status','scope':['SH:518880','SZ:002354','SZ:159599'],
            'decision_summary':'Fixture only.','limitations':['Controlled invalid contract']}))
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    monkeypatch.setattr(httpx,'AsyncHTTPTransport',lambda:httpx.MockTransport(handler))
    args=SimpleNamespace(global_budget=prepared.global_budget,prepared=prepared.output,network_route='direct',
        manager_reasoning_effort='xhigh',max_output_tokens=6000,required_fields_only=True,
        authorization_note='Controlled fixture only; no external authorization',authorized_http_attempts=1)
    assert await verifier.run_prepared(args)==2
    summary=json.loads((prepared.output/'execution/summary.json').read_text())
    assert len(sent)==summary['attempt_budget']['reserved_attempts']==1
    assert summary['shared_attempt_budget']['reserved_attempts']==10 and summary['network_route']=='direct'
    assert not summary['published'] and not summary.get('contract_accepted')
    if failure=='accepted_body_disconnect':
        assert summary['requests'][0]['request_accepted'] is True
        assert summary['requests'][0]['retry_suppressed']=='accepted_or_sent_request_outcome_unknown'
