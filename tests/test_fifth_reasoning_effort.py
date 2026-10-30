"""Captured HTTP protocol and isolated SQLite evidence, never upstream support claims."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import defaults
from app.main import create_app
from app.model import ModelClient, ModelConfigurationError, native_search_fingerprint, native_search_status
from app.store import Store
from app.domain import AccountInput
from app.service import Service
from tests.helpers_sixth import configure_test_team

EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']


def config(effort='medium', protocol='responses'):
    return {'model_name': 'fixture-model', 'model_base_url': 'http://localhost:19001/v1',
            'model_key_env': 'FIFTH_TEST_EMPTY_KEY', 'model_allow_no_key': True,
            'model_protocol': protocol, 'model_native_search': True,
            'reasoning_effort': effort, 'model_retries': 0}


@pytest.mark.parametrize('effort', EFFORTS)
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.asyncio
async def test_all_five_values_sent_verbatim_with_diagnostics(effort, protocol):
    captured = []
    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}],
            'choices': [{'message': {'role': 'assistant', 'content': 'done'}}],
            'usage': {'output_tokens': 12, 'output_tokens_details': {'reasoning_tokens': 9}}})
    client = ModelClient(config(effort, protocol), transport=httpx.MockTransport(handler))
    session = client.session('Research', {'job_id': 'task-fixture'})
    session.agent_id, session.phase = 'manager', 'synthesis'
    try:
        await client.complete(session, [])
        if protocol == 'responses':
            assert captured[0]['reasoning']['effort'] == effort
            assert 'reasoning_effort' not in captured[0]
            assert {'type': 'web_search'} in captured[0]['tools']
        else:
            assert captured[0]['reasoning_effort'] == effort
            assert 'reasoning' not in captured[0]
        event = client.requests[0]
        assert event['reasoning_effort'] == effort
        assert event['reasoning_effort_sent'] == effort
        assert event['reasoning_effort_path'] == ('reasoning.effort' if protocol == 'responses' else 'reasoning_effort')
        assert event['request_accepted'] is True
        assert event['reported_reasoning_effort'] is None
        assert event['task_id'] == 'task-fixture'
    finally:
        await client.aclose()


@pytest.mark.parametrize('effort', EFFORTS)
def test_settings_api_save_restart_and_db_precedence(tmp_path, monkeypatch, effort):
    monkeypatch.setenv('REASONING_EFFORT', 'low')
    path = tmp_path / 'settings.sqlite3'
    app = create_app(path, worker=False)
    with TestClient(app) as client:
        assert client.put('/api/settings', json={'reasoning_effort': effort}).status_code == 200
        assert client.get('/api/settings').json()['reasoning_effort'] == effort
    monkeypatch.setenv('REASONING_EFFORT', 'high')
    assert Store(path).settings()['reasoning_effort'] == effort


def test_default_env_precedence_and_missing_api_field_not_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv('REASONING_EFFORT', raising=False)
    assert defaults()['reasoning_effort'] == 'medium'
    monkeypatch.setenv('REASONING_EFFORT', 'xhigh')
    path = tmp_path / 'settings.sqlite3'
    app = create_app(path, worker=False)
    assert 'reasoning_effort' not in app.state.store.meta('settings', {})
    with TestClient(app) as client:
        result = client.put('/api/settings', json={'model_name': 'my-model'})
        assert result.status_code == 200
        assert result.json()['reasoning_effort'] == 'xhigh'
    assert 'reasoning_effort' not in app.state.store.meta('settings', {})
    monkeypatch.setenv('REASONING_EFFORT', 'max')
    assert Store(path).settings()['reasoning_effort'] == 'max'


@pytest.mark.parametrize('bad', ['', None, 'default', 'auto', 'none', 'minimal', 'HIGH', 3])
def test_illegal_values_fail_api_and_runtime(tmp_path, bad):
    app = create_app(tmp_path / 'settings.sqlite3', worker=False)
    with TestClient(app) as client:
        response = client.put('/api/settings', json={'reasoning_effort': bad})
        assert response.status_code == 422
    with pytest.raises(ModelConfigurationError, match='reasoning_effort'):
        ModelClient(config(bad))


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_rejected_effort_is_actionable_config_error_without_retry_or_search_misclassification(protocol):
    seen = []
    def reject(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'param': 'reasoning.effort' if protocol == 'responses' else 'reasoning_effort',
            'message': 'Unsupported reasoning effort max'}})
    client = ModelClient({**config('max', protocol), 'model_retries': 3}, transport=httpx.MockTransport(reject))
    try:
        with pytest.raises(ModelConfigurationError, match='max') as exc:
            await client.complete(client.session('test', {}), [])
        assert protocol in str(exc.value) and 'localhost' in str(exc.value)
        assert len(seen) == 1
        assert client.requests[0]['reasoning_effort_rejected'] is True
        assert client.requests[0]['request_accepted'] is False
        assert not client.requests[0].get('native_tool_rejected')
        if protocol == 'responses':
            assert client.native_status()['status'] == 'unverified'
    finally:
        await client.aclose()


def test_effort_invalidates_native_capability_cache():
    first = config('medium')
    cached = {'status': 'observed', 'fingerprint': native_search_fingerprint(first)}
    assert native_search_status(config('max'), cached)['status'] == 'unverified'


@pytest.mark.asyncio
async def test_client_owns_config_snapshot_and_merges_responses_reasoning():
    cfg = {**config('xhigh'), 'reasoning': {'summary': 'auto', 'mode': 'standard'}}
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}]})
    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    cfg['reasoning_effort'] = 'low'
    cfg['reasoning']['mode'] = 'other'
    try:
        await client.complete(client.session('test', {}), [])
        assert sent[0]['reasoning'] == {'summary': 'auto', 'mode': 'standard', 'effort': 'xhigh'}
    finally:
        await client.aclose()


def test_claim_freezes_queue_start_settings_and_status_lists_all_scopes(tmp_path):
    app = create_app(tmp_path / 'queue.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', {**config('low'), 'debounce_seconds': 0, 'quote_provider': 'manual'})
    configure_test_team(store, config('low'))
    account = store.save_account(AccountInput(name='effort A'))
    assert store.job(account['latest_job_id'])['config_snapshot'] is None
    store.set_meta('settings', {**store.settings(), 'reasoning_effort': 'xhigh'})
    store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'xhigh'}})
    job = store.claim_jobs()[0]
    assert job['config_snapshot']['reasoning_effort'] == 'xhigh'
    store.request_analysis(None, 'candidate_scan', 'test-global')
    run = store.claim_global()
    assert run['input_snapshot']['config']['reasoning_effort'] == 'xhigh'
    with TestClient(app) as client:
        assert client.put('/api/settings', json={'reasoning_effort': 'max'}).status_code == 200
        store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'max'}})
        status = client.get('/api/status').json()
    assert status['settings']['reasoning_effort'] == 'max'
    assert status['global_manager']['reasoning_effort'] == 'max'
    assert {r['id'] for r in status['running_model_configs']} == {job['id'], run['id']}
    assert {r['reasoning_effort'] for r in status['running_model_configs']} == {'xhigh'}
    assert Store(store.path).job(job['id'])['config_snapshot']['reasoning_effort'] == 'xhigh'


@pytest.mark.asyncio
async def test_service_account_and_global_use_claimed_config_after_setting_change(tmp_path):
    store = Store(tmp_path / 'service.sqlite3')
    store.set_meta('settings', {**config('high'), 'debounce_seconds': 0, 'quote_provider': 'manual'})
    configure_test_team(store, config('high'))
    account = store.save_account(AccountInput(name='A'))
    job = store.claim_jobs()[0]
    store.request_analysis(None, 'candidate_scan', 'one')
    run = store.claim_global()
    store.set_meta('settings', {**store.settings(), 'reasoning_effort': 'max'})
    store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'max'}})
    seen = []
    async def runner(context, cfg, tools, emit):
        seen.append((context['job_id'], cfg['reasoning_effort'], context['model_config']['reasoning_effort']))
        return {'report': 'controlled fixture'}
    service = Service(store, runner)
    await service.execute(job)
    await service.execute_global(run)
    assert seen == [(job['id'], 'high', 'high'), (run['id'], 'high', 'high')]
    assert store.job(job['id'])['result']['model_config']['reasoning_effort'] == 'high'
    assert store.global_run(run['id'])['result']['model_config']['reasoning_effort'] == 'high'


def test_v5_migration_backup_is_repeatable_and_never_persists_effort_default(tmp_path, monkeypatch):
    monkeypatch.setenv('REASONING_EFFORT', 'xhigh')
    path = tmp_path / 'migrate.sqlite3'
    original = Store(path)
    account = original.save_account(AccountInput(name='preserved'))
    original.set_meta('schema_version', 4)
    with original.connect() as db:
        db.execute('ALTER TABLE jobs DROP COLUMN config_snapshot')
    updated = Store(path)
    assert updated.meta('schema_version') == 9
    assert updated.account(account['id']) == account
    assert updated.settings()['reasoning_effort'] == 'xhigh'
    assert 'reasoning_effort' not in updated.meta('settings')
    backup_path = Path(str(path) + '.before-v5.bak')
    before = backup_path.read_bytes()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '4'
        assert 'config_snapshot' not in {r[1] for r in backup.execute('PRAGMA table_info(jobs)')}
    assert Store(path).job(account['latest_job_id'])['config_snapshot'] is None
    assert backup_path.read_bytes() == before


def test_restore_accepts_v5_backup(tmp_path):
    source = Store(tmp_path / 'source.sqlite3')
    source.set_meta('settings', {'reasoning_effort': 'max'})
    target = tmp_path / 'restored.sqlite3'
    result = subprocess.run([sys.executable, 'scripts/restore.py', str(source.path), '--mode', 'live'],
        env={**os.environ, 'APP_DB_PATH': str(target)}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert Store(target).settings()['reasoning_effort'] == 'max'


@pytest.mark.asyncio
async def test_manager_child_followup_synthesis_and_recovery_keep_same_effort(monkeypatch):
    from app import agents
    cfg = {**config('max'), 'max_agent_turns': 20, 'max_model_calls': 40,
           'analysis_timeout_seconds': 5, 'agent_timeout_seconds': 2}
    payloads, events, checkpoints = [], [], []
    state = {'child': None, 'round': 0, 'followed': False}
    def response(name, args):
        return httpx.Response(200, json={'output': [{'type': 'function_call', 'call_id': name,
            'name': name, 'arguments': json.dumps(args)}]})
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert payload['reasoning']['effort'] == 'max'
        initial = json.loads(payload['input'][0]['content'])
        if 'task' in initial:
            return response('submit_report', {'summary': 'visible child fact'})
        if initial.get('synthesis_checkpoint'):
            return response('finish_analysis', {'report': 'complete synthesis'})
        if state['child'] is None:
            return response('spawn_research', {'task': 'ETF source scope'})
        if state['round'] < 1:
            return response('wait_agents', {'timeout_seconds': 0.05})
        if not state['followed']:
            state['followed'] = True
            return response('follow_up', {'agent_id': state['child'], 'task': 'Clarify publication time'})
        if state['round'] < 2:
            return response('wait_agents', {'timeout_seconds': 0.05})
        return response('prepare_synthesis', {'summary': 'all visible evidence ready'})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    class Tools:
        async def save_checkpoint(self, checkpoint):
            checkpoints.append(checkpoint)
    async def emit(event):
        events.append(event)
        if event['type'] == 'agent_created':
            state['child'] = event['agent_id']
            cfg['reasoning_effort'] = 'low'  # External setting mutation during work.
        if event['type'] == 'agent_completed' and event['agent_id'] != 'manager':
            state['round'] = event['payload']['round']
    result = await agents.run_research({'job_id': 'round-one', 'account': {'id': 'a'}, 'input_version': 1}, cfg, Tools(), emit)
    assert state['round'] == 2 and result['model_config']['reasoning_effort'] == 'max'
    assert checkpoints[-1]['model_config']['reasoning_effort'] == 'max'
    requests = [r for event in events if event['type'] == 'model_usage' for r in event['payload']['requests']]
    assert {r['reasoning_effort_sent'] for r in requests} == {'max'}
    assert {r['task_id'] for r in requests} == {'round-one'}
    assert {r['stage'] for r in requests} == {'research', 'synthesis'}
    assert {r['agent_id'] for r in requests} == {'manager', state['child']}
    await agents.run_research({'job_id': 'resume', 'synthesis_checkpoint': checkpoints[-1]}, {**cfg, 'reasoning_effort': 'max'}, Tools(), emit)
    assert payloads[-1]['reasoning']['effort'] == 'max'


def test_recovery_records_old_and_current_config_without_claiming_old_effort(tmp_path):
    from app.config import model_config_summary
    store = Store(tmp_path / 'recovery.sqlite3')
    store.set_meta('settings', {**config('high'), 'debounce_seconds': 0})
    configure_test_team(store, config('high'))
    account = store.save_account(AccountInput(name='recovery'))
    job = store.claim_jobs()[0]
    context = {'account': account, 'universe_version': 1, 'model_config': model_config_summary(job['config_snapshot'])}
    store.save_synthesis_checkpoint(job, {'reports': [{'summary': 'visible evidence'}], 'material_ids': []}, context)
    store.set_job_state(job['id'], 'failed', 'controlled timeout')
    store.set_meta('settings', {**store.settings(), 'reasoning_effort': 'xhigh'})
    store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'xhigh'}})
    store.enqueue(account['id'], 'manual')
    current = store.claim_jobs()[0]
    restored = store.reusable_synthesis_checkpoint(current, {**context, 'model_config': model_config_summary(current['config_snapshot'])})
    relation = restored['configuration_relation']
    assert relation['previous']['reasoning_effort'] == 'high'
    assert relation['current']['reasoning_effort'] == 'xhigh'
    assert relation['changed'] is True
    assert relation['strategy'] == 'reuse_visible_research_current_task_config'


@pytest.mark.asyncio
async def test_http_acceptance_and_explicit_server_report_are_separate():
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={'reasoning': {'effort': 'high'}, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'visible answer'}]}]})
    client = ModelClient(config('max'), transport=httpx.MockTransport(handler))
    try:
        await client.complete(client.session('test', {}), [])
        assert client.requests[0]['reasoning_effort_sent'] == 'max'
        assert client.requests[0]['request_accepted'] is True
        assert client.requests[0]['reported_reasoning_effort'] == 'high'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_empty_http_200_does_not_prove_research_succeeded():
    from app.model import ModelProtocolError
    client = ModelClient(config(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'output': []})))
    try:
        with pytest.raises(ModelProtocolError, match='空结果'):
            await client.complete(client.session('test', {}), [])
        assert client.requests[0]['request_accepted'] is True
        assert client.requests[0]['reported_reasoning_effort'] is None
        assert client.requests[0]['usage'] is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['Unrecognized request argument supplied: reasoning', 'Unknown parameter: reasoning', 'reasoning is not supported'])
async def test_plain_text_reasoning_parameter_rejection_is_configuration_error(message):
    client = ModelClient(config('xhigh'), transport=httpx.MockTransport(lambda req: httpx.Response(400, text=message)))
    try:
        with pytest.raises(ModelConfigurationError, match='reasoning.effort=xhigh'):
            await client.complete(client.session('test', {}), [])
        assert len(client.requests) == 1
        assert client.native_status()['status'] == 'unverified'
    finally:
        await client.aclose()


def test_environment_invalid_effort_is_not_silently_defaulted(tmp_path, monkeypatch):
    monkeypatch.setenv('REASONING_EFFORT', '')
    store = Store(tmp_path / 'bad-env.sqlite3')
    assert store.settings()['reasoning_effort'] == ''
    with pytest.raises(ModelConfigurationError, match='reasoning_effort'):
        ModelClient({**config(), **store.settings()})
    store.set_meta('settings', {'reasoning_effort': 'high'})
    assert store.settings()['reasoning_effort'] == 'high'


def test_status_invalidates_function_probe_after_environment_effort_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('REASONING_EFFORT', 'medium')
    app = create_app(tmp_path / 'probe.sqlite3', worker=False)
    store = app.state.store
    configure_test_team(store, config('medium'))
    effective, _ = app.state.service.manager_config(credentials=False)
    store.set_meta('model_probe', {'ok': True, 'fingerprint': native_search_fingerprint(effective)})
    with TestClient(app) as client:
        assert client.get('/api/status').json()['model_probe']['ok'] is True
        # An environment change no longer overrides an explicit global role.
        monkeypatch.setenv('REASONING_EFFORT', 'max')
        assert client.get('/api/status').json()['model_probe']['ok'] is True
        store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'max'}})
        result = client.get('/api/status').json()['model_probe']
        assert result['ok'] is False and result['status'] == 'unverified'


@pytest.mark.asyncio
async def test_rejected_same_config_not_retried_on_unrelated_save_or_restart(tmp_path, monkeypatch):
    from app import agents
    app = create_app(tmp_path / 'rejection.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', {**config('max'), 'quote_provider': 'manual', 'debounce_seconds': 0})
    configure_test_team(store, config('max'))
    account = store.save_account(AccountInput(name='no repeat'))
    store.request_analysis(None, 'candidate_scan', 'public-rejection')
    requests = []
    def reject(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'param': 'reasoning.effort', 'message': 'Unsupported max'}})
    transport = httpx.MockTransport(reject)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))
    service = app.state.service
    job, run = store.claim_jobs()[0], store.claim_global()
    await service.execute(job)
    await service.execute_global(run)
    assert len(requests) == 2
    with TestClient(app) as client:
        assert client.put('/api/settings', json={'quote_cache_seconds': 45}).status_code == 200
    store.recover(config_ready=service.model_configured())
    assert store.job(job['id'])['state'] == 'waiting_config'
    assert store.global_run(run['id'])['state'] == 'waiting_config'
    assert store.claim_jobs() == [] and store.claim_global() is None
    assert len(store.jobs(account['id'])) == 1
    # The sixth iteration binds effort at the role, not the connection/settings.
    store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'high'}})
    assert service.resume_waiting_config()
    assert store.job(job['id'])['state'] == 'queued'
    assert store.global_run(run['id'])['state'] == 'superseded'


def test_restart_attempt_preserves_old_and_new_configuration_relation(tmp_path):
    store = Store(tmp_path / 'restart.sqlite3')
    store.set_meta('settings', {**config('high'), 'debounce_seconds': 0})
    configure_test_team(store, config('high'))
    account = store.save_account(AccountInput(name='restart'))
    first = store.claim_jobs()[0]
    store.set_meta('settings', {**store.settings(), 'reasoning_effort': 'max'})
    store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'max'}})
    store.recover(config_ready=True)
    second = store.claim_jobs()[0]
    assert first['id'] == second['id'] and second['attempts'] == 2
    events = store.job(second['id'], events=True)['events']
    bindings = [event['payload'] for event in events if event['type'] == 'model_configuration_bound']
    assert bindings[0]['current']['reasoning_effort'] == 'high'
    assert bindings[-1]['previous']['reasoning_effort'] == 'high'
    assert bindings[-1]['current']['reasoning_effort'] == 'max'
    assert bindings[-1]['changed'] is True


@pytest.mark.parametrize('scope', ['account', 'global'])
def test_recovery_age_uses_current_task_claim_snapshot(tmp_path, scope):
    from app.domain import TZ
    store = Store(tmp_path / 'age.sqlite3')
    store.set_meta('settings', {**config(), 'debounce_seconds': 0, 'synthesis_checkpoint_max_age_seconds': 60})
    account = store.save_account(AccountInput(name='age')) if scope == 'account' else None
    if scope == 'account':
        job = store.claim_jobs()[0]
    else:
        store.request_analysis(None, 'candidate_scan', 'age')
        job = store.claim_global()
    context = {'account': account, 'universe_version': 1, 'input_snapshot': job.get('input_snapshot')}
    store.save_synthesis_checkpoint(job, {'reports': [{'summary': 'old'}]}, context)
    with store.connect() as db:
        old_time = (datetime.now(TZ) - timedelta(seconds=90)).isoformat()
        db.execute('UPDATE synthesis_checkpoints SET created_at=?', (old_time,))
    store.set_meta('settings', {**store.settings(), 'synthesis_checkpoint_max_age_seconds': 1800})
    assert store.reusable_synthesis_checkpoint(job, context) is None


@pytest.mark.parametrize('source,bad', [('env', ''), ('saved', None), ('saved', 'unknown')])
def test_invalid_effective_effort_is_visible_in_status_without_breaking_accounts(tmp_path, monkeypatch, source, bad):
    path = tmp_path / 'invalid-visible.sqlite3'
    cfg = config()
    cfg.pop('reasoning_effort')
    if source == 'env':
        monkeypatch.setenv('REASONING_EFFORT', bad)
    else:
        cfg['reasoning_effort'] = bad
    # A real pre-upgrade settings row migrates to the role without silently
    # replacing an explicit invalid effort with medium.
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT INTO meta VALUES('schema_version','5')")
        db.execute("INSERT INTO meta VALUES('settings',?)", (json.dumps(cfg),))
    app = create_app(path, worker=False)
    assert app.state.store.list_agent_roles()[0]['resolved']['reasoning_effort'] == bad
    with TestClient(app) as client:
        status = client.get('/api/status').json()
        assert status['model_configured'] is False
        assert any('reasoning_effort' in error for error in status['configuration_errors'])
        assert client.get('/api/accounts').status_code == 200


def test_credential_or_local_authorization_change_can_resume_configuration_waiter(tmp_path, monkeypatch):
    store = Store(tmp_path / 'auth-change.sqlite3')
    store.set_meta('settings', {**config(), 'model_allow_no_key': False, 'debounce_seconds': 0})
    provider = configure_test_team(store, config())
    asyncio.run(store.clear_provider_credential(provider['id']))
    account = store.save_account(AccountInput(name='authorization'))
    job = store.claim_jobs()[0]
    store.set_job_state(job['id'], 'waiting_config', 'missing key')
    # Explicitly provision the replacement through provider discovery. Legacy
    # local no-key flags no longer mutate a role's authoritative connection.
    monkeypatch.setenv('FIFTH_TEST_EMPTY_KEY', 'synthetic-first-replacement-key')
    configure_test_team(store, config())
    assert Service(store).resume_waiting_config()
    assert store.job(job['id'])['state'] == 'queued'
    claimed = store.claim_jobs()[0]
    store.set_job_state(claimed['id'], 'waiting_config', 'credential configuration test')
    monkeypatch.setenv('FIFTH_TEST_EMPTY_KEY', 'synthetic-replacement-key')
    configure_test_team(store, config())
    store.recover(config_ready=True)
    assert store.job(job['id'])['state'] == 'queued'
    assert 'synthetic-replacement-key' not in json.dumps(store.job(job['id'], events=True))


def test_failed_explicit_function_probe_replaces_previous_success_with_request_diagnostics(tmp_path, monkeypatch):
    from app import model
    app = create_app(tmp_path / 'probe-failed.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', config('max'))
    configure_test_team(store, config('max'))
    store.set_meta('model_probe', {'ok': True, 'fingerprint': native_search_fingerprint(store.settings())})
    monkeypatch.setattr(model, 'ModelClient', lambda cfg, transport=None: ModelClient(cfg, transport=httpx.MockTransport(
        lambda req: httpx.Response(400, json={'error': {'param': 'reasoning.effort', 'message': 'unsupported value'}}))))
    with TestClient(app) as client:
        result = client.post('/api/settings/probe').json()
        assert result['ok'] is False and result['requests'][0]['reasoning_effort_sent'] == 'max'
        assert result['requests'][0]['request_accepted'] is False
        assert client.get('/api/status').json()['model_probe']['ok'] is False


@pytest.mark.asyncio
async def test_definitive_machine_parameter_error_is_not_retried_when_gateway_uses_500():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(500, json={'error': {'code': 'unsupported_parameter', 'param': 'reasoning.effort'}})
    client = ModelClient({**config('max'), 'model_retries': 1}, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelConfigurationError):
            await client.complete(client.session('test', {}), [])
        assert len(seen) == 1
    finally:
        await client.aclose()
