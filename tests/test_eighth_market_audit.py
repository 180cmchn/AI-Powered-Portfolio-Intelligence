"""Production-tool regressions for audited unavailable/budget/provenance boundaries."""
import json
from pathlib import Path

import pytest

from app import providers
from app.service import ResearchTools
from app.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'market-audit.sqlite3')
    account = store.save_account({'name': 'isolated audit', 'cash': '0', 'holdings': []})
    return store, ResearchTools(store, store.job(account['latest_job_id']), {'max_tool_result_chars': 2200})


def saved(store, symbol='SZ:159278', **extra):
    payload = {'symbol': symbol, 'source': 'offline fixture', 'interval': 'daily', 'adjustment': 'none',
               'status': 'ok', 'rows': [{'time': '2026-10-27', 'close': '100'}, {'time': '2026-10-28', 'close': '101'}], **extra}
    return store.save_material({'scope': 'public', 'access': 'structured_data',
        'source_identifier': 'audit:' + symbol, 'content': json.dumps(payload)})


@pytest.mark.asyncio
async def test_source_failure_is_not_a_valid_empty_or_missing_date(setup, monkeypatch):
    store, tools = setup
    async def unavailable(self, **kwargs):
        return {'symbol': kwargs['symbol'], 'source': 'offline HTTP failure fixture', 'interval': 'daily',
                'adjustment': 'none', 'status': 'unavailable', 'rows': [], 'error': '网络连接失败 (ConnectError)'}
    monkeypatch.setattr(providers.MarketProvider, 'history', unavailable)
    view = await tools.history('SZ:159278', start='2026-10-28', end='2026-10-28')
    assert view['status'] == 'unavailable' and view['source_status'] == 'unavailable'
    assert view['error'] == '网络连接失败 (ConnectError)' and view['reason'] == 'source_unavailable'
    exact = await tools.query_market_rows(view['material_id'], start='2026-10-28', end='2026-10-28')
    assert exact['missing_exact_date'] is None and exact['status'] == 'unavailable' and exact['next_action']
    assert exact['row_count'] == 0 and not exact['has_more'] and exact['next_offset'] is None
    calculation = await tools.market_indicators(view['material_id'], window=2)
    assert calculation['status'] == 'unavailable' and calculation['error'] == view['error']
    assert 'return' not in calculation
    valid_empty = saved(store, rows=[])
    valid = await tools.query_market_rows(valid_empty['id'], start='2026-10-28', end='2026-10-28')
    assert valid['status'] == 'missing' and valid['missing_exact_date'] == '2026-10-28'


@pytest.mark.asyncio
@pytest.mark.parametrize('left_time,right_time,expected', [
    (None, None, None), ('invalid', 'invalid', None), ('2026-10-28', '2026-10-28', None),
    ('2026-10-28T16:00:00+08:00', None, None),
    ('2026-10-28T16:00:00+08:00', '2026-10-28T16:01:00+08:00', False),
    ('2026-10-28T16:00:00+08:00', '2026-10-28T08:00:00+00:00', True)])
async def test_fetch_snapshot_is_unknown_without_actual_timestamps(setup, left_time, right_time, expected):
    store, tools = setup
    tools.config['max_tool_result_chars'] = 6000
    left = saved(store, fetched_at=left_time)
    right = saved(store, symbol='SH:600519', fetched_at=right_time)
    result = await tools.market_indicators(left['id'], window=2, benchmark_material_id=right['id'])
    assert result['comparison_status'] == 'ok' and result['same_fetch_snapshot'] is expected
    assert result['fetch_snapshot_comparison_basis'] == ('missing_or_invalid_fetch_timestamps' if expected is None else 'timestamp_equality_not_atomic_snapshot_guarantee')
    assert len(json.dumps(result, ensure_ascii=False)) <= 6000


@pytest.mark.asyncio
async def test_oversized_single_field_never_produces_zero_progress_pagination(setup):
    store, tools = setup
    material = saved(store, rows=[{'time': '2026-10-28', 'close': '1' * 4000}])
    result = await tools.query_market_rows(material['id'], fields=['close'])
    assert result['status'] == 'response_budget_exceeded' and result['error_kind'] == 'row_exceeds_response_budget'
    assert result['row_count'] == 0 and result['has_more'] is False and result['next_offset'] is None
    assert result['remaining_stored_rows'] == 1 and 'read_material_range' in result['next_action']
    assert len(json.dumps(result, ensure_ascii=False)) <= 2200
    narrow = await tools.query_market_rows(material['id'], fields=['time'])
    assert narrow['rows'] == [{'time': '2026-10-28'}] and not narrow['has_more']
    raw = await tools.read_material_range(material['id'], start=2000, length=1000)
    assert len(raw['text']) == 1000 and raw['next_start'] > raw['start']


@pytest.mark.asyncio
async def test_oversized_metadata_has_small_actionable_error(setup):
    store, tools = setup
    material = saved(store, source_url='https://fixture.example/' + 'x' * 5000)
    result = await tools.query_market_rows(material['id'])
    assert result['error_kind'] == 'metadata_exceeds_response_budget'
    assert not result['has_more'] and result['next_offset'] is None and result['next_action']
    assert len(json.dumps(result, ensure_ascii=False)) <= 2200


@pytest.mark.asyncio
async def test_unavailable_benchmark_preserves_asset_calculation_and_source_reason(setup):
    store, tools = setup
    tools.config['max_tool_result_chars'] = 6000
    left = saved(store)
    right = saved(store, symbol='CSI300', status='unavailable', rows=[], error='网络请求超时')
    result = await tools.market_indicators(left['id'], window=2, benchmark_material_id=right['id'])
    assert result['status'] == 'ok' and result['return'] == '0.01'
    assert result['comparison_status'] == 'benchmark_source_unavailable' and result['relative_return'] is None
    assert result['benchmark']['error'] == '网络请求超时'
