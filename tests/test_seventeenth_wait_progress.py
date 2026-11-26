"""Actual Runtime handles distinguish useful waits from repeated failed work.

HTTP completions are controlled local fixtures. A live child enters the real
ModelClient handler and remains pending on an Event; no status flag substitutes
for evidence of an active asyncio Task.
"""
import asyncio
import json

import httpx
import pytest

from app import agents
from app.model import ModelClient
from tests.context_helpers import effective_context
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import assert_hot_closeout, config, forced_function, output


def context():
    return {'account': {'id': 'controlled-account', 'version': 1, 'cash': '100', 'holdings': []}}


@pytest.mark.parametrize('tool', ['agent_status', 'wait_agents'])
@pytest.mark.parametrize('target', ['unknown', 'completed'])
async def test_unrelated_live_child_does_not_exempt_repeated_invalid_or_terminal_target(monkeypatch, tool, target):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None,
                 analysis_timeout_seconds=60, agent_timeout_seconds=40,
                 model_request_hard_timeout_seconds=20, synthesis_request_hard_timeout_seconds=20)
    entered, release = asyncio.Event(), asyncio.Event()
    requests, events = [], []
    selected_id = 'unknown-child-id'
    runtime, live = None, None

    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload['input'][0]['content'])
        if seed.get('task'):
            if seed['task'] == 'controlled live child':
                entered.set()
                await release.wait()
            return output('responses', [('submit_report', {'summary': '受控子任务已完成，保留现金并在取得价格资料后复核。'})])
        requests.append(payload)
        if forced_function(payload) == 'finish_analysis':
            assert_hot_closeout(payload, requests[-2], 'finish_analysis')
            return output('responses', [('finish_analysis', delivery_arguments(
                '重复查询未带来进展，有限交付并保留未决目标；不据此作交易。'))])
        assert len(requests) <= 5, 'Unrelated live child incorrectly exempts repeated unproductive calls'
        args = {'agent_id': selected_id} if tool == 'agent_status' else {'agent_ids': [selected_id], 'timeout_seconds': 0}
        return output('responses', [(tool, args)])

    async def emit(event):
        events.append(event)
        if event['type'] == 'research_no_progress':
            assert live.task_handle is not None and not live.task_handle.done()
            release.set()  # Let ordinary closeout observe the unrelated completion.

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    runtime = agents.ResearchRuntime(context(), cfg, object(), emit)
    try:
        if target == 'completed':
            created = await runtime._spawn({'task': 'controlled already completed child'})
            selected_id = created['agent_id']
            selected = runtime.children[selected_id]
            await asyncio.wait_for(selected.task_handle, 2)
            assert selected.status == 'completed' and selected.task_handle.done()
        created = await runtime._spawn({'task': 'controlled live child'})
        live = runtime.children[created['agent_id']]
        await asyncio.wait_for(entered.wait(), 2)
        result = await runtime.run()
        no_progress = [event for event in events if event['type'] == 'research_no_progress']
        assert len(no_progress) == 1
        assert no_progress[0]['payload']['identical_tool_rounds'] == 3
        assert len(requests) == 4
        assert_hot_closeout(requests[-1], requests[-2], 'finish_analysis')
        child = next(item for item in effective_context(requests[-1])['runtime_status']['children']
                     if item['agent_id'] == live.id)
        assert child['status'] == 'completed'
        assert child['latest_report']['summary'] == '受控子任务已完成，保留现金并在取得价格资料后复核。'
        assert result['submission']['action'] == 'finish_analysis'
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize('selection', ['wait_all', 'wait_selected', 'status_selected'])
async def test_confirmed_selected_live_handle_can_be_waited_past_old_turn_cap(monkeypatch, selection):
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None,
                 max_context_chars=1000000, analysis_timeout_seconds=60, agent_timeout_seconds=40,
                 model_request_hard_timeout_seconds=20, synthesis_request_hard_timeout_seconds=20)
    entered, release = asyncio.Event(), asyncio.Event()
    requests, events = [], []
    live = None

    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload['input'][0]['content'])
        if seed.get('task'):
            entered.set()
            await release.wait()
            return output('responses', [('submit_report', {'summary': '受控真实活跃句柄现已完成；数据缺口局部保留。'})])
        requests.append(payload)
        assert forced_function(payload) != 'finish_analysis', 'Useful live-handle wait was prematurely forced to stop'
        if len(requests) <= 14:
            assert live.task_handle is not None and not live.task_handle.done()
            if len(requests) == 14:
                release.set()
                await asyncio.wait_for(live.task_handle, 2)
            if selection == 'status_selected':
                return output('responses', [('agent_status', {'agent_id': live.id})])
            return output('responses', [('wait_agents', {'agent_ids': [] if selection == 'wait_all' else [live.id],
                                                          'timeout_seconds': 0})])
        assert len(requests) == 15 and live.status == 'completed'
        child = next(item for item in effective_context(payload)['runtime_status']['children']
                     if item['agent_id'] == live.id)
        assert child['status'] == 'completed'
        assert child['latest_report']['summary'] == '受控真实活跃句柄现已完成；数据缺口局部保留。'
        return output('responses', [('finish_analysis', delivery_arguments(
            '已等到本轮子研究实际完成后交付；保留现金，资料缺口只限制相应判断。'))])

    async def emit(event):
        events.append(event)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    runtime = agents.ResearchRuntime(context(), cfg, object(), emit)
    try:
        created = await runtime._spawn({'task': 'controlled selected live child'})
        live = runtime.children[created['agent_id']]
        await asyncio.wait_for(entered.wait(), 2)
        result = await runtime.run()
        assert len(requests) == 15  # Exceeds the previous 12-turn default.
        assert not any(event['type'] == 'research_no_progress' for event in events)
        assert live.status == 'completed' and live.task_handle.done()
        assert result['submission']['action'] == 'finish_analysis'
    finally:
        release.set()
        await runtime.close()
