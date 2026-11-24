"""Actual ModelClient payloads and reported counters; no claims of live cache hits."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from app.model import ModelClient, ModelError


def config(protocol='responses', **extra):
    return {'model_protocol': protocol, 'model_name': 'grok-fixture',
        'model_base_url': 'http://localhost:19001/v1', 'model_allow_no_key': True,
        'model_key_env': 'UNUSED_SEVENTEENTH_MODEL_KEY', 'model_retries': 0,
        'model_native_search': False, 'reasoning_effort': 'xhigh',
        'max_context_chars': 240000, **extra}


def response(protocol='responses', usage=None):
    if protocol == 'responses':
        return {'id': 'fixture-response', 'status': 'completed', 'usage': usage,
            'output': [{'type': 'reasoning', 'encrypted_content': 'OPAQUE-DO-NOT-LOG', 'summary': []},
                       {'type': 'message', 'content': [{'type': 'output_text', 'text': 'visible answer'}]}]}
    return {'id': 'fixture-chat', 'usage': usage, 'choices': [{'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': 'visible answer', 'reasoning_content': 'OPAQUE-DO-NOT-LOG'}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_actual_payload_append_prefix_instance_key_and_opaque_preservation(protocol):
    sent = []
    def handler(request):
        sent.append((json.loads(request.content), dict(request.headers), bytes(request.content)))
        return httpx.Response(200, json=response(protocol))
    client = ModelClient(config(protocol), transport=httpx.MockTransport(handler))
    try:
        session = client.session('stable system', {'account': {'name': 'private-a'}, 'task_id': 'task-a'})
        session.agent_id = 'market-instance-1'
        await client.complete(session, [])
        client.add_user(session, {'follow_up': 'verify changed price', 'round': 2})
        await client.complete(session, [])
        client.add_user(session, {'follow_up': 'summarize new source', 'round': 3})
        await client.complete(session, [])
        other = client.session('stable system', {'account': {'name': 'private-b'}, 'task_id': 'task-b'})
        other.agent_id = session.agent_id
        await client.complete(other, [])
        input_key = 'input' if protocol == 'responses' else 'messages'
        for previous, current in zip(sent[:2], sent[1:3]):
            assert current[0][input_key][:len(previous[0][input_key])] == previous[0][input_key]
        keys = [payload.get('prompt_cache_key') if protocol == 'responses' else headers.get('x-grok-conv-id')
                for payload, headers, _ in sent]
        assert len(set(keys[:3])) == 1 and keys[0] != keys[3]
        assert all('private' not in key and 'market' not in key for key in keys)
        assert 'x-grok-conv-id' not in client.http.headers
        assert client.requests[0]['session_id'] != client.requests[3]['session_id']
        for row, (_, _, wire) in zip(client.requests, sent):
            assert row['prefix_diagnostic']['payload_hash'] == hashlib.sha256(wire).hexdigest()
            assert row['prefix_diagnostic']['payload_bytes'] == len(wire)
            assert row['cache_hint']['forwarding_status'] == 'unconfirmed'
            assert row['normalized_usage']['cache_read_tokens'] is None
        assert all(row['prefix_diagnostic']['history_is_prefix'] for row in client.requests[1:3])
        assert 'OPAQUE-DO-NOT-LOG' in json.dumps(sent[1][0])
        assert 'OPAQUE-DO-NOT-LOG' not in json.dumps(client.requests)
        assert 'private-a' not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_parallel_headers_are_bound_to_actual_session_not_shared_client():
    gate = asyncio.Event()
    sent = []
    async def handler(request):
        sent.append(request.headers['x-grok-conv-id'])
        if len(sent) == 2:
            gate.set()
        await gate.wait()
        return httpx.Response(200, json=response('chat_completions'))
    client = ModelClient(config('chat_completions'), transport=httpx.MockTransport(handler))
    try:
        a, b = client.session('a', {}), client.session('b', {})
        await asyncio.gather(client.complete(a, []), client.complete(b, []))
        assert set(sent) == {a.cache_key, b.cache_key}
        assert 'x-grok-conv-id' not in client.http.headers
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_explicit_cache_rejection_has_one_recorded_compatibility_retry():
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return (httpx.Response(400, json={'error': {'param': 'prompt_cache_key', 'message': 'unknown parameter prompt_cache_key'}})
                if 'prompt_cache_key' in payload else httpx.Response(200, json=response()))
    client = ModelClient(config(), transport=httpx.MockTransport(handler))
    try:
        session = client.session('s', {})
        await client.complete(session, [])
        client.add_user(session, 'follow up')
        await client.complete(session, [])
        assert len(sent) == 3 and 'prompt_cache_key' in sent[0]
        assert all('prompt_cache_key' not in payload for payload in sent[1:])
        assert client.requests[0]['error'] == 'HTTP 400'
        assert client.requests[0]['compatibility_action'] == 'disable_optional_cache_hint_after_rejection'
        assert client.requests[1]['cache_hint']['compatibility_status'] == 'disabled_after_explicit_rejection'
        assert all(payload['reasoning']['effort'] == 'xhigh' and payload['store'] is False for payload in sent)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['ambiguous_400', 'accepted_read_error'])
async def test_unknown_or_accepted_outcome_never_removes_hint_and_replays(kind):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if kind == 'ambiguous_400':
            return httpx.Response(400, json={'error': {'message': 'invalid request'}})
        raise httpx.ReadError('untrusted raw error')
    client = ModelClient(config(model_retries=3), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):
            await client.complete(client.session('s', {}), [])
        assert calls == 1 and client.requests[0]['cache_hint']['sent'] is True
        assert not client._cache_hint_rejected
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_boundary_explains_prefix_rewrite_tools_and_epoch_changes():
    client = ModelClient(config(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response())))
    try:
        session = client.session('stable', {})
        tools = [{'name': 'z', 'description': 'tool', 'parameters': {'type': 'object', 'properties': {}}},
                 {'name': 'a', 'description': 'tool', 'parameters': {'type': 'object', 'properties': {}}}]
        await client.complete(session, tools)
        await client.complete(session, list(reversed(tools)))
        assert client.requests[-1]['prefix_diagnostic']['tools_hash'] == client.requests[0]['prefix_diagnostic']['tools_hash']
        session.history[0]['content'] = 'changed actual seed'
        await client.complete(session, tools)
        diag = client.requests[-1]['prefix_diagnostic']
        assert diag['history_is_prefix'] is False and diag['first_changed_path'] == 'input[0].content'
        assert diag['change_reason'] == 'history_rewritten'
        key = session.cache_key
        session.begin_cache_epoch('synthesis')
        session.history[:] = [{'role': 'user', 'content': 'new evidence package'}]
        await client.complete(session, [])
        assert session.cache_key == key
        assert client.requests[-1]['prefix_diagnostic']['change_reason'] == 'synthesis'
        assert client.requests[-1]['cache_epoch'] == 1
    finally:
        await client.aclose()


def test_weighted_aggregation_nested_zero_missing_anomaly_and_forwarder_not_added():
    rows = [
        {'protocol': 'responses', 'usage': {'input_tokens': 10000, 'output_tokens': 100,
            'input_tokens_details': {'cached_tokens': 8000}, 'output_tokens_details': {'reasoning_tokens': 40}},
            'forwarder_usage': {'input_tokens': 999999}},
        {'protocol': 'responses', 'usage': {'input_tokens': 10000, 'output_tokens': 100,
            'input_tokens_details': {'cached_tokens': 9000}}},
        {'protocol': 'chat_completions', 'usage': {'prompt_tokens': 1000, 'prompt_tokens_details': {'cached_tokens': 0}}},
        {'protocol': 'responses', 'usage': {'input_tokens': 2000}},
        {'protocol': 'responses', 'usage': None, 'error': 'ReadError'},
    ]
    summary = ModelClient.usage_summary(rows)
    assert summary['usage']['input_tokens_details']['cached_tokens'] == 17000
    cache = summary['cache_summary']
    assert cache['complete_requests'] == 3 and cache['unknown_requests'] == 2 and cache['coverage'] == .6
    assert cache['weighted_read_ratio'] == 17000 / 21000
    assert cache['reported_input_tokens'] == 21000
    assert summary['normalized_totals']['input_tokens'] == 23000
    assert summary['normalized_totals']['known_requests']['input_tokens'] == 4
    assert summary['normalized_totals']['coverage']['cache_read_tokens'] == .6
    assert summary['sequence'][0]['visible_output_tokens'] == 60
    assert summary['sequence'][1]['visible_output_tokens'] is None
    assert summary['sequence'][2]['cache_read_tokens'] == 0
    assert summary['sequence'][3]['cache_read_tokens'] is None
    anomaly = ModelClient.usage_summary([{'usage': {'input_tokens': 10, 'input_tokens_details': {'cached_tokens': 15}}}])
    assert anomaly['cache_summary']['weighted_read_ratio'] == 1.5
    assert anomaly['sequence'][0]['uncached_input_tokens'] == -5
    assert anomaly['cache_summary']['anomalous_requests'] == 1
    json.dumps(summary, allow_nan=False)


def test_protocol_usage_vocabulary_wins_over_duplicate_alias_fields():
    usage = {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 80},
             'prompt_tokens': 250, 'prompt_tokens_details': {'cached_tokens': 125}}
    chat = ModelClient.usage_summary([{'protocol': 'chat_completions', 'usage': usage}])
    responses = ModelClient.usage_summary([{'protocol': 'responses', 'usage': usage}])
    assert chat['normalized_totals']['input_tokens'] == 250
    assert chat['cache_summary']['weighted_read_ratio'] == .5
    assert responses['normalized_totals']['input_tokens'] == 100
    assert responses['cache_summary']['weighted_read_ratio'] == .8


@pytest.mark.asyncio
async def test_unknown_chat_vendor_has_no_guessed_cache_header():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response('chat_completions'))
    client = ModelClient(config('chat_completions', model_name='unrelated-model'), transport=httpx.MockTransport(handler))
    try:
        await client.complete(client.session('s', {}), [])
        assert 'x-grok-conv-id' not in captured[0].headers
        assert 'prompt_cache_key' not in json.loads(captured[0].content)
        assert client.requests[0]['cache_hint']['compatibility_status'] == 'no_documented_mapping'
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 400])
async def test_acceptance_callback_only_after_real_2xx_headers_once(status):
    order, accepted = [], []
    async def before_request():
        order.append('before_http')
        assert not accepted
    def handler(request):
        order.append('transport')
        assert not accepted
        return httpx.Response(status, json=response())
    async def on_accepted(observation):
        assert order == ['before_http', 'transport']
        assert observation['request_accepted'] and observation['response_headers_received']
        assert observation['accepted_context_notified']
        accepted.append(observation['local_request_id'])
    client = ModelClient(config(), before_request, httpx.MockTransport(handler))
    try:
        session = client.session('stable', {})
        session.on_request_accepted = on_accepted
        if status == 200:
            await client.complete(session, [])
            assert accepted == [client.requests[0]['local_request_id']]
        else:
            with pytest.raises(ModelError):
                await client.complete(session, [])
            assert accepted == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_read_error_exception_class_name_cannot_reflect_credential():
    secret = 'credential_must_never_escape'
    error = type('ReadError_' + secret, (httpx.ReadError,), {})
    def handler(request):
        raise error('contains ' + secret)
    client = ModelClient(config(_model_api_key=secret), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as raised:
            await client.complete(client.session('stable', {}), [])
        assert secret not in str(raised.value)
        assert secret not in json.dumps(client.requests)
        assert client.requests[0]['exception_types'][0] in {'redacted', 'unrecognized'}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_explicit_protocol_failure_is_retained_in_request_statistics():
    client = ModelClient(config(), transport=httpx.MockTransport(lambda req:
        httpx.Response(200, json={'status': 'incomplete', 'output': [],
            'usage': {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 0}}})))
    try:
        with pytest.raises(ModelError):
            await client.complete(client.session('stable', {}), [])
        assert ModelClient.usage_summary(client.requests)['failed_requests'] == 1
        assert client.requests[0]['protocol_terminal'] == 'responses.incomplete'
        assert client.requests[0]['normalized_usage']['cache_read_tokens'] == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('child_count', [5, 20])
async def test_small_runtime_compaction_does_not_rebuild_an_unchanged_evidence_package(tmp_path, monkeypatch, child_count):
    from app import agents
    from app.context import compact, serialized
    from tests.test_ninth_calculation_context import config as runtime_config, setup
    cfg = runtime_config(monkeypatch, max_context_chars=10000, max_model_calls=None, max_agent_turns=None)
    store, tools, context = setup(tmp_path, cfg)
    context['research_goal'] = 'Concrete tasks and source requirements; ' * (10000 // 130)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        for i in range(child_count):
            child = agents.AgentState(f'child-{i}', 'assigned topic ' * 5, 'research',
                runtime.client.session('child', {}), status='completed')
            child.reports = [{'report_id': f'report-{i}', 'summary': 'Complete finding and caveat ' * 40,
                'open_questions': ['Unresolved specific question ' * 4], 'research_round': 1}]
            runtime.children[child.id] = child
        runtime._pin_runtime_status(session, 'manager', None, False)
        session.history.append({'role': 'user', 'content': 'already visible input ' * 10000})
        assert compact(session, 10000)
        assert len(serialized(session.history)) < 10000
        frozen, epochs = copy.deepcopy(session.history), session.compactions
        assert not compact(session, 10000)
        assert session.history == frozen and session.compactions == epochs
        seed = json.loads(session.history[0]['content'])
        assert seed['retained_child_reports']['truncated_for_context']
        assert seed['retained_child_reports']['record_id']
    finally:
        await runtime.close()
