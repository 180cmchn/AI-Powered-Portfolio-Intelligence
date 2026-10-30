"""Protocol substitutes only: these tests do not establish real provider access."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.agent_config import INITIAL_ROLES, normalize_api_base
from app.credentials import CredentialError, fingerprint
from app.domain import AccountInput
from app.model import ModelClient
from app.store import Store


class MemoryVault:
    """Explicit deterministic fixture; never selected by production code."""
    def __init__(self):
        self.values = {}

    def put(self, value):
        name = f'test-secret-{len(self.values)}'
        self.values[name] = value
        return {'kind': 'keychain', 'id': name, 'fingerprint': fingerprint(value)}

    def get(self, reference):
        value = self.values.get(reference.get('id'))
        if not value or fingerprint(value) != reference.get('fingerprint'):
            raise CredentialError('fixture credential missing')
        return value

    def delete(self, reference):
        self.values.pop(reference.get('id'), None)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'config.sqlite3')
    store._credential_store = MemoryVault()
    return store


def response_transport(payload=None, status=200, captured=None):
    def respond(request):
        if captured is not None:
            captured.append(request)
        return httpx.Response(status, json=payload if payload is not None else {'data': [{'id': 'vendor/model-A'}, {'id': 'same'}]})
    return httpx.MockTransport(respond)


async def supplier(store, key='fixture-credential-one', url='https://fixture.test/custom/v1', protocol='responses', transport=None):
    return await store.save_provider({'api_key': key, 'base_url': url, 'protocol': protocol}, transport=transport or response_transport())


def account(store, name='甲'):
    return store.save_account(AccountInput(name=name, holdings=[]))['id']


@pytest.mark.parametrize('url,expected', [
    ('https://fixture.test/v1/', 'https://fixture.test/v1'),
    ('https://fixture.test/custom/v1/responses', 'https://fixture.test/custom/v1'),
    ('https://fixture.test/forward/api/chat/completions/', 'https://fixture.test/forward/api'),
    ('http://127.0.0.1:1234/v1', 'http://127.0.0.1:1234/v1'),
])
def test_base_normalization_preserves_prefix(url, expected):
    assert normalize_api_base(url) == expected


@pytest.mark.parametrize('url', ['http://remote.test', 'https://user:pass@fixture.test/v1', 'https://fixture.test/v1?key=secret', 'file:///tmp/key'])
def test_untrusted_connection_rejected(url):
    with pytest.raises(ValueError):
        normalize_api_base(url)


@pytest.mark.asyncio
async def test_three_fields_auto_discovery_and_no_secret_in_database_or_get(store):
    requests = []
    provider = await supplier(store, transport=response_transport(captured=requests))
    assert str(requests[0].url) == 'https://fixture.test/custom/v1/models'
    assert requests[0].headers['authorization'] == 'Bearer fixture-credential-one'
    assert provider['catalog']['status'] == 'success'
    assert provider['key_configured']
    assert provider['catalog']['models'][0]['id'] == 'vendor/model-A'
    assert 'fixture-credential-one' not in json.dumps(store.list_providers())
    assert 'credential_reference' not in json.dumps(store.list_providers())
    with store.connect() as db:
        assert 'fixture-credential-one' not in '\n'.join(db.iterdump())
    for invalid in ({'model_name': 'x'}, {'reasoning_effort': 'high'}, {'models_url': 'x'}, {'model_key_env': 'x'}, {'display_name': 'x'}):
        with pytest.raises(ValueError, match='三项'):
            await store.save_provider(invalid)


@pytest.mark.asyncio
async def test_empty_key_retains_secret_mask_rejected_explicit_revoke(store):
    p = await supplier(store)
    saved = await store.save_provider({'api_key': ''}, p['id'], transport=response_transport())
    assert saved['revision'] == p['revision']
    with pytest.raises(ValueError, match='掩码'):
        await store.save_provider({'api_key': '••••••••'}, p['id'])
    cleared = await store.clear_provider_credential(p['id'])
    assert not cleared['key_configured']
    assert cleared['revision'] == p['revision'] + 1
    assert not store._credential_store.values


@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind', [(401, 'authentication_failed'), (403, 'authentication_failed'), (429, 'rate_limited'), (404, 'unsupported'), (405, 'unsupported'), (500, 'http_error'), (302, 'redirect_blocked')])
async def test_directory_failures_are_not_empty_and_do_not_guess_paths(store, status, kind):
    requests = []
    p = await supplier(store, transport=response_transport({'error': 'fixture-credential-one'}, status, requests))
    assert p['catalog']['status'] == kind
    assert not p['catalog']['selectable']
    assert len(requests) == 1
    assert 'fixture-credential-one' not in json.dumps(p)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,expected', [
    ({'data': []}, 'empty'),
    ({'models': [{'slug': 'vendor/Keep-CASE'}]}, 'success'),
    ({'success': True, 'data': {'data': [{'id': 'wrapped'}]}}, 'success'),
    ({'data': {'data': [{'id': 'implicit'}]}}, 'invalid_format'),
    ({'success': False, 'data': []}, 'invalid_format'),
    ({'error': 'unknown', 'data': []}, 'invalid_format'),
    ({'data': ['not-an-object']}, 'invalid_format'),
    ({'success': True, 'data': {'success': True, 'data': {'data': []}}}, 'invalid_format'),
])
async def test_standard_known_envelope_and_true_empty(store, payload, expected):
    p = await supplier(store, transport=response_transport(payload))
    assert p['catalog']['status'] == expected


@pytest.mark.asyncio
async def test_html_and_timeout_keep_previous_cache(store):
    p = await supplier(store)
    html = httpx.MockTransport(lambda r: httpx.Response(200, text='<html>not JSON</html>'))
    failed = await store.discover_models(p['id'], force=True, transport=html)
    assert failed['status'] == 'invalid_format'
    assert failed['cache_retained'] and failed['selectable']
    assert len(failed['models']) == 2
    def timeout(request):
        raise httpx.ReadTimeout('fixture', request=request)
    failed = await store.discover_models(p['id'], force=True, transport=httpx.MockTransport(timeout))
    assert failed['status'] == 'timeout'
    assert failed['fetched_at'] == p['catalog']['fetched_at']
    role = store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same'}})
    assert role['resolved']['model_id'] == 'same'


@pytest.mark.asyncio
async def test_paginated_ids_exact_and_continue_limit(store):
    requests = []
    def respond(request):
        requests.append(request)
        after = int(request.url.params.get('after', '0'))
        return httpx.Response(200, json={'data': [{'id': str(after + 1)}, {'id': 'X/Y'}],
             'has_more': after < 6, 'last_id': str(after + 1)})
    p = await supplier(store, transport=httpx.MockTransport(respond))
    assert p['catalog']['status'] == 'partial'
    assert len(requests) == 5
    done = await store.discover_models(p['id'], continue_page=True, transport=httpx.MockTransport(respond))
    assert done['status'] == 'success'
    assert [m['id'] for m in done['models']] == ['1', 'X/Y', '2', '3', '4', '5', '6', '7']


@pytest.mark.asyncio
@pytest.mark.parametrize('next_url', ['https://evil.test/models', 'http://fixture.test/custom/v1/models', 'https://fixture.test/steal', 'https://user:pw@fixture.test/custom/v1/models'])
async def test_malicious_next_never_receives_auth(store, next_url):
    requests = []
    p = await supplier(store, transport=response_transport({'data': [{'id': 'real'}], 'next': next_url}, captured=requests))
    assert p['catalog']['status'] == 'invalid_format'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_singleflight_and_late_old_connection_response(store):
    entered, release = asyncio.Event(), asyncio.Event()
    p = await supplier(store)
    count = 0
    async def delayed(request):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'data': [{'id': 'late-old'}]})
    old_task = asyncio.create_task(store.discover_models(p['id'], force=True, transport=httpx.MockTransport(delayed)))
    await entered.wait()
    duplicate = asyncio.create_task(store.discover_models(p['id'], force=True, transport=httpx.MockTransport(delayed)))
    await asyncio.sleep(0)
    updated = await store.save_provider({'api_key': 'fixture-credential-two'}, p['id'], transport=response_transport({'data': [{'id': 'fresh'}]}))
    release.set()
    assert (await old_task)['status'] == 'superseded'
    assert (await duplicate)['status'] == 'superseded'
    assert count == 1
    assert updated['revision'] == 2
    assert store.provider(p['id'])['catalog']['models'][0]['id'] == 'fresh'
    with pytest.raises(ValueError, match='实际发现'):
        store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'vendor/model-A'}})


@pytest.mark.asyncio
async def test_shared_directory_and_effort_only_override_restore(store):
    p = await supplier(store)
    a, b = account(store), account(store, '乙')
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same', 'reasoning_effort': 'high'}})
    row = store.set_account_agent_override(a, 'manager', {'reasoning_effort': 'max'})
    assert row['resolved']['provider_id'] == p['id']
    assert row['resolved']['reasoning_effort'] == 'max'
    assert row['resolved']['binding_source'] == 'global_default'
    ids = {r[0] for r in INITIAL_ROLES}
    assert {r['role_id'] for r in store.list_agent_roles(a)} == ids == {r['role_id'] for r in store.list_agent_roles(b)}
    c = account(store, '丙')
    assert {r['role_id'] for r in store.list_agent_roles(c)} == ids
    store.clear_account_agent_override(a, 'manager')
    assert store.list_agent_roles(a)[0]['resolved']['reasoning_effort'] == 'high'
    assert store.list_agent_roles(b)[0]['resolved']['reasoning_effort'] == 'high'
    for payload in ({'enabled': False}, {'tools': []}, {'system_prompt': 'private'}, {'provider_id': p['id']}, {'model_id': 'same'}, {'reasoning_effort': 'ultra'}):
        with pytest.raises(ValueError):
            store.set_account_agent_override(a, 'manager', payload)


@pytest.mark.asyncio
async def test_global_role_add_archive_snapshots_and_isolated_binding_error(store):
    p = await supplier(store)
    a, b = account(store), account(store, '乙')
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same'}})
    before = store.freeze_agent_team(a)
    store.update_agent_definition('custom_researcher', {'name': '全局专题', 'responsibility': '专题职责', 'system_prompt': '统一提示词'})
    assert len(store.list_agent_roles(a)) == len(store.list_agent_roles(b)) == 7
    store.update_agent_definition('market_researcher', {'enabled': False})
    assert 'market_researcher' in before['roles']
    assert 'market_researcher' not in store.freeze_agent_team(a)['roles']
    assert 'market_researcher' not in store.freeze_agent_team(b)['roles']
    assert before['roles']['filing_researcher']['system_prompt'] != before['roles']['etf_researcher']['system_prompt']
    with pytest.raises(ValueError, match='越权'):
        store.update_agent_definition('custom_researcher', {'tools': ['finish_analysis']})
    p2 = await supplier(store, key='other-key')
    store.set_account_agent_override(a, 'manager', {'provider_id': p2['id'], 'model_id': 'same'})
    store.archive_provider(p2['id'])
    assert store.list_agent_roles(a)[0]['config_error']
    assert not store.list_agent_roles(b)[0]['config_error']
    assert {r['role_id'] for r in store.list_agent_roles(a)} == {r['role_id'] for r in store.list_agent_roles(b)}


@pytest.mark.asyncio
async def test_disappeared_model_not_removed_partial_not_deletion_evidence(store):
    p = await supplier(store)
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same'}})
    await store.discover_models(p['id'], force=True, transport=response_transport({'data': []}))
    manager = store.list_agent_roles()[0]
    assert manager['resolved']['model_id'] == 'same'
    assert '未列出' in manager['config_error']
    i = 0
    def partial(request):
        nonlocal i
        i += 1
        return httpx.Response(200, json={'data': [{'id': f'other-{i}'}], 'has_more': True})
    await store.discover_models(p['id'], force=True, transport=httpx.MockTransport(partial))
    assert store.list_agent_roles()[0]['config_error'] is None


@pytest.mark.asyncio
async def test_runtime_models_keys_efforts_preserved_by_frozen_role(store):
    p1 = await supplier(store)
    p2 = await supplier(store, key='fixture-credential-two', protocol='chat_completions')
    a, b = account(store), account(store, '乙')
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p1['id'], 'model_id': 'vendor/model-A', 'reasoning_effort': 'xhigh'}})
    store.update_agent_definition('etf_researcher', {'default_binding': {'provider_id': p1['id'], 'model_id': 'vendor/model-A', 'reasoning_effort': 'low'}})
    store.set_account_agent_override(b, 'manager', {'provider_id': p2['id'], 'model_id': 'same', 'reasoning_effort': 'max'})
    store.set_account_agent_override(b, 'etf_researcher', {'provider_id': p2['id'], 'model_id': 'same', 'reasoning_effort': 'medium'})
    ta, tb = store.freeze_agent_team(a), store.freeze_agent_team(b)
    captured = []
    async def respond(request):
        captured.append((request.headers['authorization'], str(request.url), json.loads(request.content)))
        if request.url.path.endswith('responses'):
            return httpx.Response(200, json={'model': 'reported-alias', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}]})
        return httpx.Response(200, json={'model': 'reported-alias', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})
    clients = [ModelClient(store.runtime_role_config(team, role, {'model_native_search': False}), transport=httpx.MockTransport(respond))
               for team in (ta, tb) for role in ('manager', 'etf_researcher')]
    # Updating bindings and rotating key cannot mutate existing role snapshots.
    await store.save_provider({'api_key': 'fixture-rotated-key'}, p1['id'], transport=response_transport())
    assert store.runtime_role_config(ta, 'manager')['_model_api_key'] == 'fixture-credential-one'
    for client in clients:
        session = client.session('role', {})
        await client.complete(session, [])
        assert client.requests[0]['reported_model'] == 'reported-alias'
        assert client.requests[0]['role_id'] in {'manager', 'etf_researcher'}
        await client.aclose()
    assert [r[0] for r in captured] == ['Bearer fixture-credential-one'] * 2 + ['Bearer fixture-credential-two'] * 2
    assert [r[1] for r in captured] == ['https://fixture.test/custom/v1/responses'] * 2 + ['https://fixture.test/custom/v1/chat/completions'] * 2
    assert [r[2]['model'] for r in captured] == ['vendor/model-A'] * 2 + ['same'] * 2
    assert [r[2].get('reasoning', {}).get('effort') or r[2].get('reasoning_effort') for r in captured] == ['xhigh', 'low', 'max', 'medium']
    await store.clear_provider_credential(p1['id'])
    with pytest.raises(CredentialError, match='撤销'):
        store.runtime_role_config(ta, 'manager')


def test_migration_is_idempotent_and_preserves_unverified_history(store):
    before = store.list_providers()
    reopened = Store(store.path)
    assert len(reopened.list_providers()) == len(before)
    assert reopened.list_agent_roles()[0]['default_binding']['validation'] == 'legacy_unverified'
    with reopened.connect() as db:
        assert 'model_name' not in _provider_text(db)
        assert 'reasoning_effort' not in _provider_text(db)


def _provider_text(db):
    return '\n'.join(r[0] for r in db.execute('SELECT body FROM model_providers'))


@pytest.mark.asyncio
async def test_team_identity_ignores_discovery_times_cache_and_inheritance_labels(store):
    p = await supplier(store)
    a, b = account(store), account(store, '乙')
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same', 'reasoning_effort': 'high'}})
    before = store.freeze_agent_team(a)
    assert before['team_id'] == store.freeze_agent_team(b)['team_id']
    await store.discover_models(p['id'], force=True, transport=response_transport())
    assert store.freeze_agent_team(a)['team_id'] == before['team_id']
    await store.discover_models(p['id'], force=True, transport=response_transport(status=429))
    assert store.freeze_agent_team(a)['team_id'] == before['team_id']
    await store.save_provider({'api_key': ''}, p['id'], transport=response_transport())
    assert store.freeze_agent_team(a)['team_id'] == before['team_id']
    store.set_account_agent_override(a, 'manager', {'provider_id': p['id'], 'model_id': 'same', 'reasoning_effort': 'high'})
    assert store.freeze_agent_team(a)['team_id'] == before['team_id']
    unchanged = store.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'high'}})
    assert unchanged['version'] == before['roles']['manager']['version']
    store.set_account_agent_override(a, 'manager', {'reasoning_effort': 'max'})
    assert store.freeze_agent_team(a)['team_id'] != before['team_id']
    assert store.freeze_agent_team(b)['team_id'] == before['team_id']
    assert 'fixture-credential-one' not in json.dumps(before)


@pytest.mark.asyncio
async def test_provider_cannot_reflect_credentials_into_public_model_catalog(store):
    p = await supplier(store, transport=response_transport({'data': [{'id': 'fixture-credential-one'}]}))
    assert p['catalog']['status'] == 'invalid_format'
    assert 'fixture-credential-one' not in json.dumps(p)
    p = await supplier(store, transport=response_transport({'data': [{'id': 'valid', 'owned_by': 'fixture-credential-one'}]}))
    assert p['catalog']['models'][0]['metadata'] == {'redacted': True}
    assert 'fixture-credential-one' not in json.dumps(p)


@pytest.mark.asyncio
async def test_refresh_failure_does_not_undo_last_complete_catalog_absence(store):
    p = await supplier(store)
    store.update_agent_definition('manager', {'default_binding': {'provider_id': p['id'], 'model_id': 'same'}})
    await store.discover_models(p['id'], force=True, transport=response_transport({'data': []}))
    assert store.list_agent_roles()[0]['config_error']
    await store.discover_models(p['id'], force=True, transport=response_transport(status=429))
    assert '未列出' in store.list_agent_roles()[0]['config_error']


@pytest.mark.asyncio
async def test_legacy_environment_snapshot_never_silently_switches_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_KEY_ENV', 'SIXTH_LEGACY_FIXTURE_KEY')
    monkeypatch.setenv('SIXTH_LEGACY_FIXTURE_KEY', 'legacy-fixture-key-one')
    monkeypatch.setenv('MODEL_NAME', 'legacy-model')
    store = Store(tmp_path / 'legacy.sqlite3')
    # A legacy model name remains pending until the actual directory confirms it.
    provider = store.list_providers()[0]
    assert store.list_agent_roles()[0]['config_error']
    await store.discover_models(provider['id'], transport=response_transport({'data': [{'id': 'legacy-model'}]}))
    team = store.freeze_agent_team()
    assert store.runtime_role_config(team, 'manager')['_model_api_key'] == 'legacy-fixture-key-one'
    monkeypatch.setenv('SIXTH_LEGACY_FIXTURE_KEY', 'legacy-fixture-key-two')
    with pytest.raises(CredentialError, match='已变更'):
        store.runtime_role_config(team, 'manager')
    assert 'legacy-fixture-key-one' not in json.dumps(team)
    assert 'legacy-fixture-key-two' not in json.dumps(team)


@pytest.mark.asyncio
async def test_second_page_failure_retains_cached_and_newly_observed_models(store):
    p = await supplier(store)
    def fail_second(request):
        if 'after' in request.url.params:
            return httpx.Response(429)
        return httpx.Response(200, json={'data': [{'id': 'new-first-page'}], 'has_more': True})
    partial = await store.discover_models(p['id'], force=True, transport=httpx.MockTransport(fail_second))
    assert partial['status'] == 'rate_limited'
    assert partial['complete'] is False and partial['next_url']
    assert {m['id'] for m in partial['models']} == {'vendor/model-A', 'same', 'new-first-page'}
    assert partial['selectable'] is True


@pytest.mark.asyncio
async def test_malformed_configuration_payloads_fail_as_validation_errors(store):
    p = await supplier(store)
    a = account(store)
    for payload in (None, [], {'provider_id': [], 'model_id': 'same'}, {'provider_id': p['id'], 'model_id': None}):
        with pytest.raises(ValueError):
            store.set_account_agent_override(a, 'manager', payload)
    with pytest.raises(ValueError):
        store.update_agent_definition('manager', {'default_binding': None})
    with pytest.raises(ValueError):
        await store.save_provider({'protocol': []}, p['id'])


def test_invalid_legacy_connection_does_not_block_account_database(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_BASE_URL', 'http://remote-not-allowed.example/v1')
    store = Store(tmp_path / 'legacy-invalid.sqlite3')
    assert len(store.list_agent_roles()) == 6
    assert 'Base URL' in store.list_agent_roles()[0]['config_error']
    assert account(store)
