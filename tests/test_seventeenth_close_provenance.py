"""Real localhost streams exercise close attribution below ModelClient.aclose.

Controlled local actions establish these fixture causes. They do not establish
the cause of a production disconnect or capture request/response content.
"""
import asyncio
from contextlib import asynccontextmanager
import copy
import gc
import json
import ssl
import weakref

import pytest

from app.model import ModelClient, ModelError
from app import model_network
from tests.test_fourteenth_transport_close import chunk, event, terminal
from tests.test_sixteenth_model_network import certificate


SECRET = 'close_provenance_credential_must_not_escape'


@asynccontextmanager
async def persistent_origin(handler, certificate, *, tls=False):
    """Keep HTTP/1.1 sockets alive; each handler owns response boundaries."""
    requests, tasks, failures = [], set(), []

    async def accepted(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            while True:
                try:
                    headers = await reader.readuntil(b'\r\n\r\n')
                except asyncio.IncompleteReadError:
                    break
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                request = json.loads(await reader.readexactly(length))
                requests.append(request)
                if await handler(reader, writer, request, len(requests)) is False:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
            tasks.discard(task)

    context = None
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate[0]), str(certificate[1]))
    server = await asyncio.start_server(accepted, '127.0.0.1', 0, ssl=context)
    try:
        host = 'localhost' if tls else '127.0.0.1'
        yield f'{"https" if tls else "http"}://{host}:{server.sockets[0].getsockname()[1]}/v1', requests
    finally:
        server.close()
        await server.wait_closed()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        assert not failures, failures


async def headers(writer):
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                 b'Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n')
    await writer.drain()


async def complete_response(writer):
    await headers(writer)
    await chunk(writer, terminal())
    writer.write(b'0\r\n\r\n')
    await writer.drain()


def model_client(url, certificate, monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE', str(certificate[0]))
    return ModelClient({'model_base_url': url, 'model_name': 'local-close-provenance',
        '_model_api_key': SECRET, 'model_network_route': 'direct',
        'model_protocol': 'responses', 'model_native_search': False,
        'reasoning_effort': 'max', 'model_retries': 0,
        'model_request_hard_timeout_seconds': 3,
        'model_first_event_timeout_seconds': 2, 'model_stream_idle_timeout_seconds': 2})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.002)


def active_connection(client):
    connections = [connection for connection in client.http._transport._pool._connections
                   if connection._connection is not None and
                   connection._connection._state.name == 'ACTIVE']
    assert len(connections) == 1
    return connections[0]


def assert_close_targets(observation):
    close = observation['first_network_stream_close']
    assert close['event'] == 'stream.aclose.started'
    assert close['target_request_ids'] == [observation['local_transport_request_id']]
    assert close['target_request_count'] == 1 and not close['targets_truncated']
    assert close['connection_lifecycle_id'] == observation['connection_lifecycle_id']
    assert close['stream_id'] and close['task_id_hash']
    assert observation['network_stream_coverage']['status'] == 'partial'
    assert set(observation['network_stream_coverage']['observed_methods']) == {'read', 'write', 'aclose'}
    assert SECRET not in json.dumps(observation)
    return close


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
@pytest.mark.parametrize('layer', ['http11', 'pool'])
async def test_foreign_low_level_close_precedes_fault_without_high_level_close(
        certificate, monkeypatch, tls, layer):
    async def handler(reader, writer, request, ordinal):
        await headers(writer)
        await chunk(writer, event('response.created', response={'id': 'local-complete'}))
        await reader.read()
        return False

    async with persistent_origin(handler, certificate, tls=tls) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        task = asyncio.create_task(client.complete(client.session('foreign close', {}), []))
        try:
            await until(lambda: client.requests and client.requests[0].get('stream_event_count') == 1)
            connection = active_connection(client)

            async def close_elsewhere():
                if layer == 'pool':
                    await client.http._transport._pool._close_connections([connection])
                else:
                    await connection._connection.aclose()

            await asyncio.create_task(close_elsewhere(), name='unrelated-local-cleanup')
            with pytest.raises(ModelError):
                await task
            observation = client.requests[0]
            close = assert_close_targets(observation)
            fault = observation['first_transport_failure']
            assert observation['client_close_requests'] == []
            assert close['source_request_id'] is None
            assert close['sequence'] < fault['sequence']
            assert fault['network_stream_close_before_failure'] is True
            assert fault['closing_party'] == 'unknown'
            expected_frame = ('httpcore.pool.close_connections' if layer == 'pool'
                              else 'httpcore.http11.aclose')
            assert expected_frame in close['stack']
            assert close['close_origin'] == ('pool_maintenance' if layer == 'pool'
                                            else 'http11_connection_close')
            assert len(requests) == len(client.requests) == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose(source='test_fixture')


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
async def test_reused_connection_is_bound_before_second_response_headers(
        certificate, monkeypatch, tls):
    second_received = asyncio.Event()

    async def handler(reader, writer, request, ordinal):
        if ordinal == 1:
            await complete_response(writer)
        else:
            second_received.set()
            await reader.read()
            return False

    async with persistent_origin(handler, certificate, tls=tls) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        task = None
        try:
            await client.complete(client.session('first', {}), [])
            first = copy.deepcopy(client.requests[0])
            task = asyncio.create_task(client.complete(client.session('second', {}), []))
            await asyncio.wait_for(second_received.wait(), 2)
            second = client.requests[1]
            assert second['status_code'] is None and second['request_accepted'] is None
            assert second['connection_lifecycle_id'] == first['connection_lifecycle_id']
            gc.collect()
            await asyncio.sleep(0)
            assert not task.done() and second['first_network_stream_close'] is None
            assert client.requests[0] == first
            connection = active_connection(client)

            async def close_elsewhere():
                await connection._connection.aclose()

            await asyncio.create_task(close_elsewhere())
            with pytest.raises(ModelError):
                await task
            close = assert_close_targets(second)
            assert close['source_request_id'] is None
            assert close['sequence'] < second['first_transport_failure']['sequence']
            assert second['client_close_requests'] == []
            assert client.requests[0] == first
            assert len(requests) == 2
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose(source='test_fixture')


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
async def test_controlled_peer_eof_is_recorded_before_fault_and_local_cleanup(
        certificate, monkeypatch, tls):
    async def handler(reader, writer, request, ordinal):
        await headers(writer)
        # No complete SSE event and no final HTTP chunk. This is deliberately
        # malformed fixture output, never a captured production response.
        await chunk(writer, b'data: {"type":"response.created","fixture":"' + b'x' * 11900)
        return False

    async with persistent_origin(handler, certificate, tls=tls) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        try:
            with pytest.raises(ModelError):
                await client.complete(client.session('controlled EOF', {}), [])
            observation = client.requests[0]
            eof = observation['first_network_stream_eof']
            fault = observation['first_transport_failure']
            close = assert_close_targets(observation)
            assert eof['event'] == 'stream.read.eof'
            assert eof['sequence'] < fault['sequence'] < close['sequence']
            assert fault['network_stream_close_before_failure'] is False
            assert observation['stream_event_count'] == 0 and not observation['http_body_complete']
            assert observation['client_close_requests'] == [] and len(requests) == 1
            assert fault['closing_party'] == 'unknown'
        finally:
            await client.aclose(source='test_fixture')


async def test_http_eof_keeps_socket_open_and_later_close_does_not_rewrite_completed_observation(
        certificate, monkeypatch):
    async def handler(reader, writer, request, ordinal):
        await complete_response(writer)

    async with persistent_origin(handler, certificate) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        await client.complete(client.session('natural completion', {}), [])
        observation = client.requests[0]
        assert observation['http_body_complete'] and observation['stream_close_reason'] == 'http_eof'
        assert observation['first_transport_failure'] is None
        assert observation['first_network_stream_close'] is None
        assert observation['first_network_stream_eof'] is None  # HTTP EOF is not a socket EOF.
        frozen = copy.deepcopy(observation)
        await client.aclose(source='test_fixture')
        assert observation == frozen and len(requests) == 1
        closes = [item for item in client.network_diagnostics.lifecycle_events
                  if item['event'] == 'stream.aclose.started']
        assert closes and all(not item['target_request_ids'] for item in closes)


async def test_shared_client_cancel_does_not_target_parallel_session_or_reused_success(
        certificate, monkeypatch):
    ready = {name: asyncio.Event() for name in ('cancel', 'survive')}
    release = asyncio.Event()

    async def handler(reader, writer, request, ordinal):
        name = json.loads(request['input'][0]['content'])['name']
        await headers(writer)
        await chunk(writer, event('response.created', response={'id': 'local-complete'}))
        ready[name].set()
        if name == 'cancel':
            await reader.read()
            return False
        await release.wait()
        await chunk(writer, terminal())
        writer.write(b'0\r\n\r\n')
        await writer.drain()

    async with persistent_origin(handler, certificate) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        sessions = [client.session('parallel', {'name': name}) for name in ('cancel', 'survive')]
        tasks = [asyncio.create_task(client.complete(session, [])) for session in sessions]
        try:
            await asyncio.wait_for(asyncio.gather(*(item.wait() for item in ready.values())), 2)
            tasks[0].cancel('user_cancelled')
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            cancelled, surviving = (session.requests[0] for session in sessions)
            close = assert_close_targets(cancelled)
            assert close['source_request_id'] == cancelled['local_transport_request_id']
            assert surviving['first_network_stream_close'] is None
            assert surviving['local_transport_request_id'] not in close['target_request_ids']
            assert not tasks[1].done()
            release.set()
            await tasks[1]
            assert surviving['first_transport_failure'] is None and surviving['http_body_complete']
            assert cancelled['connection_lifecycle_id'] != surviving['connection_lifecycle_id']
            assert len(requests) == 2
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose(source='test_fixture')


async def test_close_provenance_never_reflects_task_names_or_code_symbols(
        certificate, monkeypatch):
    async def handler(reader, writer, request, ordinal):
        await headers(writer)
        await chunk(writer, event('response.created', response={'id': 'local-complete'}))
        await reader.read()
        return False

    async with persistent_origin(handler, certificate) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        task = asyncio.create_task(client.complete(client.session('safe symbols', {}), []), name=SECRET)
        try:
            await until(lambda: client.requests and client.requests[0].get('stream_event_count') == 1)
            connection = active_connection(client)

            async def reflected_symbol():
                await connection._connection.aclose()

            reflected_symbol.__code__ = reflected_symbol.__code__.replace(co_name=SECRET, co_qualname=SECRET)
            await asyncio.create_task(reflected_symbol(), name=SECRET)
            with pytest.raises(ModelError):
                await task
            assert_close_targets(client.requests[0])
            serialized = json.dumps({'requests': client.requests,
                                     'lifecycle': client.network_diagnostics.lifecycle_events})
            assert SECRET not in serialized
            assert 'Authorization' not in serialized and 'Bearer ' not in serialized
            assert 'response.created' not in json.dumps(client.requests[0]['network_stream_events'])
            assert len(requests) == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose(source='test_fixture')


async def test_wrapped_stream_does_not_keep_client_diagnostics_or_stream_alive(
        certificate, monkeypatch):
    async def handler(reader, writer, request, ordinal):
        await complete_response(writer)

    async with persistent_origin(handler, certificate) as (url, requests):
        async def consume_and_release():
            client = model_client(url, certificate, monkeypatch)
            session = client.session('weak lifecycle', {})
            await client.complete(session, [])
            pool = client.http._transport._pool
            stream = pool._connections[0]._connection._network_stream
            refs = [weakref.ref(value) for value in (client, client.network_diagnostics, stream)]
            frozen = copy.deepcopy(session.requests[0])
            await client.aclose(source='test_fixture')
            assert session.requests[0] == frozen
            return refs, session.requests[0]

        refs, retained_observation = await consume_and_release()
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
        assert all(reference() is None for reference in refs)
        assert retained_observation['http_body_complete'] and len(requests) == 1


async def test_stack_observer_failure_cannot_prevent_actual_stream_close(
        certificate, monkeypatch):
    async def handler(reader, writer, request, ordinal):
        await headers(writer)
        await chunk(writer, event('response.created', response={'id': 'local-complete'}))
        await reader.read()
        return False

    async with persistent_origin(handler, certificate) as (url, requests):
        client = model_client(url, certificate, monkeypatch)
        task = asyncio.create_task(client.complete(client.session('observer isolation', {}), []))
        stream = None
        try:
            await until(lambda: client.requests and client.requests[0].get('stream_event_count') == 1)
            stream = active_connection(client)._connection._network_stream

            def broken_stack():
                raise RuntimeError(SECRET)

            with monkeypatch.context() as broken:
                broken.setattr(model_network, '_close_stack', broken_stack)
                # Instrumentation must delegate to the original method even
                # when collecting its own diagnostics fails.
                await stream.aclose()
            with pytest.raises(ModelError):
                await task
            assert client.requests[0]['http_body_complete'] is False
            assert client.requests[0]['network_stream_coverage']['status'] == 'unavailable'
            assert client.requests[0]['network_stream_coverage']['reason'] == 'caller_observation_failed'
            assert SECRET not in json.dumps(client.network_diagnostics.lifecycle_events)
            assert len(requests) == 1
        finally:
            if stream is not None:
                await stream.aclose()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose(source='test_fixture')
