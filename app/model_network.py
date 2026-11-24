"""Model-only connection policy. Market/material clients do not use this module.

HTTPX 0.28 treats proxy and certificate environment as one trust_env flag. A
direct client therefore gets an explicit verified SSLContext before disabling
proxy environment lookup. No system environment or network setting is changed.
"""
from __future__ import annotations

import os
import asyncio
import hashlib
import inspect
import ssl
import copy
import re
import sys
import time
import uuid
import weakref
from datetime import timezone

import certifi

from .clock import now_datetime


# These are diagnostic symbols, never an exception's free-form message/args.
# Unknown TLS reason strings are deliberately omitted rather than logged.
_SSL_REASONS = frozenset({
    'CERTIFICATE_VERIFY_FAILED', 'UNEXPECTED_EOF_WHILE_READING',
    'SSLV3_ALERT_HANDSHAKE_FAILURE', 'SSLV3_ALERT_BAD_CERTIFICATE',
    'TLSV1_ALERT_INTERNAL_ERROR', 'TLSV1_ALERT_PROTOCOL_VERSION',
    'TLSV1_ALERT_UNKNOWN_CA', 'TLSV1_ALERT_DECODE_ERROR',
    'WRONG_VERSION_NUMBER', 'DECRYPTION_FAILED_OR_BAD_RECORD_MAC',
    'APPLICATION_DATA_AFTER_CLOSE_NOTIFY', 'SHUTDOWN_WHILE_IN_INIT',
    'UNSUPPORTED_PROTOCOL', 'NO_SHARED_CIPHER', 'BAD_RECORD_TYPE',
    'HTTP_REQUEST', 'HTTPS_PROXY_REQUEST', 'UNKNOWN_PROTOCOL',
})
_TRACE_OPERATIONS = frozenset({
    'connect_tcp', 'connect_unix_socket', 'start_tls', 'send_request_headers',
    'send_request_body', 'receive_response_headers', 'receive_response_body',
    'response_closed', 'close',
})
_CLOSE_SOURCES = frozenset({
    'unspecified', 'runtime_close', 'runtime_completed', 'runtime_failed',
    'runtime_cancelled', 'configuration_probe', 'test_fixture',
})
_EXCEPTION_MODULE_ROOTS = frozenset({'builtins', 'ssl', 'socket', 'asyncio',
    'httpcore', 'httpx', 'anyio', 'concurrent', 'h11', 'h2'})
_STREAM_TYPES = frozenset({
    ('httpcore._backends.anyio', 'AnyIOStream'),
    ('httpcore._backends.trio', 'TrioStream'),
    ('httpcore._async.http11', 'AsyncHTTP11UpgradeStream'),
})
# Only fixed labels leave this lookup. No frame locals, source lines, file
# paths, task names, or arbitrary caller symbols enter observations.
_CLOSE_FRAMES = {
    ('app.model', 'ModelClient.aclose'): 'model.client.aclose',
    ('app.model', 'ModelClient._receive_stream'): 'model.response_context',
    ('app.model', 'ModelClient._receive_stream.<locals>.drain_terminal_tail'): 'model.terminal_tail',
    ('httpx._client', 'AsyncClient.aclose'): 'httpx.client.aclose',
    ('httpx._client', 'AsyncClient.stream'): 'httpx.response_context',
    ('httpx._client', 'BoundAsyncStream.aclose'): 'httpx.bound_stream.aclose',
    ('httpx._models', 'Response.aclose'): 'httpx.response.aclose',
    ('httpx._transports.default', 'AsyncHTTPTransport.aclose'): 'httpx.transport.aclose',
    ('httpx._transports.default', 'AsyncResponseStream.aclose'): 'httpx.response_stream.aclose',
    ('httpcore._async.connection_pool', 'AsyncConnectionPool.aclose'): 'httpcore.pool.aclose',
    ('httpcore._async.connection_pool', 'AsyncConnectionPool._close_connections'): 'httpcore.pool.close_connections',
    ('httpcore._async.connection_pool', 'PoolByteStream.aclose'): 'httpcore.pool_response.aclose',
    ('httpcore._async.connection', 'AsyncHTTPConnection.aclose'): 'httpcore.connection.aclose',
    ('httpcore._async.http11', 'AsyncHTTP11Connection.aclose'): 'httpcore.http11.aclose',
    ('httpcore._async.http11', 'AsyncHTTP11Connection._response_closed'): 'httpcore.http11.response_closed',
    ('httpcore._async.http11', 'HTTP11ConnectionByteStream.aclose'): 'httpcore.http11.response_stream.aclose',
    ('httpcore._async.http2', 'AsyncHTTP2Connection.aclose'): 'httpcore.http2.aclose',
    ('httpcore._async.http2', 'AsyncHTTP2Connection._response_closed'): 'httpcore.http2.response_closed',
    ('httpcore._async.http2', 'HTTP2ConnectionByteStream.aclose'): 'httpcore.http2.response_stream.aclose',
    ('httpcore._backends.anyio', 'AnyIOStream.start_tls'): 'httpcore.stream.start_tls',
    ('httpcore._backends.trio', 'TrioStream.start_tls'): 'httpcore.stream.start_tls',
}


def _close_stack():
    labels = []
    frame = sys._getframe(1)
    try:
        # Bound traversal as well as output; unknown callers are not rendered.
        for _ in range(32):
            if frame is None:
                break
            label = _CLOSE_FRAMES.get((frame.f_globals.get('__name__'), frame.f_code.co_qualname))
            if label and label not in labels:
                labels.append(label)
            frame = frame.f_back
    finally:
        del frame
    # Preserve the immediate operation and the outer known owner when the
    # normal close delegation chain has more than six recognized frames.
    return (labels if len(labels) <= 6 else labels[:3] + labels[-3:]) or ['unknown']


def _close_origin(stack):
    for label, origin in (
        ('model.client.aclose', 'model_client_close'),
        ('httpx.client.aclose', 'httpx_client_close'),
        ('httpx.transport.aclose', 'httpx_transport_close'),
        ('httpcore.pool.aclose', 'pool_close'),
        ('httpcore.pool.close_connections', 'pool_maintenance'),
        ('httpcore.http11.response_closed', 'http11_response_cleanup'),
        ('httpcore.http2.response_closed', 'http2_response_cleanup'),
        ('httpcore.stream.start_tls', 'tls_upgrade_cleanup'),
        ('httpcore.http11.aclose', 'http11_connection_close'),
        ('httpcore.http2.aclose', 'http2_connection_close'),
    ):
        if label in stack:
            return origin
    return 'unknown'


def _stream_method_observer(diagnostics, stream, metadata, name, method):
    # Capture weak object references and an unbound class function. Retaining
    # an original bound method here would create a stream -> wrapper -> stream
    # cycle and change resource lifetime merely by observing it.
    owner_ref, stream_ref = weakref.ref(diagnostics), weakref.ref(stream)

    async def observed(*args, **kwargs):
        target, owner = stream_ref(), owner_ref()
        if target is None:
            raise RuntimeError('Observed network stream is no longer available')
        if owner is not None and name in {'read', 'write'}:
            owner._observe_safely(owner._use_stream, metadata)
        stack = None
        if name == 'aclose':
            try:
                stack = _close_stack()
            except Exception:
                stack = ['unknown']
                if owner is not None:
                    owner._observe_safely(owner._coverage_failed, metadata, 'caller_observation_failed')
        if owner is not None and name == 'aclose':
            owner._observe_safely(owner._stream_event, 'stream.aclose.started', metadata, stack=stack)
        try:
            result = await method(target, *args, **kwargs)
        except BaseException as error:
            if owner is not None and name in {'read', 'aclose'}:
                owner._observe_safely(owner._stream_event, 'stream.' + name + '.failed',
                                      metadata, error=error, stack=stack)
            raise
        if owner is not None:
            if name == 'read' and result == b'':
                owner._observe_safely(owner._stream_event, 'stream.read.eof', metadata)
            elif name == 'aclose':
                owner._observe_safely(owner._stream_event, 'stream.aclose.complete', metadata, stack=stack)
        return result

    return observed


def _safe_symbol(value, secret='', *, dotted=False):
    pattern = r'[A-Za-z_][A-Za-z0-9_.]{0,127}' if dotted else r'[A-Za-z_][A-Za-z0-9_]{0,95}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return 'unrecognized'
    if secret and secret in value:
        return 'redacted'
    return value


def safe_exception_details(exception, *, secret=''):
    """Bounded cause *and context* graph before httpcore clears the cause.

    No str/repr, arguments, headers, stream contents or arbitrary attributes are
    retained. An absent errno/SSL symbol means unknown, not a clean network.
    """
    pending, seen, result = [(exception, None, 'exception')], set(), []
    while pending and len(result) < 8:
        error, parent, relation = pending.pop(0)
        if not isinstance(error, BaseException) or id(error) in seen:
            continue
        seen.add(id(error))
        module = _safe_symbol(type(error).__module__, secret, dotted=True)
        trusted_module = module.split('.', 1)[0] in _EXCEPTION_MODULE_ROOTS
        item = {'type': _safe_symbol(type(error).__name__, secret) if trusted_module else
                        'redacted' if module == 'redacted' else 'unrecognized',
                'module': module if trusted_module or module == 'redacted' else 'unrecognized',
                'relation': relation, 'parent_index': parent}
        for field in ('errno', 'winerror', 'verify_code'):
            value = getattr(error, field, None)
            if type(value) is int and -(2 ** 31) <= value < 2 ** 31:
                item[field] = value
        if isinstance(error, ssl.SSLError):
            reason, library = getattr(error, 'reason', None), getattr(error, 'library', None)
            if isinstance(reason, str) and reason in _SSL_REASONS and not (secret and secret in reason):
                item['ssl_reason'] = reason
            elif reason is not None:
                item['ssl_reason_unrecognized'] = True
            if library == 'SSL' and not (secret and secret in library):
                item['ssl_library'] = library
        index = len(result)
        result.append(item)
        for field, link in (('__cause__', 'cause'), ('__context__', 'context')):
            linked = getattr(error, field, None)
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append((linked, index, link))
    return result


class NetworkDiagnostics:
    """Small local lifecycle observer; identifiers do not imply wire evidence.

    Connection IDs correlate observed transport objects within one ModelClient,
    not TCP tuples or the identity of the party closing a physical connection.
    Completed observations are never revised by a later client.close event.
    """
    def __init__(self, *, secret=''):
        self.client_id = uuid.uuid4().hex
        self._secret = secret
        self._sequence = 0
        self._active = {}
        self._connections = weakref.WeakKeyDictionary()
        self._streams = weakref.WeakKeyDictionary()
        self._task_requests = weakref.WeakKeyDictionary()
        self._task_hashes = weakref.WeakKeyDictionary()
        self._request_tasks = {}
        self._request_streams = {}
        self.lifecycle_events = []

    def _stamp(self, event):
        self._sequence += 1
        return {'event': event, 'sequence': self._sequence,
                'at': now_datetime(timezone.utc).isoformat(),
                'monotonic_ns': time.monotonic_ns()}

    def begin_request(self, observation):
        request_id = uuid.uuid4().hex
        observation.update(model_client_id=self.client_id,
            local_transport_request_id=request_id,
            response_lifecycle_id=uuid.uuid4().hex,
            connection_lifecycle_id=None, first_transport_failure=None,
            response_lifecycle_events=[], client_close_requests=[],
            network_stream_events=[], first_network_stream_close=None,
            first_network_stream_eof=None,
            network_stream_coverage=self._coverage('unavailable', 'stream_not_observed'))
        self._active[request_id] = observation
        self._request_streams[request_id] = set()
        task = self._current_task()
        if task is not None:
            self._task_requests.setdefault(task, []).append(request_id)
            self._request_tasks[request_id] = weakref.ref(task)
            observation['request_task_hash'] = self._task_hash(task)

    def finish_request(self, observation):
        request_id = observation.get('local_transport_request_id')
        self._active.pop(request_id, None)
        self._request_streams.pop(request_id, None)
        task_ref = self._request_tasks.pop(request_id, None)
        task = task_ref() if task_ref else None
        if task is not None:
            remaining = [item for item in self._task_requests.get(task, []) if item != request_id]
            if remaining:
                self._task_requests[task] = remaining
            else:
                self._task_requests.pop(task, None)

    @staticmethod
    def _coverage(status, reason):
        return {'layer': 'httpcore_stream', 'status': status, 'reason': reason,
                'observed_methods': ['read', 'write', 'aclose'] if status == 'partial' else [],
                'excludes': ['os_transport_close', 'direct_underlying_socket_close']}

    @staticmethod
    def _current_task():
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _task_hash(self, task):
        if task is None:
            return None
        value = self._task_hashes.get(task)
        if value is None:
            # Task names and repr may contain caller data. Hash only a new
            # opaque nonce, never a memory address or caller-controlled name.
            value = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            self._task_hashes[task] = value
        return value

    def _current_request(self):
        task = self._current_task()
        if task is None:
            return None
        return next((item for item in reversed(self._task_requests.get(task, []))
                     if item in self._active), None)

    def _observe_safely(self, operation, *args, **kwargs):
        try:
            operation(*args, **kwargs)
        except Exception:
            # Observation must not interrupt an actual socket operation. Mark
            # degraded coverage without retaining the observer exception text.
            try:
                metadata = next((item for item in args if isinstance(item, dict)
                                 and 'stream_id' in item and 'connection_lifecycle_id' in item), None)
                if metadata is not None:
                    self._coverage_failed(metadata, 'observer_failed')
            except Exception:
                pass  # Even a failed degradation marker cannot block I/O.

    def _coverage_failed(self, metadata, reason):
        metadata['unavailable_reason'] = reason
        for request_id, streams in self._request_streams.items():
            if metadata['stream_id'] in streams and request_id in self._active:
                self._active[request_id]['network_stream_coverage'] = self._coverage('unavailable', reason)

    def _use_stream(self, metadata):
        request_id = self._current_request()
        if request_id is None:
            return
        observation = self._active[request_id]
        self._request_streams[request_id].add(metadata['stream_id'])
        observation['connection_lifecycle_id'] = metadata['connection_lifecycle_id']
        observation['network_stream_coverage'] = (
            self._coverage('unavailable', metadata['unavailable_reason'])
            if metadata.get('unavailable_reason') else
            self._coverage('partial', 'python_method_boundary_only'))

    def _stream_event(self, event, metadata, *, error=None, stack=None):
        target_ids = [request_id for request_id, streams in self._request_streams.items()
                      if metadata['stream_id'] in streams and request_id in self._active]
        stack = list(stack or [])
        stamp = {**self._stamp(event), 'stream_id': metadata['stream_id'],
            'connection_lifecycle_id': metadata['connection_lifecycle_id'],
            'task_id_hash': self._task_hash(self._current_task()),
            'source_request_id': self._current_request(),
            'target_request_ids': target_ids[:64], 'target_request_count': len(target_ids),
            'targets_truncated': len(target_ids) > 64,
            'close_origin': _close_origin(stack) if event.startswith('stream.aclose.') else None,
            'stack': stack[:6],
            'stack_unavailable': metadata.get('unavailable_reason') == 'caller_observation_failed'}
        if error is not None:
            stamp['exception_chain'] = safe_exception_details(error, secret=self._secret)
        # This independent ring also retains closes of idle/unassociated streams.
        self.lifecycle_events = [*self.lifecycle_events, copy.deepcopy(stamp)][-128:]
        for request_id in target_ids:
            observation = self._active[request_id]
            observation['network_stream_events'] = [
                *observation['network_stream_events'], copy.deepcopy(stamp)][-32:]
            if event == 'stream.aclose.started' and observation['first_network_stream_close'] is None:
                observation['first_network_stream_close'] = copy.deepcopy(stamp)
            if event == 'stream.read.eof' and observation['first_network_stream_eof'] is None:
                observation['first_network_stream_eof'] = copy.deepcopy(stamp)

    def _instrument_stream(self, stream, identity):
        existing = self._streams.get(stream)
        if existing is not None:
            return existing, None
        stream_type = type(stream)
        if (stream_type.__module__, stream_type.__name__) not in _STREAM_TYPES:
            return None, 'unsupported_stream_type'
        namespace = getattr(stream, '__dict__', None)
        if not isinstance(namespace, dict):
            return None, 'instance_not_writable'
        if '_model_network_diagnostic_owner' in namespace:
            return None, 'stream_already_observed_elsewhere'
        names = ('read', 'write', 'aclose')
        if any(name in namespace for name in names):
            return None, 'instance_method_override'
        methods = {name: getattr(stream_type, name, None) for name in names}
        if not all(inspect.iscoroutinefunction(method) for method in methods.values()):
            return None, 'unsupported_stream_methods'
        metadata = {'stream_id': uuid.uuid4().hex, 'connection_lifecycle_id': identity}
        installed = []
        try:
            for name, method in methods.items():
                setattr(stream, name, _stream_method_observer(self, stream, metadata, name, method))
                installed.append(name)
            stream._model_network_diagnostic_owner = weakref.ref(self)
        except Exception:
            metadata['unavailable_reason'] = 'instance_not_writable'
            for name in installed:
                try:
                    delattr(stream, name)
                except (AttributeError, TypeError):
                    pass
            return None, 'instance_not_writable'
        self._streams[stream] = metadata
        return metadata, None

    def _bind_stream(self, observation, stream):
        request_id = observation.get('local_transport_request_id')
        if request_id not in self._active:
            return
        if stream is None:
            return
        try:
            identity = self._connections.get(stream)
            if identity is None:
                identity = observation.get('connection_lifecycle_id') or uuid.uuid4().hex
                self._connections[stream] = identity
            observation['connection_lifecycle_id'] = identity
            metadata, unavailable = self._instrument_stream(stream, identity)
            if metadata is not None:
                self._request_streams[request_id].add(metadata['stream_id'])
                observation['network_stream_coverage'] = (
                    self._coverage('unavailable', metadata['unavailable_reason'])
                    if metadata.get('unavailable_reason') else
                    self._coverage('partial', 'python_method_boundary_only'))
            else:
                observation['network_stream_coverage'] = self._coverage('unavailable', unavailable)
        except TypeError:
            # Some injected transports expose objects without weakrefs. Do not
            # claim pooled-connection identity from a recycled memory address.
            observation['connection_identity_unavailable'] = True
            observation['network_stream_coverage'] = self._coverage('unavailable', 'weak_identity_unavailable')
        except Exception:
            observation['network_stream_coverage'] = self._coverage('unavailable', 'observer_install_failed')

    def bind_response(self, observation, extensions):
        self._bind_stream(observation, extensions.get('network_stream'))

    def trace(self, event, info, observation, started):
        if observation.get('local_transport_request_id') not in self._active:
            return
        pieces = event.rsplit('.', 2) if isinstance(event, str) else []
        if len(pieces) < 2:
            return
        operation, outcome = pieces[-2:]
        if operation not in _TRACE_OPERATIONS or outcome not in {'started', 'complete', 'failed'}:
            return
        safe_event = operation + '.' + outcome
        stamp = {**self._stamp(safe_event),
                 'trace_namespace': pieces[0] if len(pieces) == 3 and pieces[0] in
                    {'connection', 'http11', 'http2', 'proxy', 'socks_proxy'} else 'unrecognized',
                 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}
        if operation in {'connect_tcp', 'connect_unix_socket'} and outcome == 'started':
            observation['connection_lifecycle_id'] = uuid.uuid4().hex
        if operation in {'connect_tcp', 'connect_unix_socket', 'start_tls'} and outcome == 'complete':
            self._bind_stream(observation, info.get('return_value'))
        if outcome == 'failed' and observation.get('first_transport_failure') is None:
            observation['first_transport_failure'] = {
                **stamp, 'exception_chain': safe_exception_details(info.get('exception'), secret=self._secret),
                'close_requested_before_failure': bool(observation.get('client_close_requests')),
                'network_stream_close_before_failure': observation.get('first_network_stream_close') is not None,
                'closing_party': 'unknown',
            }
        if operation in {'response_closed', 'close'}:
            observation['response_lifecycle_events'] = [
                *observation.get('response_lifecycle_events', []), stamp][-16:]

    def close_requested(self, source='unspecified'):
        source = source if source in _CLOSE_SOURCES else 'unspecified'
        stamp = {**self._stamp('client.close_requested'), 'source': source,
            'model_client_id': self.client_id,
            'active_requests': [{key: item.get(key) for key in
                ('local_transport_request_id', 'response_lifecycle_id', 'connection_lifecycle_id',
                 'task_id', 'agent_id', 'attempt')} for item in self._active.values()]}
        self.lifecycle_events = [*self.lifecycle_events, stamp][-128:]
        for observation in self._active.values():
            observation['client_close_requests'] = [*observation['client_close_requests'],
                {key: stamp[key] for key in ('event', 'sequence', 'at', 'monotonic_ns', 'source')}][-16:]
        return copy.deepcopy(stamp)

    def close_finished(self):
        stamp = {**self._stamp('client.close_finished'), 'model_client_id': self.client_id}
        self.lifecycle_events = [*self.lifecycle_events, stamp][-128:]
        return copy.deepcopy(stamp)


def network_policy(config: dict) -> dict:
    route = config.get('model_network_route', 'environment')
    if route not in {'environment', 'direct'}:
        raise ValueError('model_network_route 必须为 environment 或 direct')
    return {'route': route, 'revision': config.get('model_network_revision', 0)}


def network_observation(config: dict, *, custom_transport=False) -> dict:
    """Configuration is not evidence of an actually observed physical path."""
    return {**network_policy(config),
            'transport': 'injected' if custom_transport else 'httpx',
            'environment_proxy_present': any(os.environ.get(key) for key in
                ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy')),
            'environment_no_proxy_present': bool(os.environ.get('NO_PROXY') or os.environ.get('no_proxy')),
            'ca_source': 'transport_controlled' if custom_transport else
                         'SSL_CERT_FILE' if os.environ.get('SSL_CERT_FILE') else
                         'SSL_CERT_DIR' if os.environ.get('SSL_CERT_DIR') else 'certifi',
            'certificate_verification': None if custom_transport else True,
            'proxy_connect_observed': False,
            'physical_route': 'not_observable'}


def model_http_options(config: dict, transport=None) -> dict:
    route = network_policy(config)['route']
    if route == 'environment':
        return {'trust_env': True, 'transport': transport}
    # Match HTTPX's documented SSL_CERT_FILE > SSL_CERT_DIR > certifi order,
    # without using the private httpx._config API or disabling TLS validation.
    if os.environ.get('SSL_CERT_FILE'):
        context = ssl.create_default_context(cafile=os.environ['SSL_CERT_FILE'])
    elif os.environ.get('SSL_CERT_DIR'):
        context = ssl.create_default_context(capath=os.environ['SSL_CERT_DIR'])
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    return {'trust_env': False, 'verify': context, 'transport': transport}
