"""Ninth iteration configuration reconciliation, with explicit protocol/vault doubles."""
import asyncio
import json
import uuid

import httpx
import pytest

from app.domain import AccountInput
from app.store import Store
from tests.helpers_sixth import FixtureCredentials


def directory(status=200, models=('observed/model',)):
    return httpx.MockTransport(lambda request: httpx.Response(status, json={'data': [{'id': name} for name in models]}))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://environment.example.test/v1')
    monkeypatch.setenv('MODEL_KEY_ENV', 'NINTH_CONFIGURATION_FIXTURE_KEY')
    monkeypatch.setenv('MODEL_PROTOCOL', 'responses')
    monkeypatch.setenv('MODEL_NAME', '')
    monkeypatch.delenv('NINTH_CONFIGURATION_FIXTURE_KEY', raising=False)
    return monkeypatch


@pytest.mark.asyncio
async def test_empty_migration_then_environment_import_is_explicit_idempotent_and_unbound(tmp_path, env):
    store = Store(tmp_path / 'empty.sqlite3')
    initial = store.list_providers()
    assert store.environment_connection()['status'] == 'missing_key'
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-key-present-later')
    preview = store.environment_connection()
    assert preview['status'] == 'pending_import'
    assert not store.configuration_readiness()['manager_ready']
    saved = await store.import_environment_connection(preview['preview_id'], transport=directory())
    assert saved['id'] == initial[0]['id']
    assert saved['catalog']['models'][0]['id'] == 'observed/model'
    readiness = store.configuration_readiness()
    assert readiness['provider_saved'] and not readiness['manager_ready']
    assert not readiness['manager_binding']['model_id']
    assert '选择模型' in readiness['next_step']
    again = await store.import_environment_connection(store.environment_connection()['preview_id'], transport=directory())
    assert again['revision'] == saved['revision']
    assert len(store.list_providers()) == len(initial)
    assert len(store.list_agent_roles()) == 6
    with store.connect() as db:
        assert 'fixture-key-present-later' not in '\n'.join(db.iterdump())
    assert 'fixture-key-present-later' not in json.dumps([saved, preview, readiness])


@pytest.mark.asyncio
async def test_initial_environment_only_connection_does_not_randomly_choose_model(tmp_path, env):
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-initial-key')
    store = Store(tmp_path / 'new.sqlite3')
    preview = store.environment_connection()
    assert preview['status'] == 'current'
    provider = await store.import_environment_connection(preview['preview_id'], transport=directory(models=('first', 'second')))
    assert len(provider['catalog']['models']) == 2
    assert store.configuration_readiness()['manager_binding']['model_id'] == ''
    store.update_agent_definition('manager', {'default_binding': {'provider_id': provider['id'], 'model_id': 'second'}})
    assert store.configuration_readiness()['manager_ready']


@pytest.mark.asyncio
async def test_environment_change_does_not_overwrite_saved_web_connection_or_account_override(tmp_path, env):
    store = Store(tmp_path / 'saved.sqlite3')
    store._credential_store = FixtureCredentials()
    legacy = store.list_providers()[0]
    saved = await store.save_provider({'base_url': 'https://saved.example.test/v1', 'protocol': 'responses', 'api_key': 'fixture-web-key'}, legacy['id'], transport=directory())
    store.update_agent_definition('manager', {'default_binding': {'provider_id': saved['id'], 'model_id': 'observed/model'}})
    account = store.save_account(AccountInput(name='configuration fixture', holdings=[]))
    store.set_account_agent_override(account['id'], 'manager', {'reasoning_effort': 'max'})
    before = store.list_agent_roles(account['id'])
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-env-key')
    preview = store.environment_connection()
    assert preview['provider_id'] is None
    imported = await store.import_environment_connection(preview['preview_id'], transport=directory())
    assert imported['id'] != saved['id']
    assert store.provider(saved['id'])['base_url'] == 'https://saved.example.test/v1'
    assert store.list_agent_roles(account['id']) == before
    assert store.configuration_readiness(account['id'])['manager_binding']['reasoning_effort'] == 'max'


@pytest.mark.asyncio
async def test_revoked_environment_is_not_resurrected_or_sent_to_new_endpoint(tmp_path, env):
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-revoked-key')
    store = Store(tmp_path / 'revoked.sqlite3')
    provider = store.list_providers()[0]
    await store.clear_provider_credential(provider['id'])
    env.setenv('MODEL_BASE_URL', 'https://changed.example.test/v1')
    preview = store.environment_connection()
    assert preview['status'] == 'revoked' and not preview['can_import']
    calls = []
    with pytest.raises(ValueError, match='撤销'):
        await store.import_environment_connection(preview['preview_id'], transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert calls == []
    assert not store.provider(provider['id'])['key_configured']


@pytest.mark.asyncio
async def test_environment_preview_rejects_changed_target_and_invalidates_catalog(tmp_path, env):
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-key-one')
    store = Store(tmp_path / 'target.sqlite3')
    original = await store.import_environment_connection(store.environment_connection()['preview_id'], transport=directory())
    store.update_agent_definition('manager', {'default_binding': {'provider_id': original['id'], 'model_id': 'observed/model'}})
    preview = store.environment_connection()
    env.setenv('MODEL_BASE_URL', 'https://replacement.example.test/v1')
    with pytest.raises(ValueError, match='变化'):
        await store.import_environment_connection(preview['preview_id'], transport=directory())
    changed = await store.import_environment_connection(store.environment_connection()['preview_id'], transport=directory(401))
    assert changed['revision'] > original['revision']
    assert changed['catalog']['status'] == 'authentication_failed'
    assert changed['catalog']['models'] == []
    assert not store.configuration_readiness()['manager_ready']


@pytest.mark.asyncio
async def test_operation_identity_survives_lost_response_without_second_write(tmp_path, env):
    store = Store(tmp_path / 'operations.sqlite3')
    store._credential_store = FixtureCredentials()
    operation = str(uuid.uuid4())
    payload = {'base_url': 'https://saved.example.test/v1', 'protocol': 'responses', 'api_key': 'fixture-private-write-value'}
    first, second = await asyncio.gather(*(store.save_provider(payload, operation_id=operation, transport=directory(401)) for _ in range(2)))
    assert first['id'] == second['id']
    assert len(store._credential_store.values) == 1
    assert first['catalog']['status'] == 'authentication_failed'
    checked = store.provider_operation(operation)
    assert checked['status'] == 'committed'
    reopened = Store(store.path)
    replay = await reopened.save_provider(payload, operation_id=operation, transport=directory())
    assert replay['id'] == first['id'] and replay['revision'] == first['revision']
    with store.connect() as db:
        raw = '\n'.join(db.iterdump())
        ledger = db.execute('SELECT value FROM meta WHERE key=?', ('provider_operation:' + operation,)).fetchone()[0]
    assert payload['api_key'] not in raw
    assert 'api_key' not in ledger
    assert payload['api_key'] not in json.dumps(checked)
    with pytest.raises(ValueError, match='其他输入'):
        await reopened.save_provider({**payload, 'base_url': 'https://different.example.test/v1'}, operation_id=operation)


@pytest.mark.asyncio
async def test_legacy_model_only_runs_after_actual_directory_confirmation(tmp_path, env):
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-legacy-key')
    env.setenv('MODEL_NAME', 'old-name')
    store = Store(tmp_path / 'legacy.sqlite3')
    provider = store.list_providers()[0]
    assert not store.configuration_readiness()['manager_ready']
    await store.discover_models(provider['id'], transport=directory(models=('other',)))
    assert not store.configuration_readiness()['manager_ready']
    await store.discover_models(provider['id'], force=True, transport=directory(models=('old-name',)))
    assert store.configuration_readiness()['manager_ready']
    env.setenv('NINTH_CONFIGURATION_FIXTURE_KEY', 'fixture-rotated-key')
    assert store.configuration_readiness()['key_state'] == 'environment_changed'
    assert not store.configuration_readiness()['manager_ready']


def test_dotenv_source_explains_pending_edit_shell_precedence_and_removal(tmp_path, monkeypatch):
    import app.config as config
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED', raising=False)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, '_SHELL_ENV_NAMES', frozenset({'MODEL_PROTOCOL'}))
    monkeypatch.setattr(config, '_INITIAL_DOTENV_NAMES', frozenset({'MODEL_BASE_URL', 'NINTH_SOURCE_KEY'}))
    monkeypatch.setenv('MODEL_BASE_URL', 'https://loaded.example.test/v1')
    monkeypatch.setenv('MODEL_PROTOCOL', 'responses')
    monkeypatch.setenv('MODEL_KEY_ENV', 'NINTH_SOURCE_KEY')
    monkeypatch.setenv('NINTH_SOURCE_KEY', 'source-fixture-private-value')
    (tmp_path / '.env').write_text('MODEL_BASE_URL=https://edited.example.test/v1\nMODEL_PROTOCOL=chat_completions\n')
    result = config.environment_connection_source()
    assert result['restart_required']
    assert result['fields']['MODEL_BASE_URL']['source'] == 'dotenv'
    assert result['fields']['MODEL_PROTOCOL']['shell_overrides_file']
    assert result['fields']['NINTH_SOURCE_KEY']['file_differs']
    assert 'source-fixture-private-value' not in json.dumps(result)
    assert 'loaded.example.test' not in json.dumps(result)


def test_exact_builtin_prompt_upgrade_is_idempotent_and_preserves_custom_and_bindings(tmp_path, env, monkeypatch):
    import hashlib
    import app.agent_config as agent_config
    store = Store(tmp_path / 'prompt-upgrade.sqlite3')
    before = store.list_agent_roles(include_archived=True)
    old = 'Exact previous built-in prompt for deterministic migration fixture'
    custom = 'User-maintained portfolio instruction'
    monkeypatch.setattr(agent_config, 'NINTH_PREVIOUS_PROMPTS', {
        'manager': hashlib.sha256(old.encode()).hexdigest(),
        'portfolio_researcher': hashlib.sha256(old.encode()).hexdigest()})
    with store.connect() as db:
        db.execute("DELETE FROM meta WHERE key='ninth_role_migration'")
        for role_id, prompt in [('manager', old), ('portfolio_researcher', custom)]:
            role = next(r for r in before if r['role_id'] == role_id)
            body = {k: v for k, v in role.items() if k not in {'resolved', 'override', 'config_error', 'config_warning'}}
            body['system_prompt'] = prompt
            db.execute('UPDATE agent_definitions SET body=? WHERE role_id=?', (json.dumps(body), role_id))
    reopened = Store(store.path)
    manager = next(r for r in reopened.list_agent_roles() if r['role_id'] == 'manager')
    portfolio = next(r for r in reopened.list_agent_roles() if r['role_id'] == 'portfolio_researcher')
    assert manager['system_prompt'] != old
    assert manager['version'] == before[0]['version'] + 1
    assert manager['default_binding'] == before[0]['default_binding']
    assert portfolio['system_prompt'] == custom
    assert Store(store.path).list_agent_roles()[0]['version'] == manager['version']


@pytest.mark.asyncio
async def test_committed_operation_remains_queryable_when_directory_phase_crashes(tmp_path, env, monkeypatch):
    store = Store(tmp_path / 'committed-before-error.sqlite3')
    store._credential_store = FixtureCredentials()
    operation = str(uuid.uuid4())
    async def broken_discovery(*args, **kwargs):
        raise RuntimeError('controlled post-commit failure')
    monkeypatch.setattr(store, 'discover_models', broken_discovery)
    payload = {'base_url': 'https://saved.example.test/v1', 'protocol': 'responses', 'api_key': 'fixture-post-commit-private-value'}
    with pytest.raises(RuntimeError, match='post-commit'):
        await store.save_provider(payload, operation_id=operation)
    checked = store.provider_operation(operation)
    assert checked['status'] == 'committed'
    again = await store.save_provider(payload, operation_id=operation)
    assert again['id'] == checked['provider']['id']
    assert len(store._credential_store.values) == 1
    assert payload['api_key'] not in json.dumps(checked)
