"""Production HTTP/Store/worker boundaries; all prices and model output are fixtures."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.domain import metrics
from app.main import create_app
from app.store import Store


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'ledger-http.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    for sid, name, kind in [('SH:518880', '华安黄金ETF', 'etf'),
            ('SZ:002354', '天娱数科', 'stock'), ('SZ:159599', '东财中证芯片产业ETF', 'etf')]:
        store.put_cache('securities', sid, {'symbol': sid, 'name': name, 'kind': kind,
            'status': 'ok', 'identity_status': 'confirmed', 'source': 'controlled directory fixture'})
    with TestClient(app) as client:
        yield app, client


def account(client, name='三证券模拟'):
    response = client.post('/api/accounts', json={'name': name, 'cash': '100000', 'holdings': []})
    assert response.status_code == 201, response.text
    return response.json()


def buy(client, a, code, quantity, price, operation):
    current = client.get('/api/accounts/' + a['id']).json()
    data = {'operation_id': operation, 'expected_version': current['version'], 'fee_plan_version': 1,
        'symbol': code, 'side': 'buy', 'quantity': quantity, 'price': price,
        'traded_at': '2026-10-30T10:00:00+08:00', 'mode': 'simulated', 'note': '算术夹具，非用户成交'}
    response = client.post(f"/api/accounts/{a['id']}/trades", json=data)
    assert response.status_code == 201, response.text
    return data, response.json()


@pytest.mark.asyncio
async def test_three_security_http_atomic_research_and_model_failure(api):
    app, client = api
    a, b = account(client), account(client, '隔离B')
    before_b = client.get('/api/accounts/' + b['id']).json()
    buy(client, a, '518880', '100', '10.000', 'http-gold-1')
    buy(client, a, '002354', '1000', '10.00', 'http-stock-2')
    payload, receipt = buy(client, a, '159599', '1000', '2.500', 'http-etf-3')
    assert receipt['cash_after'] == '86484.90'
    assert client.post(f"/api/accounts/{a['id']}/trades", json=payload).json()['replayed']
    assert client.post(f"/api/accounts/{a['id']}/trades", json={**payload, 'price': '2.6'}).status_code == 409
    assert client.get(f"/api/accounts/{b['id']}/ledger-operations/http-etf-3").status_code == 404
    assert Store(app.state.store.path).ledger_operation(a['id'], 'http-etf-3')['trade_id'] == receipt['trade_id']
    store = app.state.store
    job = store.claim_jobs(account_id=a['id'])[0]
    captured = []
    async def failed(context, *args, **kwargs):
        captured.append(deepcopy(context))
        raise RuntimeError('controlled model failure after committed trade')
    app.state.service.runner = failed
    await app.state.service.execute(job)
    assert store.job(job['id'])['state'] == 'failed'
    snapshot = captured[0]['account']
    assert snapshot['cash'] == '86484.90' and snapshot['version'] == 4
    assert {h['symbol'] for h in snapshot['holdings']} == {'SH:518880', 'SZ:002354', 'SZ:159599'}
    assert snapshot['ledger']['total_fees'] == '15.10'
    assert store.account(a['id'])['cash'] == '86484.90'
    assert client.get('/api/accounts/' + b['id']).json()['holdings'] == before_b['holdings']
    assert client.get('/api/accounts/' + b['id']).json()['cash'] == before_b['cash']
    # Account version protects publication even if an older worker returns late.
    payload2 = {**payload, 'operation_id': 'http-gold-sell-4', 'symbol': '518880',
        'expected_version': 4, 'side': 'sell', 'quantity': '100', 'price': '10.500',
        'traded_at': '2026-10-30T11:00:00+08:00'}
    sold = client.post(f"/api/accounts/{a['id']}/trades", json=payload2)
    assert sold.status_code == 201, sold.text
    assert sold.json()['cash_after'] == '87529.90'
    assert sold.json()['entry']['realized_pnl'] == '40.00'
    store.publish(job['id'], {'report': 'late controlled result'}, None)
    assert not store.previous_analysis(a['id'])
    assert store.account(a['id'])['version'] == 5


def test_metadata_cas_receipt_and_legacy_put_cannot_bypass_ledger(api):
    app, client = api
    a = account(client)
    path = '/api/accounts/' + a['id']
    data = {'name': '新名称', 'preferences': a['preferences'], 'expected_version': 1}
    first = client.patch(path, json=data, headers={'X-Operation-ID': 'metadata-receipt-1'})
    assert first.status_code == 200, first.text
    assert first.json()['version'] == 2
    replay = client.patch(path, json=data, headers={'X-Operation-ID': 'metadata-receipt-1'})
    assert replay.json()['save_receipt']['replayed'] and replay.json()['version'] == 2
    assert client.patch(path, json={**data, 'name': 'different'}, headers={'X-Operation-ID': 'metadata-receipt-1'}).status_code == 409
    assert client.put(path, json={'name': '新名称', 'cash': '90000', 'holdings': [], 'expected_version': 2}).status_code == 422
    assert client.get(path).json()['cash'] == '100000'
    role = client.get('/api/agent-roles').json()[0]
    assert client.put('/api/agent-roles/' + role['role_id'], json={'name': '更新角色', 'expected_version': role['version']}).status_code == 200
    assert client.put('/api/agent-roles/' + role['role_id'], json={'name': '过期角色', 'expected_version': role['version']}).status_code == 409


def test_metrics_use_exact_total_cost_and_no_double_fee_subtraction():
    a = {'cash': '10', 'holdings': [{'symbol': 'SZ:002354', 'quantity': '3', 'cost_price': '3.333333'}],
        'ledger': {'positions': {'SZ:002354': {'total_cost': '10', 'fees_included_in_cost': True}}, 'total_fees': '5', 'realized_pnl': '0'}}
    result = metrics(a, {'SZ:002354': {'price': '4', 'status': 'ok'}}, {})
    assert result['total_assets'] == '22.00' and result['unrealized_pnl'] == '2.00'
    assert result['positions'][0]['total_cost'] == '10'


def test_fee_correction_and_available_baseline_http(api):
    app, client = api
    a = account(client)
    _, receipt = buy(client, a, '002354', '1000', '10.00', 'fee-http-buy-1')
    path = f"/api/accounts/{a['id']}/ledger/{receipt['trade_id']}/fees"
    data = {'operation_id': 'fee-http-fix-1', 'expected_version': 2,
        'fees': {'commission': '2.50', 'transfer': '0.10', 'stamp': '0.00'}, 'reason': '受控交割单费用夹具'}
    fixed = client.post(path, json=data)
    assert fixed.status_code == 201, fixed.text
    assert fixed.json()['cash_after'] == '89997.40'
    assert client.post(path, json=data).json()['replayed']
    assert app.state.store.account(a['id'])['ledger']['positions']['SZ:002354']['total_cost'] == '10002.60'
    current = client.get('/api/accounts/' + a['id']).json()
    corrected = client.post(f"/api/accounts/{a['id']}/corrections", json={'operation_id': 'available-http-fix',
        'expected_version': current['version'], 'cash': current['cash'], 'reason': '期初可卖依据夹具',
        'holdings': [{'symbol': '002354', 'quantity': '1000', 'cost_price': None, 'available_quantity': '500'}]})
    assert corrected.status_code == 201, corrected.text
    pos = client.get(f"/api/accounts/{a['id']}/ledger").json()['positions'][0]
    assert pos['available_quantity'] == '500' and pos['availability_unknown_quantity'] == '500'
    assert pos['total_cost'] is None
