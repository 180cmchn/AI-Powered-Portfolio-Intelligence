"""Real HTTP -> resolver -> ledger -> SQLite correction regression.

No provider calls, model output, user database, or copied accounting functions.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.domain import metrics
from app.store import Store


@pytest.fixture
def api(tmp_path, frozen_clock, monkeypatch):
    frozen_clock.set('2026-11-02T12:00:00+08:00')
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps({'coverage': {'start': '2026-10-28', 'end': '2026-11-06'},
        'trading_dates': ['2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02',
                          '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06'],
        'source': 'thirteenth controlled calendar'}))
    monkeypatch.setenv('STOCK_CALENDAR_PATH', str(calendar))
    app = create_app(tmp_path / 'correction.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    for sid, name, kind in [('SZ:002354', '受控股票', 'stock'), ('SH:518880', '受控黄金ETF', 'etf'),
                            ('SZ:159599', '受控芯片ETF', 'etf')]:
        store.put_cache('securities', sid, {'symbol': sid, 'name': name, 'kind': kind,
            'status': 'ok', 'identity_status': 'confirmed', 'source': 'controlled directory'})
    with TestClient(app) as client:
        yield store, client


def create(client, holdings=None, cash='100000'):
    response = client.post('/api/accounts', json={'name': '第十三轮模拟算术账户',
        'cash': cash, 'holdings': holdings or []})
    assert response.status_code == 201, response.text
    return '/api/accounts/' + response.json()['id']


def get(client, base):
    return client.get(base).json()


def trade(client, base, operation, **extra):
    payload = {'operation_id': operation, 'expected_version': get(client, base)['version'],
        'fee_plan_version': 1, 'symbol': '002354', 'side': 'buy', 'quantity': '100', 'price': '10',
        'traded_at': '2026-10-29T10:00:00+08:00', 'mode': 'simulated', **extra}
    preview = client.post(base + '/trades/preview', json=payload)
    assert preview.status_code == 200, preview.text
    response = client.post(base + '/trades', json=payload)
    assert response.status_code == 201, response.text
    assert response.json()['entry']['total_cost_after'] == preview.json()['total_cost_after']
    return response.json()


def correction(client, base, operation, **extra):
    payload = {'operation_id': operation, 'expected_version': get(client, base)['version'],
        'reason': '受控对账依据；非真实交易或行情', **extra}
    preview = client.post(base + '/corrections/preview', json=payload)
    assert preview.status_code == 200, preview.text
    payload['preview_token'] = preview.json()['preview_token']
    response = client.post(base + '/corrections', json=payload)
    assert response.status_code == 201, response.text
    return response.json(), preview.json(), payload


def raw_positions(store, base):
    with store.connect() as db:
        return {row['symbol']: row['body'] for row in db.execute(
            'SELECT symbol,body FROM ledger_positions WHERE account_id=?', (base.rsplit('/', 1)[1],))}


def ledger(client, base):
    return client.get(base + '/ledger').json()


def test_cash_only_roundtrip_preserves_exact_trade_lots_and_fees(api):
    store, client = api
    base = create(client)
    bought = trade(client, base, 'cash-only-buy', quantity='1000')
    assert bought['entry']['total_cost_after'] == '10005.10'
    before, rows = get(client, base), raw_positions(store, base)
    receipt, preview, payload = correction(client, base, 'cash-only-fix', cash='90094.90', holdings=before['holdings'])
    assert raw_positions(store, base) == rows
    assert preview['changes'] == [] and preview['creates_trade'] is False
    pos = ledger(client, base)['positions'][0]
    assert pos['lots'] == [{'quantity': '1000', 'sellable_on': '2026-10-30', 'source': 'trade', 'traded_at': '2026-10-29T10:00:00+08:00'}]
    assert pos['total_cost'] == '10005.10' and pos['fees_included_in_cost'] is True
    assert pos['available_quantity'] == '1000' and pos['cost_basis'] == 'trade_cost'
    assert receipt['cash_after'] == '90094.90' and receipt['entry']['cash_effect'] == '100.00'
    assert ledger(client, base)['summary']['total_fees'] == '5.10'
    assert ledger(client, base)['summary']['realized_pnl'] == '0.00'
    assert client.post(base + '/corrections', json=payload).json()['replayed'] is True
    assert get(client, base)['version'] == 3
    assert len(store.demands(base.rsplit('/', 1)[1])) == 2
    assert len({d['job_id'] for d in store.demands(base.rsplit('/', 1)[1])}) == 1
    assert Store(store.path).account(base.rsplit('/', 1)[1])['ledger']['positions'] == before['ledger']['positions']


def test_multibuy_repeating_cost_cash_and_unrelated_correction(api):
    store, client = api
    base = create(client)
    trade(client, base, 'repeating-buy-100')
    trade(client, base, 'repeating-buy-200', quantity='200')
    a = get(client, base)
    with localcontext() as context:
        context.prec = 50
        assert a['holdings'][0]['cost_price'] == format(Decimal('3010.03') / 300, 'f')
    assert len(a['holdings'][0]['cost_price'].split('.')[1]) > 6
    original = raw_positions(store, base)['SZ:002354']
    correction(client, base, 'repeating-cash-fix', cash='97089.97', holdings=a['holdings'])
    assert raw_positions(store, base)['SZ:002354'] == original
    correction(client, base, 'unrelated-security-fix', holdings=[{'symbol': '159599', 'quantity': '100', 'cost_price': '2.123456'}])
    assert raw_positions(store, base)['SZ:002354'] == original
    assert get(client, base)['cash'] == '97089.97'
    assert ledger(client, base)['summary']['total_fees'] == '10.03'
    assert ledger(client, base)['summary']['realized_pnl'] == '0.00'
    assert json.loads(raw_positions(store, base)['SZ:159599'])['total_cost'] == '212.345600'


def test_partial_sale_repeating_total_survives_cash_and_unrelated_correction(api):
    store, client = api
    base = create(client)
    trade(client, base, 'partial-buy-100')
    trade(client, base, 'partial-buy-200', quantity='200')
    sold = trade(client, base, 'partial-sell-100', side='sell', quantity='100', price='11', traded_at='2026-10-30T10:00:00+08:00')
    with localcontext() as context:
        context.prec = 50
        relieved = Decimal('3010.03') * 100 / 300
        remaining = Decimal('3010.03') - relieved
        realized = Decimal('1100') - Decimal('5.56') - relieved
    pos = get(client, base)['ledger']['positions']['SZ:002354']
    assert Decimal(pos['total_cost']) == remaining
    assert sold['entry']['realized_pnl_effect'] == format(realized, 'f')
    raw = raw_positions(store, base)['SZ:002354']
    correction(client, base, 'partial-cash-fix', cash='98000', holdings=get(client, base)['holdings'])
    correction(client, base, 'partial-other-fix', holdings=[{'symbol': '518880', 'quantity': '100'}])
    assert raw_positions(store, base)['SZ:002354'] == raw
    assert ledger(client, base)['summary']['total_fees'] == '15.59'
    assert ledger(client, base)['summary']['known_realized_pnl'] == '91.10'
    assert get(client, base)['cash'] == '98000.00'
    final = trade(client, base, 'partial-clear-last', side='sell', quantity='200', price='11', traded_at='2026-11-02T10:00:00+08:00')
    assert final['entry']['cost_relieved'] == format(remaining, 'f')
    assert final['entry']['total_cost_after'] == '0'
    assert get(client, base)['cash'] == '100193.88'
    assert ledger(client, base)['summary']['realized_pnl'] == '278.29'
    assert ledger(client, base)['summary']['total_fees'] == '21.71'


def test_last_fill_fee_adjustment_and_reversal_keep_repeating_cost(api):
    store, client = api
    base = create(client)
    trade(client, base, 'fee-repeat-buy-100')
    receipt = trade(client, base, 'fee-repeat-buy-200', quantity='200')
    before, rows = get(client, base), raw_positions(store, base)
    fixed = client.post(base + '/ledger/' + receipt['entry_id'] + '/fees', json={
        'operation_id': 'fee-repeat-adjust', 'expected_version': before['version'],
        'reason': '受控费用核对', 'fees': {'commission': '5.01'}})
    assert fixed.status_code == 201, fixed.text
    fixed = fixed.json()
    assert fixed['cash_after'] == '96989.96'
    assert get(client, base)['ledger']['positions']['SZ:002354']['total_cost'] == '3010.04'
    assert ledger(client, base)['summary']['total_fees'] == '10.04'
    reverse = client.post(base + '/ledger/' + fixed['entry_id'] + '/reverse', json={
        'operation_id': 'fee-repeat-reverse', 'expected_version': 4, 'reason': '受控撤销费用校正'})
    assert reverse.status_code == 201, reverse.text
    assert raw_positions(store, base) == rows
    assert get(client, base)['cash'] == before['cash']
    assert ledger(client, base)['summary']['total_fees'] == '10.03'
    correction(client, base, 'fee-after-reverse-cash', cash='97000', holdings=get(client, base)['holdings'])
    assert raw_positions(store, base) == rows


def test_missing_unknown_zero_clear_and_cost_only_keep_availability(api):
    store, client = api
    base = create(client)
    trade(client, base, 'scope-buy-A', quantity='1000')
    trade(client, base, 'scope-buy-B', symbol='159599', price='2')
    before = raw_positions(store, base)
    correction(client, base, 'scope-cost-only', holdings=[{'symbol': '002354', 'cost_price': '0'}])
    a = json.loads(raw_positions(store, base)['SZ:002354'])
    assert a['total_cost'] == '0' and a['lots'] == json.loads(before['SZ:002354'])['lots']
    assert raw_positions(store, base)['SZ:159599'] == before['SZ:159599']
    correction(client, base, 'scope-cost-unknown', holdings=[{'symbol': '002354', 'cost_price': None}])
    a = json.loads(raw_positions(store, base)['SZ:002354'])
    assert a['total_cost'] is None and a['lots'] == json.loads(before['SZ:002354'])['lots']
    correction(client, base, 'scope-available-unknown', holdings=[{'symbol': '002354', 'available_quantity': None}])
    assert ledger(client, base)['positions'][0]['available_quantity'] == '0'
    correction(client, base, 'scope-new-known-today', holdings=[{'symbol': '002354', 'available_quantity': '500'}])
    a = json.loads(raw_positions(store, base)['SZ:002354'])
    assert a['lots'] == [{'quantity': '500', 'sellable_on': '2026-11-02', 'source': 'explicit_correction'},
                        {'quantity': '500', 'sellable_on': None, 'source': 'explicit_correction'}]
    assert a['total_cost'] is None
    correction(client, base, 'scope-clear-A', holdings=[{'symbol': '002354', 'quantity': '0'}])
    a = json.loads(raw_positions(store, base)['SZ:002354'])
    assert a['quantity'] == '0' and a['total_cost'] == '0' and a['lots'] == []
    assert raw_positions(store, base)['SZ:159599'] == before['SZ:159599']
    assert get(client, base)['cash'] == '89789.90'
    assert ledger(client, base)['summary']['realized_pnl'] == '0.00'
    assert ledger(client, base)['summary']['total_fees'] == '10.10'


def test_quantity_rules_preserve_exact_cost_and_disclose_changed_lots(api):
    store, client = api
    base = create(client)
    trade(client, base, 'qty-scope-buy', quantity='1000')
    old = get(client, base)['ledger']['positions']['SZ:002354']
    receipt, preview, _ = correction(client, base, 'qty-scope-increase', holdings=[{'symbol': '002354', 'quantity': '1100'}])
    pos = receipt['entry']['after_positions']['SZ:002354']
    assert pos['total_cost'] == '10005.10'
    assert pos['lots'][:1] == old['lots'] and pos['lots'][-1]['sellable_on'] is None
    assert '原有批次保持' in preview['changes'][0]['notes'][0]
    receipt, preview, _ = correction(client, base, 'qty-scope-decrease', holdings=[{'symbol': '002354', 'quantity': '900'}])
    assert receipt['entry']['after_positions']['SZ:002354']['total_cost'] == '10005.10'
    assert receipt['entry']['after_positions']['SZ:002354']['lots'] == [{'quantity': '900', 'sellable_on': None, 'source': 'explicit_correction'}]
    assert '重新明确' in preview['changes'][0]['notes'][0]


def test_zero_available_confirmation_unknown_and_closed_position_reopening(api):
    store, client = api
    base = create(client, [{'symbol': '002354', 'quantity': '100'}])
    correction(client, base, 'confirm-none-available', holdings=[{'symbol': '002354', 'available_quantity': '0'}])
    pos = get(client, base)['ledger']['positions']['SZ:002354']
    assert pos['total_cost'] is None
    assert pos['availability_confirmation'] == {'quantity': '0', 'effective_on': '2026-11-02'}
    correction(client, base, 'clear-available-basis', holdings=[{'symbol': '002354', 'available_quantity': None}])
    pos = get(client, base)['ledger']['positions']['SZ:002354']
    assert 'availability_confirmation' not in pos and pos['lots'][0]['sellable_on'] is None
    correction(client, base, 'close-unknown-position', holdings=[{'symbol': '002354', 'quantity': '0'}])
    correction(client, base, 'reopen-without-cost', holdings=[{'symbol': '002354', 'quantity': '100'}])
    pos = get(client, base)['ledger']['positions']['SZ:002354']
    assert pos['total_cost'] is None and pos['lots'][0]['sellable_on'] is None


def test_same_numeric_format_does_not_rebuild_or_queue_noop(api):
    store, client = api
    base = create(client, [{'symbol': '002354', 'quantity': '1000', 'cost_price': '10.0'}])
    correction(client, base, 'same-format-available', holdings=[{'symbol': '002354', 'available_quantity': '1000'}])
    before, rows, entries = get(client, base), raw_positions(store, base), ledger(client, base)['entries']
    payload = {'operation_id': 'same-format-noop', 'expected_version': before['version'], 'reason': '同值不是新事实',
        'cash': '100000.00', 'holdings': [{'symbol': '002354', 'quantity': '1000.00', 'cost_price': '10.000000', 'available_quantity': '1000.0'}]}
    response = client.post(base + '/corrections', json=payload)
    assert response.status_code == 422 and '没有账本事实变化' in response.text
    assert raw_positions(store, base) == rows
    assert get(client, base)['version'] == before['version']
    assert ledger(client, base)['entries'] == entries
    correction(client, base, 'same-format-cash', cash='100001', holdings=payload['holdings'])
    assert raw_positions(store, base) == rows


@pytest.mark.parametrize('fields', [
    {'cost_price': '10.1234567'}, {'cost_price': 'NaN'}, {'cost_price': 'Infinity'},
    {'cost_price': '-1'}, {'cost_price': '1' * 81}, {'cost_price': '1e-999999'},
    {'total_cost': '1.1234567'}, {'cost_price': '10', 'total_cost': '1000'},
    {'available_quantity': '1001'}, {'quantity': None}, {'quantity': '-1'},
])
def test_new_user_cost_is_bounded_and_invalid_patch_is_atomic(api, fields):
    store, client = api
    base = create(client)
    trade(client, base, 'bounded-input-buy', quantity='1000')
    before, rows = get(client, base), raw_positions(store, base)
    response = client.post(base + '/corrections', json={'operation_id': 'bounded-invalid',
        'expected_version': before['version'], 'reason': '无效输入不得保存', 'cash': '91000',
        'holdings': [{'symbol': '002354', **fields}]})
    assert response.status_code == 422, response.text
    assert raw_positions(store, base) == rows
    assert get(client, base)['version'] == before['version'] and get(client, base)['cash'] == before['cash']
    assert client.get(base + '/ledger-operations/bounded-invalid').status_code == 404


def test_preview_input_version_date_and_operation_isolation(api, frozen_clock):
    store, client = api
    a, b = create(client), create(client)
    trade(client, a, 'preview-protection-buy')
    payload = {'operation_id': 'preview-protection-fix', 'expected_version': 2, 'reason': '受控预览', 'cash': '99000'}
    original = ledger(client, a)
    preview = client.post(a + '/corrections/preview', json=payload)
    assert preview.status_code == 200
    assert ledger(client, a) == original
    payload['preview_token'] = preview.json()['preview_token']
    assert client.post(a + '/corrections', json={**payload, 'cash': '99001'}).status_code == 409
    assert client.post(b + '/corrections', json={**payload, 'expected_version': 1}).status_code == 409
    frozen_clock.advance(days=1)
    assert client.post(a + '/corrections', json=payload).status_code == 409
    frozen_clock.advance(days=-1)
    saved = client.post(a + '/corrections', json=payload)
    assert saved.status_code == 201
    assert client.post(a + '/corrections', json=payload).json()['replayed'] is True
    assert client.post(a + '/corrections', json={**payload, 'cash': '99001'}).status_code == 409
    assert client.post(a + '/corrections', json={**payload, 'operation_id': 'preview-stale-new-id'}).status_code == 409
    assert client.get(b + '/ledger-operations/preview-protection-fix').status_code == 404
    assert get(client, b)['version'] == 1


def test_patch_queue_failure_rolls_back_all_facts(api, monkeypatch):
    store, client = api
    base = create(client)
    trade(client, base, 'rollback-production-buy')
    before, rows = get(client, base), raw_positions(store, base)
    entries, demands = ledger(client, base)['entries'], store.demands(base.rsplit('/', 1)[1])
    def fail(*args, **kwargs):
        raise RuntimeError('controlled failure before atomic queue commit')
    monkeypatch.setattr(store, '_enqueue', fail)
    with pytest.raises(RuntimeError, match='controlled failure'):
        client.post(base + '/corrections', json={'operation_id': 'rollback-production-fix',
            'expected_version': before['version'], 'reason': '故障回滚', 'cash': '99999',
            'holdings': [{'symbol': '002354', 'total_cost': '0'}]})
    assert raw_positions(store, base) == rows
    assert get(client, base)['cash'] == before['cash'] and get(client, base)['version'] == before['version']
    assert ledger(client, base)['entries'] == entries and store.demands(base.rsplit('/', 1)[1]) == demands
    assert client.get(base + '/ledger-operations/rollback-production-fix').status_code == 404


def test_legacy_put_repeating_cost_roundtrip_and_explicit_full_snapshot(api):
    store, client = api
    base = create(client)
    trade(client, base, 'legacy-repeat-buy-100')
    trade(client, base, 'legacy-repeat-buy-200', quantity='200')
    before, rows = get(client, base), raw_positions(store, base)
    payload = {k: before[k] for k in ('name', 'cash', 'holdings', 'preferences')}
    payload.update(cash='97000', expected_version=before['version'], ledger_action='correction', adjustment_reason='兼容旧全量快照有依据校正')
    response = client.put(base, json=payload, headers={'X-Operation-ID': 'legacy-repeat-put'})
    assert response.status_code == 200, response.text
    assert raw_positions(store, base) == rows
    assert get(client, base)['cash'] == '97000.00'
    assert client.put(base, json=payload, headers={'X-Operation-ID': 'legacy-repeat-put'}).json()['save_receipt']['replayed'] is True
    invalid = {**payload, 'expected_version': get(client, base)['version'],
        'holdings': [{'symbol': '002354', 'quantity': '300', 'cost_price': '10.1234567'}]}
    assert client.put(base, json=invalid).status_code == 422
    correction(client, base, 'explicit-snapshot-clear', mode='snapshot', holdings=[])
    assert get(client, base)['holdings'] == []
    assert get(client, base)['ledger']['positions']['SZ:002354']['quantity'] == '0'
    assert get(client, base)['cash'] == '97000.00'


def test_high_amount_unknown_cost_and_metrics_fee_scope(api):
    store, client = api
    base = create(client, [{'symbol': '159599', 'quantity': '100'}], cash='1000000000000')
    trade(client, base, 'large-value-stock-buy', quantity='1000')
    unknown = raw_positions(store, base)['SZ:159599']
    correction(client, base, 'large-total-cost-fix', holdings=[{'symbol': '002354', 'total_cost': '999999999999.123456'}])
    assert get(client, base)['ledger']['positions']['SZ:002354']['total_cost'] == '999999999999.123456'
    assert raw_positions(store, base)['SZ:159599'] == unknown
    before = raw_positions(store, base)
    correction(client, base, 'large-cash-only', cash='999999990000')
    assert raw_positions(store, base) == before
    account = get(client, base)
    result = metrics(account, {'SZ:159599': {'price': '2', 'status': 'ok'}, 'SZ:002354': {'price': '11', 'status': 'ok'}}, {})
    assert result['unrealized_pnl'] is None
    assert next(p for p in result['positions'] if p['symbol'] == 'SZ:159599')['total_cost'] is None
    assert all(p['cost_fee_scope'] == 'opening_or_correction_fee_scope_unknown' for p in result['positions'])
    assert account['ledger']['fees_included_in_cost'] is False
    assert '未来卖出费用' in result['notes'][-1] and '重复扣除' in result['notes'][-1]


def test_complete_snapshot_requires_explicit_preview_but_patch_preserves_omissions(api):
    store, client = api
    base = create(client)
    trade(client, base, 'snapshot-preview-buy')
    before, rows = get(client, base), raw_positions(store, base)
    payload = {'operation_id': 'snapshot-preview-clear', 'expected_version': before['version'],
        'reason': '明确重建完整期初快照', 'mode': 'snapshot', 'holdings': []}
    assert client.post(base + '/corrections/preview', json={k: v for k, v in payload.items() if k != 'holdings'}).status_code == 422
    response = client.post(base + '/corrections', json=payload)
    assert response.status_code == 422 and '先预览全部差异' in response.text
    assert raw_positions(store, base) == rows and get(client, base)['version'] == before['version']
    response = client.post(base + '/corrections/preview', json=payload)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['changes'][0]['before']['quantity'] == '100'
    assert preview['changes'][0]['after']['quantity'] == '0'
    assert client.post(base + '/corrections', json={**payload, 'preview_token': preview['preview_token']}).status_code == 201


def test_unrelated_correction_preserves_zero_rows_order_and_unit_text(api):
    store, client = api
    base = create(client, [{'symbol': '159599', 'quantity': '0', 'cost_price': '2.00'},
        {'symbol': '518880', 'quantity': '0', 'cost_price': None},
        {'symbol': '002354', 'quantity': '100', 'cost_price': '10.00'}])
    before, rows = get(client, base), raw_positions(store, base)
    correction(client, base, 'zero-row-cash-only', cash='100001', holdings=before['holdings'])
    assert get(client, base)['holdings'] == before['holdings']
    assert raw_positions(store, base) == rows
    correction(client, base, 'zero-row-availability', holdings=[{'symbol': '002354', 'available_quantity': '100'}])
    assert get(client, base)['holdings'] == before['holdings']
    assert raw_positions(store, base)['SZ:159599'] == rows['SZ:159599']


def test_legacy_missing_cost_preserves_repeating_facts_but_null_clears(api):
    store, client = api
    base = create(client)
    trade(client, base, 'legacy-missing-buy-100')
    trade(client, base, 'legacy-missing-buy-200', quantity='200')
    before, rows = get(client, base), raw_positions(store, base)
    payload = {'name': before['name'], 'cash': '97000', 'expected_version': before['version'],
        'ledger_action': 'correction', 'adjustment_reason': '仅改现金；未填写成本不是清除',
        'holdings': [{'symbol': '002354', 'quantity': '300'}]}
    response = client.put(base, json=payload, headers={'X-Operation-ID': 'legacy-missing-cost'})
    assert response.status_code == 200, response.text
    assert raw_positions(store, base) == rows
    assert get(client, base)['holdings'] == before['holdings']
    assert client.put(base, json=payload, headers={'X-Operation-ID': 'legacy-missing-cost'}).json()['save_receipt']['replayed']
    explicit = {**payload, 'expected_version': response.json()['version'],
                'holdings': [{'symbol': '002354', 'quantity': '300', 'cost_price': None}]}
    assert client.put(base, json=explicit, headers={'X-Operation-ID': 'legacy-missing-cost'}).status_code == 409
    response = client.put(base, json=explicit, headers={'X-Operation-ID': 'legacy-explicit-unknown'})
    assert response.status_code == 200, response.text
    pos = get(client, base)['ledger']['positions']['SZ:002354']
    assert pos['total_cost'] is None
    assert pos['lots'] == json.loads(rows['SZ:002354'])['lots']
    assert get(client, base)['cash'] == '97000.00'


@pytest.mark.parametrize('intent', ['cash_only', 'holdings_only', 'metadata_only', 'cash_unknown', 'clear_holdings'])
def test_legacy_top_level_omission_preserves_facts_and_explicit_values_apply(api, intent):
    store, client = api
    base = create(client)
    trade(client, base, 'legacy-omit-buy-100')
    before, rows = get(client, base), raw_positions(store, base)
    payload = {'name': '兼容省略字段', 'expected_version': before['version']}
    if intent != 'metadata_only':
        payload.update(ledger_action='correction', adjustment_reason='受控显式字段意图')
    if intent == 'cash_only':
        payload['cash'] = '99000'
    elif intent == 'holdings_only':
        payload['holdings'] = [{'symbol': '002354', 'quantity': '100', 'cost_price': '11'}]
    elif intent == 'cash_unknown':
        payload['cash'] = None
    elif intent == 'clear_holdings':
        payload['holdings'] = []
    response = client.put(base, json=payload, headers={'X-Operation-ID': 'legacy-omit-' + intent})
    assert response.status_code == 200, response.text
    after = get(client, base)
    if intent in ('cash_only', 'cash_unknown', 'metadata_only'):
        assert raw_positions(store, base) == rows
        assert after['holdings'] == before['holdings']
    if intent in ('holdings_only', 'clear_holdings', 'metadata_only'):
        assert after['cash'] == before['cash']
    if intent == 'cash_only':
        assert after['cash'] == '99000.00'
    elif intent == 'cash_unknown':
        assert after['cash'] is None
    elif intent == 'clear_holdings':
        assert after['holdings'] == []
        assert after['ledger']['positions']['SZ:002354']['total_cost'] == '0'
    elif intent == 'holdings_only':
        assert after['ledger']['positions']['SZ:002354']['total_cost'] == '1100'
        assert after['ledger']['positions']['SZ:002354']['lots'] == before['ledger']['positions']['SZ:002354']['lots']


@pytest.mark.parametrize('mode', ['patch', 'snapshot', 'legacy'])
def test_explicit_zero_row_removal_changes_scope_but_preserves_ledger_history(api, mode):
    store, client = api
    base = create(client, [{'symbol': '159599', 'quantity': '0', 'cost_price': '2.00'},
        {'symbol': '002354', 'quantity': '100', 'cost_price': '10.00'}])
    before, rows = get(client, base), raw_positions(store, base)
    old_entries = ledger(client, base)['entries']
    if mode == 'legacy':
        response = client.put(base, json={'name': before['name'], 'expected_version': before['version'],
            'ledger_action': 'correction', 'adjustment_reason': '明确完整列表移除零行',
            'holdings': [before['holdings'][1]]})
        assert response.status_code == 200, response.text
        changes = ledger(client, base)['entries'][0]['changes']
    else:
        rows_to_send = ([{'symbol': '159599', 'quantity': '0', 'remove': True}]
            if mode == 'patch' else [before['holdings'][1]])
        _, preview, _ = correction(client, base, 'zero-remove-' + mode, mode=mode, holdings=rows_to_send)
        changes = preview['changes']
    assert changes[0]['fields'] == ['holding_removed']
    after = get(client, base)
    assert after['holdings'] == [before['holdings'][1]]
    assert after['cash'] == before['cash']
    assert raw_positions(store, base) == rows
    assert ledger(client, base)['entries'][1:] == old_entries
    latest_job = store.job(after['latest_job_id'])
    assert latest_job['target']['symbols'] == ['SZ:002354']


@pytest.mark.parametrize('extra', [{'quantity': '100'}, {'cost_price': None}, {'total_cost': '0'}, {'available_quantity': '0'}])
def test_remove_rejects_conflicting_field_intent(api, extra):
    store, client = api
    base = create(client, [{'symbol': '159599', 'quantity': '0', 'cost_price': '2.00'}])
    before, rows = get(client, base), raw_positions(store, base)
    response = client.post(base + '/corrections', json={'operation_id': 'zero-remove-conflict',
        'expected_version': before['version'], 'reason': '矛盾意图不能保存',
        'holdings': [{'symbol': '159599', 'remove': True, **extra}]})
    assert response.status_code == 422 and '移除持仓不能同时' in response.text
    assert raw_positions(store, base) == rows and get(client, base)['holdings'] == before['holdings']


@pytest.mark.parametrize('reverse', [False, True])
def test_legacy_alias_duplicates_cannot_hide_missing_versus_null(api, reverse):
    store, client = api
    base = create(client)
    trade(client, base, 'legacy-alias-intent-buy')
    before, rows = get(client, base), raw_positions(store, base)
    holdings = [{'symbol': '002354', 'quantity': '100'},
                {'symbol': 'SZ:002354', 'quantity': '100', 'cost_price': None}]
    response = client.put(base, json={'name': before['name'], 'cash': '99000',
        'expected_version': before['version'], 'ledger_action': 'correction',
        'adjustment_reason': '不允许重复行隐去不同字段意图', 'holdings': holdings[::-1] if reverse else holdings})
    assert response.status_code == 422 and '成本缺省与显式填写意图不同' in response.text
    assert raw_positions(store, base) == rows and get(client, base)['cash'] == before['cash']
