"""Actual HTTP payload regressions with production Runtime/Store/ResearchTools.

All prices and protocol responses are controlled fixtures, never live results.
"""
import copy
import asyncio
from datetime import date, timedelta
import json

import httpx
import pytest

from app import agents
from app.context import recall
from app.model import ModelClient
from app.service import ResearchTools
from app.store import Store


def config(monkeypatch, **extra):
    monkeypatch.setenv('NINTH_CALCULATION_TEST_KEY', 'fixture-only-not-a-real-secret')
    return {'model_base_url': 'https://calculation-fixture.example/v1', 'model_name': 'fixture',
        'model_key_env': 'NINTH_CALCULATION_TEST_KEY', 'model_protocol': 'responses',
        'model_native_search': False, 'model_retries': 0, 'max_context_chars': 240000,
        'max_tool_result_chars': 6000, 'max_model_calls': 4, 'max_agent_turns': 8,
        'analysis_timeout_seconds': 20, 'model_request_hard_timeout_seconds': 2,
        'synthesis_request_hard_timeout_seconds': 2, **extra}


def output(protocol, calls):
    if protocol == 'responses':
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'reasoning', 'encrypted_content': 'NEVER_LOG_PRIVATE_REASONING'},
            *[{'type': 'function_call', 'call_id': f'call-{i}', 'name': name,
               'arguments': json.dumps(args)} for i, (name, args) in enumerate(calls)]]})
    return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'reasoning_content': 'NEVER_LOG_PRIVATE_REASONING',
        'tool_calls': [{'id': f'call-{i}', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(calls)]}}]})


def source(store, symbol, index, *, count=65, benchmark=False):
    rows = [{'time': str(date(2026, 1, 1) + timedelta(days=i)),
             'close': str(100 + (i if not benchmark else i * 2) + index)} for i in range(count)]
    return store.save_material({'scope': 'public', 'access': 'structured_data',
        'source_identifier': f'controlled-calculation:{symbol}:{index}',
        'content': json.dumps({'symbol': symbol, 'source': 'controlled offline prices',
            'status': 'ok', 'interval': 'daily', 'adjustment': 'none', 'rows': rows})})


def setup(tmp_path, cfg):
    store = Store(tmp_path / 'calculations.sqlite3')
    account = store.save_account({'name': 'calculation fixture', 'cash': '10000', 'holdings': []})
    job = store.job(account['latest_job_id'])
    tools = ResearchTools(store, job, cfg)
    context = {'job_id': job['id'], 'account': account, 'input_version': account['version']}
    return store, tools, context


def seed(payload):
    return json.loads(next(item['content'] for item in payload.get('input', payload.get('messages'))
                           if item.get('role') == 'user'))


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('count', [1, 40])
async def test_default_budget_preserves_every_value_in_final_http_after_archive(tmp_path, monkeypatch, protocol, count):
    cfg = config(monkeypatch, model_protocol=protocol)
    store, tools, context = setup(tmp_path, cfg)
    benchmark = source(store, 'CSI300', 0, benchmark=True)
    assets = [source(store, 'SZ:159278', i) for i in range(count)]
    expected = [await tools.market_indicators(item['id'], benchmark_material_id=benchmark['id']) for item in assets]
    assert all(item['comparison_status'] == 'ok' for item in expected)
    assert all(5000 < len(json.dumps(item)) <= 6000 for item in expected)
    payloads, events = [], []

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return output(protocol, [('market_indicators', {'material_id': item['id'],
                'benchmark_material_id': benchmark['id']}) for item in assets])
        if len(payloads) == 2:
            return output(protocol, [('calculate', {'operation': 'percent', 'values': [7, 20]})])
        visible = seed(payload)
        summaries = visible['calculation_summaries']
        results = {item['material_id']: item for item in summaries if item['tool'] == 'market_indicators'}
        assert len(results) == count
        for original in expected:
            item = results[original['material_id']]
            for key in ('return', 'relative_return', 'aligned_asset_return', 'aligned_benchmark_return',
                        'returned_range', 'valid_samples', 'return_samples', 'adjustment', 'methods', 'material_identity'):
                assert item[key] == original[key]
            assert item['benchmark']['material_identity'] == original['benchmark']['material_identity']
            assert item['agent_id'] == 'manager' and item['input_version'] == context['input_version']
            assert item['full_inputs']['record_id']
            assert 'input_closes' not in item and 'comparison_input_closes' not in item
        assert next(item for item in summaries if item['tool'] == 'calculate')['result'] == '35.00'
        assert visible['calculation_indexes']['omitted_summary_count'] == 0
        assert '不能调用 recall_context' in visible['calculation_access']
        function_names = {tool.get('name') or tool.get('function', {}).get('name') for tool in payload['tools']}
        assert function_names == {'finish_analysis'}
        assert len(json.dumps(payload.get('input', payload.get('messages')), ensure_ascii=False)) < cfg['max_context_chars'] * .78
        assert 'NEVER_LOG_PRIVATE_REASONING' not in json.dumps(payload)
        return output(protocol, [('finish_analysis', {'report': '确定性结果已可见；本测试不评价模型采用或投资方向。'})])

    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == 3
    assert (result['context_compactions'] > 0) == (count == 40)
    coverage = result['calculation_coverage'][-1]
    assert len(coverage['calculations']) == count + 1
    assert {item['visibility'] for item in coverage['calculations']} == {'summary'}
    assert 'NEVER_LOG_PRIVATE_REASONING' not in json.dumps(events)
    assert 'fixture-only-not-a-real-secret' not in json.dumps(events)
    assert 'input_closes' not in json.dumps(coverage)


@pytest.mark.asyncio
async def test_large_calculate_inputs_recover_without_recompute_and_keep_earlier_success(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=12)
    store, tools, context = setup(tmp_path, cfg)
    events = []
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        # More than the previous last-32 retention cap; ordered full inputs are
        # deliberately too large to fit in a compact result view.
        for index in range(40):
            await runtime._dispatch('manager', 'calculate', {'operation': 'sum', 'values': [str(index)] * 1000})
        await runtime._dispatch('manager', 'calculate', {'operation': 'divide', 'values': [1, 0]})
        await runtime._dispatch('manager', 'calculate', {'operation': 'divide', 'values': [9, 3]})
        saved = await runtime._save_checkpoint({'summary': '保留成功和失败，未作方向推断。'})
        assert len(saved['calculations']) == 42
        assert len(saved['calculations'][0]['arguments']['values']) == 1000
    finally:
        await runtime.close()
    original = copy.deepcopy(saved)
    payloads = []
    async def handler(request):
        payloads.append(json.loads(request.content))
        summaries = seed(payloads[-1])['calculation_summaries']
        assert len(summaries) == 42
        assert summaries[0]['result'] == '0' and summaries[39]['result'] == '39000'
        assert summaries[-2]['status'] == 'failed' and summaries[-1]['result'] == '3'
        assert all(item['source_task_id'] == context['job_id'] for item in summaries)
        assert all(item['agent_id'] == 'manager' for item in summaries)
        return output('responses', [('finish_analysis', {'report': '从已保存计算恢复。'})])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    resumed = agents.ResearchRuntime({**context, 'job_id': 'resumed-job', 'synthesis_checkpoint': saved}, cfg, tools, emit)
    try:
        restored = resumed.calculations[39]['summary']
        original_input = recall(resumed.manager_session, record_id=restored['full_inputs']['record_id'], length=12000)
        full_text = original_input['text']
        while original_input['next_start'] is not None:
            original_input = recall(resumed.manager_session, record_id=restored['full_inputs']['record_id'],
                start=original_input['next_start'], length=12000)
            full_text += original_input['text']
        assert len(json.loads(full_text)['arguments']['values']) == 1000
        resumed.calls = cfg['max_model_calls'] - 2
        resumed.synthesis_started_calls = resumed.calls
        result = await resumed.run()
        assert result['synthesis_recovered'] and len(payloads) == 1
        assert saved == original
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_small_budget_reports_exact_omissions_without_false_recall_permission(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_context_chars=10000, max_model_calls=3)
    store, tools, context = setup(tmp_path, cfg)
    async def emit(event):
        pass
    payloads = []
    async def handler(request):
        payloads.append(json.loads(request.content))
        return output('responses', [('finish_analysis', {'report': '未在最终上下文纳入的计算保持明确缺口。'})])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    for index in range(40):
        await runtime._dispatch('manager', 'calculate', {'operation': 'sum', 'values': [index, 2]})
    runtime.calls = 1
    result = await runtime.run()
    visible = seed(payloads[-1])
    assert visible['calculation_indexes']['omitted_summary_count'] > 0
    assert len(visible['calculation_summaries']) + visible['calculation_indexes']['omitted_summary_count'] == 40
    assert '不能调用 recall_context' in visible['calculation_access']
    coverage = result['calculation_coverage'][-1]['calculations']
    assert len(coverage) == 40 and any(item['visibility'] != 'summary' for item in coverage)
    assert len(json.dumps(payloads[-1]['input'], ensure_ascii=False)) < 10000


@pytest.mark.asyncio
async def test_prepare_keeps_distinct_windows_and_permits_actual_input_recall(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=12)
    store, tools, context = setup(tmp_path, cfg)
    asset = source(store, 'SZ:159278', 0, count=20)
    benchmark = source(store, 'CSI300', 0, count=20, benchmark=True)
    requests, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return output('responses', [('market_indicators', {'material_id': asset['id'],
                'benchmark_material_id': benchmark['id'], 'window': window,
                'start': start, 'end': '2026-01-20'}) for window, start in [(5, '2026-01-01'), (10, '2026-01-10')]])
        if len(requests) == 2:
            return output('responses', [('prepare_synthesis', {'summary': '两个不同窗口保持分别解释。',
                'material_ids': [asset['id'], benchmark['id']]})])
        summaries = seed(payload)['calculation_summaries']
        assert len(summaries) == 2
        assert {item['window'] for item in summaries} == {5, 10}
        assert {item['valid_samples'] for item in summaries} == {20, 11}
        assert len({item['calculation_id'] for item in summaries}) == 2
        assert all(item['relative_return'].startswith('-') for item in summaries)
        if len(requests) == 3:
            assert 'recall_context' in {tool['name'] for tool in payload['tools']}
            return output('responses', [('recall_context', {'record_id': summaries[1]['full_inputs']['record_id'], 'length': 6000})])
        recalled = next(json.loads(item['output']) for item in payload['input']
                        if item.get('type') == 'function_call_output' and 'next_start' in item['output'])
        original = json.loads(recalled['text'])
        assert original['arguments']['window'] == 10
        assert len(original['output']['input_closes']) == 11
        return output('responses', [('finish_analysis', {'report': '已取回第二个窗口的完整输入。'})])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert len(requests) == 4 and result['model_calls'] == 4
    assert len(result['calculation_coverage'][-1]['calculations']) == 2
    assert all(item['visibility'] == 'summary' for item in result['calculation_coverage'][-1]['calculations'])
    assert any(event['type'] == 'synthesis_context_projected' and event['payload']['reason'] == 'prepare_synthesis' for event in events)


@pytest.mark.asyncio
async def test_parallel_children_publish_calculations_before_shared_budget_closeout(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=18, max_agent_turns=18, max_concurrent_agents=2)
    store, tools, context = setup(tmp_path, cfg)
    requests, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        visible = seed(payload)
        identity = visible['runtime_status']['agent_id']
        names = {tool['name'] for tool in payload['tools']}
        await asyncio.sleep(.002)
        if identity == 'manager':
            if len(requests) == 1:
                return output('responses', [('spawn_research', {'task': f'Controlled arithmetic {i}'}) for i in range(2)])
            if names == {'finish_analysis'}:
                summaries = visible['calculation_summaries']
                assert len({item['agent_id'] for item in summaries}) == 2
                assert all(item['result'] == '37' for item in summaries)
                assert all(item['research_round'] == 1 for item in summaries)
                assert all(item['full_inputs']['session_agent_id'] == 'manager' for item in summaries)
                assert all(child['status'] == 'completed' for child in visible['runtime_status']['children'])
                return output('responses', [('finish_analysis', {'report': '两位子研究者实际交付的计算可见。'})])
            return output('responses', [('wait_agents', {'timeout_seconds': .01})])
        if names == {'submit_report'}:
            assert visible['calculation_summaries']
            assert all(item['agent_id'] == identity for item in visible['calculation_summaries'])
            return output('responses', [('submit_report', {'summary': '已完成计算，来源口径未另行推断。'})])
        return output('responses', [('calculate', {'operation': 'sum', 'values': [17, 20]})])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] <= 18
    assert len(result['agents']) == 2
    assert all(child['reports'][0]['calculation_ids'] for child in result['agents'])
    final = [item for item in result['calculation_coverage'] if item['agent_id'] == 'manager'][-1]
    assert all(item['visibility'] == 'summary' for item in final['calculations'])
    assert any(event['type'] == 'budget_closeout' for event in events)


@pytest.mark.asyncio
async def test_late_child_computation_reaches_the_single_final_update(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=10, max_agent_turns=12)
    store, tools, context = setup(tmp_path, cfg)
    parent_started, child_waiting, child_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    manager_requests, child_requests = [], []
    async def handler(request):
        payload = json.loads(request.content)
        visible = seed(payload)
        if visible.get('task'):
            child_requests.append(payload)
            if len(child_requests) == 1:
                return output('responses', [('calculate', {'operation': 'subtract', 'values': [100, 7]})])
            child_waiting.set()
            await parent_started.wait()
            assert visible['calculation_summaries'][0]['result'] == '93'
            return output('responses', [('submit_report', {'summary': '迟到但已完成的确定性结果。'})])
        manager_requests.append(payload)
        if len(manager_requests) == 1:
            assert not visible.get('calculation_summaries')
            parent_started.set()
            await child_done.wait()
            return output('responses', [('finish_analysis', {'report': '第一次请求时尚未交付子成果。'})])
        assert len(manager_requests) == 2
        assert {tool['name'] for tool in payload['tools']} == {'finish_analysis'}
        summaries = visible['calculation_summaries']
        assert len(summaries) == 1 and summaries[0]['result'] == '93'
        assert summaries[0]['agent_id'] != 'manager'
        return output('responses', [('finish_analysis', {'report': '这一次更新已收到迟到子成果。'})])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        if event['type'] == 'agent_completed' and event['agent_id'] != 'manager':
            child_done.set()
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    runtime.closeout_waited = True
    await runtime._spawn({'task': 'Arithmetic child'})
    await asyncio.wait_for(child_waiting.wait(), 2)
    runtime.calls = 7
    result = await runtime.run()
    assert len(manager_requests) == 2 and result['late_child_update_performed']
    assert result['model_calls'] == 9 <= 10
    final = [item for item in result['calculation_coverage'] if item['agent_id'] == 'manager'][-1]
    assert len(final['calculations']) == 1 and final['calculations'][0]['visibility'] == 'summary'
