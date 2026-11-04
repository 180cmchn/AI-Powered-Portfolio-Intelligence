"""Model route policy through real TCP/TLS and normal managed worker processes.

All keys, model responses, proxy and certificates here are controlled fixtures;
these checks do not represent successful third-party model research.
"""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import ssl
import subprocess
import threading
import time

import httpx
import pytest

from app.config import model_config_summary
from app.domain import AccountInput
from app.model import ModelClient, ModelError, native_search_fingerprint
from app.store import Store
from tests.helpers_sixth import configure_test_team, FixtureCredentials
from tests.test_ninth_lifecycle import repository, control


def reply(output):
    return {'id': 'controlled-response', 'status': 'completed', 'output': output}


def call(name, arguments):
    return {'type': 'function_call', 'call_id': 'controlled-call', 'name': name,
            'arguments': json.dumps(arguments, ensure_ascii=False)}


@pytest.fixture
def certificate(tmp_path):
    cfg = tmp_path / 'certificate.conf'
    cfg.write_text('[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n'
                   '[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost\n'
                   'basicConstraints=critical,CA:TRUE\nkeyUsage=digitalSignature,keyEncipherment,keyCertSign\n')
    key, cert = tmp_path / 'fixture.key', tmp_path / 'fixture.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(cert), '-config', str(cfg)],
                   check=True, capture_output=True)
    return cert, key


@contextmanager
def origin(certificate):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_):
            pass
        def answer(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            requests.append({'method': 'GET', 'path': self.path})
            self.answer({'data': [{'id': 'controlled-model'}]})
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'method': 'POST', 'path': self.path, 'payload': payload})
            tools = [t.get('name') for t in payload.get('tools', [])]
            if 'capability_echo' in tools:
                results = [item for item in payload['input'] if item.get('type') == 'function_call_output']
                output = ([{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text',
                           'text': json.loads(results[-1]['output'])['token']}]}] if results else [call('capability_echo', {})])
            elif 'finish_analysis' in tools:
                output = [call('finish_analysis', {'report': '受控路线验证账户现金 100000 元，零持仓。暂不投入；待明确候选及风险承受范围后重新评估。未读取真实市场材料，本报告仅作通信夹具。',
                    'decision_summary': '保留现金，明确候选与风险承受范围后复核。', 'completion': 'limited',
                    'scope': ['cash_only'], 'limitations': ['受控响应，未读取真实市场材料。']})]
            else:
                output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'controlled-completion'}]}]
            self.answer(reply(output))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate[0]), str(certificate[1]))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'https://localhost:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def proxy():
    tunnels = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_CONNECT(self):
            host, port = self.path.rsplit(':', 1)
            # This fixture can tunnel only to the test's loopback origin.
            assert host in {'localhost', '127.0.0.1'}
            tunnels.append({'host': host, 'port': int(port)})
            with socket.create_connection(('127.0.0.1', int(port)), timeout=5) as upstream:
                self.send_response(200, 'Connection established')
                self.end_headers()
                self.connection.setblocking(False)
                upstream.setblocking(False)
                while True:
                    readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                    if not readable:
                        return
                    for source in readable:
                        body = source.recv(65536)
                        if not body:
                            return
                        target = upstream if source is self.connection else self.connection
                        target.sendall(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://fixture-user:fixture-password@127.0.0.1:{server.server_port}', tunnels
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def config(base, route='environment'):
    return {'model_base_url': base, 'model_protocol': 'responses', 'model_name': 'controlled-model',
            '_model_api_key': 'controlled-key', 'reasoning_effort': 'medium', 'model_native_search': False,
            'model_responses_stream': False, 'model_retries': 0, 'model_network_route': route,
            'model_network_revision': 4}


def install_network(monkeypatch, proxy_url, cert):
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HTTPS_PROXY', proxy_url)
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))


@pytest.mark.parametrize('route,bypass,connect', [('environment', False, True), ('direct', False, False), ('environment', True, False)])
async def test_real_tls_proxy_direct_and_no_proxy(certificate, monkeypatch, route, bypass, connect):
    with origin(certificate) as (base, seen), proxy() as (proxy_url, tunnels):
        install_network(monkeypatch, proxy_url, certificate[0])
        if bypass:
            monkeypatch.setenv('NO_PROXY', 'localhost')
        client = ModelClient(config(base, route))
        try:
            result = await client.complete(client.session('controlled transport test', {}), [])
            assert result.text == 'controlled-completion'
            request = client.requests[0]
            assert bool(tunnels) is connect
            assert request['proxy_tunnel']['observed'] is connect
            assert request['model_network']['proxy_connect_observed'] is connect
            assert request['model_network']['route'] == route
            assert request['model_network']['ca_source'] == 'SSL_CERT_FILE'
            assert request['model_network']['certificate_verification'] is True
            assert request['model_network']['physical_route'] == 'not_observable'
            assert len(seen) == 1
            assert 'fixture-password' not in json.dumps(request)
        finally:
            await client.aclose()


@pytest.mark.parametrize('route', ['environment', 'direct'])
async def test_certificate_and_hostname_validation_not_disabled(certificate, monkeypatch, route):
    with origin(certificate) as (base, seen), proxy() as (proxy_url, tunnels):
        install_network(monkeypatch, proxy_url, certificate[0])
        # Trusted certificate but hostname intentionally mismatches its DNS SAN.
        client = ModelClient(config(base.replace('localhost', '127.0.0.1'), route))
        try:
            with pytest.raises(ModelError):
                await client.complete(client.session('invalid hostname must fail', {}), [])
            assert not seen
            assert client.requests[0]['error'] is not None
        finally:
            await client.aclose()
        monkeypatch.delenv('SSL_CERT_FILE')
        client = ModelClient(config(base, route))
        try:
            with pytest.raises(ModelError):
                await client.complete(client.session('untrusted CA must fail', {}), [])
            assert not seen
        finally:
            await client.aclose()


async def test_direct_retains_environment_certificate_directory(certificate, monkeypatch, tmp_path):
    with origin(certificate) as (base, _), proxy() as (proxy_url, tunnels):
        install_network(monkeypatch, proxy_url, certificate[0])
        monkeypatch.delenv('SSL_CERT_FILE')
        cert_dir = tmp_path / 'ca-directory'
        cert_dir.mkdir()
        cert_dir.joinpath('ca.pem').write_bytes(certificate[0].read_bytes())
        digest = subprocess.check_output(['openssl', 'x509', '-hash', '-noout', '-in', str(certificate[0])], text=True).strip()
        cert_dir.joinpath(digest + '.0').symlink_to('ca.pem')
        monkeypatch.setenv('SSL_CERT_DIR', str(cert_dir))
        client = ModelClient(config(base, 'direct'))
        try:
            assert (await client.complete(client.session('directory CA', {}), [])).text == 'controlled-completion'
            assert not tunnels and client.requests[0]['model_network']['ca_source'] == 'SSL_CERT_DIR'
        finally:
            await client.aclose()


async def test_task_roles_frozen_and_capability_cache_changes_with_route(tmp_path):
    store = Store(tmp_path / 'frozen.sqlite3')
    cfg = {**store.settings(), **config('https://fixture.invalid/v1')}
    store.set_meta('settings', {k: v for k, v in cfg.items() if k != '_model_api_key'})
    configure_test_team(store, cfg)
    account = store.save_account(AccountInput(name='frozen-route', cash='100000', holdings=[], research_scope='cash_only'))
    with store.connect() as db:
        frozen = store.task_config_snapshot(db, account['id'])
    store.set_meta('settings', {**store.settings(), 'model_network_route': 'direct', 'model_network_revision': 5})
    with store.connect() as db:
        current = store.task_config_snapshot(db, account['id'])
    for role_id, role in frozen['_agent_team']['roles'].items():
        if role['enabled']:
            resolved = store.runtime_role_config(frozen['_agent_team'], role_id, frozen)
            assert resolved['model_network_route'] == 'environment'
            assert resolved['model_network_revision'] == 4
    assert current['_model_configuration_fingerprint'] != frozen['_model_configuration_fingerprint']
    assert native_search_fingerprint(current) != native_search_fingerprint(frozen)
    assert model_config_summary(current)['model_network_route'] == 'direct'
    reopened = Store(store.path)
    assert reopened.settings()['model_network_revision'] == 5


async def test_multiple_provider_catalogs_use_policy_and_invalidate_cache(certificate, monkeypatch, tmp_path):
    with origin(certificate) as (base, seen), proxy() as (proxy_url, tunnels):
        install_network(monkeypatch, proxy_url, certificate[0])
        store = Store(tmp_path / 'catalogs.sqlite3')
        store._credential_store = FixtureCredentials()
        first = await store.save_provider({'base_url': base, 'protocol': 'responses', 'api_key': 'first-controlled'})
        second = await store.save_provider({'base_url': base + '/second', 'protocol': 'responses', 'api_key': 'second-controlled'})
        assert len(tunnels) == len(seen) == 2
        assert first['catalog']['model_network']['proxy_connect_observed']
        assert second['catalog']['model_network']['route'] == 'environment'
        store.set_meta('settings', {**store.settings(), 'model_network_route': 'direct', 'model_network_revision': 1})
        for provider in (first, second):
            catalog = await store.discover_models(provider['id'])
            assert catalog['model_network_policy'] == {'route': 'direct', 'revision': 1}
            assert not catalog['model_network']['proxy_connect_observed']
        assert len(tunnels) == 2 and len(seen) == 4
        await store.discover_models(first['id'])
        assert len(seen) == 4
        assert 'fixture-password' not in json.dumps(store.list_providers())


def test_normal_manage_start_save_restart_directory_probe_and_worker(repository, certificate):
    repo, env, port = repository
    with origin(certificate) as (base, seen), proxy() as (proxy_url, tunnels):
        env.update(MODEL_BASE_URL=base, MODEL_PROTOCOL='responses', MODEL_NAME='controlled-model',
                   MODEL_KEY_ENV='SIXTEENTH_NETWORK_KEY', SIXTEENTH_NETWORK_KEY='controlled-subprocess-key',
                   HTTPS_PROXY=proxy_url, NO_PROXY='', SSL_CERT_FILE=str(certificate[0]))
        started = control(repository, 'start', handbook=True)
        assert started.returncode == 0, started.stdout + started.stderr
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=20) as api:
                saved = api.put('/api/settings', json={'scheduled_research_enabled': False,
                    'market_refresh_enabled': False, 'model_native_search': False,
                    'model_responses_stream': False, 'model_retries': 0}).json()
                assert saved['model_network_route'] == 'environment'
                provider = api.get('/api/providers').json()[0]
                catalog = api.post(f"/api/providers/{provider['id']}/models/refresh").json()
                assert catalog['status'] == 'success', catalog
                assert catalog['model_network']['proxy_connect_observed']
                probe = api.post('/api/settings/probe').json()
                assert probe['ok'], probe
                assert any(r['model_network']['proxy_connect_observed'] for r in probe['requests'])
                # A pooled second request need not emit another CONNECT event.
                assert all(r['model_network']['route'] == 'environment' for r in probe['requests'])
                before = len(tunnels)
                saved = api.put('/api/settings', json={'model_network_route': 'direct'}).json()
                assert saved['model_network_revision'] == 1
                assert api.put('/api/settings', json={'model_network_route': 'direct'}).json()['model_network_revision'] == 1
                assert api.put('/api/settings', json={'model_network_route': 'auto'}).status_code == 422
                assert api.put('/api/settings', json={'model_network_revision': 999}).status_code == 409
                assert control(repository, 'stop').returncode == 0
                assert control(repository, 'start').returncode == 0
                assert api.get('/api/settings').json()['model_network_route'] == 'direct'
                readiness = api.get('/api/status').json()['configuration_readiness']['model_network']
                assert readiness['route'] == 'direct' and not readiness['capability_verified']
                catalog = api.get(f"/api/providers/{provider['id']}/models").json()
                assert catalog['model_network_policy'] == {'route': 'direct', 'revision': 1}
                probe = api.post('/api/settings/probe').json()
                assert probe['ok'] and all(r['model_network']['route'] == 'direct' for r in probe['requests'])
                assert not any(r['model_network']['proxy_connect_observed'] for r in probe['requests'])
                # The actual normal worker makes an ordinary cash-only report.
                response = api.post('/api/accounts', json={'name': 'normal route fixture', 'cash': '100000', 'holdings': [], 'research_scope': 'cash_only'})
                assert response.status_code == 201, response.text
                account_id = response.json()['id']
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    jobs = api.get(f'/api/accounts/{account_id}/jobs').json()
                    if jobs and jobs[0]['state'] in {'completed', 'failed', 'waiting_config'}:
                        break
                    threading.Event().wait(.1)
                assert jobs[0]['state'] == 'completed', jobs
                assert jobs[0]['config_snapshot']['model_network_route'] == 'direct'
                assert len(tunnels) == before
                # The same environment policy honors NO_PROXY after normal restart.
                api.put('/api/settings', json={'model_network_route': 'environment'}).raise_for_status()
                assert control(repository, 'stop').returncode == 0
                env['NO_PROXY'] = 'localhost'
                assert control(repository, 'start').returncode == 0
                before = len(tunnels)
                catalog = api.post(f"/api/providers/{provider['id']}/models/refresh").json()
                assert catalog['model_network']['route'] == 'environment'
                assert not catalog['model_network']['proxy_connect_observed']
                assert len(tunnels) == before
                assert 'fixture-password' not in json.dumps([saved, catalog, probe, jobs])
        finally:
            assert control(repository, 'stop').returncode == 0


async def test_late_catalog_cannot_relabel_new_route_evidence(tmp_path):
    store = Store(tmp_path / 'late.sqlite3')
    store._credential_store = FixtureCredentials()
    first = await store.save_provider({'base_url': 'https://controlled.invalid/v1',
        'protocol': 'responses', 'api_key': 'controlled-only'},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'data': [{'id': 'initial-model'}]})))
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'data': [{'id': 'old-route-model'}]})
    old = asyncio.create_task(store.discover_models(first['id'], force=True, transport=httpx.MockTransport(delayed)))
    await asyncio.wait_for(entered.wait(), 2)
    store.set_meta('settings', {**store.settings(), 'model_network_route': 'direct', 'model_network_revision': 1})
    current = await store.discover_models(first['id'], force=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'data': [{'id': 'new-route-model'}]})))
    release.set()
    assert (await old)['status'] == 'superseded'
    retained = store.provider(first['id'])['catalog']
    assert retained['model_network_policy'] == current['model_network_policy'] == {'route': 'direct', 'revision': 1}
    assert retained['models'][0]['id'] == 'new-route-model'
