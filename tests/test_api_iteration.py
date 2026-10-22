"""HTTP integration regressions using local SQLite, no model or provider network calls."""
import json

import pytest
from fastapi.testclient import TestClient

from app.domain import AccountInput, now
from app.main import create_app


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'iteration.sqlite3', worker=False)
    app.state.store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0})
    with TestClient(app) as client:
        yield client, app.state.store


def make_account(store):
    return store.save_account(AccountInput(name='API 账户', holdings=[{'symbol': 'SH:600519', 'quantity': '100'}]))


def test_material_index_is_bounded_and_text_ranges_preserve_access(api):
    client, store = api
    account = make_account(store)
    body = ('原文数据' * 10000) + '末尾关键数据'
    material = store.save_material({'title': '长财报', 'url': 'https://example.com/report.pdf', 'text': body,
                                    'access': 'excerpt', 'truncated': True, 'page_count': 50,
                                    'returned_range': {'start_page': 25, 'end_page': 26}},
                                   account['latest_job_id'], account['id'])
    response = client.get('/api/materials', params={'account_id': account['id'], 'limit': 1})
    assert response.status_code == 200
    index = response.json()
    assert len(index) == 1 and index[0]['id'] == material['id']
    assert 'text' not in index[0] and 'content' not in index[0]
    assert len(json.dumps(index, ensure_ascii=False)) < 2500
    assert client.get('/api/materials', params={'account_id': account['id'], 'offset': 1}).json() == []
    found = client.get('/api/materials', params={'query': '末尾关键数据'}).json()
    assert found[0]['id'] == material['id']
    fragment = client.get(f"/api/materials/{material['id']}/range", params={'start': 40000, 'length': 100}).json()
    assert fragment['text'] == '末尾关键数据'
    assert fragment['start'] == 40000 and fragment['next_start'] is None
    assert fragment['access'] == 'excerpt' and fragment['truncated'] is True
    assert fragment['returned_range'] == {'start_page': 25, 'end_page': 26}
    assert client.get(f"/api/materials/{material['id']}").json()['text'] == body
    assert client.get('/api/materials', params={'index': False, 'limit': 1}).json()[0]['text'] == body


def test_job_events_increment_and_report_body_is_on_demand(api):
    client, store = api
    account = make_account(store)
    job = store.claim_jobs()[0]
    for i in range(3):
        store.event(job['id'], {'type': 'progress', 'message': f'步骤 {i}'})
    store.publish(job['id'], {'report': '长报告正文' * 1000}, now())
    jobs = client.get(f"/api/accounts/{account['id']}/jobs", params={'limit': 1}).json()
    assert len(jobs) == 1 and 'events' not in jobs[0] and 'result' not in jobs[0]
    assert jobs[0]['event_count'] >= 3
    first = client.get(f"/api/jobs/{job['id']}/events", params={'limit': 2}).json()
    second = client.get(f"/api/jobs/{job['id']}/events", params={'after': first['next_after']}).json()
    assert first['has_more'] is True
    assert {e['seq'] for e in first['events']}.isdisjoint(e['seq'] for e in second['events'])
    assert all(e['seq'] > first['next_after'] for e in second['events'])
    summaries = client.get(f"/api/accounts/{account['id']}/analyses", params={'summary': True}).json()
    assert 'report' not in summaries[0] and 'result' not in summaries[0]
    report = client.get(f"/api/analyses/{summaries[0]['id']}").json()
    assert report['report'] == '长报告正文' * 1000
    assert client.get(f"/api/accounts/{account['id']}/analyses", params={'offset': 1}).json() == []


def test_suggestions_public_research_and_demand_traceability(api):
    client, store = api
    account = make_account(store)
    job = store.claim_jobs()[0]
    store.publish(job['id'], {'report': '已过期建议', 'suggestions': [{'symbol': 'SH:600519', 'action': 'hold',
                  'valid_until': '2020-01-01T00:00:00+08:00', 'basis': '需复核的已保存依据'}]}, now())
    result = client.get(f"/api/accounts/{account['id']}/recommendations").json()
    assert result[0]['state'] == 'expired' and result[0]['input_version'] == 1
    store.save_research_records([{'kind': 'candidate', 'security_id': 'SH:600519', 'title': '公共候选', 'reason': '可复用依据'}], 'global-fixture')
    assert client.get('/api/research', params={'account_id': account['id']}).json()[0]['scope'] == 'public'
    store.request_analysis(account['id'], 'scheduled', event_key='test-scheduled')
    demands = client.get('/api/demands', params={'account_id': account['id']}).json()
    assert any(row['event_key'] == 'test-scheduled' and row['state'] == 'assigned' for row in demands)
    assert client.get('/api/global-research').status_code == 200


def test_settings_roundtrip_preserves_active_job_and_tracks_new_demand(api):
    client, store = api
    account = make_account(store)
    job = store.claim_jobs()[0]
    settings = client.get('/api/settings').json()
    settings.update(news_interval_minutes=45, news_start_hour=8, news_end_hour=23, news_read_sources=False,
                    research_provider='tushare', tushare_token_env='TEST_TUSHARE_TOKEN', read_cache_seconds=600)
    saved = client.put('/api/settings', json=settings)
    assert saved.status_code == 200, saved.text
    assert saved.json()['news_interval_minutes'] == 45
    assert saved.json()['news_read_sources'] is False
    assert store.job(job['id'])['state'] == 'running'
    assert any(d['kind'] == 'configuration_changed' and d['state'] == 'pending' for d in store.demands(account['id']))


@pytest.mark.parametrize('path', ['/api/materials?limit=0', '/api/materials?offset=-1', '/api/global-research?limit=1000', '/api/demands?offset=-1'])
def test_invalid_pagination_is_rejected(api, path):
    client, _ = api
    assert client.get(path).status_code == 422
