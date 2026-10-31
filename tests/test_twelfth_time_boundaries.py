"""Production ledger/API with one explicit clock and a synthetic exchange calendar.

The calendar only supplies the named cases; it is not live exchange data.
"""
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import clock, domain
from app.calendar import market_state
from app.main import create_app
from app.store import Store


@pytest.fixture
def boundary_api(tmp_path, frozen_clock, monkeypatch):
    calendar = tmp_path / 'calendar-fixture.json'
    calendar.write_text(json.dumps({
        'coverage': {'start': '2026-09-30', 'end': '2026-11-03'},
        'trading_dates': ['2026-09-30', '2026-10-08', '2026-10-09',
                          '2026-10-23', '2026-10-26', '2026-10-27',
                          '2026-10-28', '2026-10-29', '2026-10-30',
                          '2026-11-02', '2026-11-03'],
        'source': 'controlled-twelfth-calendar', 'version': 'fixture-1',
    }))
    monkeypatch.setenv('STOCK_CALENDAR_PATH', str(calendar))
    app = create_app(tmp_path / 'boundary.sqlite3', worker=False)
    app.state.store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    for sid, name, kind in [('SZ:002354', '受控股票', 'stock'),
                            ('SH:518880', '受控黄金ETF', 'etf')]:
        app.state.store.put_cache('securities', sid, {
            'symbol': sid, 'name': name, 'kind': kind, 'status': 'ok',
            'identity_status': 'confirmed', 'source': 'controlled-directory'})
    with TestClient(app) as client:
        yield app.state.store, client, calendar


def create(client, holdings=None):
    response = client.post('/api/accounts', json={
        'name': '时间边界夹具', 'cash': '100000', 'holdings': holdings or []})
    assert response.status_code == 201, response.text
    return response.json()


def trade(client, account_id, **extra):
    a = client.get('/api/accounts/' + account_id).json()
    return {'operation_id': 'boundary-trade-001', 'expected_version': a['version'],
        'fee_plan_version': 1, 'symbol': '002354', 'side': 'buy', 'quantity': '1000',
        'price': '10', 'traded_at': '2026-10-30T10:00:00+08:00', 'mode': 'simulated', **extra}


@pytest.mark.parametrize('wall,effective,can_sell', [
    ('2026-10-30T15:59:59+00:00', '2026-10-30', True),
    ('2026-10-30T16:00:00+00:00', '2026-10-31', False),
    ('2026-10-31T00:00:01+08:00', '2026-10-31', False),
])
def test_correction_date_never_backdates_availability(boundary_api, frozen_clock, wall, effective, can_sell):
    store, client, _ = boundary_api
    frozen_clock.set(wall)
    a = create(client, [{'symbol': '002354', 'quantity': '1000'}])
    base = '/api/accounts/' + a['id']
    correction = client.post(base + '/corrections', json={
        'operation_id': 'boundary-correction-001', 'expected_version': 1,
        'cash': a['cash'],
        'reason': '受控时点可卖依据，不证明此前日期',
        'holdings': [{'symbol': '002354', 'quantity': '1000', 'available_quantity': '1000'}]})
    assert correction.status_code == 201, correction.text
    entry = correction.json()['entry']
    assert entry['availability_effective_on'] == effective
    assert entry['recorded_at'] == clock.now_datetime().isoformat()
    assert entry['after_positions']['SZ:002354']['lots'][0]['sellable_on'] == effective
    assert client.get(base + '/ledger').json()['positions'][0]['available_quantity'] == '1000'
    payload = trade(client, a['id'], side='sell', traded_at='2026-10-30T10:00:00+08:00')
    before = store.ledger(a['id'])
    preview = client.post(base + '/trades/preview', json=payload)
    assert store.ledger(a['id']) == before
    response = client.post(base + '/trades', json=payload)
    if can_sell:
        assert preview.status_code == 200, preview.text
        assert response.status_code == 201, response.text
        assert response.json()['entry']['available_before'] == '1000'
    else:
        assert preview.status_code == response.status_code == 422
        assert '2026-10-30' in response.text and '2026-10-31' in response.text
        assert '不证明此前日期' in response.text
        assert store.ledger(a['id']) == before
    # Reopening cannot reinterpret a correction as a historical buy.
    assert Store(store.path).ledger(a['id']) == store.ledger(a['id'])


def test_explicit_backdated_correction_is_rejected(boundary_api):
    store, client, _ = boundary_api
    a = create(client)
    before = store.ledger(a['id'])
    payload = {
        'operation_id': 'backdate-correction-001', 'expected_version': 1,
        'cash': a['cash'], 'holdings': [],
        'reason': '不能用自填日期倒推可卖', 'effective_on': '2026-10-01'}
    response = client.post('/api/accounts/' + a['id'] + '/corrections', json=payload)
    assert response.status_code == 422 and 'extra_forbidden' in response.text
    with pytest.raises(ValueError, match='不支持指定历史'):
        store.correct_snapshot(a['id'], payload)
    assert store.ledger(a['id']) == before


@pytest.mark.parametrize('buy_at,blocked_at,sell_at', [
    ('2026-10-30T10:00:00+08:00', '2026-10-31T10:00:00+08:00', '2026-11-02T10:00:00+08:00'),
    ('2026-09-30T10:00:00+08:00', '2026-10-07T10:00:00+08:00', '2026-10-08T10:00:00+08:00'),
])
def test_t1_weekend_and_holiday_then_reversal(boundary_api, frozen_clock, buy_at, blocked_at, sell_at):
    store, client, _ = boundary_api
    frozen_clock.set(buy_at)
    a = create(client)
    base = '/api/accounts/' + a['id']
    bought = client.post(base + '/trades', json=trade(client, a['id'], mode='historical', traded_at=buy_at))
    assert bought.status_code == 201, bought.text
    original = bought.json()['entry']
    assert original['after_positions']['SZ:002354']['lots'][0]['sellable_on'] == sell_at[:10]
    frozen_clock.set(blocked_at)
    assert client.get(base + '/ledger').json()['positions'][0]['available_quantity'] == '0'
    blocked = client.post(base + '/trades', json=trade(client, a['id'],
        operation_id='boundary-blocked-sell', side='sell', traded_at=blocked_at))
    assert blocked.status_code == 422 and '可卖数量不足' in blocked.text
    frozen_clock.set(sell_at)
    sold_payload = trade(client, a['id'], operation_id='boundary-eligible-sell',
                         side='sell', mode='historical', traded_at=sell_at)
    sold = client.post(base + '/trades', json=sold_payload)
    assert sold.status_code == 201, sold.text
    assert client.post(base + '/trades', json=sold_payload).json()['replayed'] is True
    frozen_clock.advance(minutes=1)
    reversed_ = client.post(base + '/ledger/' + sold.json()['entry_id'] + '/reverse', json={
        'operation_id': 'boundary-sale-reversal', 'expected_version': 3, 'reason': '受控撤销'})
    assert reversed_.status_code == 201, reversed_.text
    assert reversed_.json()['cash_after'] == bought.json()['cash_after']
    ledger = client.get(base + '/ledger').json()
    assert ledger['positions'][0]['available_quantity'] == '1000'
    assert ledger['summary']['total_fees'] == bought.json()['fees']['total']
    assert store.ledger_operation(a['id'], 'boundary-trade-001')['entry'] == original


def test_t0_future_input_and_calendar_gap_are_independent(boundary_api, frozen_clock):
    store, client, calendar = boundary_api
    frozen_clock.set('2026-10-30T02:00:00+00:00')
    a = create(client)
    base = '/api/accounts/' + a['id']
    future = client.post(base + '/trades', json=trade(client, a['id'],
        traded_at='2026-10-30T10:01:01+08:00'))
    assert future.status_code == 422 and '未来成交' in future.text
    payload = trade(client, a['id'], symbol='518880', quantity='100')
    bought = client.post(base + '/trades', json=payload)
    assert bought.status_code == 201, bought.text
    assert client.get(base + '/ledger').json()['positions'][0]['available_quantity'] == '100'
    sold = client.post(base + '/trades', json=trade(client, a['id'],
        operation_id='boundary-t0-sale', symbol='518880', side='sell', quantity='100'))
    assert sold.status_code == 201, sold.text
    # A truncated fixture never silently guesses the next weekday.
    data = json.loads(calendar.read_text())
    data['coverage']['end'] = '2026-10-30'
    calendar.write_text(json.dumps(data))
    before = store.ledger(a['id'])
    missing = client.post(base + '/trades', json=trade(client, a['id'], operation_id='boundary-calendar-gap'))
    assert missing.status_code == 422 and '下一交易日历' in missing.text
    assert store.ledger(a['id']) == before


def test_shared_clock_drives_domain_calendar_queue_and_all_direct_readers(boundary_api, frozen_clock):
    store, client, _ = boundary_api
    frozen_clock.set('2026-10-30T16:00:00+00:00')
    assert domain.now() == '2026-10-31T00:00:00+08:00'
    assert clock.now_datetime(timezone.utc).isoformat() == '2026-10-30T16:00:00+00:00'
    assert market_state()['date'] == '2026-10-31'
    a = create(client)
    assert a['created_at'] == domain.now()
    store.set_meta('settings', {'debounce_seconds': 30})
    job = store.enqueue(a['id'], 'manual', delay=30)
    assert store.job(job['id'])['scheduled_at'] == '2026-10-31T00:00:30+08:00'
    # Architectural guard: new wall-clock readers must not bypass the fixture.
    root = Path(inspect.getfile(clock)).parent
    for source in root.glob('*.py'):
        if source.name != 'clock.py':
            text = source.read_text()
            assert 'datetime.now(' not in text, source.name
            assert 'date.today(' not in text, source.name
            assert 'time.time(' not in text, source.name
