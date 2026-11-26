"""Exact short operands survive real Runtime projection; HTTP is local fixture.

The arithmetic labels below describe the fixture only. calculate does not accept
source, financial period or unit metadata, so the projection must not invent it.
"""
import copy
import json

import httpx
import pytest

from app import agents
from app.calculation_context import projection
from app.context import compact, recall
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments
from tests.test_fourteenth_deadlines import ControlledDeadlineClock
from tests.test_ninth_calculation_context import assert_hot_closeout, config, output, seed, setup


@pytest.mark.parametrize('operation,inputs,result,unit', [
    ('subtract', ['12.3400', '5.6700'], '6.6700', 'input-dependent; not inferred'),
    ('subtract', ['-37.09', '-37.72'], '0.63', 'input-dependent; not inferred'),
    ('percent', ['0.63', '37.72'], '1.670201484623541887592788971', 'percent'),
    ('sum', [str(value) for value in range(20)], '190', 'input-dependent; not inferred'),
])
def test_short_operands_are_exact_and_do_not_invent_financial_attribution(operation, inputs, result, unit):
    original = {'operation': operation, 'inputs': inputs, 'result': result}
    before = copy.deepcopy(original)
    summary = projection('calculate', original, {'operation': operation, 'values': [999]})
    assert summary['inputs'] == inputs
    assert summary['inputs_visibility'] == {'status': 'complete', 'source': 'output.inputs'}
    assert summary['result'] == result and summary['unit'] == unit
    assert summary['valid_samples'] == len(inputs)
    for key in ('security', 'date_range', 'source_material_version'):
        assert summary[key] == 'not supplied by calculate'
    summary['inputs'].append('mutation must not reach original')
    assert original == before


@pytest.mark.parametrize('values,reason', [
    ([str(value) for value in range(1000)], 'input representation exceeds short-expression bound'),
    (['0.' + '1' * 600, '1'], 'input representation exceeds short-expression bound'),
    (['0.' + '1' * 300] * 2, 'input representation exceeds short-expression bound'),
    ([{'close': '1'}], 'inputs are not a scalar list'),
    ([float('nan')], 'inputs are not finite JSON scalars'),
])
def test_large_or_non_scalar_inputs_are_explicitly_archived_without_prefix(values, reason):
    summary = projection('calculate', {'operation': 'sum', 'inputs': values, 'result': 'unchanged'})
    assert 'inputs' not in summary
    assert summary['result'] == 'unchanged'
    assert summary['valid_samples'] == len(values)
    assert summary['inputs_visibility'] == {'status': 'archive_only', 'source': 'output.inputs',
        'archive_ref': 'full_inputs', 'reason': reason}
    assert len(json.dumps(summary, ensure_ascii=False)) < 1000


def test_failed_and_missing_input_sources_are_distinct_and_no_label_is_fabricated():
    summary = projection('calculate', {'error': 'division by zero'},
        {'operation': 'divide', 'values': ['9', '0'], 'label': 'unsupported caller label'})
    assert summary['status'] == 'failed' and summary['result'] is None
    assert summary['inputs'] == ['9', '0']
    assert summary['inputs_visibility'] == {'status': 'complete', 'source': 'arguments.values'}
    assert 'label' not in summary and summary['unit'] == 'input-dependent; not inferred'
    missing = projection('calculate', {'operation': 'sum', 'result': '3'})
    assert 'inputs' not in missing
    assert missing['valid_samples'] is None
    assert missing['inputs_visibility'] == {'status': 'unavailable', 'source': None,
                                            'reason': 'inputs not recorded'}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('count', [9, 20])
async def test_short_expense_series_is_recheckable_in_final_http(tmp_path, monkeypatch, protocol, count):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=3)
    _, tools, context = setup(tmp_path, cfg)
    # A small expense ledger has a meaningful sum even with more than eight
    # entries. The unit is supplied in the task, never guessed by calculate.
    context['task'] = f'离线夹具：核对 {count} 笔费用明细合计；各笔单位为元，不代表真实账户交易。'
    values = [str(value) for value in range(1, count + 1)]
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return output(protocol, [('calculate', {'operation': 'sum', 'values': values})])
        assert len(requests) == 2
        visible = seed(payload)
        assert visible['task'] == context['task']
        summary, = visible['calculation_summaries']
        assert summary['inputs'] == values
        assert summary['valid_samples'] == count
        assert summary['result'] == str(sum(range(1, count + 1)))
        assert summary['inputs_visibility'] == {'status': 'complete', 'source': 'output.inputs'}
        assert summary['unit'] == 'input-dependent; not inferred'
        assert_hot_closeout(payload, requests[0], 'finish_analysis')
        assert visible['calculation_indexes']['omitted_summary_count'] == 0
        return output(protocol, [('finish_analysis', delivery_arguments(report='按任务给定的元口径汇总费用；逐项输入与合计可复核。'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('max_chars', [10000, 240000])
async def test_operands_survive_compaction_and_submit_only_real_http(
        tmp_path, monkeypatch, protocol, max_chars):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=max_chars,
                 max_model_calls=1, max_agent_turns=1)
    _, tools, context = setup(tmp_path, cfg)
    requests, events = [], []
    arguments = [
        {'operation': 'subtract', 'values': ['12.3400', '5.6700']},  # fixture H1 minus Q1
        {'operation': 'subtract', 'values': ['-37.09', '-37.72']},  # return difference
        {'operation': 'divide', 'values': ['906135934.998', '5']},
    ]
    expected = [await tools.calculate(**item) for item in arguments]

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        visible = seed(payload)
        summaries = visible['calculation_summaries']
        # Two short financial differences fit even in the constrained context.
        # Any additional whole result must fit or have an explicit omission;
        # this change does not enlarge the production context budget.
        assert len(summaries) >= 2
        assert len(summaries) + visible['calculation_indexes']['omitted_summary_count'] == len(expected)
        if max_chars == 240000:
            assert len(summaries) == len(expected)
        assert {item.get('name') or item.get('function', {}).get('name') for item in payload['tools']} == {'finish_analysis'}
        assert '不能调用 recall_context' in visible['calculation_access']
        for summary, original in zip(summaries, expected):
            assert summary['inputs'] == original['inputs']
            assert summary['result'] == original['result']
            assert summary['operation'] == original['operation']
            assert summary['inputs_visibility']['status'] == 'complete'
            assert summary['source_task_id'] == context['job_id']
            assert summary['agent_id'] == 'manager' and summary['full_inputs']['record_id']
            metadata = {**visible.get('calculation_contexts', {}).get(summary.get('context_ref'), {}), **summary}
            assert metadata['date_range'] == 'not supplied by calculate'
            assert metadata['source_material_version'] == 'not supplied by calculate'
        history = [item for item in payload.get('input', payload.get('messages')) if item.get('role') != 'system']
        assert len(json.dumps(history, ensure_ascii=False)) <= max_chars
        return output(protocol, [('finish_analysis', delivery_arguments(report='已保存操作数与结果；来源与期间未另行推断。'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        for item in arguments:
            await runtime._dispatch('manager', 'calculate', item)
        originals = copy.deepcopy(runtime.calculations)
        runtime._pin_runtime_status(runtime.manager_session, 'manager', 1, False)
        # Each completed visible note is within add_user's individual limit;
        # only their accumulated history crosses the production compaction bound.
        for index in range(max_chars // 1000 + 1):
            runtime.client.add_user(runtime.manager_session, {'earlier_visible_fixture_note': index,
                                                             'text': 'x' * 1000})
        assert compact(runtime.manager_session, max_chars)
        assert runtime.manager_session.compactions == 1
        await runtime.run()
        assert len(requests) == 1
        assert runtime.calculations == originals
        coverage = runtime.calculation_coverage[-1]['calculations']
        assert [item['visibility'] for item in coverage[:2]] == ['summary', 'summary']
        visible_ids = {item['calculation_id'] for item in seed(requests[0])['calculation_summaries']}
        assert {item['calculation_id'] for item in coverage if item['visibility'] == 'summary'} == visible_ids
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_restored_long_inputs_stay_archived_and_short_operands_reach_final_without_recompute(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=4)
    _, tools, context = setup(tmp_path, cfg)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    long_values = [str(value) for value in range(1000)]
    try:
        await runtime._dispatch('manager', 'calculate', {'operation': 'subtract', 'values': ['12.34', '5.67']})
        await runtime._dispatch('manager', 'calculate', {'operation': 'sum', 'values': long_values})
        await runtime._dispatch('manager', 'calculate', {'operation': 'divide', 'values': ['9', '0']})
        checkpoint = await runtime._save_checkpoint({'summary': 'Exact visible arithmetic fixture.'})
    finally:
        await runtime.close()
    before = copy.deepcopy(checkpoint)
    requests = []
    async def cannot_recalculate(*args, **kwargs):
        pytest.fail('Restoration must use the saved arithmetic, never recompute it')
    monkeypatch.setattr(tools, 'calculate', cannot_recalculate)

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        short, large, failed = seed(payload)['calculation_summaries']
        assert short['inputs'] == ['12.34', '5.67'] and short['result'] == '6.67'
        assert 'inputs' not in large and large['inputs_visibility']['status'] == 'archive_only'
        assert large['valid_samples'] == 1000 and large['result'] == '499500'
        assert failed['status'] == 'failed' and failed['inputs'] == ['9', '0']
        assert all(item['source_task_id'] == context['job_id'] for item in (short, large, failed))
        assert {item['name'] for item in payload['tools']} == {'finish_analysis'}
        return output('responses', [('finish_analysis', delivery_arguments(report='大序列仅存档；短算式输入完整，计算不重做。'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    resumed = agents.ResearchRuntime({**context, 'job_id': 'resumed-arithmetic', 'synthesis_checkpoint': checkpoint},
                                    cfg, tools, emit)
    try:
        summary = resumed.calculations[1]['summary']
        page = recall(resumed.manager_session, record_id=summary['full_inputs']['record_id'], length=12000)
        text = page['text']
        while page['next_start'] is not None:
            page = recall(resumed.manager_session, record_id=summary['full_inputs']['record_id'],
                          start=page['next_start'], length=12000)
            text += page['text']
        assert json.loads(text)['arguments']['values'] == long_values
        assert json.loads(text)['output']['inputs'] == long_values
        resumed.calls = cfg['max_model_calls'] - 2
        resumed.synthesis_started_calls = resumed.calls
        result = await resumed.run()
        assert result['synthesis_recovered'] and len(requests) == 1
        assert checkpoint == before
    finally:
        await resumed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_child_deadline_submission_retains_real_short_inputs(tmp_path, monkeypatch, protocol):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None, max_agent_turns=None,
        analysis_timeout_seconds=1000, agent_timeout_seconds=100,
        model_request_hard_timeout_seconds=80, synthesis_request_hard_timeout_seconds=80)
    _, tools, context = setup(tmp_path, cfg)
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return output(protocol, [
                ('calculate', {'operation': 'subtract', 'values': ['12.34', '5.67']}),
                ('calculate', {'operation': 'subtract', 'values': ['-37.09', '-37.72']}),
            ])
        assert len(requests) == 2
        visible = seed(payload)
        summaries = visible['calculation_summaries']
        assert [item['inputs'] for item in summaries] == [['12.34', '5.67'], ['-37.09', '-37.72']]
        assert [item['result'] for item in summaries] == ['6.67', '0.63']
        assert all(item['agent_id'] == visible['runtime_status']['agent_id'] for item in summaries)
        assert all(item['research_round'] == 1 for item in summaries)
        assert_hot_closeout(payload, requests[0], 'submit_report')
        assert '不能调用 recall_context' in visible['calculation_access']
        return output(protocol, [('submit_report', {'summary': '短算式输入与结果实际可见，未补造来源和期间。'})])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        if event['type'] == 'model_usage' and len(requests) == 1:
            timeline.advance(60)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    runtime.deadline = timeline.start + 1000
    try:
        created = await runtime._spawn({'task': 'Local short arithmetic evidence fixture'})
        child = runtime.children[created['agent_id']]
        await child.task_handle
        assert child.status == 'completed' and len(child.reports) == 1
        assert len(requests) == 2 and child.deadline_closing
        assert not runtime.budget_closing
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 100
    finally:
        await runtime.close()
