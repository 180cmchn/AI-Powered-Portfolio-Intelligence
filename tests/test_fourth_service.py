"""Production Store/Service/API integration with explicitly controlled inputs."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.domain import AccountInput, now
from app.main import create_app
from app.model import native_search_fingerprint
from app.service import ResearchTools, Service
from app.store import Store


def test_v3_native_migration_backup_and_explicit_off(tmp_path):
    path = tmp_path / 'upgrade.sqlite3'
    original = Store(path)
    a = original.save_account(AccountInput(name='unchanged'))
    original.set_meta('schema_version', 3)
    with original.connect() as db:
        db.execute("DELETE FROM meta WHERE key='native_search_migration'")
    original.set_meta('settings', {'search_provider': 'disabled', 'search_key_env': 'PRIVATE_LEGACY_ENV'})
    upgraded = Store(path)
    assert upgraded.settings()['model_native_search'] is True
    assert 'search_provider' not in upgraded.settings()
    assert upgraded.account(a['id']) == original.account(a['id'])
    with sqlite3.connect(str(path) + '.before-v4.bak') as backup:
        assert json.loads(backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) == 3
    assert upgraded.meta('native_search_migration')['legacy_search_provider'] == 'disabled'
    upgraded.set_meta('settings', {**upgraded.meta('settings'), 'model_native_search': False})
    assert Store(path).settings()['model_native_search'] is False
    assert upgraded.meta('settings')['search_key_env'] == 'PRIVATE_LEGACY_ENV'


def test_native_status_read_never_probes_and_explicit_probe_is_cached(tmp_path, monkeypatch):
    from app import model
    app = create_app(tmp_path / 'api.sqlite3', worker=False)
    store = app.state.store
    calls = []
    async def probe(config):
        calls.append(config)
        return {'status': 'observed', 'fingerprint': native_search_fingerprint(config), 'checked_at': now()}
    monkeypatch.setattr(model, 'probe_native_search', probe)
    client = TestClient(app)
    for _ in range(3):
        assert client.get('/api/status').json()['native_search']['status'] == 'unverified'
    assert calls == []
    assert client.post('/api/settings/probe-native-search').json()['status'] == 'observed'
    assert client.post('/api/settings/probe-native-search').json()['cache_hit'] is True
    assert len(calls) == 1
    store.set_meta('settings', {**store.settings(), 'model_native_search': False})
    assert client.get('/api/status').json()['native_search']['status'] == 'disabled'


@pytest.mark.asyncio
async def test_native_sources_preserve_scope_and_no_fulltext_claim(tmp_path):
    store = Store(tmp_path / 'sources.sqlite3')
    a = store.save_account(AccountInput(name='A'))
    tools = ResearchTools(store, store.job(a['latest_job_id']), store.settings())
    sources = [{'url': 'https://example.test/native-link', 'title': 'link only'},
               {'url': 'https://example.test/native-snippet', 'title': 'visible', 'snippet': 'provided snippet'}]
    saved = await tools.record_native_search('research-child', [{'type': 'web_search_call', 'status': 'completed'}], sources, [])
    assert len(saved) == 2
    for item in saved:
        body = store.material(item['id'])
        assert body['scope'] == 'public'
        assert not body.get('document_id')
        assert body['representation_kind'] == 'native_search_source'
        assert body['native_search']['local_document_downloaded'] is False
    assert store.material(saved[0]['id'])['access'] == 'source_link'
    assert store.material(saved[1]['id'])['content'] == 'provided snippet'
    assert (await tools.search('obsolete'))['available'] is False
    claim = await tools.save_material({'content': 'private conclusion', 'source_material_ids': [saved[0]['id']]})
    assert store.material(claim['id'])['scope'] == 'account'
    assert not store.material_accessible(claim['id'], None)


@pytest.mark.asyncio
async def test_scheduler_native_news_uses_public_worker_once(tmp_path, monkeypatch):
    from app import providers
    from datetime import datetime
    from app.domain import TZ
    store = Store(tmp_path / 'news.sqlite3')
    store.set_meta('settings', {**store.settings(), 'news_queries': ['controlled topic'], 'quote_provider': 'manual'})
    called = []
    async def forbidden(*args, **kwargs):
        raise AssertionError('legacy search called')
    monkeypatch.setattr(providers, 'search', forbidden)
    async def runner(context, cfg, tools, emit):
        called.append(context)
        return {'report': 'controlled public discovery', 'information_impacts': []}
    service = Service(store, runner)
    stamp = datetime(2026, 10, 25, 10, tzinfo=TZ)  # Sunday: only information scheduling.
    await service.scheduler_tick(stamp)
    await asyncio.gather(*list(service.maintenance))
    await service.scheduler_tick(stamp)
    assert len([d for d in store.demands() if d['kind'] == 'news_observation']) == 1
    run = store.claim_global()
    await service.execute_global(run)
    assert len(called) == 1 and called[0]['news_topics'] == ['controlled topic']
    assert store.global_run(run['id'])['state'] == 'completed'


def test_public_relevance_deferred_durable_and_filtered(tmp_path):
    store = Store(tmp_path / 'routing.sqlite3')
    a = store.save_account(AccountInput(name='A', holdings=[{'symbol': 'SH:600519', 'quantity': '100'}]))
    b = store.save_account(AccountInput(name='B', holdings=[{'symbol': 'SZ:000001', 'quantity': '500'}]))
    material = store.save_material({'scope': 'public', 'url': 'https://example.test/event', 'content': 'external'}, notify=True)
    service = Service(store)
    service.notify_material_changes()
    service.notify_material_changes()
    assert store.pending_material_notifications() == [material['id']]
    assert not any(d['kind'] == 'public_information' for d in store.demands(a['id']))
    result = {'information_impacts': [{'material_id': material['id'], 'status': 'relevant', 'symbols': ['SH:600519'], 'explanation': 'issuer match'}]}
    service.route_material_changes('controlled-complete-public', result, [material['id']])
    assert not store.pending_material_notifications()
    assert len([d for d in store.demands(a['id']) if d['kind'] == 'public_information']) == 1
    assert not any(d['kind'] == 'public_information' for d in store.demands(b['id']))
    count = len(store.demands())
    service.route_material_changes('controlled-complete-public', result, [material['id']])
    assert len(store.demands()) == count
    generated = store.save_material({'scope': 'public', 'access': 'generated_report', 'content': 'model prose'}, notify=True)
    assert generated['id'] not in store.pending_material_notifications()


def test_malformed_routing_is_uncertain_and_reprint_event_is_idempotent(tmp_path):
    store = Store(tmp_path / 'safe-routing.sqlite3')
    account = store.save_account(AccountInput(name='A'))
    service = Service(store)
    mids = [store.save_material({'scope': 'public', 'url': 'https://example.test/'+str(i), 'content': 'source'}, notify=True)['id'] for i in range(3)]
    service.notify_material_changes()
    service.route_material_changes('r', {'information_impacts': [{'material_id': mids[0], 'status': 'irrelevant', 'symbols': [{}], 'explanation': 'malformed'}]}, [mids[0]])
    assert any(d['kind'] == 'public_information' for d in store.demands(account['id']))
    impacts = [{'material_id': mid, 'status': 'relevant', 'market_wide': True, 'explanation': 'same disclosed event', 'event_key': 'controlled-report-v1'} for mid in mids[1:]]
    service.route_material_changes('r', {'information_impacts': impacts}, mids[1:])
    assert len([d for d in store.demands(account['id']) if d['kind'] == 'public_information']) == 2
    assert not store.pending_material_notifications()
