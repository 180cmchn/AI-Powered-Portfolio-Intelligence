"""Visible closeout evidence, source boundaries and archive continuity.

Controlled protocol substitutes validate runtime mechanics, not live quality.
"""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import archive, recall
from app.model import ModelClient, ToolCall


def config(monkeypatch, **overrides):
    monkeypatch.setenv('CLOSEOUT_TEST_KEY', 'controlled-test-only')
    return {'model_base_url': 'https://model.example/v1', 'model_name': 'current-model',
            'model_key_env': 'CLOSEOUT_TEST_KEY', 'model_protocol': 'responses',
            'model_native_search': False, 'model_retries': 0, 'max_model_calls': 16,
            'max_agent_turns': 6, 'analysis_timeout_seconds': 5,
            'max_context_chars': 100000, 'max_tool_result_chars': 6000, **overrides}


def response(protocol, name, args, identity):
    call = {'type': 'function_call', 'call_id': identity, 'name': name,
            'arguments': json.dumps(args)}
    if protocol == 'responses':
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'reasoning', 'encrypted_content': 'OPAQUE_PRIVATE_REASONING'}, call]})
    return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'reasoning_content': 'OPAQUE_PRIVATE_REASONING',
        'tool_calls': [{'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_manager_only_forced_final_keeps_delivered_results_and_updated_position(monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol)
    payloads, events = [], []
    previous = {'report': 'Prior conclusion, not current position.', 'result': {
        'report': 'Prior conclusion, not current position.',
        'team_snapshot': {'team_id': 'old-team', 'roles': {'manager': {
            'resolved': {'model_name': 'old-model'}, 'system_prompt': 'DUPLICATE_PROMPT' * 2000}}},
        'materials': [{'id': f'old-{i}', 'content': 'UNREAD_OLD_BODY' * 500,
                       'url': f'https://source.example/{i}', 'source_title': 'Old title ' * 40}
                      for i in range(50)]}}

    class Tools:
        async def get_material(self, identity):
            number = identity.split('-')[-1]
            if number == '5':
                return {'id': identity, 'url': 'https://source.example/current',
                        'access': 'partial', 'text': 'VISIBLE_IMPORTANT_5 ' + 'long content ' * 800 + 'UNEXPOSED_TAIL'}
            return {'id': identity, 'text': f'VISIBLE_IMPORTANT_{number}', 'access': 'partial',
                    'open_questions': [f'UNKNOWN_{number}']}

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        turn = len(payloads)
        if turn < 6:
            return response(protocol, 'get_material', {'material_id': f'm-{turn}'}, f'call-{turn}')
        history = payload.get('input', payload.get('messages'))
        encoded = json.dumps(history, ensure_ascii=False)
        seed = next(json.loads(item['content']) for item in history if item.get('role') == 'user')
        assert seed['account']['id'] == 'A' and seed['input_version'] == 2
        assert seed['account']['holdings'][0]['quantity'] == 20000
        assert seed['forecast_targets'][0]['security_id'] == 'SZ:159278'
        assert seed['runtime_status']['children'] == []
        assert seed['previous_analysis']['result']['team_snapshot']['roles']['manager']['resolved']['model_name'] == 'old-model'
        for index in range(1, 6):
            assert f'VISIBLE_IMPORTANT_{index}' in encoded
        for index in range(1, 5):
            assert f'UNKNOWN_{index}' in encoded
        assert 'UNEXPOSED_TAIL' not in encoded and 'UNREAD_OLD_BODY' not in encoded
        assert 'OPAQUE_PRIVATE_REASONING' not in encoded and 'DUPLICATE_PROMPT' not in encoded
        assert len(encoded) < cfg['max_context_chars'] * .78
        return response(protocol, 'finish_analysis', {'report': '原文仍有缺口，未作方向判断。'}, 'final')

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    async def emit(event):
        events.append(event)

    result = await agents.run_research({'job_id': 'A-updated', 'account': {'id': 'A', 'version': 2,
        'holdings': [{'security_id': 'SZ:159278', 'quantity': 20000}]}, 'input_version': 2,
        'forecast_targets': [{'security_id': 'SZ:159278', 'horizon': 'T+1'}], 'previous_analysis': previous}, cfg, Tools(), emit)
    assert result['model_calls'] == 6 <= cfg['max_model_calls']
    assert not result['agents']
    projection = [event['payload'] for event in events if event['type'] == 'synthesis_context_projected']
    assert len(projection) == 1 and projection[0]['visible_tool_outputs'] == 5


@pytest.mark.asyncio
async def test_prepare_preserves_archive_ids_and_only_delivered_wrappers(monkeypatch):
    cfg = config(monkeypatch, max_tool_result_chars=1000)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime({'account': {'id': 'A', 'version': 2}}, cfg, object(), emit)
    session = runtime.manager_session
    try:
        raw = archive(session, {'text': 'VISIBLE_PREFIX ' + 'x' * 3000 + 'NOT_YET_DELIVERED'}, 'tool_result')
        runtime.client.add_user(session, {'native_source_materials': [{'id': 'native',
            'text': 'VISIBLE_NATIVE_START ' + 'x' * 3000 + 'NOT_YET_DELIVERED_NATIVE'}]})
        call = ToolCall('prepare', 'prepare_synthesis', {'summary': 'Manager verified the visible excerpt; valuation unknown.',
                                                       'material_ids': ['native'], 'open_questions': ['Valuation unknown.']})
        session.history.append({'type': 'function_call', 'call_id': call.id, 'name': call.name,
                                'arguments': json.dumps(call.arguments)})
        session.pending_calls = [call]
        result = await runtime._dispatch('manager', call.name, call.arguments)
        runtime.client.add_results(session, [(call, result)])
        ids_before = [record['record_id'] for record in session.archive]
        await runtime._enter_synthesis_context(session, reason='prepare_synthesis')
        assert [record['record_id'] for record in session.archive][:len(ids_before)] == ids_before
        assert 'NOT_YET_DELIVERED' in recall(session, record_id=raw['record_id'], start=3000)['text']
        encoded = json.dumps(session.history, ensure_ascii=False)
        assert 'VISIBLE_NATIVE_START' in encoded
        assert 'NOT_YET_DELIVERED' not in encoded
        assert 'Valuation unknown.' in encoded
        assert not session.pending_calls
        assert session.phase == 'synthesis' and session.native_search is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_large_checkpoint_projection_retains_original_reports_unknowns_and_provenance(monkeypatch):
    cfg = config(monkeypatch)
    checkpoint = {'phase': 'synthesis', 'account_id': 'A', 'input_version': 2,
        'model_config': {'model_name': 'prior-manager'},
        'reports': [{'agent_id': 'old-child', 'summary': 'Completed old model report.',
                     'material_ids': ['cited'], 'model_config': {'model_name': 'old-researcher'},
                     'open_questions': ['Valuation unknown.']}],
        'manager_summary': {'summary': 'Correction evidence compared.', 'material_ids': ['cited']},
        'unresolved_tasks': [{'agent_id': 'old-child', 'status': 'failed', 'round': 2,
                              'error': 'followup unavailable', 'pending_messages': ['Check correction.']}],
        'unknown_material_ids': ['unknown'],
        'material_ids': ['cited', *[f'index-{i}' for i in range(200)]],
        'materials': [{'id': 'cited', 'url': 'https://source.example/cited', 'access': 'partial'},
                      *[{'id': f'index-{i}', 'source_title': 'Repeated long title ' * 200,
                         'url': f'https://source.example/{i}'} for i in range(200)]],
        'material_deliveries': ['AUDIT_RECEIPT' * 10000],
        'verified_material_versions': ['DUPLICATE_VERSION_GUARD' * 10000],
        'source_coverage': {'source': {'status': 'unresolved', 'reason': 'New correction unread.',
                                     'latest_material_ids': ['unknown'], 'audit_proof': 'AUDIT_PROOF' * 10000}}}
    original = copy.deepcopy(checkpoint)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime({'account': {'id': 'A', 'version': 2}, 'synthesis_checkpoint': checkpoint}, cfg, object(), emit)
    try:
        # A resumed final request must not overwrite existing reports with an
        # empty live-child set while rebuilding its visible context.
        await runtime._enter_synthesis_context(runtime.manager_session, reason='bounded_final_request')
        seed = json.loads(runtime.manager_session.history[0]['content'])
        visible = seed['synthesis_checkpoint']
        assert visible['reports'][0]['model_config']['model_name'] == 'old-researcher'
        assert visible['reports'][0]['summary'] == 'Completed old model report.'
        assert visible['unresolved_tasks'][0]['status'] == 'failed'
        assert visible['unresolved_tasks'][0]['pending_messages'] == ['Check correction.']
        assert visible['unresolved_source_coverage'][0]['reason'] == 'New correction unread.'
        assert visible['unknown_material_ids'] == ['unknown']
        assert len(visible['material_ids']) == 201
        assert visible['materials'] == [{'id': 'cited', 'url': 'https://source.example/cited', 'access': 'partial'}]
        encoded = json.dumps(runtime.manager_session.history, ensure_ascii=False)
        assert len(encoded) < 15000
        assert 'AUDIT_' not in encoded and 'DUPLICATE_VERSION_GUARD' not in encoded
        assert 'truncated_for_context' not in seed
        assert checkpoint == original and runtime.synthesis_checkpoint == original
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_many_visible_native_sources_cannot_trigger_generic_recompaction(monkeypatch):
    cfg = config(monkeypatch)
    payloads = []
    async def emit(event):
        pass
    async def handler(request):
        payloads.append(json.loads(request.content))
        return response('responses', 'finish_analysis', {'report': 'Only delivered source excerpts support this summary.'}, 'final')
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    runtime = agents.ResearchRuntime({'account': {'id': 'A', 'version': 2}}, cfg, object(), emit)
    try:
        session = runtime.manager_session
        for index in range(60):
            runtime.client.add_user(session, {'native_source_materials': [{'id': f'source-{index}',
                'url': f'https://source.example/{index}', 'access': 'search_excerpt',
                'text': f'Visible source {index} ' + 'source excerpt ' * 250}]})
        await runtime._enter_synthesis_context(session, reason='bounded_final_request')
        assert len(json.dumps(session.history, ensure_ascii=False)) < cfg['max_context_chars'] * .75
        compact_before = session.compactions
        await runtime.client.complete(session, [next(t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis')])
        assert session.compactions == compact_before
        assert 'delivered_research_evidence' in json.dumps(payloads[0])
        assert 'truncated_for_synthesis' in json.dumps(payloads[0])
        assert len(session.archive) >= 60
    finally:
        await runtime.close()


def test_history_schema_matches_provider_supported_intervals():
    properties = next(tool for tool in agents.RESEARCH_TOOLS if tool['name'] == 'history')['parameters']['properties']
    assert properties['interval']['enum'] == ['daily', 'weekly', 'monthly', '1m', '5m', '15m', '30m', '60m']
    assert properties['adjust']['enum'] == ['none', 'qfq', 'hfq']
    assert '默认 daily' in properties['interval']['description']
    assert '默认 none' in properties['adjust']['description']
    assert 'SZ:159278' in properties['symbol']['description']


@pytest.mark.parametrize('bad_ids', [None, 'must-not-be-split-into-characters'])
def test_legacy_nullable_or_malformed_indexes_remain_unknown(bad_ids):
    material = {'id': 'm', 'url': 'https://source.example/m'}
    projected = agents._checkpoint_context({'phase': 'synthesis', 'reports': [{'material_ids': bad_ids}],
        'material_ids': bad_ids, 'materials': [material]})
    assert projected['material_ids'] == ['m']
    assert projected['materials'] == []
    for legacy_result in (None, [], 'legacy serialized value'):
        value = {'report': 'Stored top-level conclusion.', 'result': legacy_result}
        original = copy.deepcopy(value)
        previous = agents._previous_analysis_context(value)
        assert previous['report'] == 'Stored top-level conclusion.'
        assert previous['result'] == {}
        assert value == original
