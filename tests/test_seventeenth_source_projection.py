"""Exact source sharing and bounded final-wire diversity; offline fixtures only."""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import archive
from app.model import ModelClient
from app.source_projection import diverse_source_order, project_source_item, projected_result_matches
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, seed, setup
from tests.test_seventeenth_child_final_evidence import add_tool


def financial_result():
    # Controlled rows with repeated identity metadata and opposite financial
    # signs. No account/checkpoint body is copied into this fixture.
    common = {f'identity_{n}': f'controlled identity field {n} ' * 2 for n in range(12)}
    return {'symbol': 'fixture-company', 'amount_unit': 'CNY', 'status': 'ok',
        'source': 'controlled structured source', 'as_of': '2026-09-30',
        'rows': [{'report_period': f'controlled-period-{n}', 'revenue': str(1000 + n),
            'raw': {**common, 'row_specific_note': 'visible original detail ' * 20,
                    'period_identifier': n, 'value': str(1000 + n)}} for n in range(8)],
        'comparisons': [
            {'metric': 'revenue', 'period': '2026-09-30', 'comparison_period': '2025-09-30',
             'current_value': '979.60', 'baseline_value': '1000.00', 'amount_unit': 'CNY',
             'difference': '-20.40', 'yoy_pct': '-2.04', 'yoy_unit': 'percent'},
            {'metric': 'parent_net_profit', 'period': '2026-09-30', 'comparison_period': '2025-09-30',
             'current_value': '119.11', 'baseline_value': '100.00', 'amount_unit': 'CNY',
             'difference': '19.11', 'yoy_pct': '19.11', 'yoy_unit': 'percent'}],
        'coverage_note': 'Known historical cumulative periods; no current quote inferred.'}


def test_shared_rows_keep_every_value_type_and_variable_metadata():
    original = financial_result()
    original['rows'][0]['raw']['typed_value'] = 1
    original['rows'][1]['raw']['typed_value'] = True
    for n, row in enumerate(original['rows']):
        row['raw']['signed_zero'] = -0.0 if n % 2 else 0.0
    item = {'tool': 'fundamentals', 'result': original, 'record_id': 'owned-visible-record',
            'material_id': 'visible-source', 'visibility': 'previously_delivered_tool_output'}
    before = copy.deepcopy(item)
    projected = project_source_item(item)
    assert len(json.dumps(projected)) < len(json.dumps(item))
    assert projected_result_matches(projected, original)
    assert projected['result']['comparisons'] == original['comparisons']
    assert projected['result']['amount_unit'] == original['amount_unit']
    assert 'typed_value' not in projected['result_projection']['common_fields']
    assert 'signed_zero' not in projected['result_projection']['common_fields']
    assert [repr(row['raw']['signed_zero']) for row in projected['result']['rows']] == ['0.0', '-0.0'] * 4
    assert projected['result']['rows'][0]['raw']['typed_value'] is not True
    assert projected['result']['rows'][1]['raw']['typed_value'] is True
    assert [row['raw']['period_identifier'] for row in projected['result']['rows']] == list(range(8))
    assert item == before
    assert all(projected[key] == item[key] for key in ('tool', 'record_id', 'material_id', 'visibility'))


@pytest.mark.parametrize('change', ['common', 'row', 'type', 'collision', 'kind', 'hash', 'rule', 'partial', 'missing', 'extra'])
def test_shared_result_receipt_rejects_changed_or_ambiguous_projection(change):
    original = financial_result()
    projected = project_source_item({'tool': 'fundamentals', 'result': original})
    proof = projected['result_projection']
    if change == 'common': proof['common_fields']['identity_0'] += ' changed'
    elif change == 'row': projected['result']['rows'][0]['raw']['value'] = 'different'
    elif change == 'type': projected['result']['rows'][0]['raw']['period_identifier'] = False
    elif change == 'collision': projected['result']['rows'][0]['raw']['identity_0'] = proof['common_fields']['identity_0']
    elif change == 'kind': proof['kind'] = 'unknown_projection'
    elif change == 'hash': proof['source_result_sha256'] = 'not-the-original'
    elif change == 'rule': proof['rule'] = 'ambiguous inheritance'
    elif change == 'partial': projected['truncated_for_synthesis'] = True
    elif change == 'missing': proof.pop('common_fields')
    else: proof['unexpected'] = 'not an authorized representation'
    assert not projected_result_matches(projected, original)


def test_projection_collisions_small_rows_and_direct_types_are_not_silently_rewritten():
    for item in ({'result': financial_result(), 'result_projection': {'existing': True}},
                 {'result': {'rows': [{'raw': {'x': 1}}, {'raw': {'x': 1}}]}},
                 {'result': {'rows': [{'raw': {'x': 1}}]}}):
        assert project_source_item(item) == item
    assert not projected_result_matches({'result': {'value': 1}}, {'value': True})
    assert not projected_result_matches({'result': {'value': 1}}, {'value': 1.0})
    assert not projected_result_matches({'result': {'value': 1}, 'result_projection': None}, {'value': 1})


def test_tool_diversity_keeps_latest_representatives_then_original_recent_order():
    candidates = [('tool', {'tool': tool, 'id': n}) for n, tool in enumerate(
        ['fundamentals', 'read', 'query_market_rows', 'query_market_rows', 'read', 'visible_user_context'])]
    ordered = diverse_source_order(candidates, {'visible_user_context'})
    assert [item['id'] for _, (_, item) in ordered] == [4, 3, 0, 2, 1, 5]
    assert len({index for index, _ in ordered}) == len(candidates)


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('room', ['sufficient', 'insufficient'])
async def test_final_wire_diversity_keeps_whole_signed_period_comparisons_or_explicit_omission(tmp_path, monkeypatch, protocol, room):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=80000 if room == 'sufficient' else 10000,
        max_tool_result_chars=30000, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    _, tools, context = setup(tmp_path, cfg)
    payloads = []
    original = financial_result()
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        current = seed(payload)
        evidence = current.get('delivered_research_evidence', [])
        complete = [item for item in evidence if item.get('tool') == 'fundamentals'
                    and projected_result_matches(item, original)]
        if room == 'sufficient':
            item, = complete
            assert item['result_projection']['kind'] == 'shared_row_raw_v1'
            assert item['result']['amount_unit'] == 'CNY'
            revenue, profit = item['result']['comparisons']
            assert (revenue['period'], revenue['comparison_period'], revenue['current_value'],
                    revenue['baseline_value'], revenue['yoy_pct'], revenue['yoy_unit']) == (
                    '2026-09-30', '2025-09-30', '979.60', '1000.00', '-2.04', 'percent')
            assert (profit['period'], profit['comparison_period'], profit['current_value'],
                    profit['baseline_value'], profit['yoy_pct'], profit['yoy_unit']) == (
                    '2026-09-30', '2025-09-30', '119.11', '100.00', '19.11', 'percent')
            assert any(item.get('tool') == 'query_market_rows' and item.get('result', {}).get('latest_marker') == 9
                       for item in evidence)
        else:
            assert complete == []
            assert current['omitted_evidence']['truncated_for_synthesis'] is True
            assert current['bounded_or_omitted_outputs'] > 0
        assert 'UNREAD-RAW-SOURCE' not in json.dumps(payload)
        return output(protocol, [('finish_analysis', delivery_arguments(report='Controlled source budget boundary.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        # Leave room under the 58% wire envelope for both whole representatives
        # in the sufficient case, without raising the context budget.
        runtime.context['other_visible_context'] = 'known unrelated context ' * (1500 if room == 'sufficient' else 110)
        add_tool(runtime.client, session, 'fundamentals', original, 'financial-source')
        for n in range(10):
            add_tool(runtime.client, session, 'query_market_rows', {'latest_marker': n,
                'status': 'unavailable', 'visible_range_note': 'market result detail ' * 110}, f'market-{n}')
        archive(session, {'text': 'UNREAD-RAW-SOURCE'}, 'tool_result')
        runtime.synthesis_only = True
        await runtime._enter_synthesis_context(session, reason='bounded_final_request')
        before = session.compactions
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(session, [tool for tool in agents.MANAGER_TOOLS if tool['name'] == 'finish_analysis'],
                tool_choice='finish_analysis')
        finally:
            runtime.request_owner.reset(token)
        assert len(payloads) == 1 and session.compactions == before
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('changed', [False, True])
async def test_pending_shared_result_receipt_requires_accepted_exact_reconstruction(tmp_path, monkeypatch, protocol, changed):
    cfg = config(monkeypatch, model_protocol=protocol, max_tool_result_chars=30000,
        max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    original = financial_result()
    material = store.save_material({'scope': 'account', 'access': 'structured_data',
        'source_identifier': 'controlled:exact-shared-result', 'content': json.dumps(original)},
        context['job_id'], context['account']['id'])
    original['material_id'] = material['id']
    proof = store.prepare_pending_tool_output(context['job_id'], context['account']['id'], 'manager', 'fundamentals', original)
    assert proof
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        item, = [item for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'fundamentals']
        assert projected_result_matches(item, original) is not changed
        assert store.checkpoint_deliveries(context['job_id']) == []  # Not accepted yet.
        return output(protocol, [('finish_analysis', delivery_arguments(report='Exact source projection acceptance.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        call = add_tool(runtime.client, session, 'fundamentals', original, 'pending-financial')
        session._pending_material_deliveries = [{'call': call, 'message': copy.deepcopy(session.history[-1]),
            'result': copy.deepcopy(original), 'proof': proof}]
        runtime.synthesis_only = True
        await runtime._enter_synthesis_context(session, reason='bounded_final_request')
        if changed:
            current = json.loads(session.history[0]['content'])
            item, = current['delivered_research_evidence']
            item['result_projection']['common_fields']['identity_0'] += ' altered'
            session.history[0]['content'] = json.dumps(current)
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(session, [tool for tool in agents.MANAGER_TOOLS if tool['name'] == 'finish_analysis'])
        finally:
            runtime.request_owner.reset(token)
        receipts = store.checkpoint_deliveries(context['job_id'])
        assert len(requests) == 1
        if changed:
            assert receipts == [] and len(session._pending_material_deliveries) == 1
        else:
            assert len(receipts) == 1 and session._pending_material_deliveries == []
            assert receipts[0]['body']['visible_result'] == original
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_necessary_correction_compaction_preserves_selected_shared_source(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=80000, max_tool_result_chars=30000,
        max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    _, tools, context = setup(tmp_path, cfg)
    original = financial_result()
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        item, = [item for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'fundamentals']
        assert projected_result_matches(item, original)
        assert item['result']['comparisons'] == original['comparisons']
        if len(requests) == 1:
            return output(protocol, [('finish_analysis', delivery_arguments(report='candidate ' * 3000)),
                ('finish_analysis', delivery_arguments(report='duplicate ' * 3000))])
        return output(protocol, [('finish_analysis', delivery_arguments(report='Protocol corrected; same full visible values.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        add_tool(runtime.client, runtime.manager_session, 'fundamentals', original, 'financial-source')
        runtime.budget_closing = True
        await runtime._loop('manager', runtime.manager_session, manager=True)
        assert len(requests) == 2 and runtime.manager_session.compactions == 1
        assert runtime.client.requests[-1]['prefix_diagnostic']['change_reason'] == 'compression'
    finally:
        await runtime.close()
