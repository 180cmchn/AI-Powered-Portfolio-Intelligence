"""Zero-paid-call checks for the optional two-route transport diagnostic."""
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.domain import AccountInput
from app.model import ModelClient as ActualModelClient
from app.store import Store
from scripts import verify_fourteenth_transport as probe
from tests.helpers_sixth import configure_test_team


def fixture_response():
    args = {'report': '受控现金100元，暂无指定用途；用途明确后重新评估。',
        'decision_summary': '用途明确后重新评估受控现金安排。',
        'completion': 'full', 'scope': ['cash_only'], 'limitations': []}
    return httpx.Response(200, json={'id': 'controlled-probe', 'status': 'completed',
        'output': [{'type': 'function_call', 'call_id': 'cash-only', 'name': 'finish_analysis',
                    'arguments': json.dumps(args, ensure_ascii=False)}],
        'usage': {'input_tokens': 12, 'output_tokens': 5}})


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FOURTEENTH_TRANSPORT_TEST_KEY', 'explicit-transport-fixture-key')
    source = Store(tmp_path/'source.sqlite3', 'live')
    cfg = {**source.settings(), 'model_name': 'fixture-transport-manager',
        'model_base_url': 'https://fixture.invalid/v1', 'model_protocol': 'responses',
        'model_key_env': 'FOURTEENTH_TRANSPORT_TEST_KEY',
        'reasoning_effort': 'max', 'model_retries': 3, 'max_output_tokens': 4000,
        'model_native_search': True}
    source.set_meta('settings', cfg)
    configure_test_team(source, cfg)
    account = source.save_account(AccountInput(name='private-source-account-name', cash='98765', holdings=[]))
    original = probe.configure_isolated
    def configure(origin, target, account_id):
        assert origin == source.path
        store, settings = original(origin, target, account_id)
        store._credential_store = source._credential_store
        return store, settings
    monkeypatch.setattr(probe, 'configure_isolated', configure)
    args = SimpleNamespace(run_real=True, authorized_http_attempts=2,
        authorization_note='Explicit test-only allowance; HTTPX fixtures, no real network or paid calls',
        source=source.path, reference_account=account['id'], output=tmp_path/'diagnostic')
    return source, cfg, args


@pytest.mark.asyncio
@pytest.mark.parametrize('first_fails', [False, True])
async def test_two_routes_keep_exact_payload_config_and_independent_budget(tmp_path, monkeypatch, first_fails):
    source, cfg, args = setup(tmp_path, monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', 'http://private-user:private-secret@proxy.invalid:4444')
    monkeypatch.setenv('NO_PROXY', 'localhost,127.0.0.1')
    environment = dict(os.environ)
    # Prove a separately existing authorization ledger is neither found nor modified.
    previous = tmp_path/'authorized-attempts.sqlite3'
    previous.write_bytes(b'explicit unrelated existing ledger fixture')
    before = hashlib.sha256(previous.read_bytes()).hexdigest()
    requests, routes, configs = [], [], []
    direct_transports = []
    def client(config, before_request=None, transport=None):
        route = 'environment' if transport is None else 'direct'
        routes.append(route)
        configs.append(config)
        if transport is not None:
            assert isinstance(transport, httpx.AsyncHTTPTransport)
            assert transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            direct_transports.append(transport)
        async def handler(request):
            requests.append((route, json.loads(request.content)))
            if first_fails and route == 'environment':
                raise httpx.RemoteProtocolError('controlled transport failure', request=request)
            return fixture_response()
        return ActualModelClient(config, before_request, httpx.MockTransport(handler))
    monkeypatch.setattr(probe, 'ModelClient', client)
    try:
        assert await probe.probe(args) == (2 if first_fails else 0)
    finally:
        for transport in direct_transports:
            await transport.aclose()
    summary = json.loads((args.output/'summary.json').read_text())
    assert routes == ['environment', 'direct']
    assert len(requests) == 2  # Failure is not retried, regardless of original retries=3.
    assert requests[0][1] == requests[1][1]
    for config in configs:
        assert config['model_retries'] == 0
        assert config['model_name'] == cfg['model_name']
        assert config['reasoning_effort'] == cfg['reasoning_effort'] == 'max'
        assert config['model_protocol'] == cfg['model_protocol']
        assert config['max_output_tokens'] == cfg['max_output_tokens'] == 4000
    for _, payload in requests:
        assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
        assert payload['tools'][0]['name'] == 'finish_analysis'
        assert len(payload['tools']) == 1 and payload['tools'][0]['strict'] is False
        assert set(payload['tools'][0]['parameters']['required']) == {'report', 'decision_summary', 'scope', 'completion', 'limitations'}
        seed = json.loads(payload['input'][0]['content'])
        assert seed == probe.INPUT
        assert seed['account']['cash'] == '100.00'
        assert seed['account']['holdings'] == []
    assert summary['attempt_budget'] == {'maximum': 2, 'reserved_attempts': 2,
        'stages': {'final': {'maximum': 2, 'reserved_attempts': 2}, 'full': {'maximum': 0, 'reserved_attempts': 0}}}
    assert summary['observed_http_attempts'] == 2 and summary['reserved_minus_observed'] == 0
    assert summary['predictions_published'] == summary['reports_published'] == 0
    assert summary['content_assessment'] == 'not_evaluated'
    assert summary['variants'][1]['delivery_contract_satisfied'] is True
    if first_fails:
        assert summary['variants'][0]['error_code'] == 'upstream_transport_error'
        assert summary['variants'][0]['delivery_contract_satisfied'] is False
    assert summary['proxy_and_certificate_environment_unchanged'] is True
    assert dict(os.environ) == environment
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == before
    encoded = json.dumps(summary)
    assert 'private-secret' not in encoded and 'private-user' not in encoded
    assert 'private-source-account-name' not in encoded and '98765' not in encoded
    with source.connect() as db:
        assert db.execute('SELECT count(*) FROM analyses').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0
    assert args.output.stat().st_mode & 0o777 == 0o700
    assert (args.output/'summary.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_one_authorized_attempt_does_not_start_direct_route(tmp_path, monkeypatch):
    _, _, args = setup(tmp_path, monkeypatch)
    args.authorized_http_attempts = 1
    calls = []
    def client(config, before_request=None, transport=None):
        assert transport is None
        def handler(request):
            calls.append(request)
            return fixture_response()
        return ActualModelClient(config, before_request, httpx.MockTransport(handler))
    monkeypatch.setattr(probe, 'ModelClient', client)
    assert await probe.probe(args) == 2  # One route is a partial comparison.
    summary = json.loads((args.output/'summary.json').read_text())
    assert len(calls) == 1
    assert summary['not_run'] == {'variant': 'direct', 'reason': 'authorized_http_attempts_exhausted'}
    assert summary['attempt_budget']['reserved_attempts'] == 1


@pytest.mark.asyncio
async def test_explicit_httpx_transport_omits_env_proxy_mounts_and_preserves_tls(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:4444')
    monkeypatch.setenv('NO_PROXY', '')
    transport = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport) as direct:
        assert direct._mounts == {}
        assert transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    async with httpx.AsyncClient() as environment:
        assert any(value is not None for value in environment._mounts.values())
    # Construction only: neither client sent a network request.


@pytest.mark.parametrize('extra', [[], ['--run-real'], ['--run-real', '--authorized-http-attempts', '0'],
    ['--run-real', '--authorized-http-attempts', '3'], ['--authorized-http-attempts', '2']])
def test_cli_requires_new_explicit_authorization_and_never_defaults_to_calls(tmp_path, extra):
    output = tmp_path/'must-not-exist'
    result = subprocess.run([sys.executable, str(Path(probe.__file__)), *extra,
        '--output', str(output)], text=True, capture_output=True)
    assert result.returncode == 2
    assert 'Default allowance is 0' in result.stderr
    assert not output.exists()


def test_cli_help_has_no_side_effect(tmp_path):
    result = subprocess.run([sys.executable, str(Path(probe.__file__)), '--help'],
                            text=True, capture_output=True)
    assert result.returncode == 0
    assert '--authorized-http-attempts' in result.stdout
    assert '--authorization-note' in result.stdout
