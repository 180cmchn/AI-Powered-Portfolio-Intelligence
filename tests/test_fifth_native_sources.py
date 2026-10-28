"""Visible hosted-search evidence, never a claim that the source was downloaded."""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model import ModelClient
from app.service import ResearchTools
from app.store import Store

URL = 'https://example.org/159278'

@pytest.mark.parametrize('field', ['description', 'snippet', 'excerpt'])
async def test_visible_field_reaches_body_index_range_and_reference(tmp_path, field):
    app = create_app(tmp_path/'sources.sqlite3', worker=False)
    store = app.state.store
    tools = ResearchTools(store, {'id':'public-search', 'account_id':None}, {})
    source = ModelClient._source({'url':URL,field:'实际可见的机器人ETF片段'})
    rows = await tools.record_native_search('child-1', [], [source], [])
    row = rows[0]
    assert row['access'] == 'search_excerpt'
    assert row['char_count'] == len('实际可见的机器人ETF片段')
    assert (await tools.get_material(row['id']))['text'] == '实际可见的机器人ETF片段'
    assert (await tools.search_materials('机器人ETF'))[0]['id'] == row['id']
    refs = await tools.resolve_references({'source_material_ids':[row['id']]})
    assert refs['references'][0]['status'] == 'resolved'
    assert refs['references'][0]['range']['read_completeness'] == 'visible_excerpt'
    with TestClient(app) as client:
        body = client.get(f"/api/materials/{row['id']}/range").json()
        assert body['text'] == '实际可见的机器人ETF片段'
        assert body['access'] == 'search_excerpt'
        assert client.get('/api/materials?query=机器人ETF').json()[0]['id'] == row['id']

async def test_distinct_fields_merge_once_preserve_field_provenance_and_deduplicate(tmp_path):
    store = Store(tmp_path/'sources.sqlite3')
    tools = ResearchTools(store, {'id':'public-search', 'account_id':None}, {})
    source = ModelClient._source({'url':URL, 'snippet':'甲', 'excerpt':'甲', 'description':'乙'})
    first = (await tools.record_native_search('manager', [], [source], []))[0]
    second = (await tools.record_native_search('manager', [], [source], []))[0]
    assert first['id'] == second['id']
    body = store.material(first['id'])
    assert body['content'] == '甲\n\n乙'
    assert body['native_search']['visible_metadata']['description'] == '乙'
    assert body['native_search']['visible_metadata']['excerpt'] == '甲'

@pytest.mark.parametrize('fields', [{}, {'snippet':'', 'description':None}, {'snippet':[], 'excerpt':{}, 'description':42}, {'snippet':'  ', 'excerpt':False}])
async def test_missing_or_non_string_text_stays_link_only(tmp_path, fields):
    store = Store(tmp_path/'sources.sqlite3')
    tools = ResearchTools(store, {'id':'public-search', 'account_id':None}, {})
    row = (await tools.record_native_search('manager', [], [ModelClient._source({'url':URL, **fields})], []))[0]
    assert row['access'] == 'source_link' and row['char_count'] == 0
    assert (await tools.get_material(row['id']))['text'] == ''

async def test_long_visible_text_has_honest_bounds(tmp_path):
    store = Store(tmp_path/'sources.sqlite3')
    tools = ResearchTools(store, {'id':'public-search', 'account_id':None}, {})
    row = (await tools.record_native_search('manager', [], [ModelClient._source({'url':URL, 'description':'文'*15000})], []))[0]
    body = store.material(row['id'])
    assert len(body['content']) == 12000
    assert body['truncated'] is True
    assert body['native_search']['visible_metadata']['visible_field_lengths']['description'] == 15000
    assert body['returned_range'] == {'start':0, 'end':12000, 'unit':'character', 'basis':'merged_visible_search_text'}
    assert body['access'] == 'search_excerpt' and 'document_id' not in body
