"""Real local HTTP/1.1 framing; never a paid endpoint or MockTransport."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import pytest

from app.model import ModelClient, ModelError, ModelProtocolError


def event(kind, *, response=None, **extra):
    body = {'type': kind, **extra}
    if response is not None:
        body['response'] = response
    return ('event: ' + kind + '\ndata: ' + json.dumps(body) + '\n\n').encode()


def terminal():
    return event('response.completed', response={
        'id': 'local-complete', 'status': 'completed', 'output': [
            {'type': 'message', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': 'Complete visible result'}]}]})


async def chunk(writer, body):
    writer.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n')
    await writer.drain()


@asynccontextmanager
async def local_origin(handler):
    """Consume the actual request before emitting deliberately controlled framing."""
    requests, tasks, failures = [], set(), []

    async def accepted(reader, writer):
        current = asyncio.current_task()
        tasks.add(current)
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1].strip()) for line in head.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            requests.append(json.loads(await reader.readexactly(length)))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                         b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            await writer.drain()
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # The client is deliberately allowed to leave a stalled tail.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(current)

    server = await asyncio.start_server(accepted, '127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1', requests
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        assert not failures, failures


def client_for(url, monkeypatch):
    # Keep a configured external proxy out of this localhost-only fixture.
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setenv('no_proxy', '*')
    return ModelClient({'model_base_url': url, 'model_name': 'local-framing-fixture',
        'model_allow_no_key': True, 'model_key_env': 'UNUSED_LOCAL_FRAMING_TEST_KEY',
        'model_protocol': 'responses', 'model_native_search': False,
        'reasoning_effort': 'max', 'model_retries': 3,
        'model_request_hard_timeout_seconds': 2,
        'model_first_event_timeout_seconds': 1, 'model_stream_idle_timeout_seconds': 1})


@pytest.mark.asyncio
async def test_peer_closes_mid_body_is_remote_error_without_local_cancel_or_retry(monkeypatch):
    async def handler(reader, writer):
        await chunk(writer, event('response.created', response={'id': 'local-interrupted'}))
        await chunk(writer, event('response.function_call_arguments.delta', delta='{"report":'))
        # TCP EOF without the terminating HTTP chunk is an actual h11 error.

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            session = client.session('fixture', {})
            with pytest.raises(ModelError) as error:
                await client.complete(session, [])
            observation = client.requests[0]
            assert error.value.code == 'upstream_transport_error'
            assert observation['error'] == 'RemoteProtocolError'
            assert observation['transport_failure_kind'] == 'incomplete_response_body'
            assert observation['cancelled_by'] is None
            assert observation['response_id'] == 'local-interrupted'
            assert observation['http_body_complete'] is False
            assert observation['stream_terminal'] is None
            assert observation['retry_suppressed'] == 'accepted_or_sent_request_outcome_unknown'
            assert len(requests) == 1 and session.pending_calls == [] and len(session.history) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_complete_terminal_waits_for_short_delayed_http_terminator(monkeypatch):
    monkeypatch.setattr(ModelClient, 'STREAM_TAIL_DRAIN_SECONDS', .3)
    sent_terminator, closed_early = asyncio.Event(), []

    async def handler(reader, writer):
        await chunk(writer, terminal())
        await asyncio.sleep(.04)
        closed_early.append(reader.at_eof())
        writer.write(b'0\r\n\r\n')
        await writer.drain()
        sent_terminator.set()

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            result = await client.complete(client.session('fixture', {}), [])
            await asyncio.wait_for(sent_terminator.wait(), 1)
            observation = client.requests[0]
            assert result.text == 'Complete visible result' and requests[0]['stream'] is True
            assert closed_early == [False]
            assert observation['http_body_complete'] is True
            assert observation['stream_close_reason'] == 'http_eof'
            assert observation['stream_tail_drain_ms'] >= 20
            assert len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_stalled_tail_is_bounded_without_repeating_accepted_response(monkeypatch):
    monkeypatch.setattr(ModelClient, 'STREAM_TAIL_DRAIN_SECONDS', .04)

    async def handler(reader, writer):
        await chunk(writer, terminal())
        await reader.read()  # HTTP terminator deliberately never arrives.

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            started = time.monotonic()
            result = await client.complete(client.session('fixture', {}), [])
            elapsed = time.monotonic() - started
            observation = client.requests[0]
            assert result.text == 'Complete visible result'
            assert .02 <= elapsed < .6
            assert observation['http_body_complete'] is False
            assert observation['stream_close_reason'] == 'terminal_drain_timeout'
            assert observation['error'] is None and len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_tail_drain_finishes_before_earliest_parent_deadline(monkeypatch):
    monkeypatch.setattr(ModelClient, 'STREAM_TAIL_DRAIN_SECONDS', 1)

    async def handler(reader, writer):
        await chunk(writer, terminal())
        await reader.read()

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            session = client.session('fixture', {})
            deadline = time.monotonic() + .3
            session.deadlines['analysis_budget_exhausted'] = deadline
            result = await client.complete(session, [])
            observation = client.requests[0]
            assert result.text == 'Complete visible result'
            assert time.monotonic() < deadline
            assert observation['http_body_complete'] is False
            assert observation['stream_close_reason'] == 'terminal_drain_deadline'
            assert observation['cancelled_by'] is None and len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_clean_http_eof_without_response_terminal_still_cannot_publish(monkeypatch):
    async def handler(reader, writer):
        await chunk(writer, event('response.created', response={'id': 'local-no-terminal'}))
        await chunk(writer, event('response.output_text.delta', delta='Not a finished report'))
        writer.write(b'0\r\n\r\n')
        await writer.drain()

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            session = client.session('fixture', {})
            with pytest.raises(ModelProtocolError):
                await client.complete(session, [])
            observation = client.requests[0]
            assert observation['http_body_complete'] is True
            assert observation['stream_terminal'] is None
            assert observation['error'] == 'stream_missing_terminal'
            assert len(requests) == 1 and session.pending_calls == [] and len(session.history) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_truncated_http_tail_does_not_discard_already_complete_terminal(monkeypatch):
    async def handler(reader, writer):
        await chunk(writer, terminal())
        # Valid complete protocol object followed by an incomplete HTTP body.

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            result = await client.complete(client.session('fixture', {}), [])
            observation = client.requests[0]
            assert result.text == 'Complete visible result'
            assert observation['http_body_complete'] is False
            assert observation['stream_close_reason'] == 'terminal_tail_transport_error'
            assert observation['stream_tail_error']
            assert observation['error'] is None and len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_incomplete_terminal_keeps_safe_reason_and_http_completion_distinct(monkeypatch):
    private_text = 'partial-body-must-never-appear-in-diagnostics'
    private_detail = 'private-provider-detail-must-not-be-exported'

    async def handler(reader, writer):
        await chunk(writer, event('response.incomplete', response={
            'id': 'local-incomplete', 'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens', 'internal': private_detail},
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'incomplete',
                        'content': [{'type': 'output_text', 'text': private_text}]}]}))
        writer.write(b'0\r\n\r\n')
        await writer.drain()

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            session = client.session('fixture', {})
            with pytest.raises(ModelProtocolError):
                await client.complete(session, [])
            observation = client.requests[0]
            assert observation['response_incomplete_reason'] == 'max_output_tokens'
            assert observation['response_status'] == 'incomplete'
            assert observation['stream_terminal'] == 'response.incomplete'
            assert observation['http_body_complete'] is True
            assert observation['stream_close_reason'] == 'http_eof'
            assert private_text not in json.dumps(observation)
            assert private_detail not in json.dumps(observation)
            assert len(requests) == 1 and session.pending_calls == [] and len(session.history) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_local_first_event_timeout_is_explicitly_not_peer_close(monkeypatch):
    async def handler(reader, writer):
        await reader.read()  # Headers alone do not count as model progress.

    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        client.config['model_first_event_timeout_seconds'] = .1
        try:
            session = client.session('fixture', {})
            with pytest.raises(ModelError) as error:
                await client.complete(session, [])
            observation = client.requests[0]
            assert error.value.code == 'first_event_timeout'
            assert observation['cancelled_by'] == 'first_event_timeout'
            assert observation['stream_close_reason'] == 'local_timeout'
            assert observation['http_body_complete'] is False
            assert observation['stream_terminal'] is None
            assert observation['error'] != 'RemoteProtocolError'
            assert len(requests) == 1 and session.pending_calls == [] and len(session.history) == 1
        finally:
            await client.aclose()
