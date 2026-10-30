"""Scaled time tests exercise the real client/parser/runtime, not asyncio copies.

All streams are controlled HTTPX transports; these are not provider results.
"""
import asyncio
import json
import time

import httpx
import pytest

from app import agents
from app.config import defaults, migrate_timeout_policy, timeout_policy, timeout_policy_warnings
from app.model import ModelClient, ModelError, ModelTimeoutError


def config(**extra):
    return {'model_name': 'controlled-timeout-fixture', 'model_base_url': 'http://localhost/v1',
            'model_allow_no_key': True, 'model_key_env': 'UNUSED_ELEVENTH_KEY',
            'model_protocol': 'responses', 'reasoning_effort': 'max',
            'model_native_search': False, 'model_retries': 3,
            'model_first_event_timeout_seconds': .06,
            'model_stream_idle_timeout_seconds': .06,
            'model_request_hard_timeout_seconds': .6,
            'synthesis_request_hard_timeout_seconds': .6, **extra}


def event(kind, **fields):
    return ('data: ' + json.dumps({'type': kind, **fields}) + '\n\n').encode()


def terminal(output=None):
    if output is None:
        output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': '完整报告'}]}]
    return event('response.completed', response={'id': 'controlled', 'status': 'completed', 'output': output})


class TimedStream(httpx.AsyncByteStream):
    def __init__(self, sequence):
        self.sequence, self.closed = sequence, False

    async def __aiter__(self):
        for delay, chunk in self.sequence:
            await asyncio.sleep(delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def client_for(sequence, **options):
    streams, payloads = [], []
    def handler(request):
        payloads.append(json.loads(request.content))
        stream = TimedStream(sequence)
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-request-id': 'controlled-request-id'}, stream=stream)
    return ModelClient(config(**options), transport=httpx.MockTransport(handler)), streams, payloads


@pytest.mark.parametrize('phase,legacy', [('research', .09), ('synthesis', .12)])
async def test_fast_first_event_continuous_stream_survives_old_absolute_limit(phase, legacy):
    seq = [(.02, event('response.output_text.delta', delta='visible')) for _ in range(10)] + [(0, terminal())]
    client, streams, payloads = client_for(seq, model_request_timeout_seconds=legacy, synthesis_request_timeout_seconds=legacy)
    try:
        session = client.session('system', {}); session.phase = phase
        assert (await client.complete(session, [])).text == '完整报告'
        row = client.requests[0]
        assert len(payloads) == 1 and row['duration_ms'] > legacy * 1000
        assert row['stream_first_effective_event_ms'] < 60 and row['stream_max_idle_ms'] < 60
        assert row['stream_terminal'] == 'response.completed' and row['error_code'] is None
        assert row['request_id'] == 'controlled-request-id'
        assert row['reasoning_effort_sent'] == 'max' and row['cost_known'] is False
        assert streams[0].closed
    finally:
        await client.aclose()


@pytest.mark.parametrize('sequence,overrides,code', [
    ([(.15, terminal())], {}, 'first_event_timeout'),
    ([(0, event('response.created', response={'id': 'controlled'})), (.15, terminal())], {}, 'first_event_timeout'),
    ([(.01, b': ping\n\n')] * 20, {}, 'first_event_timeout'),
    ([(0, event('response.output_text.delta', delta='visible')), (.15, terminal())], {}, 'stream_idle_timeout'),
    ([(0, event('response.output_text.delta', delta='visible'))] + [(.01, b': ping\n\n')] * 20, {}, 'stream_idle_timeout'),
    ([(.01, event('response.output_text.delta', delta='visible'))] * 20, {'model_request_hard_timeout_seconds': .075}, 'request_hard_timeout'),
])
async def test_distinct_timeout_layers_never_retry_accepted_generation(sequence, overrides, code):
    client, streams, payloads = client_for(sequence, **overrides)
    try:
        session = client.session('system', {})
        with pytest.raises(ModelTimeoutError) as error:
            await client.complete(session, [])
        assert error.value.code == code
        row = client.requests[0]
        assert row['error_code'] == row['cancelled_by'] == code
        assert len(payloads) == 1 and streams[0].closed
        assert len(session.history) == 1 and not session.pending_calls
        assert '检查点恢复' not in str(error.value)
        if code == 'stream_idle_timeout':
            assert row['stream_last_progress_ms'] is not None
            assert row['stream_max_idle_ms'] >= 50
    finally:
        await client.aclose()


@pytest.mark.parametrize('layer', ['child_budget_exhausted', 'analysis_budget_exhausted'])
async def test_parent_deadline_is_not_reported_as_upstream_timeout(layer):
    client, streams, payloads = client_for([(.01, event('response.output_text.delta', delta='visible'))] * 20)
    try:
        session = client.session('system', {})
        session.deadlines[layer] = time.monotonic() + .045
        with pytest.raises(ModelTimeoutError) as error:
            await client.complete(session, [])
        assert error.value.code == layer
        row = client.requests[0]
        assert row['effective_limit'] == row['cancelled_by'] == row['error_code'] == layer
        assert 'ReadTimeout' not in json.dumps(row)
        assert len(payloads) == 1 and streams[0].closed
    finally:
        await client.aclose()


async def test_native_tool_progress_without_text_and_fragmented_function_terminal():
    call = {'type': 'function_call', 'call_id': 'only-call', 'name': 'calculate', 'arguments': '{"values":[1,2]}'}
    end = terminal([call])
    sequence = [(.02, event('response.web_search_call.in_progress', item_id='search')),
                (.03, event('response.web_search_call.searching', item_id='search')),
                (.03, event('response.web_search_call.completed', item_id='search'))]
    sequence += [(0, end[index:index + 7]) for index in range(0, len(end), 7)]
    client, streams, _ = client_for(sequence)
    try:
        session = client.session('system', {})
        turn = await client.complete(session, [])
        assert not turn.text and turn.calls[0].arguments == {'values': [1, 2]}
        row = client.requests[0]
        assert row['stream_terminal'] == 'response.completed' and row['stream_first_effective_event_ms'] < 60
        assert row['stream_last_progress_kind'] == 'response.completed' and streams[0].closed
    finally:
        await client.aclose()


async def test_connection_retry_is_bounded_and_checks_remaining_parent_budget(monkeypatch):
    attempts, budget_calls = [], []
    async def budget():
        budget_calls.append(True)
    async def no_backoff(_):
        pass
    monkeypatch.setattr('app.model.asyncio.sleep', no_backoff)
    def handler(request):
        attempts.append(True)
        if len(attempts) == 1:
            raise httpx.ConnectError('controlled unreachable', request=request)
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'complete'}]}]})
    client = ModelClient(config(), budget, httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        await client.complete(session, [])
        assert len(attempts) == len(budget_calls) == 2
        assert client.requests[0]['retry_delay_seconds'] == 1
        attempts.clear(); budget_calls.clear()
        session = client.session('system', {})
        session.deadlines['analysis_budget_exhausted'] = time.monotonic() + .2
        with pytest.raises(ModelError, match='ConnectError'):
            await client.complete(session, [])
        assert len(attempts) == len(budget_calls) == 1
        assert client.requests[-1]['retry_suppressed'] == 'insufficient_parent_budget'
    finally:
        await client.aclose()


def test_legacy_migration_is_explicit_without_reinterpreting_frozen_snapshots():
    old = {'model_request_timeout_seconds': 90, 'synthesis_request_timeout_seconds': 120,
           'agent_timeout_seconds': 120, 'analysis_timeout_seconds': 600}
    migrated = migrate_timeout_policy(old)
    assert timeout_policy(old)['request_hard_seconds'] == 90
    assert timeout_policy(old, 'synthesis')['request_hard_seconds'] == 120
    assert old['analysis_timeout_seconds'] == 600
    assert migrated['model_request_hard_timeout_seconds'] == 300
    assert migrated['synthesis_request_hard_timeout_seconds'] == 360
    assert migrated['agent_timeout_seconds'] == 600 and migrated['analysis_timeout_seconds'] == 1200
    assert migrate_timeout_policy(migrated) == migrated
    custom = migrate_timeout_policy({**old, 'model_request_timeout_seconds': 180, 'agent_timeout_seconds': 90})
    assert custom['model_request_hard_timeout_seconds'] == 180 and custom['agent_timeout_seconds'] == 90
    assert any('子任务总预算短于' in text for text in timeout_policy_warnings(custom))
    assert all(key in defaults() for key in ('model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds'))


async def test_runtime_child_queue_and_execution_have_separate_observations(monkeypatch):
    # Exercise the actual child semaphore and deadline, with actual ModelClient.
    from test_agents import ResearchTools
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'},
        stream=TimedStream([(.01, event('response.output_text.delta', delta='visible'))] * 30)))
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    events = []
    async def emit(value): events.append(value)
    runtime = agents.ResearchRuntime({'job_id': 'controlled-child'},
        config(agent_timeout_seconds=.09, analysis_timeout_seconds=2, max_model_calls=20), ResearchTools(), emit)
    runtime.deadline = time.monotonic() + 2
    try:
        await runtime.semaphore.acquire()
        await runtime.semaphore.acquire()
        await runtime.semaphore.acquire()
        result = await runtime._spawn({'task': 'bounded child'})
        await asyncio.sleep(.025)
        runtime.semaphore.release()
        child = runtime.children[result['agent_id']]
        await child.task_handle
        assert child.status == 'failed'
        row = child.session.requests[0]
        assert row['error_code'] == 'child_budget_exhausted'
        assert row['queue_wait_ms'] >= 20 and row['duration_ms'] < 90
        failure = next(e for e in events if e['type'] == 'agent_failed')
        assert failure['payload']['error_code'] == 'child_budget_exhausted'
    finally:
        await runtime.close()


async def test_runtime_inherited_account_deadline_includes_time_before_model(monkeypatch):
    from test_agents import ResearchTools
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'},
        stream=TimedStream([(.01, event('response.output_text.delta', delta='visible'))] * 30)))
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    events = []
    async def emit(value): events.append(value)
    runtime = agents.ResearchRuntime({'job_id': 'controlled-account', '_analysis_deadline': time.monotonic() + .06},
        config(analysis_timeout_seconds=2), ResearchTools(), emit)
    with pytest.raises(agents.ResearchTimeout):
        await runtime.run()
    assert runtime.client.requests[0]['error_code'] == 'analysis_budget_exhausted'
    assert runtime.client.requests[0]['duration_ms'] < 100
    assert '_analysis_deadline' not in json.dumps(runtime.manager_session.history)
    assert any(e['payload'].get('error_code') == 'analysis_budget_exhausted' for e in events)


@pytest.mark.parametrize('source', ['user_cancelled', 'input_version_updated', 'service_shutdown'])
async def test_explicit_cancellation_keeps_source_and_never_retries(source):
    client, streams, payloads = client_for([(.3, terminal())])
    try:
        session = client.session('system', {})
        task = asyncio.create_task(client.complete(session, []))
        await asyncio.sleep(.01)
        task.cancel(source)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(payloads) == 1 and streams[0].closed
        assert client.requests[0]['cancelled_by'] == source
        assert client.requests[0]['error_code'] == 'cancelled'
        assert len(session.history) == 1 and not session.pending_calls
    finally:
        await client.aclose()


@pytest.mark.parametrize('error,code', [(httpx.ConnectTimeout, 'connect_timeout'),
    (httpx.PoolTimeout, 'pool_timeout'), (httpx.WriteTimeout, 'write_timeout'),
    (httpx.ReadTimeout, 'network_read_timeout')])
async def test_transport_phases_have_distinct_stable_codes(error, code):
    def handler(request):
        raise error('controlled fixture', request=request)
    client = ModelClient(config(model_retries=0), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as failure:
            await client.complete(client.session('system', {}), [])
        assert failure.value.code == client.requests[0]['error_code'] == code
        assert client.requests[0]['response_headers_received'] is False
        assert client.requests[0]['cost_known'] is False
    finally:
        await client.aclose()


def test_recovery_diagnostic_does_not_consume_attempt_or_promise_without_evidence(tmp_path):
    from app.domain import AccountInput
    from app.store import Store
    store = Store(tmp_path / 'read-only-recovery.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='diagnostic', cash='100000'))
    job = store.claim_jobs(account_id=account['id'])[0]
    context = {'account': account, 'universe_version': store.universe_snapshot()['universe_version']}
    assert store.checkpoint_recovery_diagnostic(job, context)['state'] == 'no_checkpoint'
    # A genuine saved visible manager calculation/summary needs no invented
    # source receipt. The original guard machinery decides source coverage.
    store.save_synthesis_checkpoint(job, {'manager_summary': {'summary': 'Only local arithmetic'},
        'partial_research': True, 'material_ids': []}, context)
    before = store.checkpoint_status(job['id'])['resume_count']
    diagnostic = store.checkpoint_recovery_diagnostic(job, context)
    assert diagnostic['state'] == 'partial_synthesis_ready' and diagnostic['eligible']
    assert store.checkpoint_recovery_diagnostic(job)['state'] == 'requires_revalidation'
    assert store.checkpoint_status(job['id'])['resume_count'] == before
    store.save_universe({'candidates': ['SH:600519'], 'watchlist': []})
    diagnostic = store.checkpoint_recovery_diagnostic(job, context)
    assert diagnostic['state'] == 'invalidated' and not diagnostic['eligible']
    assert 'universe_changed' in diagnostic['reasons']
    assert store.checkpoint_status(job['id'])['resume_count'] == before


def test_new_job_freezes_migrated_policy_and_current_edits_do_not_change_old_snapshot(tmp_path):
    from app.domain import AccountInput
    from app.store import Store
    store = Store(tmp_path / 'frozen-timeout-policy.sqlite3')
    old = {'debounce_seconds': 0, 'quote_provider': 'manual',
        'model_request_timeout_seconds': 90, 'synthesis_request_timeout_seconds': 120,
        'agent_timeout_seconds': 120, 'analysis_timeout_seconds': 600}
    store.set_meta('settings', old)
    account = store.save_account(AccountInput(name='frozen', cash='100000'))
    job = store.claim_jobs(account_id=account['id'])[0]
    frozen = job['config_snapshot']
    assert frozen['model_request_hard_timeout_seconds'] == 300
    assert frozen['synthesis_request_hard_timeout_seconds'] == 360
    assert frozen['agent_timeout_seconds'] == 600
    store.set_meta('settings', {**store.settings(), 'synthesis_request_hard_timeout_seconds': 500})
    assert store.settings()['synthesis_request_hard_timeout_seconds'] == 500
    assert store.job(job['id'])['config_snapshot'] == frozen
    assert frozen['reasoning_effort'] == store.job(job['id'])['config_snapshot']['reasoning_effort']


async def test_manager_does_not_start_ordinary_request_that_could_consume_final_reserve(monkeypatch):
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert {tool.get('name') for tool in payload['tools']} == {'finish_analysis'}
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': 'final', 'name': 'finish_analysis',
             'arguments': json.dumps({'report': '预算内汇总；尚未完成的研究保持未知。'})}]})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    async def emit(value):
        pass
    cfg = config(model_request_hard_timeout_seconds=.3,
        synthesis_request_hard_timeout_seconds=.36, analysis_timeout_seconds=1.2,
        max_model_calls=20, max_agent_turns=12)
    runtime = agents.ResearchRuntime({'job_id': 'reserve-boundary'}, cfg, object(), emit)
    try:
        # Reserve .36 + an ordinary request .30: .70 has room, .64 does not.
        runtime.deadline = time.monotonic() + .70
        assert not runtime._shared_closeout()
        runtime.inherited_deadline = time.monotonic() + .64
        result = await runtime.run()
        assert result['report'].startswith('预算内汇总')
        assert len(sent) == 1
        assert runtime.client.requests[0]['stage'] == 'synthesis'
        assert runtime.client.requests[0]['timeout_policy']['request_hard_seconds'] == .36
    finally:
        await runtime.close()
