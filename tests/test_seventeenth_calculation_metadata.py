"""Exact shared metadata remains explicit in real final HTTP requests."""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import compact_calculation_metadata, serialized
from app.model import ModelClient
from tests.context_helpers import effective_context
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, setup, source


def resolve(rows, contexts, seed_config=None):
    expanded = []
    for row in rows:
        row = copy.deepcopy(row)
        reference = row.pop('context_ref', None)
        if reference:
            assert reference in contexts
            row = {**copy.deepcopy(contexts[reference]), **row}
        if row.pop('model_config_ref', None) == 'seed.model_config':
            assert seed_config is not None
            row.setdefault('model_config', copy.deepcopy(seed_config))
        expanded.append(row)
    return expanded


def test_exact_shared_method_units_and_visibility_preserve_every_field():
    summaries = [{
        'calculation_id': f'calculation-{index}', 'result': str(index * 7),
        'inputs': [str(index), '7'], 'unit': 'input-dependent',
        'model_config': {'model': 'controlled', 'effort': 'high', 'details': 'shared configuration ' * 30},
        'methods': {'formula': 'last_close / moving_average - 1', 'scope': 'stored rows only ' * 15},
        'units': {'return': 'decimal', 'close_to_ma_change_pct': 'percent_change'},
        'inputs_visibility': {'status': 'complete', 'source': 'output.inputs'},
        'date_range': {'start': '2026-10-01', 'end': '2026-11-05'},
        'source_material_version': 'snapshot-one', 'security': 'fixture-security',
        'source_task_id': 'fixture-task', 'full_inputs': {'record_id': f'context-{index}'},
        'access': 'already_visible', 'unknown': None,
    } for index in range(12)]
    original = copy.deepcopy(summaries)
    rows, contexts = compact_calculation_metadata(summaries)
    assert resolve(rows, contexts) == original
    assert summaries == original
    assert len(contexts) == 1
    assert all('methods' not in row and 'units' not in row and 'inputs_visibility' not in row for row in rows)
    assert len(serialized(rows)) + len(serialized(contexts)) < len(serialized(original))
    rows[0]['inputs'][0] = 'must not mutate original'
    next(iter(contexts.values()))['units']['return'] = 'must not mutate original'
    assert summaries == original


def test_distinct_metadata_does_not_break_existing_profitable_group():
    # Every extra dictionary differs: blindly extending the combined key would
    # undo the large shared model configuration and increase the request size.
    summaries = [{
        'calculation_id': f'calculation-{index}', 'result': str(index),
        'model_config': {'model': 'fixture', 'details': 'shared ' * 300},
        'methods': {'unique_formula': str(index)}, 'units': {'result': str(index)},
        'inputs_visibility': {'status': 'complete', 'source': f'fixture-{index}'},
    } for index in range(8)]
    rows, contexts = compact_calculation_metadata(summaries)
    assert resolve(rows, contexts) == summaries
    assert len(contexts) == 1
    assert all('model_config' not in row and 'methods' in row and 'units' in row for row in rows)
    assert len(serialized(rows)) + len(serialized(contexts)) < len(serialized(summaries))


def test_differing_units_periods_and_permissions_never_inherit_other_values():
    summaries = []
    for unit, period, permission in [('percent', '2026-Q1', 'already_visible'),
                                     ('percentage_points', '2026-Q2', 'agent_extraction')]:
        for index in range(4):
            summaries.append({'calculation_id': f'{period}-{index}', 'result': '0.63',
                'model_config': {'model': 'fixture', 'details': 'same ' * 100},
                'methods': {'formula': 'left minus right'}, 'units': {'result': unit},
                'date_range': period, 'visibility': permission,
                'source_material_version': f'snapshot-{period}',
                'inputs_visibility': {'status': 'complete', 'source': 'output.inputs'}})
    rows, contexts = compact_calculation_metadata(summaries)
    assert resolve(rows, contexts) == summaries
    assert len(contexts) == 2
    assert contexts[rows[0]['context_ref']]['units'] != contexts[rows[-1]['context_ref']]['units']


def test_context_reader_uses_same_request_values_and_rejects_dangling_reference():
    context = {'calculation_summaries': [{'calculation_id': 'one', 'context_ref': 'shared',
        'units': {'result': 'explicit_override'}}], 'calculation_contexts': {
        'shared': {'units': {'result': 'shared'}, 'methods': {'formula': 'left minus right'}}}}
    payload = {'input': [{'role': 'user', 'content': json.dumps(context)}]}
    original = copy.deepcopy(payload)
    item, = effective_context(payload)['calculation_summaries']
    assert item['units'] == {'result': 'explicit_override'}
    assert item['methods'] == {'formula': 'left minus right'}
    assert payload == original
    context.pop('calculation_contexts')
    with pytest.raises(AssertionError, match='missing calculation context'):
        effective_context({'input': [{'role': 'user', 'content': json.dumps(context)}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('max_chars', [10000, 240000])
async def test_final_http_resolves_exact_metadata_and_partial_selection_has_no_dangling_ref(
        tmp_path, monkeypatch, protocol, max_chars):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=max_chars,
                 max_model_calls=None, max_agent_turns=None)
    store, tools, context = setup(tmp_path, cfg)
    requests, expected = [], {}

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        history = payload.get('input', payload.get('messages'))
        visible = next(json.loads(item['content']) for item in history if item.get('role') == 'user')
        raw = visible['calculation_summaries']
        contexts = visible.get('calculation_contexts', {})
        assert contexts and raw
        assert set(contexts) == {item['context_ref'] for item in raw if item.get('context_ref')}
        expanded = resolve(raw, contexts, visible.get('model_config'))
        assert all(item == expected[item['calculation_id']] for item in expanded)
        assert len(expanded) + visible['calculation_indexes']['omitted_summary_count'] == len(expected)
        if max_chars == 240000:
            assert len(expanded) == len(expected)
            assert any('methods' in item for item in contexts.values())
            assert any('units' in item for item in contexts.values())
        else:
            assert visible['calculation_indexes']['omitted_summary_count'] > 0
        assert all('inputs_visibility' not in item for item in raw if item['tool'] == 'calculate')
        # ModelClient's context budget applies to session history. Chat puts
        # the system prompt in messages; Responses sends it separately.
        session_history = [item for item in history if item.get('role') != 'system']
        assert len(serialized(session_history)) < max_chars * .78
        assert 'context_ref' in visible['calculation_access']
        assert 'calculation_contexts' in visible['calculation_access']
        return output(protocol, [('finish_analysis', delivery_arguments(report='Controlled shared metadata and exact values.'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        for index in range(8):
            await runtime._dispatch('manager', 'calculate', {'operation': 'subtract',
                'values': [str(20 + index), '7']})
        for index in range(3):
            material = source(store, 'OFFLINE', index, count=30)
            await runtime._dispatch('manager', 'market_indicators', {'material_id': material['id']})
        expected.update({item['calculation_id']: copy.deepcopy(item['summary'])
                         for item in runtime._available_calculations('manager')})
        await runtime._enter_synthesis_context(runtime.manager_session, reason='bounded_final_request')
        runtime._pin_runtime_status(runtime.manager_session, 'manager', None, True)
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(runtime.manager_session,
                [tool for tool in agents.MANAGER_TOOLS if tool['name'] == 'finish_analysis'],
                tool_choice='finish_analysis')
        finally:
            runtime.request_owner.reset(token)
        assert len(requests) == 1
    finally:
        await runtime.close()
