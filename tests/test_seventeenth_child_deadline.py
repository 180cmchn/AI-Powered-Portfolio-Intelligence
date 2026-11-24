"""Child deadlines use real Runtime/ModelClient paths and one controlled clock.

Only HTTP responses and elapsed time are fixtures. No supplier request, loop
replacement, extra call cap, or extension of the production deadline is used.
"""
import asyncio
import json

import httpx
import pytest

from app import agents
from app.model import ModelClient
from tests.test_fourteenth_deadlines import ControlledDeadlineClock, wait_for_stage


def response(name, number):
    if name == 'research':
        output = [{'type': 'web_search_call', 'id': f'search-{number}', 'status': 'completed',
                   'action': {'type': 'search', 'query': f'controlled evidence {number}'}}]
    else:
        output = [{'type': 'function_call', 'call_id': f'report-{number}', 'name': 'submit_report',
                   'arguments': json.dumps({'summary': f'Visible completed report {number}; missing facts remain unknown.'})}]
    return httpx.Response(200, json={'id': f'response-{number}', 'status': 'completed', 'output': output})


def only_submission(payload):
    return [(item['type'], item.get('name')) for item in payload['tools']] == [('function', 'submit_report')]


def assert_submission(payload):
    assert only_submission(payload)
    assert payload['tool_choice'] == {'type': 'function', 'name': 'submit_report'}
    assert 'web_search_call.action.sources' not in payload['include']


def runtime_fixture(monkeypatch, handler, emit, **overrides):
    cfg = {'model_base_url': 'https://child-deadline-fixture.invalid/v1',
        'model_name': 'controlled-child-model', '_model_api_key': 'local-fixture-only',
        'model_protocol': 'responses', 'reasoning_effort': 'high', 'model_native_search': True,
        'model_retries': 0, 'max_model_calls': None, 'max_agent_turns': None,
        'synthesis_max_calls': None, 'max_context_chars': 240000,
        'analysis_timeout_seconds': 1000, 'agent_timeout_seconds': 100,
        'model_request_hard_timeout_seconds': 80,
        'synthesis_request_hard_timeout_seconds': 80,
        'model_first_event_timeout_seconds': 90, 'model_stream_idle_timeout_seconds': 300,
        **overrides}
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
                        ModelClient(config, before_request, transport))
    return agents.ResearchRuntime({'task_id': 'child-deadline-fixture',
        'account': {'id': 'controlled-account', 'version': 1}}, cfg, object(), emit)


@pytest.mark.parametrize('client_hard,elapsed,reserve', [(80, 60, 40), (10, 80, 20)])
async def test_child_uses_actual_client_reserve_and_submits_before_shared_closeout(
        monkeypatch, client_hard, elapsed, reserve):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests, events = [], []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert not only_submission(payload) and {'type': 'web_search'} in payload['tools']
            return response('research', 1)
        assert len(requests) == 2
        assert_submission(payload)
        return response('submit', 2)

    async def emit(event):
        events.append(event)
        if event['type'] == 'model_usage' and len(requests) == 1:
            timeline.advance(elapsed)  # Completed HTTP; elapsed research work remains inside the child round.

    runtime = runtime_fixture(monkeypatch, handler, emit)
    runtime.deadline = timeline.start + 1000
    # The selected child's actual client can have a different request policy
    # from the manager/global configuration; the reserve must use that client.
    runtime.client.config['model_request_hard_timeout_seconds'] = client_hard
    try:
        created = await runtime._spawn({'task': 'bounded child research'})
        child = runtime.children[created['agent_id']]
        await child.task_handle
        assert child.status == 'completed' and len(child.reports) == 1
        assert child.deadline_closing and not child.session.native_search
        assert child.round_time_budget == 100 and child.submission_reserve_seconds == reserve
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 100
        assert runtime.calls == len(requests) == 2
        assert not runtime.budget_closing and runtime.closeout_reasons == set()
        assert not runtime._shared_closeout() and runtime.manager_session.native_search
        assert sum(event['type'] == 'tool_started' and event['payload']['tool'] == 'submit_report'
                   for event in events) == 1
    finally:
        await runtime.close()


async def test_parent_clipping_reduces_actual_child_round_before_reserve_is_computed(monkeypatch):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response('research', 1)
        assert len(requests) == 2
        assert_submission(payload)
        return response('submit', 2)

    async def emit(event):
        if event['type'] == 'model_usage' and len(requests) == 1:
            timeline.advance(36)

    runtime = runtime_fixture(monkeypatch, handler, emit,
        model_request_hard_timeout_seconds=2, synthesis_request_hard_timeout_seconds=5)
    runtime.deadline = timeline.start + 70  # Parent reserve 10 leaves this child 60, not configured 100.
    runtime.client.config['model_request_hard_timeout_seconds'] = 80
    try:
        created = await runtime._spawn({'task': 'parent-clipped child'})
        child = runtime.children[created['agent_id']]
        await child.task_handle
        assert child.status == 'completed'
        assert child.round_time_budget == 60 and child.submission_reserve_seconds == 24
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 60
        assert not runtime.budget_closing and not runtime._shared_closeout()
    finally:
        await runtime.close()


async def test_send_guard_rebuilds_when_child_enters_reserve_after_payload_preparation(monkeypatch):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert_submission(payload)
        return response('submit', 1)

    async def emit(event):
        pass

    runtime = runtime_fixture(monkeypatch, handler, emit)
    runtime.deadline = timeline.start + 1000
    before_request = runtime.client.before_request
    guard_calls = []

    async def late_guard():
        guard_calls.append(runtime.request_owner.get())
        if len(guard_calls) == 1:
            timeline.advance(60)
        await before_request()

    runtime.client.before_request = late_guard
    try:
        created = await runtime._spawn({'task': 'send boundary race'})
        child = runtime.children[created['agent_id']]
        await child.task_handle
        assert child.status == 'completed' and child.deadline_closing
        assert [final for _, final in guard_calls] == [False, True]
        assert runtime.calls == len(child.session.requests) == len(requests) == 1
        assert not runtime.budget_closing and runtime.manager_session.native_search
    finally:
        await runtime.close()


@pytest.mark.parametrize('queue_seconds', [65, 101])
async def test_semaphore_wait_consumes_original_child_deadline(monkeypatch, queue_seconds):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert_submission(payload)
        return response('submit', 1)

    async def emit(event):
        pass

    runtime = runtime_fixture(monkeypatch, handler, emit)
    runtime.deadline = timeline.start + 1000
    # Hold every slot, using the actual semaphore acquired by _run_child.
    held = runtime.semaphore._value
    for _ in range(held):
        await runtime.semaphore.acquire()
    try:
        created = await runtime._spawn({'task': 'queued child'})
        child = runtime.children[created['agent_id']]
        await asyncio.sleep(0)
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 100
        timeline.advance(queue_seconds)
        for _ in range(held):
            runtime.semaphore.release()
        await child.task_handle
        assert child.round_time_budget == 100 and child.submission_reserve_seconds == 40
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 100
        if queue_seconds < 100:
            assert child.status == 'completed' and len(requests) == 1
            assert child.session.queue_wait_ms == 65000
        else:
            assert child.status == 'failed' and requests == [] and runtime.calls == 0
        assert not runtime.budget_closing
    finally:
        await runtime.close()


async def test_stalled_final_submission_is_cancelled_at_unchanged_child_deadline(monkeypatch):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests, events, cancelled = [], [], []
    entered, never_release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response('research', 1)
        assert len(requests) == 2
        assert_submission(payload)
        entered.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        pytest.fail('The original child deadline must cancel this unfinished response')

    async def emit(event):
        events.append(event)
        if event['type'] == 'model_usage' and len(requests) == 1:
            timeline.advance(60)

    runtime = runtime_fixture(monkeypatch, handler, emit)
    runtime.deadline = timeline.start + 1000
    try:
        created = await runtime._spawn({'task': 'stalled final child response'})
        child = runtime.children[created['agent_id']]
        await wait_for_stage(child.task_handle, entered)
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 100
        timeline.advance(39)
        await asyncio.sleep(0)
        assert not child.task_handle.done()
        timeline.advance(1)
        await child.task_handle
        assert cancelled == [True] and child.status == 'failed' and child.reports == []
        assert len(requests) == runtime.calls == 2
        assert child.session.requests[-1]['cancelled_by'] == 'child_budget_exhausted'
        assert any(event['type'] == 'agent_failed' and
                   event['payload'].get('error_code') == 'child_budget_exhausted' for event in events)
        assert not runtime.budget_closing and runtime.manager_session.native_search
    finally:
        await runtime.close()


async def test_follow_up_resets_only_child_round_closeout_and_restores_research_tools(monkeypatch):
    timeline = ControlledDeadlineClock(monkeypatch, 2000)
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response('research', 1)
        if len(requests) == 2:
            assert_submission(payload)
        else:
            assert len(requests) == 3 and not only_submission(payload)
            assert {'type': 'web_search'} in payload['tools'] and 'tool_choice' not in payload
            assert 'new follow-up evidence' in json.dumps(payload['input'])
        return response('submit', len(requests))

    async def emit(event):
        if event['type'] == 'model_usage' and len(requests) == 1:
            timeline.advance(60)

    runtime = runtime_fixture(monkeypatch, handler, emit)
    runtime.deadline = timeline.start + 1000
    try:
        created = await runtime._spawn({'task': 'initial child round'})
        child = runtime.children[created['agent_id']]
        await child.task_handle
        original_session = child.session
        assert child.deadline_closing and not child.session.native_search
        await runtime._dispatch('manager', 'follow_up', {'agent_id': child.id, 'task': 'new follow-up evidence'})
        await child.task_handle
        assert child.session is original_session and child.round == 2
        assert child.status == 'completed' and len(child.reports) == 2
        assert child.session.deadlines['child_budget_exhausted'] == timeline.start + 160
        assert not child.deadline_closing and child.session.native_search
        assert not runtime.budget_closing and runtime.closeout_reasons == set()
    finally:
        await runtime.close()
