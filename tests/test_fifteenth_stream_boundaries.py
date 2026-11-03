"""Real localhost framing at the parser/watchdog boundary; no paid endpoint.

The historical provider stream was not retained. These reproduce local defects,
not a claim about the line endings or events in that historical request.
"""
import asyncio
import json
import time

import pytest

from app.model import ModelClient, ModelError, ModelProtocolError
from tests.test_fourteenth_transport_close import local_origin, chunk, client_for


TOOLS = [{'name': 'finish_analysis', 'description': 'controlled local fixture',
          'parameters': {'type': 'object', 'properties': {}}}]
CALL = {'type': 'function_call', 'id': 'item-fifteenth', 'call_id': 'call-fifteenth',
        'name': 'finish_analysis', 'status': 'completed',
        'arguments': '{"report":"controlled visible report"}'}
BODY = {'id': 'response-fifteenth', 'status': 'completed', 'output': [CALL]}


def frame(kind='response.completed', *, ending='\n', **extra):
    event = {'type': kind, **({'response': BODY} if kind == 'response.completed' else {}), **extra}
    return ('event: ' + kind + ending + 'data: ' + json.dumps(event) + ending * 2).encode()


async def eof(writer):
    writer.write(b'0\r\n\r\n')
    await writer.drain()


def observe_progress(monkeypatch):
    """Observe the production watchdog callback, without replacing its clock."""
    progress_kinds, done_seen = [], asyncio.Event()
    receive = ModelClient._receive_stream
    async def observed(self, endpoint, payload, timeout, trace, observation, started, progress, *, deadline=None):
        def callback(kind, semantic=True):
            progress(kind, semantic)
            progress_kinds.append(kind)
            if kind == 'response.function_call_arguments.done':
                done_seen.set()
        return await receive(self, endpoint, payload, timeout, trace, observation, started, callback, deadline=deadline)
    monkeypatch.setattr(ModelClient, '_receive_stream', observed)
    return progress_kinds, done_seen


@pytest.mark.asyncio
@pytest.mark.parametrize('framing', ['lf', 'cr', 'crlf_split', 'cr_one_byte_chunks'])
async def test_complete_terminal_is_consumed_without_waiting_for_next_byte_or_eof(monkeypatch, framing):
    monkeypatch.setattr(ModelClient, 'STREAM_TAIL_DRAIN_SECONDS', .03)
    sent, client_closed = asyncio.Event(), asyncio.Event()
    async def handler(reader, writer):
        wire = frame(ending='\r' if framing.startswith('cr') and framing != 'crlf_split' else '\r\n' if framing == 'crlf_split' else '\n')
        if framing == 'crlf_split':
            # Each CR ends one HTTP chunk; the LF belongs to the next chunk.
            parts = wire.split(b'\r')
            for part in parts[:-1]:
                await chunk(writer, part + b'\r')
            if parts[-1]:
                await chunk(writer, parts[-1])
        elif framing == 'cr_one_byte_chunks':
            for byte in wire:
                await chunk(writer, bytes([byte]))
        else:
            await chunk(writer, wire)
        sent.set()
        await reader.read()  # No HTTP EOF or extra SSE byte from the server.
        client_closed.set()
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            turn = await client.complete(client.session('fixture', {}), TOOLS, tool_choice='finish_analysis')
            assert sent.is_set() and len(turn.calls) == len(requests) == 1
            assert turn.calls[0].arguments == {'report': 'controlled visible report'}
            observation = client.requests[0]
            assert observation['stream_terminal'] == 'response.completed'
            assert observation['stream_close_reason'] == 'terminal_drain_timeout'
            assert observation['http_body_complete'] is False and observation['cancelled_by'] is None
            await asyncio.wait_for(client_closed.wait(), 1)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_cr_terminal_before_remote_missing_http_chunk_terminator_is_preserved(monkeypatch):
    async def handler(reader, writer):
        await chunk(writer, frame(ending='\r'))
        # All terminal bytes were sent. Closing without HTTP's zero chunk used
        # to hide the pending final CR behind a RemoteProtocolError.
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            turn = await client.complete(client.session('fixture', {}), TOOLS, tool_choice='finish_analysis')
            observation = client.requests[0]
            assert len(turn.calls) == len(requests) == 1
            assert observation['stream_terminal'] == 'response.completed'
            assert observation['stream_close_reason'] == 'terminal_tail_transport_error'
            assert observation['stream_tail_error'] == 'RemoteProtocolError'
            assert observation['error'] is None and observation['http_body_complete'] is False
        finally:
            await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['\r', '\r\n', '\n'])
async def test_full_function_item_without_response_terminal_is_still_not_success(monkeypatch, ending):
    async def handler(reader, writer):
        await chunk(writer, frame('response.function_call_arguments.done', ending=ending,
                                 item_id=CALL['id'], arguments=CALL['arguments']))
        await chunk(writer, frame('response.output_item.done', ending=ending, item=CALL))
        await eof(writer)
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            session = client.session('fixture', {})
            with pytest.raises(ModelProtocolError):
                await client.complete(session, TOOLS)
            assert client.requests[0]['error'] == 'stream_missing_terminal'
            assert client.requests[0]['http_body_complete'] is True
            assert session.pending_calls == [] and len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_cr_terminal_tail_still_respects_parent_deadline(monkeypatch):
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock, wait_for_stage
    timeline = ControlledDeadlineClock(monkeypatch, 60)
    monkeypatch.setattr(ModelClient, 'STREAM_TAIL_DRAIN_SECONDS', 10)
    terminal_seen = asyncio.Event()
    receive = ModelClient._receive_stream
    async def observed(self, endpoint, payload, timeout, trace, observation, started, progress, *, deadline=None):
        def callback(kind, semantic=True):
            progress(kind, semantic)
            if kind == 'response.completed':
                terminal_seen.set()
        return await receive(self, endpoint, payload, timeout, trace, observation, started, callback, deadline=deadline)
    monkeypatch.setattr(ModelClient, '_receive_stream', observed)
    async def handler(reader, writer):
        await chunk(writer, frame(ending='\r'))
        await reader.read()
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        task = None
        try:
            session = client.session('fixture', {})
            deadline = time.monotonic() + .5
            session.deadlines['analysis_budget_exhausted'] = deadline
            task = asyncio.create_task(client.complete(session, TOOLS))
            await wait_for_stage(task, terminal_seen)
            assert len(requests) == 1 and not task.done()
            # The real socket delivered the terminal and the reader is now
            # draining its tail. Advance past the .45s drain allowance while
            # the parent's .5s deadline remains open; preparation costs no time.
            timeline.advance(.46)
            turn = await task
            assert len(turn.calls) == len(requests) == 1 and time.monotonic() < deadline
            assert client.requests[0]['stream_close_reason'] == 'terminal_drain_deadline'
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()


@pytest.mark.asyncio
async def test_nonempty_arguments_done_reaches_real_progress_watchdog_before_terminal(monkeypatch):
    kinds, seen = observe_progress(monkeypatch)
    async def handler(reader, writer):
        await chunk(writer, frame('response.function_call_arguments.done', item_id=CALL['id'], arguments=CALL['arguments']))
        # Event barrier proves this exact event was consumed as progress before
        # allowing the final response. No machine-speed-based phase assumption.
        await seen.wait()
        await chunk(writer, frame())
        await eof(writer)
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            turn = await client.complete(client.session('fixture', {}), TOOLS)
            assert kinds == ['response.function_call_arguments.done', 'response.completed']
            assert len(turn.calls) == len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['', '  ', None, {}, 0])
async def test_empty_or_invalid_done_and_heartbeats_do_not_renew_progress(monkeypatch, arguments):
    kinds, _ = observe_progress(monkeypatch)
    async def handler(reader, writer):
        await chunk(writer, b': heartbeat\n\n')
        await chunk(writer, frame('response.function_call_arguments.done', item_id=CALL['id'], arguments=arguments))
        await chunk(writer, frame())
        await eof(writer)
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        try:
            turn = await client.complete(client.session('fixture', {}), TOOLS)
            assert kinds == ['response.completed']
            assert client.requests[0]['stream_heartbeat_count'] == 1
            assert len(turn.calls) == len(requests) == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_arguments_done_progress_cannot_extend_request_hard_limit(monkeypatch):
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock, wait_for_stage
    timeline = ControlledDeadlineClock(monkeypatch, 60)
    kinds, seen = observe_progress(monkeypatch)
    send_next = asyncio.Queue()
    async def handler(reader, writer):
        for index in range(4):
            await chunk(writer, frame('response.function_call_arguments.done', item_id=CALL['id'], arguments=CALL['arguments']))
            if index < 3:
                await send_next.get()
        await reader.read()  # Let the real client cancellation close this peer.
    async with local_origin(handler) as (url, requests):
        client = client_for(url, monkeypatch)
        client.config['model_request_hard_timeout_seconds'] = .5
        task = None
        try:
            session = client.session('fixture', {})
            task = asyncio.create_task(client.complete(session, TOOLS))
            await wait_for_stage(task, seen)
            assert len(requests) == 1
            # Each advance follows confirmed production progress, rather than
            # assuming a real-time sleep let the socket handler run in time.
            for increment in (.2, .2, .09):
                seen.clear()
                timeline.advance(increment)
                send_next.put_nowait(True)
                await wait_for_stage(task, seen)
            assert len(kinds) == 4 and not task.done()
            timeline.advance(.02)
            with pytest.raises(ModelError) as caught:
                await task
            assert caught.value.code == 'request_hard_timeout'
            assert kinds and set(kinds) == {'response.function_call_arguments.done'}
            observation = client.requests[0]
            assert observation['cancelled_by'] == 'request_hard_timeout'
            assert observation['stream_terminal'] is None
            assert session.pending_calls == [] and len(requests) == 1
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
