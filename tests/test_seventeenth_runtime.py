"""Production Runtime + HTTP payload regressions; model responses are fixtures."""
import asyncio
import copy
import json

import httpx
import pytest

from app import agents
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, setup


def visible(payload):
    return payload.get('input', payload.get('messages'))


def objects(payload):
    return [json.loads(m['content']) for m in visible(payload) if m.get('role') == 'user']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_real_manager_continuations_append_state_and_only_new_calculations(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None, max_agent_turns=None,
                 synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    requests, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        n = len(requests)
        if n <= 4:
            return output(protocol, [('calculate', {'operation': 'sum', 'values': [n, 2]})])
        return output(protocol, [('finish_analysis', delivery_arguments(report='按完成条件交付。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 5
    first = copy.deepcopy(visible(requests[0])[0])
    for old, new in zip(requests, requests[1:]):
        assert visible(new)[:len(visible(old))] == visible(old)
        assert visible(new)[0] == first
        assert old['tools'] == new['tools']
        update = objects(new)[-1]
        assert update['runtime_status']['remaining_shared_model_calls'] is None
        assert update['runtime_status']['remaining_agent_turns'] is None
        assert len(update['calculation_summaries']) == 1
    final_calculations = [item for body in objects(requests[-1]) for item in body.get('calculation_summaries', [])]
    assert len(final_calculations) == len({item['calculation_id'] for item in final_calculations}) == 4
    assert {item['visibility'] for item in result['calculation_coverage'][-1]['calculations']} == {'summary'}
    records = [r for e in events if e['type'] == 'model_usage' for r in e['payload']['requests']]
    assert len({r['session_id'] for r in records}) == 1
    assert len({r['cache_epoch'] for r in records}) == 1


@pytest.mark.asyncio
async def test_unlimited_crosses_old_caps_and_stops_at_natural_submission(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None,
                 max_context_chars=1000000)
    store, tools, context = setup(tmp_path, cfg)
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 42:
            return output('responses', [('calculate', {'operation': 'sum', 'values': [calls, 3]})])
        return output('responses', [('finish_analysis', delivery_arguments(report='43次有用工具循环后正常交付。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    result = await agents.run_research(context, cfg, tools, emit)
    assert calls == result['model_calls'] == 43


@pytest.mark.asyncio
async def test_unlimited_identical_tool_rounds_close_for_no_progress(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    requests, events = [], []
    async def handler(request):
        p = json.loads(request.content); requests.append(p)
        if p.get('tool_choice'):
            assert [t['name'] for t in p['tools']] == ['finish_analysis']
            return output('responses', [('finish_analysis', delivery_arguments(report='重复计算无新进展，有限交付。'))])
        return output('responses', [('calculate', {'operation': 'sum', 'values': [1, 3]})])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == 4
    assert any(e['type'] == 'research_no_progress' for e in events)


@pytest.mark.asyncio
async def test_child_follow_up_keeps_prefix_and_other_child_state_is_not_injected(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    requests = []
    async def handler(request):
        payload = json.loads(request.content); requests.append(payload)
        n = len(requests)
        if n in (1, 2, 4):
            return output('responses', [('calculate', {'operation': 'sum', 'values': [n, 7]})])
        return output('responses', [('submit_report', {'summary': f'已回答当前专题第{n}轮；事实来源为受控算术。'})])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        child_info = await runtime._spawn({'task': '逐步计算并说明输入。'})
        await asyncio.gather(*list(runtime.handles))
        child = runtime.children[child_info['agent_id']]
        session_id = child.session.session_id
        assert child.status == 'completed'
        await runtime._dispatch('manager', 'follow_up', {'agent_id': child.id, 'task': '补充一个不同输入的计算。'})
        await asyncio.gather(*list(runtime.handles))
        assert child.status == 'completed' and len(child.reports) == 2
        assert child.session.session_id == session_id
        for old, new in zip(requests, requests[1:]):
            assert visible(new)[:len(visible(old))] == visible(old)
        assert len({r['session_id'] for r in child.session.requests}) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
async def test_report_receipt_waits_for_actual_endpoint_acceptance(tmp_path, monkeypatch, accepted):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"fixture"}}\n\n'
            raise httpx.ReadError('controlled missing terminal')
    async def handler(request):
        payload = json.loads(request.content)
        if objects(payload)[0].get('task'):
            return output('responses', [('submit_report', {'summary': 'Completed child evidence and adverse factor.'})])
        if not accepted:
            raise httpx.ConnectError('controlled before response headers')
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Interrupted())
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    events = []
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        await runtime._spawn({'task': 'Independent completed topic.'})
        await asyncio.gather(*list(runtime.handles))
        with pytest.raises(Exception):
            await runtime._loop('manager', runtime.manager_session, manager=True)
        receipts = store.checkpoint_report_deliveries(runtime.task_id)
        assert bool(receipts) is accepted
        actual = [e for e in events if e['type'] == 'research_context_delivery']
        assert bool(actual) is accepted
        assert not store.analyses(context['account']['id'])
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_compaction_retained_package_does_not_immediately_compact_itself(tmp_path, monkeypatch):
    from app.context import compact, serialized
    cfg = config(monkeypatch, max_context_chars=32000, max_model_calls=None, max_agent_turns=None)
    store, tools, context = setup(tmp_path, cfg)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        child_session = runtime.client.session('child', {})
        child = agents.AgentState('retained-child', 'topic', 'task', child_session, status='completed')
        child.reports = [{'report_id': 'report-'+str(i), 'summary': 'Complete visible fact; adverse factor. '*100,
                          'open_questions': ['Unresolved original filing scope.']} for i in range(5)]
        runtime.children[child.id] = child
        for n in range(20):
            await runtime._dispatch('manager', 'calculate', {'operation': 'sum', 'values': [n, 3]})
        runtime._pin_runtime_status(session, 'manager', None, False)
        session.history.append({'role': 'user', 'content': json.dumps({'read_excerpt': 'original visible context '*1500})})
        assert compact(session, 32000)
        assert len(serialized(session.history)) < 32000 * .78
        frozen = copy.deepcopy(session.history)
        assert not compact(session, 32000)
        assert session.history == frozen
        seed = json.loads(session.history[0]['content'])
        assert seed['runtime_status']['children'][0]['latest_report']['open_questions']
        packet = seed['retained_child_reports']
        assert packet['truncated_for_context'] and packet['record_id']
        assert 'calculation_summaries' in seed
    finally:
        await runtime.close()
