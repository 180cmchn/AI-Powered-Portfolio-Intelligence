"""Bounded diagnostics at real localhost HTTP/TLS lifecycle boundaries.

Fixture-controlled FIN/RST actions establish this test's cause. Production
diagnostics intentionally keep the closing party unknown without such evidence.
No paid endpoint, global TLS bypass, or replacement production transport.
"""
import asyncio
from contextlib import asynccontextmanager
import copy
import errno
import json
import socket
import ssl
import struct
import time

import httpcore
import pytest

from app.model import ModelClient, ModelError
from app.model_network import NetworkDiagnostics, safe_exception_details
from tests.test_fourteenth_transport_close import chunk, event, terminal
from tests.test_sixteenth_model_network import certificate


SECRET = 'sk-credential-reflection-must-not-be-logged'


def test_exception_graph_retains_suppressed_context_without_message_args_or_secrets():
    root = OSError(errno.ECONNRESET, SECRET)
    middle = httpcore.ReadError(SECRET)
    middle.__context__ = root
    middle.__suppress_context__ = True
    outer = httpcore.ReadError(SECRET)
    outer.__cause__ = middle
    # A hostile symbolic attribute must not become a second logging channel.
    reflected = type('credential_' + SECRET.replace('-', '_'), (Exception,), {})()
    reflected.__class__.__module__ = SECRET
    outer.__context__ = reflected
    graph = safe_exception_details(outer, secret=SECRET)
    assert [node['type'] for node in graph][:2] == ['ReadError', 'ReadError']
    assert any(node.get('errno') == errno.ECONNRESET and node['relation'] == 'context' for node in graph)
    serialized = json.dumps(graph)
    assert SECRET not in serialized and 'args' not in serialized and 'message' not in serialized
    assert len(graph) <= 8
    root.__context__ = outer  # Cycles must be bounded as well.
    assert len(safe_exception_details(outer, secret=SECRET)) == len(graph)


def test_ssl_fields_are_explicit_symbols_and_ints_only():
    error = ssl.SSLCertVerificationError(1, SECRET)
    error.reason, error.library, error.verify_code = 'CERTIFICATE_VERIFY_FAILED', 'SSL', 62
    details = safe_exception_details(error, secret=SECRET)[0]
    assert details['ssl_reason'] == 'CERTIFICATE_VERIFY_FAILED'
    assert details['ssl_library'] == 'SSL' and details['verify_code'] == 62
    error.reason, error.library, error.verify_code, error.winerror = SECRET, SECRET, SECRET, True
    details = safe_exception_details(error, secret=SECRET)[0]
    assert 'ssl_reason' not in details and 'ssl_library' not in details
    assert 'verify_code' not in details and 'winerror' not in details
    assert SECRET not in json.dumps(details)
    reflected = type('CredentialReflected', (Exception,), {})()
    reflected.__class__.__name__ = 'secret_identifier'
    reflected.__class__.__module__ = 'private.secret_identifier'
    assert safe_exception_details(reflected, secret='secret_identifier')[0]['type'] == 'redacted'
    assert safe_exception_details(reflected, secret='secret_identifier')[0]['module'] == 'redacted'


def test_first_failed_trace_survives_cleanup_and_finished_requests_are_not_rewritten():
    diagnostics = NetworkDiagnostics(secret=SECRET)
    observation = {'task_id': 'controlled-task', 'agent_id': 'manager', 'attempt': 1}
    diagnostics.begin_request(observation)
    started = time.perf_counter()
    diagnostics.trace('http11.receive_response_body.failed', {'exception': OSError(errno.ECONNRESET, SECRET)}, observation, started)
    first = copy.deepcopy(observation['first_transport_failure'])
    diagnostics.trace('http11.response_closed.started', {}, observation, started)
    diagnostics.trace('http11.response_closed.failed', {'exception': RuntimeError(SECRET)}, observation, started)
    assert observation['first_transport_failure'] == first
    assert observation['response_lifecycle_events'][0]['sequence'] > first['sequence']
    diagnostics.finish_request(observation)
    frozen = copy.deepcopy(observation)
    diagnostics.close_requested(source=SECRET)
    diagnostics.close_finished()
    assert observation == frozen
    assert diagnostics.lifecycle_events[0]['source'] == 'unspecified'
    assert diagnostics.lifecycle_events[0]['active_requests'] == []
    assert SECRET not in json.dumps(diagnostics.lifecycle_events)
    diagnostics.trace('http11.response_closed.complete', {}, observation, started)
    assert observation == frozen


def test_unknown_and_nonweak_streams_explicitly_report_unavailable_coverage():
    reflected = type('stream_' + SECRET.replace('-', '_'), (), {})()
    for stream, reason in ((object(), 'weak_identity_unavailable'),
                           (reflected, 'unsupported_stream_type')):
        diagnostics = NetworkDiagnostics(secret=SECRET)
        observation = {}
        diagnostics.begin_request(observation)
        diagnostics.bind_response(observation, {'network_stream': stream})
        coverage = observation['network_stream_coverage']
        assert coverage['layer'] == 'httpcore_stream'
        assert coverage['status'] == 'unavailable' and coverage['reason'] == reason
        assert coverage['observed_methods'] == []
        assert SECRET not in json.dumps(observation)
        diagnostics.finish_request(observation)


def test_preexisting_stream_method_override_is_preserved_and_not_claimed_observed():
    from httpcore._backends.anyio import AnyIOStream

    stream = AnyIOStream(object())

    async def private_reader(*args, **kwargs):
        raise AssertionError(SECRET)  # The observer must neither call nor replace it.

    stream.read = private_reader
    original_close = stream.aclose.__func__
    diagnostics = NetworkDiagnostics(secret=SECRET)
    observation = {}
    diagnostics.begin_request(observation)
    diagnostics.bind_response(observation, {'network_stream': stream})
    assert stream.read is private_reader and stream.aclose.__func__ is original_close
    assert observation['network_stream_coverage']['status'] == 'unavailable'
    assert observation['network_stream_coverage']['reason'] == 'instance_method_override'
    assert SECRET not in json.dumps(observation)
    diagnostics.finish_request(observation)


@asynccontextmanager
async def wire_origin(handler, certificate, *, tls):
    requests, tasks, failures = [], set(), []
    async def accepted(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            requests.append(json.loads(await reader.readexactly(length)))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                         b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            await writer.drain()
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures.append(error)
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
        hostname = 'localhost' if tls else '127.0.0.1'
        yield f'{"https" if tls else "http"}://{hostname}:{server.sockets[0].getsockname()[1]}/v1', requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert not failures, failures


def wire_client(url, certificate, monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE', str(certificate[0]))
    return ModelClient({'model_base_url': url, 'model_name': 'controlled-network-model',
        '_model_api_key': SECRET, 'model_network_route': 'direct',
        'model_protocol': 'responses', 'model_native_search': False,
        'reasoning_effort': 'max', 'model_retries': 3,
        'model_request_hard_timeout_seconds': 3,
        'model_first_event_timeout_seconds': 2, 'model_stream_idle_timeout_seconds': 2})


async def body_seen(client):
    async with asyncio.timeout(2):
        while not client.requests or not client.requests[-1].get('stream_event_count'):
            await asyncio.sleep(.002)


async def fixture_close(writer, kind):
    if kind == 'rst':
        # This is controlled fixture evidence, not a classification inferred
        # from the exception message. Mac/Linux may expose different errno.
        writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        writer.transport.abort()
    else:
        writer.close()


async def test_real_tls_verification_failure_retains_symbolic_ssl_evidence(certificate, monkeypatch):
    async def handler(reader, writer):
        pytest.fail('A rejected certificate must not reach the model request handler')
    async with wire_origin(handler, certificate, tls=True) as (url, requests):
        client = wire_client(url.replace('localhost', '127.0.0.1'), certificate, monkeypatch)
        client.config['model_retries'] = 0
        try:
            with pytest.raises(ModelError):
                await client.complete(client.session('verify TLS', {}), [])
            fault = client.requests[0]['first_transport_failure']
            assert fault['event'] == 'start_tls.failed'
            assert any(node.get('ssl_reason') == 'CERTIFICATE_VERIFY_FAILED' and
                       type(node.get('verify_code')) is int for node in fault['exception_chain'])
            assert not requests and SECRET not in json.dumps(fault)
        finally:
            await client.aclose(source='test_fixture')


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
@pytest.mark.parametrize('kind', ['fin', 'rst'])
@pytest.mark.parametrize('completed', [False, True], ids=['before-terminal', 'after-terminal'])
async def test_controlled_remote_close_keeps_first_fault_without_unknown_generation_replay(
        certificate, monkeypatch, tls, kind, completed):
    async def handler(reader, writer):
        await chunk(writer, terminal() if completed else event('response.created', response={'id': 'controlled-open'}))
        await asyncio.sleep(.02)
        await fixture_close(writer, kind)
    async with wire_origin(handler, certificate, tls=tls) as (url, requests):
        client = wire_client(url, certificate, monkeypatch)
        session = client.session('controlled network fixture', {'task_id': 'task-network'})
        try:
            if completed:
                result = await client.complete(session, [])
                assert result.text == 'Complete visible result'
            else:
                with pytest.raises(ModelError):
                    await client.complete(session, [])
                assert session.pending_calls == []
                assert len(session.history) == 1
            observation = client.requests[0]
            fault = observation['first_transport_failure']
            assert fault['event'] == 'receive_response_body.failed'
            assert fault['exception_chain'] and fault['closing_party'] == 'unknown'
            assert fault['close_requested_before_failure'] is False
            assert observation['client_close_requests'] == [] and observation['cancelled_by'] is None
            assert observation['model_client_id'] and observation['connection_lifecycle_id']
            assert all(item['sequence'] > fault['sequence'] for item in observation['response_lifecycle_events'])
            assert observation['http_body_complete'] is False
            if completed:
                assert observation['error'] is None
                assert observation['stream_close_reason'] == 'terminal_tail_transport_error'
            else:
                assert observation['retry_suppressed'] == 'accepted_or_sent_request_outcome_unknown'
            assert len(requests) == len(client.requests) == 1
            assert SECRET not in json.dumps(client.requests)
        finally:
            await client.aclose(source='test_fixture')


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
@pytest.mark.parametrize('action', ['task_cancel', 'client_close'])
async def test_explicit_local_close_and_cancel_are_ordered_before_cleanup(certificate, monkeypatch, tls, action):
    async def handler(reader, writer):
        await chunk(writer, event('response.created', response={'id': 'controlled-wait'}))
        await reader.read()
    async with wire_origin(handler, certificate, tls=tls) as (url, requests):
        client = wire_client(url, certificate, monkeypatch)
        session = client.session('local lifecycle', {'task_id': 'task-local-lifecycle'})
        task = asyncio.create_task(client.complete(session, []))
        try:
            await body_seen(client)
            if action == 'task_cancel':
                task.cancel('user_cancelled')
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await client.aclose(source='test_fixture')
                with pytest.raises(ModelError):
                    await asyncio.wait_for(task, 1)
            observation = client.requests[0]
            fault = observation['first_transport_failure']
            assert fault and fault['event'] == 'receive_response_body.failed'
            assert bool(observation['client_close_requests']) is (action == 'client_close')
            assert fault['close_requested_before_failure'] is (action == 'client_close')
            if action == 'client_close':
                assert observation['client_close_requests'][0]['sequence'] < fault['sequence']
                assert observation['client_close_requests'][0]['source'] == 'test_fixture'
            else:
                assert observation['error'] == 'cancelled'
                assert observation['stream_close_reason'] == 'local_cancellation'
            assert len(requests) == 1 and session.pending_calls == []
            assert observation['http_body_complete'] is False
            assert all(item['sequence'] > fault['sequence'] for item in observation['response_lifecycle_events'])
            assert SECRET not in json.dumps(observation)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose(source='test_fixture')


@pytest.mark.parametrize('tls', [False, True], ids=['http', 'tls'])
async def test_closing_one_account_client_does_not_close_parallel_account(certificate, monkeypatch, tls):
    release = asyncio.Event()
    async def handler(reader, writer):
        await chunk(writer, event('response.created', response={'id': 'local-complete'}))
        await release.wait()
        await chunk(writer, terminal())
        writer.write(b'0\r\n\r\n')
        await writer.drain()
    async with wire_origin(handler, certificate, tls=tls) as (url, requests):
        first, second = wire_client(url, certificate, monkeypatch), wire_client(url, certificate, monkeypatch)
        sessions = [first.session('account one', {'task_id': 'task-one'}), second.session('account two', {'task_id': 'task-two'})]
        pending = [asyncio.create_task(client.complete(session, [])) for client, session in zip((first, second), sessions)]
        try:
            await asyncio.gather(body_seen(first), body_seen(second))
            await first.aclose(source='test_fixture')
            with pytest.raises(ModelError):
                await asyncio.wait_for(pending[0], 1)
            assert not pending[1].done()
            release.set()
            result = await asyncio.wait_for(pending[1], 1)
            assert result.text == 'Complete visible result'
            left, right = first.requests[0], second.requests[0]
            for field in ('model_client_id', 'response_lifecycle_id', 'local_transport_request_id', 'connection_lifecycle_id'):
                assert left[field] != right[field]
            assert right['first_transport_failure'] is None and right['client_close_requests'] == []
            assert right['http_body_complete'] is True and len(requests) == 2
            closed = first.network_diagnostics.lifecycle_events[0]
            assert [item['task_id'] for item in closed['active_requests']] == ['task-one']
        finally:
            release.set()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await first.aclose(source='test_fixture')
            await second.aclose(source='test_fixture')
