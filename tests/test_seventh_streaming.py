"""Bounded Responses SSE protocol fixtures, no real provider/model calls."""
import asyncio
import gzip
import json

import httpx
import pytest

from app.model import ModelClient, ModelError, ModelProtocolError, ReasoningEffortUnsupported


OPAQUE = 'fixture-opaque-reasoning-never-in-diagnostics'


def cfg(**extra):
    return {'model_name': 'stream-fixture', 'model_base_url': 'http://localhost/v1',
            'model_allow_no_key': True, 'model_key_env': 'UNUSED_STREAM_FIXTURE_KEY',
            'model_protocol': 'responses', 'reasoning_effort': 'medium',
            'model_native_search': False, 'model_retries': 0, **extra}


def response(text='visible final', **extra):
    return {'id': 'response-fixture', 'status': 'completed',
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text}]}], **extra}


def frame(kind, body=None, *, declared=None, sequence=None, **extra):
    event = {'type': kind, **extra}
    if body is not None:
        event['response'] = body
    if sequence is not None:
        event['sequence_number'] = sequence
    return ((f'event: {declared or kind}\n' if declared is not False else '') +
            'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, fail=None):
        self.chunks, self.delay, self.fail, self.closed = chunks, delay, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
        if self.fail:
            raise self.fail

    async def aclose(self):
        self.closed = True


def sse(chunks, **kwargs):
    stream = Bytes(chunks, **kwargs)
    return httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8',
                                      'x-request-id': 'safe-request-id'}, stream=stream), stream


@pytest.mark.asyncio
async def test_terminal_complete_only_preserves_tools_native_output_and_opaque_continuation():
    payloads, streams = [], []
    opaque = {'id': 'opaque-item', 'type': 'reasoning', 'encrypted_content': OPAQUE, 'summary': []}
    search = {'id': 'search-fixture', 'type': 'web_search_call', 'status': 'completed',
              'action': {'type': 'search', 'query': 'controlled', 'sources': [{'url': 'https://example.com/source'}]}}
    tool = {'type': 'function_call', 'id': 'function-item', 'call_id': 'call-fixture',
            'name': 'calculate', 'arguments': '{"values":[2,3]}'}
    complete = response(output=[opaque, search, tool], usage={'output_tokens': 20})
    def handler(request):
        payloads.append(json.loads(request.content))
        body = complete if len(payloads) == 1 else response('完成')
        wire = (b': heartbeat\r\n\r\n' + frame('response.created', {'id': 'response-fixture'}, sequence=0) +
                frame('response.function_call_arguments.delta', delta='invalid incomplete arguments', sequence=1) +
                frame('response.reasoning_summary_text.delta', delta=OPAQUE, sequence=2) +
                frame('response.completed', body, sequence=3))
        result, stream = sse([wire[i:i + 1] for i in range(len(wire))])
        streams.append(stream)
        return result
    client = ModelClient(cfg(model_native_search=True), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        turn = await client.complete(session, [])
        assert turn.calls[0].arguments == {'values': [2, 3]}
        assert turn.native_events[0]['status'] == 'completed'
        client.add_results(session, [(turn.calls[0], {'result': 5})])
        assert (await client.complete(session, [])).text == '完成'
        assert opaque in payloads[1]['input']
        assert all(payload['stream'] is True and payload['reasoning']['effort'] == 'medium' for payload in payloads)
        assert OPAQUE not in json.dumps(client.requests)
        assert client.requests[0]['stream_terminal'] == 'response.completed'
        assert client.requests[0]['stream_event_count'] == 4
        assert client.requests[0]['stream_first_event_ms'] is not None
        assert all(stream.closed for stream in streams)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', [b'', b'data: [DONE]\n\n', b'data: {"type":"response.completed"}\n'])
async def test_delta_without_complete_terminal_never_becomes_report_or_call(ending):
    wire = frame('response.output_text.delta', delta='must not publish') + frame('response.function_call_arguments.delta', delta='{"x":1}') + ending
    result, stream = sse([wire])
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {})
        with pytest.raises(ModelProtocolError):
            await client.complete(session, [])
        assert len(session.history) == 1 and session.pending_calls == []
        assert stream.closed and client.requests[0]['error']
        assert 'must not publish' not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'incomplete', 'cancelled'])
async def test_failed_terminal_does_not_publish_partial_content(status):
    result, _ = sse([frame('response.' + status, response('partial content', status=status))])
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {})
        with pytest.raises(ModelProtocolError, match='未完成'):
            await client.complete(session, [])
        assert len(session.history) == 1 and not session.pending_calls
        assert client.requests[0]['stream_terminal'] == 'response.' + status
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('wire_format', ['sse', 'json'])
@pytest.mark.parametrize('item_status', ['in_progress', 'incomplete', 'failed', 'cancelled', 'queued', 'unknown', '', None, 42])
async def test_completed_response_rejects_explicit_unfinished_function_item(wire_format, item_status):
    valid = {'type': 'function_call', 'call_id': 'valid-call', 'name': 'calculate',
             'arguments': '{"values":[2,3]}', 'status': 'completed'}
    unfinished = {'type': 'function_call', 'call_id': 'unfinished-call', 'name': 'finish_analysis',
                  'arguments': '{"report":"must not publish"}', 'status': item_status}
    body = response(output=[valid, unfinished])
    result = sse([frame('response.completed', body)])[0] if wire_format == 'sse' else httpx.Response(200, json=body)
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {})
        before = json.dumps(session.history)
        with pytest.raises(ModelProtocolError, match='函数调用未完成'):
            await client.complete(session, [])
        assert json.dumps(session.history) == before
        assert session.pending_calls == []
        assert len(client.requests) == 1
        assert 'must not publish' not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('wire_format', ['sse', 'json'])
@pytest.mark.parametrize('status_field', [{}, {'status': 'completed'}])
async def test_completed_or_omitted_function_item_status_remains_compatible(wire_format, status_field):
    tool = {'type': 'function_call', 'call_id': 'complete-call', 'name': 'calculate',
            'arguments': '{"values":[2,3]}', **status_field}
    body = response(output=[tool])
    result = sse([frame('response.completed', body)])[0] if wire_format == 'sse' else httpx.Response(200, json=body)
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {})
        turn = await client.complete(session, [])
        assert turn.calls[0].arguments == {'values': [2, 3]}
        assert session.pending_calls == turn.calls
        assert tool in session.history
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('wire,error', [
    (frame('response.completed', response(status='in_progress')), 'invalid_stream_terminal'),
    (frame('response.completed', response(error={'message': 'contradictory failure'})), 'invalid_stream_terminal'),
    (frame('response.completed', {'status': 'completed'}), 'invalid_stream_terminal'),
    (frame('response.completed', response(), declared='response.failed'), 'stream_event_type_mismatch'),
    (frame('response.created', {'id': 'one'}) + frame('response.completed', response(id='two')), 'stream_response_identity_changed'),
    (frame('response.created', {'id': 'response-fixture'}, sequence=1) + frame('response.completed', response(), sequence=1), 'stream_sequence_invalid'),
    (b'data: {bad json}\n\n', 'invalid_stream_json'),
    (frame('error', message='untrusted provider error'), 'stream_error'),
])
async def test_malformed_or_contradictory_stream_fails_without_second_request(wire, error):
    calls = []
    def handler(req):
        calls.append(req)
        return sse([wire])[0]
    client = ModelClient(cfg(model_retries=3), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelProtocolError):
            await client.complete(client.session('system', {}), [])
        assert len(calls) == 1 and client.requests[0]['error'] == error
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,wire,error', [
    ('STREAM_MAX_BYTES', b': ' + b'x' * 200 + b'\n\n', 'response_size_limit'),
    ('STREAM_MAX_EVENT_BYTES', b'data: ' + b'x' * 200, 'stream_event_size_limit'),
    ('STREAM_MAX_EVENTS', frame('response.in_progress') * 3, 'stream_event_limit'),
])
async def test_stream_receive_limits_are_bounded(limit, wire, error):
    result, stream = sse([wire])
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    setattr(client, limit, 2 if limit == 'STREAM_MAX_EVENTS' else 100)
    try:
        with pytest.raises(ModelProtocolError):
            await client.complete(client.session('system', {}), [])
        assert client.requests[0]['error'] == error and stream.closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True])
async def test_stream_requested_json_envelope_returned_uses_one_response(compressed):
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content))
        content = json.dumps({'success': True, 'data': response()}).encode()
        headers = {'content-type': 'application/json'}
        if compressed:
            content = gzip.compress(content); headers['content-encoding'] = 'gzip'
        return httpx.Response(200, content=content, headers=headers)
    client = ModelClient(cfg(), transport=httpx.MockTransport(handler))
    try:
        assert (await client.complete(client.session('system', {}), [])).text == 'visible final'
        assert len(payloads) == 1 and payloads[0]['stream'] is True
        assert client.requests[0]['response_format'] == 'json'
        assert client.requests[0]['response_envelope'] == 'data'
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,enabled', [('responses', False), ('chat_completions', True)])
async def test_nonstream_and_chat_payload_contracts_unchanged(protocol, enabled):
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content))
        body = response() if protocol == 'responses' else {'choices': [{'message': {'content': 'visible final'}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, json=body)
    client = ModelClient(cfg(model_protocol=protocol, model_responses_stream=enabled), transport=httpx.MockTransport(handler))
    try:
        assert (await client.complete(client.session('system', {}), [])).text == 'visible final'
        assert 'stream' not in payloads[0] and client.requests[0]['stream_requested'] is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_retry_keeps_budget_and_only_complete_attempt_tools(monkeypatch):
    requests, budgets, streams = [], [], []
    async def budget(): budgets.append(True)
    original_sleep = asyncio.sleep
    async def short_sleep(delay): await original_sleep(0)
    monkeypatch.setattr('app.model.asyncio.sleep', short_sleep)
    def handler(req):
        requests.append(json.loads(req.content))
        if len(requests) == 1:
            result, stream = sse([frame('response.output_text.delta', delta='discard me')], fail=httpx.RemoteProtocolError('peer closed connection without sending complete message body', request=req))
        else:
            result, stream = sse([frame('response.completed', response())])
        streams.append(stream)
        return result
    client = ModelClient(cfg(model_retries=1), budget, httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        assert (await client.complete(session, [])).text == 'visible final'
        assert len(requests) == len(budgets) == 2
        assert requests[0] == requests[1]
        assert client.requests[0]['error'] == 'RemoteProtocolError'
        assert client.requests[1]['error'] is None
        assert 'discard me' not in json.dumps(session.history)
        assert all(stream.closed for stream in streams)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_synthesis_total_timeout_includes_stream_body_and_closes():
    result, stream = sse([frame('response.in_progress'), frame('response.completed', response())], delay=.03)
    client = ModelClient(cfg(synthesis_request_timeout_seconds=.04), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {}); session.phase = 'synthesis'
        with pytest.raises(ModelError, match='超过时限'):
            await client.complete(session, [])
        assert stream.closed and client.requests[0]['error'] == 'RequestDeadlineExceeded'
        assert client.requests[0]['request_timeout_seconds'] == .04
        assert len(session.history) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_without_report():
    started = asyncio.Event()
    class Waiting(Bytes):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b''
    stream = Waiting([])
    result = httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        session = client.session('system', {})
        task = asyncio.create_task(client.complete(session, []))
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert stream.closed and client.requests[0]['error'] == 'cancelled'
        assert len(session.history) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_effort_rejection_keeps_classification_and_never_retries():
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(400, json={'error': {'param': 'reasoning', 'message': 'unsupported reasoning effort'}})
    client = ModelClient(cfg(model_retries=3), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ReasoningEffortUnsupported):
            await client.complete(client.session('system', {}), [])
        assert len(payloads) == 1 and payloads[0]['reasoning']['effort'] == 'medium'
        assert client.requests[0]['reasoning_effort_rejected'] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('wire_kind', ['delta', 'terminal', 'json'])
async def test_streaming_never_logs_or_replays_reflected_key(wire_kind):
    secret = 'seventh-stream-controlled-private-key'
    def handler(req):
        if wire_kind == 'json': return httpx.Response(200, json=response(secret))
        wire = frame('response.output_text.delta', delta=secret) if wire_kind == 'delta' else frame('response.completed', response(secret))
        return sse([wire])[0]
    client = ModelClient(cfg(_model_api_key=secret), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        with pytest.raises(ModelProtocolError, match='凭据反射'):
            await client.complete(session, [])
        assert secret not in json.dumps(client.requests)
        assert secret not in json.dumps(session.history)
        assert client.requests[0]['error'] == 'credential_reflected_by_provider'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_accepts_utf8_bom_and_cr_only_delimiters():
    wire = b'\xef\xbb\xbf' + frame('response.completed', response()).replace(b'\n', b'\r')
    result, stream = sse([wire[i:i + 3] for i in range(0, len(wire), 3)])
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda req: result))
    try:
        assert (await client.complete(client.session('system', {}), [])).text == 'visible final'
        assert stream.closed
    finally:
        await client.aclose()
