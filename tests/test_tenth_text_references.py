"""Ordinary-text citations use exact IDs and read-only, account-scoped locators."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.domain import AccountInput
from app.main import create_app
from app.references import explicit_material_ids


@pytest.fixture
def app(tmp_path):
    value = create_app(tmp_path / 'text-references.sqlite3', worker=False)
    value.state.store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    return value


def raw_material(store):
    return store.save_material({'scope': 'public', 'text': 'actual selected original pages',
        'url': 'https://example.test/original.pdf', 'access': 'excerpt', 'document_id': 'fixture-original-hash',
        'returned_range': {'start_page': 1, 'end_page': 4}, 'truncated': True})


def account(store, name='A'):
    return store.save_account(AccountInput(name=name, cash='10000',
        holdings=[{'symbol': 'SZ:159278', 'quantity': '10000'}]))


def test_explicit_markers_only_and_bounded_id_discovery():
    mid = 'abcdef0123456789abcdef0123456789'
    text = f'材料{mid}；材料 ID：`{mid.upper()}`；material_id={mid}；材料编号 {mid}'
    assert explicit_material_ids(text) == ([mid], False)
    for text in (mid, f'任务ID {mid}', f'报告 {mid}', f'材料{mid}{mid}',
                 f'https://example.test/材料{mid}', f'https://example.test/?material_id={mid}'):
        assert explicit_material_ids(text) == ([], False)
    ids, limited = explicit_material_ids(' '.join(f'材料{i:032x}' for i in range(201)))
    assert len(ids) == 200 and limited


def test_resolver_exact_ranges_privacy_unknown_and_link_semantics(app):
    store = app.state.store
    a, b = account(store), account(store, 'B')
    raw = raw_material(store)
    private = store.save_material({'scope': 'account', 'text': 'B private body', 'title': 'B private title'}, account_id=b['id'])
    link = store.save_material({'scope': 'public', 'url': 'https://example.test/link',
        'representation': 'native_search_source', 'access': 'source_link'})
    failed = store.save_material({'scope': 'public', 'url': 'https://example.test/failed', 'access': 'failed'})
    unknown = 'f' * 32
    report = f"材料{raw['id']}；材料ID {raw['id']}；材料{private['id']}；材料{unknown}；材料{link['id']}；材料{failed['id']}"
    original = {'report': report, 'material_ids': [raw['id']]}
    result = store.resolve_result_references(original, a['id'])
    refs = {r['material_id']: r for r in result['references']}
    assert len(refs) == len(result['references']) == 5
    assert result['report'] == report and result['material_ids'] == original['material_ids']
    assert refs[raw['id']]['status'] == 'resolved' and refs[raw['id']]['access'] == 'excerpt'
    assert refs[raw['id']]['range']['returned_range'] == {'start_page': 1, 'end_page': 4}
    assert refs[private['id']]['reason'] == refs[unknown]['reason'] == 'unknown_or_inaccessible'
    assert 'B private' not in str(result)
    assert refs[link['id']]['status'] == refs[failed['id']]['status'] == 'source_link'
    assert refs[failed['id']]['access'] == 'failed'
    assert 'locator' not in refs[link['id']] and 'locator' not in refs[failed['id']]


def test_ordinary_text_publication_keeps_text_and_resolves_its_citations(app):
    store = app.state.store
    a = account(store)
    raw = raw_material(store)
    job = store.claim_jobs()[0]
    report = f"维持观察。只读了选页；材料{raw['id']}，{raw['url']}。"
    assert store.publish(job['id'], {'report': report}, None)
    with store.connect() as db:
        row = db.execute('SELECT report,result FROM analyses WHERE job_id=?', (job['id'],)).fetchone()
        result = json.loads(row['result'])
        assert row['report'] == result['report'] == report
        assert 'material_ids' not in result and 'suggestions' not in result and 'forecasts' not in result
        assert result['reference_summary'] == {'resolved': 1, 'source_link': 0, 'unresolved': 0}
        assert result['references'][0]['material_id'] == raw['id']
        assert db.execute('SELECT count(*) FROM research_material_deliveries').fetchone()[0] == 0
    assert store.analyses(a['id'])[0]['report'] == report


def test_legacy_complete_http_reads_project_only_missing_explicit_ids(app, monkeypatch):
    store = app.state.store
    a = account(store)
    raw = raw_material(store)
    job = store.claim_jobs()[0]
    report = f"旧普通文本报告；材料{raw['id']}。"
    assert store.publish(job['id'], {'report': report}, None)
    with store.connect() as db:
        aid = db.execute('SELECT id FROM analyses WHERE job_id=?', (job['id'],)).fetchone()[0]
        legacy = json.dumps({'report': report, 'references': [],
            'reference_summary': {'resolved': 0, 'source_link': 0, 'unresolved': 0}}, ensure_ascii=False)
        db.execute('UPDATE analyses SET result=? WHERE id=?', (legacy, aid))
    with TestClient(app) as client:
        for url in (f"/api/accounts/{a['id']}/analyses", f'/api/analyses/{aid}'):
            response = client.get(url)
            assert response.status_code == 200
            body = response.json();body = body[0] if isinstance(body, list) else body
            assert body['report'] == body['result']['report'] == report
            assert body['result']['reference_summary']['resolved'] == 1
            ref = body['result']['references'][0]
            assert ref['material_id'] == raw['id'] and ref['range']['returned_range']['end_page'] == 4
            assert client.get(ref['locator']).status_code == 200
            assert 'material_ids' not in body['result']
        def forbidden(*args, **kwargs):
            raise AssertionError('summary polling must not project full report references')
        monkeypatch.setattr(store, 'project_report_references', forbidden)
        summary = client.get(f"/api/accounts/{a['id']}/analyses?summary=true")
        assert summary.status_code == 200 and 'result' not in summary.json()[0]
    with store.connect() as db:
        assert db.execute('SELECT result FROM analyses WHERE id=?', (aid,)).fetchone()[0] == legacy
        assert db.execute('SELECT count(*) FROM research_material_deliveries').fetchone()[0] == 0


def test_global_complete_read_is_public_and_summary_skips_projection(app, monkeypatch):
    store = app.state.store
    b = account(store, 'B')
    raw = raw_material(store)
    private = store.save_material({'scope': 'account', 'text': 'private B'}, account_id=b['id'])
    store.request_analysis(None, 'fixture', 'text-reference-global')
    run = store.claim_global()
    report = f"公开报告材料{raw['id']}，材料{private['id']}。"
    store.finish_global(run['id'], 'completed', {'report': report})
    legacy = json.dumps({'report': report, 'references': []}, ensure_ascii=False)
    with store.connect() as db:
        db.execute('UPDATE global_runs SET result=? WHERE id=?', (legacy, run['id']))
    with TestClient(app) as client:
        detail = client.get('/api/global-research/' + run['id'])
        assert detail.status_code == 200
        result = detail.json()['result']
        assert result['report'] == report and 'material_ids' not in result
        assert result['reference_summary'] == {'resolved': 1, 'source_link': 0, 'unresolved': 1}
        assert 'private B' not in str(result)
        assert store.global_runs()[0]['result']['reference_summary'] == result['reference_summary']
        def forbidden(*args, **kwargs):
            raise AssertionError('global index does not display a report')
        monkeypatch.setattr(store, 'project_report_references', forbidden)
        index = client.get('/api/global-research')
        assert index.status_code == 200 and 'result' not in index.json()[0]
    with store.connect() as db:
        assert db.execute('SELECT result FROM global_runs WHERE id=?', (run['id'],)).fetchone()[0] == legacy
        assert db.execute('SELECT count(*) FROM research_material_deliveries').fetchone()[0] == 0


def test_projection_is_idempotent_and_leaves_unrelated_references_unchanged(app, monkeypatch):
    store = app.state.store
    raw = raw_material(store)
    stored = {'report': f"材料{raw['id']}", 'references': [
        {'url': 'https://example.test/only-link', 'status': 'source_link', 'reason': 'not_located_in_saved_material'}]}
    original = copy.deepcopy(stored)
    projected = store.project_report_references(stored)
    assert stored == original and projected['references'][0] == stored['references'][0]
    def forbidden(*args, **kwargs):
        raise AssertionError('existing IDs must not be re-resolved or fetched')
    monkeypatch.setattr(store, 'resolve_result_references', forbidden)
    assert store.project_report_references(projected) is projected
    assert store.project_report_references({'report': 'no explicit material IDs'}) == {'report': 'no explicit material IDs'}


def test_bounded_reader_keeps_limit_notice_when_scanned_ids_already_resolved(app, monkeypatch):
    store = app.state.store
    report = ' '.join(f'材料{i:032x}' for i in range(201))
    resolved = store.resolve_result_references({'report': report})
    assert len(resolved['references']) == 200
    assert resolved['text_reference_scope']['limited'] is True
    assert resolved['reference_summary']['unresolved'] == 200
    original = copy.deepcopy(resolved)
    def forbidden(*args, **kwargs):
        raise AssertionError('all bounded IDs already present; no new queries needed')
    monkeypatch.setattr(store, 'resolve_result_references', forbidden)
    projected = store.project_report_references(resolved)
    assert projected['reference_projection'] == {'kind': 'explicit_material_ids_read_only',
        'added_material_ids': [], 'limited': True}
    assert projected['references'] == resolved['references'] and resolved == original
