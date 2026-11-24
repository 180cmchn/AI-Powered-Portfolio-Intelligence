"""Final evidence starvation counterexample; synthetic data, real Runtime/HTTP path.

The independent live task had 18 reads of three short originals, followed by
larger refreshed lookups. Its immutable original bodies are not copied here.
"""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import archive
from app.model import ModelClient
from app.source_projection import diverse_source_order, projected_result_matches
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, seed, setup
from tests.test_seventeenth_child_final_evidence import add_tool


def sized_result(identity, size, *, text_chars=0, **values):
    value = {'id': identity, **values}
    if text_chars:
        text = (f'Controlled original {identity}. ' * text_chars)[:text_chars]
        value.update(text=text, access='full_text', start=0, end=len(text), total_chars=len(text),
                     source_truncated=False, source_title=f'Original {identity}',
                     url=f'https://original.invalid/{identity}', document_version_id=f'version-{identity}')
    value['visible_detail'] = 'd' * max(0, size - len(json.dumps(value, ensure_ascii=False)) - 24)
    return value


def test_serialized_character_balanced_order_keeps_every_candidate_and_each_tools_recency():
    candidates = [('tool', {'tool': tool, 'id': n, 'text': 'x' * length}) for n, (tool, length) in enumerate([
        ('short', 80), ('large', 4000), ('short', 80), ('large', 4000), ('short', 80),
        ('index', 20)])]
    before = copy.deepcopy(candidates)
    ordered = diverse_source_order(candidates, {'index'}, balance_repeated=True)
    assert [item['id'] for _, (_, item) in ordered] == [4, 3, 2, 0, 1, 5]
    assert sorted(index for index, _ in ordered) == list(range(len(candidates)))
    assert candidates == before
    assert diverse_source_order(candidates, {'index'}) != ordered


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('boundary', ['bounded_final_request', 'prepare_synthesis'])
async def test_recent_repeated_originals_survive_larger_refreshed_lookups(tmp_path, monkeypatch, protocol, boundary):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=240000,
                 max_tool_result_chars=16000, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    _, tools, context = setup(tmp_path, cfg)
    originals = [sized_result(f'original-{n}', count, text_chars=text)
        for n, (count, text) in enumerate([(4716, 2430), (6749, 3994), (3319, 1104)])]
    payloads, events = [], []
    runtime = None
    async def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        current = seed(payload); items = current.get('delivered_research_evidence', [])
        for original in originals:
            matches = [item for item in items if item.get('tool') == 'get_material'
                       and projected_result_matches(item, original)]
            item, = matches
            # Exact body, identity and range, not accidental number-string presence.
            assert item['result'] == original
            assert item['record_id'] == newest_records[original['id']]
        assert len(current['calculation_summaries']) == 11
        assert {item['calculation_id'] for item in current['calculation_summaries']} == calculation_ids
        assert all(item['inputs'] == [str(n), '100'] for n, item in enumerate(current['calculation_summaries']))
        assert len(json.dumps(payload.get('input', payload.get('messages')), ensure_ascii=False)) < cfg['max_context_chars'] * .78
        assert [item.get('name', (item.get('function') or {}).get('name')) for item in payload['tools']] == ['finish_analysis']
        assert all(token not in json.dumps(payload) for token in ('UNREAD-ARCHIVE-TAIL', 'FOREIGN-CHILD-ORIGINAL'))
        assert not any(event['type'] == 'research_context_delivery' for event in events)
        return output(protocol, [('finish_analysis', delivery_arguments(report='Originals, ranges and arithmetic remain available.', covered_demand_ids=[]))])
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None:
        ModelClient(c, before_request, httpx.MockTransport(handler)))
    async def emit(event): events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        # Other already-visible task context consumes the same finite envelope;
        # the fix may neither raise max_context_chars nor reduce exact operands.
        runtime.context['other_visible_context'] = 'known existing task context ' * 2300
        for n in range(11):
            args = {'operation': 'percent', 'values': [str(n), '100']}
            value = await tools.calculate(**args)
            runtime._remember_calculation('manager', 'calculate', args, value)
        calculation_ids = {item['calculation_id'] for item in runtime.calculations}
        for round_number in range(18):
            for original in originals:
                add_tool(runtime.client, session, 'get_material', original, f'read-{round_number}-{original["id"]}')
            # refreshed_at is intentionally retained: changed fields are not
            # discarded or treated as exact duplicates to make space.
            for n, length in enumerate([2163, 9438, 4359]):
                add_tool(runtime.client, session, 'get_recommendation',
                    sized_result(f'recommendation-{n}', length, updated_at=f'controlled-{round_number}', action='watch'),
                    f'recommendation-{round_number}-{n}')
            for n in range(2):
                add_tool(runtime.client, session, 'query_market_rows',
                    sized_result(f'market-{n}', 4200, status='unavailable', cache_age_seconds=round_number),
                    f'rows-{round_number}-{n}')
            if round_number % 3 == 0:
                add_tool(runtime.client, session, 'fundamentals',
                    sized_result('company', [5435, 10923, 8188][round_number % 9 // 3], fetched_at=f'controlled-{round_number}',
                        comparisons=[{'metric': 'revenue', 'period': 'controlled-period', 'yoy_pct': '-2.04'}]), f'fund-{round_number}')
        for n in range(2):
            add_tool(runtime.client, session, 'read', sized_result(f'other-original-{n}', 8000, text_chars=2800), f'other-read-{n}')
        archive(session, {'text': 'UNREAD-ARCHIVE-TAIL'}, 'tool_result')
        archive(session, {'text': 'FOREIGN-CHILD-ORIGINAL'}, 'restored_material')
        raw_archives = copy.deepcopy(session.archive)
        await runtime._enter_synthesis_context(session, reason=boundary)
        # The most recent equal result owns the final projection; all older
        # immutable conversation archives still exist with their original IDs.
        newest_records = {}
        for record in session.archive:
            if record.get('kind') != 'conversation': continue
            message = json.loads(record['payload'])
            if message.get('type') == 'function_call_output' or message.get('role') == 'tool':
                value = json.loads(message.get('output', message.get('content')))
                if isinstance(value, dict) and value.get('id') in {item['id'] for item in originals}:
                    newest_records[value['id']] = record['record_id']
        assert session.archive[:len(raw_archives)] == raw_archives
        before = session.compactions
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(session, [tool for tool in agents.MANAGER_TOOLS if tool['name'] == 'finish_analysis'],
                                          tool_choice='finish_analysis')
        finally: runtime.request_owner.reset(token)
        assert len(payloads) == 1 and session.compactions == before
        assert sum(event['type'] == 'research_context_delivery' for event in events) == 1
        assert session.native_search is False and session.phase == 'synthesis'
    finally: await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('case', ['accepted', 'rejected', 'altered'])
async def test_latest_exact_source_requires_actual_acceptance_and_keeps_pending_proof(tmp_path, monkeypatch, protocol, case):
    from app.model import ModelError
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=80000,
                 max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    original = sized_result('receipt-source', 1800, text_chars=600)
    material = store.save_material({'scope': 'account', 'access': 'full_text',
        'source_identifier': 'controlled:latest-exact-source', 'content': original['text']},
        context['job_id'], context['account']['id'])
    original['material_id'] = material['id']
    proof = store.prepare_pending_tool_output(context['job_id'], context['account']['id'], 'manager', 'get_material', original)
    assert proof
    payloads, events = [], []
    async def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        item, = [item for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'get_material']
        assert item['visibility'] == 'completed_tool_output_not_previously_delivered'
        assert projected_result_matches(item, original) is (case != 'altered')
        # Changed observation metadata must remain distinct, never be silently
        # removed by exact deduplication or changed into a current fact.
        lookups = [item['result'] for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'get_recommendation']
        assert sorted(item['updated_at'] for item in lookups) == ['controlled-earlier', 'controlled-later']
        assert {item['price'] for item in lookups} == {'10.00', '10.01'}
        assert store.checkpoint_deliveries(context['job_id']) == []
        return httpx.Response(503, text='fixture unavailable') if case == 'rejected' else output(protocol, [
            ('finish_analysis', delivery_arguments(report='Controlled accepted context only; not adoption.', covered_demand_ids=[]))])
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, httpx.MockTransport(handler)))
    async def emit(event): events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        add_tool(runtime.client, session, 'get_material', original, 'earlier-same-original')
        add_tool(runtime.client, session, 'get_recommendation', {'id': 'same', 'updated_at': 'controlled-earlier', 'price': '10.00'}, 'earlier-observation')
        call = add_tool(runtime.client, session, 'get_material', original, 'latest-same-original')
        pending = {'call': call, 'message': copy.deepcopy(session.history[-1]), 'result': copy.deepcopy(original), 'proof': proof}
        session._pending_material_deliveries = [pending]
        add_tool(runtime.client, session, 'get_recommendation', {'id': 'same', 'updated_at': 'controlled-later', 'price': '10.01'}, 'later-observation')
        archive(session, {'text': 'UNREAD-PRIVATE-TAIL'}, 'tool_result')
        runtime.synthesis_only = True
        await runtime._enter_synthesis_context(session, reason='bounded_final_request')
        if case == 'altered':
            current = json.loads(session.history[0]['content'])
            item, = [item for item in current['delivered_research_evidence'] if item['tool'] == 'get_material']
            item['result']['text'] += ' altered'
            session.history[0]['content'] = json.dumps(current, ensure_ascii=False)
        token = runtime.request_owner.set(('manager', True))
        try:
            if case == 'rejected':
                with pytest.raises(ModelError):
                    await runtime.client.complete(session, [t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis'], tool_choice='finish_analysis')
            else:
                await runtime.client.complete(session, [t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis'], tool_choice='finish_analysis')
        finally: runtime.request_owner.reset(token)
        assert len(payloads) == 1
        receipts = store.checkpoint_deliveries(context['job_id'])
        assert len(receipts) == int(case == 'accepted')
        if case == 'accepted':
            assert receipts[0]['body']['visible_result'] == original
            assert session._pending_material_deliveries == []
        else:
            assert session._pending_material_deliveries == [pending]
        assert sum(e['type'] == 'research_context_delivery' for e in events) == int(case != 'rejected')
        assert 'UNREAD-PRIVATE-TAIL' not in json.dumps(payloads)
    finally: await runtime.close()
