"""Ordinary research compaction, actual protocol payloads, no external models."""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import archive, compact, serialized
from app.model import ModelClient, ModelError
from app.source_projection import projected_result_matches
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, setup, output, seed
from tests.test_seventeenth_child_final_evidence import add_tool, make_runtime, FINANCIAL, payload_history
from tests.test_seventeenth_model_cache import response
from tests.test_seventeenth_source_projection import financial_result


def own_source():
    return {'material_id': 'own-source', 'access': 'full_text', 'text': FINANCIAL['excerpt'],
        'returned_range': {'start_page': 2, 'end_page': 2}, 'source_truncated': True,
        'next_start': 5000, 'amount_unit': 'CNY'}


def flood(client, session):
    for i in range(9):
        client.add_user(session, {'discovery_index': i, 'text': 'index-only ' * 900})


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('manager', [False, True])
async def test_natural_submission_after_research_compaction_retains_owned_facts_and_prefix(monkeypatch, protocol, manager):
    payloads = []
    original = own_source()
    async def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        if len(payloads) == 1:
            assert all(value in json.dumps(payload) for value in ('-2.04%', '19.11%', '17.57%'))
            return httpx.Response(200, json=response(protocol))
        current = seed(payload)
        item, = [item for item in current['delivered_research_evidence'] if item['tool'] == 'get_material']
        assert item['result'] == original
        assert item['source_agent_id'] == agent_id and item['source_task_id'] == runtime.task_id
        saved, = [item for item in current['delivered_research_evidence'] if item['tool'] == 'save_material']
        assert saved['authored_material']['text'] == 'OWN-AUTHORED extraction, not independent original'
        assert saved['authored_material_classification'] == 'agent_extraction'
        wire = json.dumps(payload)
        assert not any(value in wire for value in ('UNREAD-RAW-TAIL', 'OTHER-SESSION-ORIGINAL', 'HIDDEN-CHAT-REASONING'))
        assert payload.get('tool_choice') is None and session.phase == 'research'
        names = [tool.get('name', (tool.get('function') or {}).get('name')) for tool in payload['tools']]
        assert 'get_material' in names and terminal in names
        if len(payloads) == 2:
            return output(protocol, [('recall_context', {'query': 'absent-local-index'})])
        assert payload_history(payload)[:len(payload_history(payloads[1]))] == payload_history(payloads[1])
        return output(protocol, [(terminal, delivery_arguments(report='Controlled voluntary report.') if manager
                                 else {'summary': 'Controlled voluntary report.'})])
    runtime, child, events = await make_runtime(monkeypatch, handler, protocol)
    agent_id, session = ('manager', runtime.manager_session) if manager else (child.id, child.session)
    terminal = 'finish_analysis' if manager else 'submit_report'
    client = runtime._client_for(agent_id)
    try:
        add_tool(client, session, 'get_material', original, 'source')
        add_tool(client, session, 'save_material', {'id': 'own-extraction', 'summary': 'saved index'}, 'save',
            {'material': {'text': 'OWN-AUTHORED extraction, not independent original'}})
        archive(session, {'text': 'UNREAD-RAW-TAIL'}, 'tool_result')
        other = client.session('other', {})
        archive(other, {'text': 'OTHER-SESSION-ORIGINAL'}, 'conversation')
        runtime._pin_runtime_status(session, agent_id, None, False)
        token = runtime.request_owner.set((agent_id, False))
        try:
            await client.complete(session, agents.RESEARCH_TOOLS)
        finally:
            runtime.request_owner.reset(token)
        flood(client, session)
        result = await runtime._loop(agent_id, session, manager=manager)
        assert result.get('report', result.get('summary')) == 'Controlled voluntary report.'
        assert len(payloads) == 3 and session.compactions == 1
        assert not any(event['type'] in ('child_final_context_projected', 'synthesis_context_projected') for event in events)
        assert client.requests[-1]['prefix_diagnostic']['history_is_prefix'] is True
        assert client.requests[-1]['cache_epoch'] == client.requests[-2]['cache_epoch']
        assert len(serialized(payload_history(payloads[1]))) < runtime.config['max_context_chars'] * .70
        if not manager:
            assert session.native_search is True
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('accepted', [False, True])
async def test_ordinary_compaction_pending_receipt_requires_exact_accepted_output(tmp_path, monkeypatch, protocol, accepted):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=80000, max_tool_result_chars=30000,
        max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    original = financial_result()
    material = store.save_material({'scope': 'account', 'access': 'structured_data',
        'source_identifier': 'controlled:ordinary-source', 'content': json.dumps(original)},
        context['job_id'], context['account']['id'])
    original['material_id'] = material['id']
    proof = store.prepare_pending_tool_output(context['job_id'], context['account']['id'], 'manager', 'fundamentals', original)
    payloads = []
    async def handler(request):
        payload=json.loads(request.content);payloads.append(payload)
        item, = [item for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'fundamentals']
        assert item['visibility'] == 'completed_tool_output_not_previously_delivered'
        assert projected_result_matches(item, original)
        assert item['result']['comparisons'] == original['comparisons']
        assert store.checkpoint_deliveries(context['job_id']) == []
        return output(protocol, [('finish_analysis', delivery_arguments(report='Controlled exact receipt.'))]) if accepted else httpx.Response(503, text='fixture unavailable')
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    session = runtime.manager_session
    try:
        call = add_tool(runtime.client, session, 'fundamentals', original, 'pending-source')
        session._pending_material_deliveries = [{'call': call, 'message': copy.deepcopy(session.history[-1]),
            'result': copy.deepcopy(original), 'proof': proof}]
        runtime._pin_runtime_status(session, 'manager', None, False)
        flood(runtime.client, session)
        token = runtime.request_owner.set(('manager', False))
        try:
            if accepted:
                await runtime.client.complete(session, agents.RESEARCH_TOOLS + agents.MANAGER_TOOLS)
            else:
                with pytest.raises(ModelError):
                    await runtime.client.complete(session, agents.RESEARCH_TOOLS + agents.MANAGER_TOOLS)
        finally:
            runtime.request_owner.reset(token)
        receipts = store.checkpoint_deliveries(context['job_id'])
        assert len(payloads) == 1 and session.compactions == 1
        assert len(receipts) == int(accepted)
        if accepted:
            assert receipts[0]['body']['visible_result'] == original
            assert session._pending_material_deliveries == []
        else:
            assert len(session._pending_material_deliveries) == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_ordinary_small_budget_reports_omission_without_expanding_unread_tail(monkeypatch, protocol):
    runtime, child, _ = await make_runtime(monkeypatch, lambda request: httpx.Response(200, json=response(protocol)), protocol)
    runtime.config['max_context_chars'] = 10000
    session = child.session
    try:
        add_tool(runtime.client, session, 'get_material', {'material_id': 'oversized', 'text': 'known original ' * 500}, 'large')
        archive(session, {'text': 'UNREAD-RAW-TAIL'}, 'tool_result')
        runtime._pin_runtime_status(session, child.id, None, False)
        flood(runtime.client, session)
        assert compact(session, 10000)
        current = json.loads(session.history[0]['content'])
        assert current['delivered_research_evidence'] == []
        assert current['omitted_output_count'] == 1
        assert current['omitted_evidence_indexes'][0]['tool'] == 'get_material'
        assert 'UNREAD-RAW-TAIL' not in serialized(session.history)
        assert len(serialized(session.history)) < 7000
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_two_research_epochs_then_submission_correction_keep_same_source(monkeypatch, protocol):
    payloads=[]
    original=own_source()
    async def handler(request):
        payload=json.loads(request.content);payloads.append(payload)
        if len(payloads)==1:
            return httpx.Response(200,json=response(protocol))
        evidence=seed(payload)['delivered_research_evidence']
        item,=[item for item in evidence if item['tool']=='get_material']
        assert item['result']==original
        if len(payloads)==2:
            assert payload.get('tool_choice') is None
            return output(protocol,[('submit_report',{'summary':'invalid duplicate candidate'}),
                                    ('submit_report',{'summary':'second invalid candidate'})])
        assert payload.get('tool_choice') is not None
        return output(protocol,[('submit_report',{'summary':'Corrected local contract, unchanged source.'})])
    runtime,child,events=await make_runtime(monkeypatch,handler,protocol)
    session=child.session
    try:
        add_tool(runtime.client,session,'get_material',original,'source')
        runtime._pin_runtime_status(session,child.id,None,False)
        token=runtime.request_owner.set((child.id,False))
        try:
            await runtime.client.complete(session,agents.RESEARCH_TOOLS)
        finally:
            runtime.request_owner.reset(token)
        for _ in range(2):
            flood(runtime.client,session)
            assert compact(session,runtime.config['max_context_chars'])
            current=json.loads(session.history[0]['content'])
            item,=[item for item in current['delivered_research_evidence'] if item['tool']=='get_material']
            assert item['result']==original
            assert len(serialized(session.history)) < runtime.config['max_context_chars']*.70
        result=await runtime._loop(child.id,session,manager=False)
        assert result['submission_corrections']==1
        assert len(payloads)==3
        projected=[event for event in events if event['type']=='child_final_context_projected']
        assert len(projected)==1
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_manager_native_sources_and_new_calculation_survive_repeated_epochs_without_duplicates(monkeypatch, protocol):
    runtime, child, _=await make_runtime(monkeypatch,lambda request:httpx.Response(200,json=response(protocol)),protocol)
    session=runtime.manager_session
    originals=[{'native_source_materials':[{'id':f'native-{n}', 'summary':f'known excerpt {n}',
        'access':'search_excerpt', 'source_truncated':True}]} for n in range(4)]
    try:
        for item in originals:runtime.client.add_user(session,item)
        runtime._pin_runtime_status(session,'manager',None,False)
        for epoch in range(3):
            runtime._remember_calculation('manager','calculate',{'operation':'sum','values':[epoch,1]},
                {'operation':'sum','inputs':[epoch,1],'result':str(epoch+1)})
            flood(runtime.client,session)
            assert compact(session,runtime.config['max_context_chars'])
            current=json.loads(session.history[0]['content'])
            assert sorted(current['retained_visible_source_contexts'],key=lambda x:x['native_source_materials'][0]['id'])==originals
            assert not any(item['tool']=='native_source_materials' for item in current['delivered_research_evidence'])
            assert len(current['calculation_summaries'])==epoch+1
            assert current['calculation_summaries'][-1]['inputs']==[epoch,1]
            assert len(serialized(session.history))<runtime.config['max_context_chars']*.70
            before=session.compactions
            token=runtime.request_owner.set(('manager',False))
            try:
                await runtime.client.complete(session,agents.RESEARCH_TOOLS)
            finally:
                runtime.request_owner.reset(token)
            assert session.compactions==before
        # A source-free fresh session has no fabricated copied package.
        fresh=runtime.client.session('new independent session',{})
        archive(fresh,{'text':'UNREAD-RAW-TAIL'},'tool_result')
        fresh_seed={}
        runtime._retain_research_sources(fresh_seed,fresh,'manager')
        assert fresh_seed=={}
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('manager', [False, True])
async def test_research_to_forced_final_and_large_correction_share_one_source_and_calculation_budget(monkeypatch, protocol, manager):
    payloads=[]
    original=own_source()
    native=[{'native_source_materials':[{'id':f'native-{n}','summary':f'owned excerpt {n}',
        'access':'search_excerpt'}]} for n in range(4)]
    async def handler(request):
        payload=json.loads(request.content);payloads.append(payload)
        current=seed(payload)
        assert 'research_evidence_retention' not in current
        item,=[item for item in current['delivered_research_evidence'] if item['tool']=='get_material']
        assert item['result']==original
        assert len(current['calculation_summaries'])==3
        assert [item['inputs'] for item in current['calculation_summaries']]==[[i,1] for i in range(3)]
        users=[json.loads(item['content']) for item in payload_history(payload) if item.get('role')=='user']
        direct=[value for value in users if 'native_source_materials' in value]
        retained=[value for user in users for value in user.get('retained_visible_source_contexts',[])]
        assert sorted(direct+retained,key=lambda value:value['native_source_materials'][0]['id'])==native
        assert len(direct)+len(retained)==4
        assert len(serialized(payload_history(payload)))<runtime.config['max_context_chars']*.78
        args=delivery_arguments(report='candidate '*3500) if manager else {'summary':'candidate '*3500}
        if len(payloads)==1:
            return output(protocol,[(terminal,args),(terminal,args)])
        assert session.compactions>ordinary_compactions
        return output(protocol,[(terminal,delivery_arguments(report='Corrected controlled report.') if manager
                                 else {'summary':'Corrected controlled report.'})])
    runtime,child,events=await make_runtime(monkeypatch,handler,protocol)
    agent_id,session=('manager',runtime.manager_session) if manager else (child.id,child.session)
    terminal='finish_analysis' if manager else 'submit_report'
    try:
        add_tool(runtime.client,session,'get_material',original,'source')
        for item in native:runtime.client.add_user(session,item)
        for i in range(3):runtime._remember_calculation(agent_id,'calculate',{'operation':'sum','values':[i,1]},
            {'operation':'sum','inputs':[i,1],'result':str(i+1)})
        runtime._pin_runtime_status(session,agent_id,None,False)
        flood(runtime.client,session)
        assert compact(session,runtime.config['max_context_chars'])
        ordinary_compactions=session.compactions
        if manager:runtime.budget_closing=True
        else:child.deadline_closing=True
        result=await runtime._loop(agent_id,session,manager=manager)
        assert result['submission_corrections']==1 and len(payloads)==2
        assert sum(event['type']==('synthesis_context_projected' if manager else 'child_final_context_projected') for event in events)==1
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_unused_joint_budget_keeps_whole_older_source_without_displacing_calculations(monkeypatch, protocol):
    runtime,child,_=await make_runtime(monkeypatch,lambda request:httpx.Response(200,json=response(protocol)),protocol)
    session=child.session
    results=[{'id':f'visible-{i}','text':'quote \\" exact \n'*250,'access':'full_text',
        'returned_range':{'start_page':i+1,'end_page':i+1}} for i in range(6)]
    try:
        for i,result in enumerate(results):add_tool(runtime.client,session,'get_material',result,f'read-{i}')
        for i in range(4):runtime._remember_calculation(child.id,'calculate',{'operation':'sum','values':[i,1]},
            {'operation':'sum','inputs':[i,1],'result':str(i+1)})
        runtime._pin_runtime_status(session,child.id,None,False)
        flood(runtime.client,session)
        assert compact(session,runtime.config['max_context_chars'])
        current=json.loads(session.history[0]['content'])
        visible=[item['result'] for item in current['delivered_research_evidence']]
        assert sorted(visible,key=lambda result:result['id'])==results
        assert current['omitted_output_count']==0
        assert len(current['calculation_summaries'])==4
        assert len(serialized(session.history[0]))<=runtime.config['max_context_chars']*.67
        assert len(serialized(session.history))<runtime.config['max_context_chars']*.70
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_only_visible_recovered_wrapper_is_retained_with_original_owner(monkeypatch, protocol):
    runtime,child,_=await make_runtime(monkeypatch,lambda request:httpx.Response(200,json=response(protocol)),protocol)
    session=runtime.manager_session
    recovered={'tool':'get_material','result':own_source(),'source_task_id':'previous-task',
        'source_agent_id':'previous-manager','previously_delivered':False,
        'visibility':'completed_tool_output_not_previously_delivered',
        'delivered_range':{'start_page':2,'end_page':2}}
    try:
        runtime.client.add_user(session,{'recovered_completed_tool_outputs':[recovered]})
        archive(session,{'tool':'get_material','result':{'text':'UNDELIVERED-RESTORED-SOURCE'}},'restored_material')
        runtime._pin_runtime_status(session,'manager',None,False)
        flood(runtime.client,session)
        assert compact(session,runtime.config['max_context_chars'])
        current=json.loads(session.history[0]['content'])
        item,=current['delivered_research_evidence']
        assert {key:item[key] for key in recovered}==recovered
        assert item['recipient_agent_id']=='manager'
        assert 'UNDELIVERED-RESTORED-SOURCE' not in serialized(session.history)
    finally:
        await runtime.close()
