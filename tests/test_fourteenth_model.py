"""Production adapter conformance; all HTTP responses are controlled fixtures."""
import json
import time

import httpx
import pytest

from app.model import (ModelClient, ModelConfigurationError, ModelProtocolError,
                       ModelTimeoutError, ToolChoiceUnsupported)


FUNCTION = {'name': 'finish_analysis', 'description': 'Explicit submission',
            'parameters': {'type': 'object', 'properties': {'report': {'type': 'string'}},
                           'required': ['report']}}


def config(protocol='responses', **extra):
    return {'model_name': 'configured-model', 'model_base_url': 'http://localhost/v1',
            'model_allow_no_key': True, 'model_key_env': 'UNUSED_FOURTEENTH_KEY',
            'model_protocol': protocol, 'reasoning_effort': 'max',
            'model_native_search': False, 'model_retries': 3, **extra}


def body(protocol, *, calls=None, text=None, **extra):
    calls = [] if calls is None else calls
    if protocol == 'responses':
        output = [{'type': 'function_call', 'call_id': item.get('id', 'call-1'),
                   'name': item.get('name', 'finish_analysis'),
                   'arguments': item.get('arguments', '{"report":"cash retained"}'),
                   **({'status': item['status']} if 'status' in item else {})}
                  for item in calls]
        if text:
            output.append({'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': text}]})
        return {'status': 'completed', 'output': output, **extra}
    return {'choices': [{'finish_reason': 'tool_calls' if calls else 'stop',
        'message': {'role': 'assistant', 'content': text, 'tool_calls': [
            {'id': item.get('id', 'call-1'), 'type': 'function', 'function': {
                'name': item.get('name', 'finish_analysis'),
                'arguments': item.get('arguments', '{"report":"cash retained"}')}}
            for item in calls]}}], **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('name', ['finish_analysis', 'submit_report'])
async def test_named_choice_mapped_once_without_changing_binding(protocol, name):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, calls=[{'name': name}],
            model='reported-model', reasoning_effort='high',
            usage={'output_tokens': 12587, 'output_tokens_details': {'reasoning_tokens': 12572}}))
    client = ModelClient(config(protocol), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        turn = await client.complete(session, [{**FUNCTION, 'name': name}], tool_choice=name)
        expected = ({'type': 'function', 'name': name} if protocol == 'responses' else
                    {'type': 'function', 'function': {'name': name}})
        assert len(sent) == 1
        assert sent[0]['tool_choice'] == expected
        assert sent[0]['model'] == 'configured-model'
        assert (sent[0]['reasoning']['effort'] if protocol == 'responses' else sent[0]['reasoning_effort']) == 'max'
        assert (sent[0]['tools'][0]['strict'] if protocol == 'responses' else sent[0]['tools'][0]['function']['strict']) is False
        observation = session.requests[0]
        assert observation['requested_tool_choice'] == expected
        assert observation['tool_choice_status'] == turn.tool_choice_status == 'observed'
        assert observation['tool_choice_satisfied'] is True
        assert observation['reasoning_effort_sent'] == 'max'
        assert observation['reported_reasoning_effort'] == 'high'
        assert observation['reported_model'] == 'reported-model'
        assert observation['reported_output_exceeds_requested'] is True
        assert observation['cost_known'] is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('choice', [None, 'auto', 'none', 'required'])
async def test_modes_and_no_choice_preserve_existing_semantics(protocol, choice):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, text='visible progress'))
    client = ModelClient(config(protocol), transport=httpx.MockTransport(handler))
    try:
        assert (await client.complete(client.session('system', {}), [FUNCTION], tool_choice=choice)).text == 'visible progress'
        assert ('tool_choice' in sent[0]) is (choice is not None)
        assert sent[0].get('tool_choice') == choice
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('calls,expected', [([], 'missing'),
    ([{'name': 'calculate'}], 'wrong_function'),
    ([{}, {'id': 'call-2'}], 'multiple_calls'),
    ([{'arguments': '{"report":'}], 'invalid_arguments')])
async def test_ignored_or_malformed_choice_recorded_separately_from_http(protocol, calls, expected):
    client = ModelClient(config(protocol), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json=body(protocol, calls=calls, text='正在整理最终报告。'))))
    try:
        session = client.session('system', {})
        turn = await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert turn.text == '正在整理最终报告。'  # Agent can retain the draft and correct the submission.
        assert turn.tool_choice_status == expected
        assert session.requests[0]['tool_choice_satisfied'] is False
        assert session.requests[0]['tool_choice_status'] == expected
        assert session.requests[0]['request_accepted'] is True
        assert session.requests[0]['error'] is None  # HTTP completion is not business delivery.
        assert len(client.requests) == 1
        if expected == 'invalid_arguments':
            assert turn.calls[0].error and turn.calls[0].arguments == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('status', [400, 422, 500])
async def test_explicit_rejection_never_removes_selection_or_retries(protocol, status):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(status, json={'error': {'param': 'tool_choice',
            'code': 'unsupported_parameter', 'message': 'PRIVATE ERROR BODY'}})
    client = ModelClient(config(protocol), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        with pytest.raises(ToolChoiceUnsupported) as caught:
            await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert caught.value.code == 'tool_choice_unsupported'
        assert len(sent) == 1
        assert session.requests[0]['tool_choice_status'] == 'rejected'
        assert session.requests[0]['error_code'] == 'tool_choice_unsupported'
        assert session.requests[0]['tool_choice_satisfied'] is False
        assert 'PRIVATE ERROR BODY' not in str(caught.value) + json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['unknown_function', '', {}, False])
async def test_invalid_selection_rejected_before_budget_or_http(choice):
    attempts = []
    async def before_request():
        attempts.append(1)
    client = ModelClient(config(), before_request=before_request,
        transport=httpx.MockTransport(lambda request: pytest.fail('no HTTP expected')))
    try:
        with pytest.raises(ModelConfigurationError):
            await client.complete(client.session('system', {}), [FUNCTION], tool_choice=choice)
        assert attempts == [] and client.requests == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_expired_parent_does_not_spend_an_http_slot():
    attempts = []
    async def before_request():
        attempts.append(1)
    client = ModelClient(config(), before_request=before_request,
        transport=httpx.MockTransport(lambda request: pytest.fail('no HTTP expected')))
    try:
        session = client.session('system', {})
        session.deadlines = {'analysis_budget_exhausted': time.monotonic() - 1}
        with pytest.raises(ModelTimeoutError, match='总预算'):
            await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert attempts == [] and client.requests == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_complete_sse_terminal_never_completes_an_incomplete_function():
    terminal = body('responses', calls=[{'status': 'incomplete'}])
    wire = ('event: response.completed\ndata: ' + json.dumps({
        'type': 'response.completed', 'response': terminal}) + '\n\n').encode()
    client = ModelClient(config(), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'})))
    try:
        session = client.session('system', {})
        initial = list(session.history)
        with pytest.raises(ModelProtocolError, match='函数调用未完成'):
            await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert session.history == initial and not session.pending_calls
        assert session.requests[0]['stream_terminal'] == 'response.completed'
        assert session.requests[0]['tool_choice_satisfied'] is False
        assert session.requests[0]['tool_choice_status'] == 'incomplete'
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,safe', [
    ('responses_incomplete', True), ('responses_item_incomplete', True),
    ('responses_item_in_progress', True), ('chat_length', True),
    ('responses_failed', False), ('responses_cancelled', False),
    ('responses_item_failed', False), ('responses_item_without_terminal', False),
    ('chat_content_filter', False), ('sse_missing_terminal', False)])
async def test_only_explicit_known_incomplete_output_allows_bounded_agent_correction(kind, safe):
    protocol = 'chat_completions' if kind.startswith('chat_') else 'responses'
    result = body(protocol, calls=[{'arguments': '{"report":"PARTIAL_UNCOMMITTED'}])
    if kind.startswith('chat_'):
        result['choices'][0]['finish_reason'] = kind.removeprefix('chat_')
    elif kind.startswith('responses_item_'):
        status = kind.removeprefix('responses_item_')
        result['output'][0]['status'] = status if status != 'without_terminal' else 'incomplete'
        if status == 'without_terminal': result.pop('status')
    elif kind.startswith('responses_'):
        result['status'] = kind.removeprefix('responses_')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        if kind == 'sse_missing_terminal':
            wire = 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"PARTIAL_UNCOMMITTED"}\n\n'
            return httpx.Response(200, text=wire, headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=result)
    client = ModelClient(config(protocol), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        initial = list(session.history)
        with pytest.raises(ModelProtocolError) as caught:
            await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert bool(getattr(caught.value, 'submission_retry_safe', False)) is safe
        if safe:
            assert '不重放半截调用' in caught.value.submission_feedback
        assert session.history == initial and not session.pending_calls
        assert len(sent) == 1  # Adapter never resends an accepted request itself.
        assert 'PARTIAL_UNCOMMITTED' not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,omit', [('responses', False), ('responses', True),
                                         ('chat_completions', False), ('chat_completions', True)])
async def test_nonstream_protocol_terminal_is_observed_without_inventing_missing_status(protocol, omit):
    result = body(protocol, calls=[{}])
    if omit:
        if protocol == 'responses': result.pop('status')
        else: result['choices'][0].pop('finish_reason')
    client = ModelClient(config(protocol, model_responses_stream=False), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result)))
    try:
        session = client.session('system', {})
        await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        observation = session.requests[0]
        expected = (protocol + '.compatible_result' if omit else
                    'responses.completed' if protocol == 'responses' else 'chat.tool_calls')
        assert observation['protocol_terminal'] == expected
        assert observation.get('response_status' if protocol == 'responses' else 'finish_reason') == (
            None if omit else 'completed' if protocol == 'responses' else 'tool_calls')
        assert observation['tool_choice_satisfied'] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['responses_incomplete', 'responses_item_incomplete', 'chat_length'])
async def test_known_incomplete_final_is_corrected_through_production_publication(tmp_path, monkeypatch, kind):
    from tests.test_fourteenth_chain import setup_app, execute, final
    protocol = 'chat_completions' if kind == 'chat_length' else 'responses'
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            result = body(protocol, calls=[{'arguments': '{"report":"NEVER_REPLAY_PARTIAL'}])
            if kind == 'responses_incomplete': result['status'] = 'incomplete'
            elif kind == 'responses_item_incomplete': result['output'][0]['status'] = 'incomplete'
            else: result['choices'][0]['finish_reason'] = 'length'
        else:
            assert len(sent) == 2
            assert 'NEVER_REPLAY_PARTIAL' not in json.dumps(payload)
            assert '不重放半截调用' in json.dumps(payload, ensure_ascii=False)
            assert payload['tool_choice'] == ({'type': 'function', 'name': 'finish_analysis'}
                if protocol == 'responses' else {'type': 'function', 'function': {'name': 'finish_analysis'}})
            result = body(protocol, calls=[{'arguments': json.dumps(final(), ensure_ascii=False)}])
        return httpx.Response(200, json=result)
    app, store, account = setup_app(tmp_path, monkeypatch, handler, model_protocol=protocol,
        max_model_calls=2, max_agent_turns=2)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(sent) == 2 and len(reports) == 1
    assert reports[0]['result']['submission_corrections'] == 1
    assert reports[0]['result']['submission_outcome'] == 'recovered'
    assert not store.material_count()
    assert all('NEVER_REPLAY_PARTIAL' not in json.dumps(event) for event in store.job(saved['id'], events=True)['events'])


@pytest.mark.asyncio
async def test_missing_final_stream_terminal_does_not_spend_a_correction_request(tmp_path, monkeypatch):
    from tests.test_fourteenth_chain import setup_app, execute
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        wire = 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"NEVER_REPLAY_PARTIAL"}\n\n'
        return httpx.Response(200, text=wire, headers={'content-type': 'text/event-stream'})
    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        model_responses_stream=True, max_model_calls=2, max_agent_turns=2)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'failed' and reports == []
    assert len(sent) == 1  # Accepted but unknown outcome is not presumed zero-cost.
    assert saved['error_code'] == 'model_protocol_error'


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('kind', ['empty', 'duplicate_ids'])
@pytest.mark.parametrize('explicit_terminal', [True, False])
async def test_empty_or_duplicate_completed_turn_is_atomic_and_only_known_terminal_can_correct(protocol, kind, explicit_terminal):
    result = body(protocol, calls=[] if kind == 'empty' else [{'id': 'duplicate'}, {'id': 'duplicate'}])
    if not explicit_terminal:
        if protocol == 'responses': result.pop('status')
        else: result['choices'][0].pop('finish_reason')
    if protocol == 'responses':
        result['output'].insert(0, {'type': 'reasoning', 'encrypted_content': 'BAD_TURN_OPAQUE'})
    else:
        result['choices'][0]['message']['reasoning_content'] = 'BAD_TURN_OPAQUE'
    client = ModelClient(config(protocol), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result)))
    try:
        session = client.session('system', {})
        original_history = list(session.history)
        with pytest.raises(ModelProtocolError) as caught:
            await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        assert bool(getattr(caught.value, 'submission_retry_safe', False)) is explicit_terminal
        assert session.history == original_history and session.pending_calls == []
        assert len(session.requests) == 1
        assert session.requests[0]['tool_choice_satisfied'] is False
        assert session.requests[0]['tool_choice_status'] == ('missing' if kind == 'empty' else 'duplicate_call_ids')
        assert 'BAD_TURN_OPAQUE' not in json.dumps(client.requests)
        assert 'BAD_TURN_OPAQUE' not in getattr(caught.value, 'submission_feedback', '')
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('kind', ['empty', 'duplicate_ids'])
async def test_empty_or_duplicate_final_corrects_once_through_service_and_store(tmp_path, monkeypatch, protocol, kind):
    from tests.test_fourteenth_chain import setup_app, execute, final
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            calls = [] if kind == 'empty' else [
                {'id': 'never-replay-duplicate', 'arguments': json.dumps(final('First uncommitted'))},
                {'id': 'never-replay-duplicate', 'arguments': json.dumps(final('Last uncommitted'))}]
            result = body(protocol, calls=calls)
        else:
            assert len(sent) == 2
            assert not store.analyses(account['id'])
            assert 'never-replay-duplicate' not in json.dumps(payload)
            assert 'First uncommitted' not in json.dumps(payload)
            assert 'Last uncommitted' not in json.dumps(payload)
            result = body(protocol, calls=[{'arguments': json.dumps(final(), ensure_ascii=False)}])
        return httpx.Response(200, json=result)
    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        model_protocol=protocol, max_model_calls=2, max_agent_turns=2)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(sent) == 2 and len(reports) == 1
    assert reports[0]['result']['submission_outcome'] == 'recovered'
    assert reports[0]['result']['submission_corrections'] == 1
