from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import sqlite3

import pytest

from app.domain import AccountInput
from app.store import Store, Conflict


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'ledger.sqlite3', mode='demo')
    result.set_meta('settings', {'debounce_seconds': 0})
    for sid, name, kind in [('SZ:002354', '天娱数科', 'stock'), ('SZ:159599', '东财中证芯片产业ETF', 'etf'),
                            ('SH:518880', '华安黄金ETF', 'etf'), ('SH:600519', '贵州茅台', 'stock')]:
        result.put_cache('securities', sid, {'symbol': sid, 'name': name, 'kind': kind, 'status': 'ok', 'identity_status': 'confirmed'})
    return result


def account(store, **kwargs):
    return store.save_account(AccountInput(name='独立模拟', cash='100000', **kwargs))


def trade(store, a, **kwargs):
    payload = {'operation_id': 'operation-buy-1', 'expected_version': store.account(a['id'])['version'],
        'fee_plan_version': store.fee_plan(a['id'])['version'], 'symbol': 'SZ:002354', 'side': 'buy',
        'quantity': '1000', 'price': '10', 'traded_at': '2026-10-26T10:00:00+08:00', 'mode': 'simulated'}
    payload.update(kwargs)
    return payload


def test_cash_stock_sample_and_exact_cost(store):
    a = account(store)
    buy = store.record_trade(a['id'], trade(store, a))
    assert buy['cash_after'] == '89994.90'
    assert buy['entry']['total_cost_after'] == '10005.10'
    sell = store.record_trade(a['id'], trade(store, a, operation_id='operation-sell-2', side='sell', quantity='400', price='11', traded_at='2026-10-27T10:00:00+08:00'))
    assert sell['cash_after'] == '94387.66'
    assert sell['entry']['cost_relieved'] == '4002.04'
    assert sell['entry']['realized_pnl'] == '390.72'
    assert sell['entry']['total_cost_after'] == '6003.06'
    ledger = store.ledger(a['id'])
    assert ledger['positions'][0]['quantity'] == '600'
    assert ledger['summary']['total_fees'] == '12.34'
    assert ledger['summary']['realized_pnl'] == '390.72'
    snap = store.snapshot(a['id'], sell['input_version'])
    assert snap['cash'] == '94387.66'
    assert snap['ledger']['positions']['SZ:002354']['total_cost'] == '6003.06'
    assert len(store.demands(a['id'])) == 2
    assert {d['job_id'] for d in store.demands(a['id'])} == {sell['research']['job_id']}
    assert store.job(buy['research']['job_id'])['state'] == 'superseded'


def test_etf_fee_and_gold_t0_equity_t1(store):
    a = account(store)
    p = trade(store, a, symbol='SZ:159599', price='2.500')
    result = store.record_trade(a['id'], p)
    assert result['cash_after'] == '97495.00'
    assert result['entry']['total_cost_after'] == '2505.00'
    assert result['fees']['components'] == {'commission': '5.00', 'transfer': '0.00', 'stamp': '0.00'}
    with pytest.raises(ValueError, match='可卖数量不足'):
        store.record_trade(a['id'], trade(store, a, operation_id='operation-sell-2', symbol='SZ:159599', side='sell', price='2.6'))
    gold = account(store)
    store.record_trade(gold['id'], trade(store, gold, symbol='SH:518880', price='5.000'))
    result = store.record_trade(gold['id'], trade(store, gold, operation_id='operation-sell-2', symbol='SH:518880', side='sell', price='5.100'))
    assert result['quantity_after'] == '0'
    assert result['entry']['total_cost_after'] == '0'
    assert result['entry']['realized_pnl'] == '90.00'


def test_fee_versions_inclusions_and_explicit_minimum(store):
    a = account(store)
    plan = store.fee_plan(a['id'])
    plan['expected_fee_plan_version'] = plan['version']
    plan['stock']['transfer_included'] = True
    plan['etf']['minimum_commission'] = '0'
    saved = store.save_fee_plan(a['id'], plan)
    assert saved['version'] == 2
    result = store.record_trade(a['id'], trade(store, a))
    assert result['cash_after'] == '89995.00'
    original_fee = deepcopy(result['fees'])
    plan = store.fee_plan(a['id'])
    plan['expected_fee_plan_version'] = plan['version']
    plan['stock']['minimum_commission'] = '20'
    store.save_fee_plan(a['id'], plan)
    assert store.ledger_operation(a['id'], 'operation-buy-1')['fees'] == original_fee
    b = account(store)
    plan = store.fee_plan(b['id'])
    plan['etf']['minimum_commission'] = '0'
    plan['expected_fee_plan_version'] = 1
    store.save_fee_plan(b['id'], plan)
    etf = store.record_trade(b['id'], trade(store, b, symbol='SZ:159599', price='2.500'))
    assert etf['fees']['total'] == '0.75'


def test_idempotency_conflict_cash_and_atomic_rollback(store, monkeypatch):
    a = account(store)
    p = trade(store, a)
    result = store.record_trade(a['id'], p)
    assert store.record_trade(a['id'], p)['entry_id'] == result['entry_id']
    assert len(store.ledger(a['id'])['entries']) == 2
    with pytest.raises(Conflict, match='不同记账输入'):
        store.record_trade(a['id'], {**p, 'price': '10.01'})
    before = store.account(a['id'])
    def fail(*args, **kwargs):
        raise RuntimeError('simulated process failure before queue')
    monkeypatch.setattr(store, '_enqueue', fail)
    with pytest.raises(RuntimeError):
        store.record_trade(a['id'], trade(store, a, operation_id='operation-buy-2'))
    assert store.account(a['id']) == before
    assert len(store.ledger(a['id'])['entries']) == 2
    with pytest.raises(KeyError):
        store.ledger_operation(a['id'], 'operation-buy-2')


def test_concurrent_buys_cas(store):
    a = account(store)
    p = trade(store, a, quantity='6000')
    def run(op):
        try:
            return store.record_trade(a['id'], {**p, 'operation_id': op})
        except (Conflict, ValueError) as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['parallel-buy-1', 'parallel-buy-2']))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert Decimal(store.account(a['id'])['cash']) >= 0
    assert len(store.ledger(a['id'])['entries']) == 2


@pytest.mark.parametrize('field,value', [('quantity', '-100'), ('quantity', '1.5'), ('quantity', 'NaN'), ('quantity', '99'), ('price', 'Infinity'), ('price', '.001'), ('price', '0'), ('quantity', '999999999999')])
def test_invalid_trades_leave_no_entry(store, field, value):
    a = account(store)
    with pytest.raises(ValueError):
        store.record_trade(a['id'], trade(store, a, **{field: value}))
    assert len(store.ledger(a['id'])['entries']) == 1
    assert store.account(a['id'])['version'] == 1


def test_unknown_cost_and_availability_require_explicit_correction(store):
    a = account(store, holdings=[{'symbol': 'SZ:002354', 'quantity': '1000'}])
    assert store.account(a['id'])['ledger']['positions']['SZ:002354']['total_cost'] is None
    with pytest.raises(ValueError, match='可卖数量不足'):
        store.record_trade(a['id'], trade(store, a, side='sell'))
    corrected = store.correct_snapshot(a['id'], {'operation_id': 'correct-opening-1', 'expected_version': 1, 'reason': '模拟期初交割单；可卖数量已确认', 'holdings': [{'symbol': 'SZ:002354', 'quantity': '1000', 'cost_price': None, 'available_quantity': '1000'}]})
    result = store.record_trade(a['id'], trade(store, a, side='sell', traded_at='2026-10-30T10:00:00+08:00'))
    assert result['entry']['realized_pnl'] is None
    assert store.ledger(a['id'])['summary']['realized_pnl'] is None
    assert result['entry']['total_cost_after'] == '0'


def test_external_funds_and_reversal_do_not_trade_or_recharge(store):
    a = account(store)
    deposited = store.adjust_funds(a['id'], {'operation_id': 'funds-deposit-1', 'expected_version': 1, 'kind': 'deposit', 'amount': '10000', 'reason': '模拟转入'})
    assert deposited['cash_after'] == '110000.00'
    assert store.ledger(a['id'])['summary']['realized_pnl'] == '0.00'
    result = store.record_trade(a['id'], trade(store, a))
    reverse = store.reverse_last_entry(a['id'], result['entry_id'], {'operation_id': 'reverse-trade-1', 'expected_version': result['input_version'], 'reason': '录入错误'})
    assert reverse['cash_after'] == '110000.00'
    assert store.ledger(a['id'])['summary']['total_fees'] == '0.00'
    assert store.account(a['id'])['holdings'] == []
    with pytest.raises(ValueError, match='末笔'):
        store.reverse_last_entry(a['id'], deposited['entry_id'], {'operation_id': 'reverse-older-1', 'expected_version': reverse['input_version'], 'reason': '不允许'})


def test_legacy_put_guard_and_explicit_correction(store):
    a = account(store)
    with pytest.raises(ValueError, match='账本'):
        store.save_account({'name': a['name'], 'cash': '90000', 'holdings': [], 'expected_version': 1}, a['id'])
    saved = store.save_account({'name': a['name'], 'cash': '90000', 'holdings': [], 'expected_version': 1, 'ledger_action': 'correction', 'adjustment_reason': '期初余额核对'}, a['id'])
    assert saved['cash'] == '90000.00'
    assert store.ledger(a['id'])['entries'][0]['kind'] == 'correction'


def test_migration_keeps_snapshot_and_is_repeatable(store):
    a = account(store, holdings=[{'symbol': 'SH:518880', 'quantity': '1000', 'cost_price': '5.123'}])
    with store.connect() as db:
        original = dict(db.execute('SELECT * FROM accounts WHERE id=?', (a['id'],)).fetchone())
        for table in ('ledger_operations', 'cash_entries', 'ledger_entries', 'ledger_positions', 'fee_plans', 'ledger_accounts'):
            db.execute('DROP TABLE ' + table)
        db.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    migrated = Store(store.path, mode='demo')
    with migrated.connect() as db:
        assert dict(db.execute('SELECT * FROM accounts WHERE id=?', (a['id'],)).fetchone()) == original
    assert migrated.path.with_name(migrated.path.name + '.before-v11.bak').exists()
    assert migrated.ledger(a['id'])['entries'][0]['source'] == 'schema10_snapshot_baseline'
    assert Store(store.path, mode='demo').ledger(a['id']) == migrated.ledger(a['id'])


def test_selling_all_preserves_exact_cost_and_no_fee_double_count(store):
    a = account(store)
    store.record_trade(a['id'], trade(store, a, quantity='300', price='10.01'))
    p1 = store.record_trade(a['id'], trade(store, a, operation_id='sell-part-001', quantity='100', side='sell', price='11', traded_at='2026-10-27T10:00:00+08:00'))
    p2 = store.record_trade(a['id'], trade(store, a, operation_id='sell-part-002', quantity='200', side='sell', price='11', traded_at='2026-10-27T10:01:00+08:00'))
    assert Decimal(p1['entry']['cost_relieved']) + Decimal(p2['entry']['cost_relieved']) == Decimal('3008.03')
    assert p2['entry']['total_cost_after'] == '0'
    assert store.ledger(a['id'])['summary']['realized_pnl'] == '280.29'


def test_market_and_side_plan_differences_and_fee_override(store):
    a = account(store)
    plan = store.fee_plan(a['id'])
    plan['expected_fee_plan_version'] = 1
    plan['market_overrides'] = {'SZ': {'stock': {'transfer_included': True, 'buy': {'minimum_commission': '1'}, 'sell': {'minimum_commission': '2'}}}}
    store.save_fee_plan(a['id'], plan)
    buy = store.record_trade(a['id'], trade(store, a, quantity='100'))
    assert buy['fees']['components'] == {'commission': '1.00', 'transfer': '0.00', 'stamp': '0.00'}
    sell = store.record_trade(a['id'], trade(store, a, operation_id='market-sell-2', side='sell', quantity='100', price='11', traded_at='2026-10-27T10:00:00+08:00'))
    assert sell['fees']['components'] == {'commission': '2.00', 'transfer': '0.00', 'stamp': '0.55'}
    with pytest.raises(ValueError, match='已包含'):
        store.preview_trade(a['id'], trade(store, a, operation_id='market-buy-3', traded_at='2026-10-28T10:00:00+08:00', fees_override={'commission': '1', 'transfer': '.01', 'reason': '应拒绝重复过户'}))


def test_actual_fee_override_retains_estimate_and_reason(store):
    a = account(store)
    p = trade(store, a, fees_override={'commission': '2.50', 'transfer': '.10', 'reason': '模拟交割单费用'})
    result = store.record_trade(a['id'], p)
    assert result['cash_after'] == '89997.40'
    assert result['fees']['estimated']['commission'] == '5.00'
    assert result['fees']['components']['commission'] == '2.50'
    assert not result['fees']['estimated_only']
    with pytest.raises(ValueError, match='依据'):
        store.preview_trade(a['id'], trade(store, a, fees_override={'commission': '2'}))


def test_cash_unknown_funding_and_previews_are_readonly(store):
    a = store.save_account(AccountInput(name='unknown', cash=None))
    with pytest.raises(ValueError, match='现金未知'):
        store.record_trade(a['id'], trade(store, a))
    with pytest.raises(ValueError, match='现金未知'):
        store.adjust_funds(a['id'], {'operation_id': 'unknown-deposit', 'expected_version': 1, 'kind': 'deposit', 'amount': '100000', 'reason': '无期初'})
    opened = store.adjust_funds(a['id'], {'operation_id': 'unknown-opening', 'expected_version': 1, 'kind': 'opening', 'amount': '100000', 'reason': '模拟期初现金来源'})
    assert opened['cash_after'] == '100000.00'
    before = store.account(a['id'])
    for _ in range(2):
        assert store.preview_trade(a['id'], trade(store, a))['cash_after'] == '89994.90'
    assert store.account(a['id']) == before
    with pytest.raises(ValueError, match='转出超过'):
        store.adjust_funds(a['id'], {'operation_id': 'too-much-withdraw', 'expected_version': 2, 'kind': 'withdraw', 'amount': '100001', 'reason': '测试'})


def test_fee_version_cas_and_effective_date_reconfirmation(store):
    a = account(store)
    payload = trade(store, a)
    plan = store.fee_plan(a['id'])
    store.preview_trade(a['id'], payload)
    store.save_fee_plan(a['id'], {**plan, 'expected_fee_plan_version': 1, 'effective_from': '2026-10-27'})
    with pytest.raises(Conflict, match='费用方案'):
        store.record_trade(a['id'], payload)
    with pytest.raises(ValueError, match='生效日'):
        store.record_trade(a['id'], {**payload, 'fee_plan_version': 2})
    assert len(store.ledger(a['id'])['entries']) == 1


def test_unknown_product_board_and_invalid_sell_quantities(store):
    a = account(store)
    for sid, name, kind in [('SH:688001', '科创板样例', 'stock'), ('BJ:920001', '北交所样例', 'stock'), ('SH:588999', '未知ETF', 'etf')]:
        store.put_cache('securities', sid, {'symbol': sid, 'name': name, 'kind': kind, 'status': 'ok'})
        with pytest.raises(ValueError, match='支持|确认'):
            store.record_trade(a['id'], trade(store, a, symbol=sid))
    with pytest.raises(ValueError, match='身份'):
        store.record_trade(a['id'], trade(store, a, symbol='SZ:999999'))
    store.record_trade(a['id'], trade(store, a))
    with pytest.raises(ValueError, match='持有量'):
        store.record_trade(a['id'], trade(store, a, side='sell', quantity='1100', operation_id='oversell-test', traded_at='2026-10-27T10:00:00+08:00'))
    with pytest.raises(ValueError, match='零股'):
        store.record_trade(a['id'], trade(store, a, side='sell', quantity='101', operation_id='odd-sell-test', traded_at='2026-10-27T10:00:00+08:00'))


def test_odd_lot_all_sale_and_tick_rounding_source(store):
    a = account(store, holdings=[{'symbol': 'SZ:159599', 'quantity': '101', 'cost_price': '2'}])
    store.correct_snapshot(a['id'], {'operation_id': 'odd-initial-correction', 'expected_version': 1, 'reason': '已核对可卖余额', 'holdings': [{'symbol': 'SZ:159599', 'quantity': '101', 'cost_price': '2', 'available_quantity': '101'}]})
    result = store.record_trade(a['id'], trade(store, a, symbol='SZ:159599', side='sell', quantity='101', price='2.501', traded_at='2026-10-30T10:00:00+08:00'))
    assert result['entry']['gross_raw'] == '252.601'
    assert result['entry']['gross'] == '252.60'
    assert result['entry']['total_cost_after'] == '0'
    assert result['entry']['realized_pnl'] == '45.60'


def test_new_cost_does_not_make_unknown_opening_basis_zero(store):
    a = account(store, holdings=[{'symbol': 'SZ:002354', 'quantity': '100'}])
    result = store.record_trade(a['id'], trade(store, a))
    assert result['entry']['total_cost_after'] is None
    assert store.account(a['id'])['holdings'][0]['cost_price'] is None
    assert store.ledger(a['id'])['positions'][0]['availability_unknown_quantity'] == '100'


def test_immutable_history_and_traded_account_delete_guard(store):
    a = account(store)
    store.record_trade(a['id'], trade(store, a))
    for table in ('ledger_entries', 'cash_entries', 'fee_plans', 'ledger_operations'):
        for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET account_id=account_id'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                with store.connect() as db:
                    db.execute(statement)
    with pytest.raises(ValueError, match='不可变'):
        store.delete_account(a['id'])
    assert len(store.ledger(a['id'])['entries']) == 2


def test_backfill_order_and_weekend_policy(store):
    a = account(store)
    store.record_trade(a['id'], trade(store, a, traded_at='2026-10-26T10:00:00+08:00'))
    with pytest.raises(ValueError, match='插单'):
        store.record_trade(a['id'], trade(store, a, operation_id='older-record-1', traded_at='2026-10-23T10:00:00+08:00'))
    b = account(store)
    with pytest.raises(ValueError, match='交易日'):
        store.record_trade(b['id'], trade(store, b, mode='historical', traded_at='2026-10-25T10:00:00+08:00'))
    result = store.record_trade(b['id'], trade(store, b, mode='simulated', traded_at='2026-10-25T10:00:00+08:00'))
    assert result['entry']['mode'] == 'simulated'
    assert result['entry']['traded_at'] != result['entry']['recorded_at']


def test_account_research_context_is_bounded_summary(store):
    a = account(store)
    store.record_trade(a['id'], trade(store, a))
    context = store.account(a['id'])['ledger']
    assert context['recent_entries'][0]['cash_after'] == '89994.90'
    assert 'after_positions' not in context['recent_entries'][0]
    assert 'plan_snapshot' not in json.dumps(context['recent_entries'])


def test_last_trade_fee_correction_buy_sell_unknown_and_reversal(store):
    a = account(store)
    bought = store.record_trade(a['id'], trade(store, a))
    payload = {'operation_id': 'fee-correct-buy-1', 'expected_version': bought['input_version'], 'fees': {'commission': '2.50'}, 'reason': '模拟交割单实际佣金'}
    corrected = store.adjust_trade_fees(a['id'], bought['entry_id'], payload)
    assert corrected['cash_after'] == '89997.40'
    assert corrected['entry']['total_cost_after'] == '10002.60'
    assert store.ledger(a['id'])['summary']['total_fees'] == '2.60'
    assert store.adjust_trade_fees(a['id'], bought['entry_id'], payload)['entry_id'] == corrected['entry_id']
    assert store.ledger_operation(a['id'], 'operation-buy-1')['fees']['total'] == '5.10'
    store.reverse_last_entry(a['id'], corrected['entry_id'], {'operation_id': 'undo-fee-correction', 'expected_version': corrected['input_version'], 'reason': '更正回退'})
    assert store.account(a['id'])['cash'] == '89994.90'
    assert store.ledger(a['id'])['summary']['total_fees'] == '5.10'
    sold = store.record_trade(a['id'], trade(store, a, operation_id='fee-correct-sale-1', side='sell', quantity='400', price='11', traded_at='2026-10-27T10:00:00+08:00'))
    fee_sale = store.adjust_trade_fees(a['id'], sold['entry_id'], {'operation_id': 'fee-correct-sale-2', 'expected_version': sold['input_version'], 'fees': {'commission': '2'}, 'reason': '模拟实际交割佣金'})
    assert fee_sale['cash_after'] == '94390.66'
    assert store.ledger(a['id'])['summary']['realized_pnl'] == '393.72'
    assert store.ledger(a['id'])['positions'][0]['total_cost'] == '6003.06'
    with pytest.raises(ValueError, match='末笔'):
        store.adjust_trade_fees(a['id'], bought['entry_id'], {'operation_id': 'fee-correct-oldest', 'expected_version': fee_sale['input_version'], 'fees': {'commission': '1'}, 'reason': '禁止旧账回放'})


def test_integral_decimal_quantity_survives_ledger_and_sell(store):
    a = account(store)
    bought = store.record_trade(a['id'], trade(store, a, symbol='SH:518880', quantity='100.0', price='10'))
    assert Decimal(store.ledger(a['id'])['positions'][0]['available_quantity']) == 100
    sold = store.record_trade(a['id'], trade(store, a, operation_id='decimal-quantity-sell', symbol='SH:518880', side='sell', quantity='100.00', price='10'))
    assert sold['quantity_after'] == '0'


def test_legacy_subcent_cash_requires_explicit_correction_before_booking(store):
    a = store.save_account(AccountInput(name='legacy subcent', cash='100000.005'))
    with pytest.raises(ValueError, match='分|精度|校正'):
        store.record_trade(a['id'], trade(store, a, symbol='SH:518880', quantity='100', price='10'))
    with pytest.raises(ValueError, match='分|精度|校正'):
        store.adjust_funds(a['id'], {'operation_id': 'subcent-cash-deposit', 'expected_version': 1, 'kind': 'deposit', 'amount': '1', 'reason': '不允许隐式舍入'})
    assert store.account(a['id'])['cash'] == '100000.005'
    assert len(store.ledger(a['id'])['entries']) == 1


def test_subcent_legacy_correction_and_reversal_reconcile_exactly(store):
    a = store.save_account(AccountInput(name='subcent migration', cash='100000.005'))
    with store.connect() as db:
        for table in ('ledger_operations', 'cash_entries', 'ledger_entries', 'ledger_positions', 'fee_plans', 'ledger_accounts'):
            db.execute('DROP TABLE ' + table)
        db.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    migrated = Store(store.path, mode='demo')
    assert migrated.account(a['id'])['cash'] == '100000.005'
    corrected = migrated.correct_snapshot(a['id'], {'operation_id': 'subcent-correction-1', 'expected_version': 1, 'reason': '受控期初余额明确到分', 'cash': '100000.01', 'holdings': []})
    assert corrected['entry']['cash_effect'] == '0.005'
    reversed_entry = migrated.reverse_last_entry(a['id'], corrected['entry_id'], {'operation_id': 'subcent-reversal-1', 'expected_version': 2, 'reason': '撤销受控校正'})
    assert reversed_entry['entry']['cash_effect'] == '-0.005'
    ledger = migrated.ledger(a['id'])
    assert sum(Decimal(row['amount']) for row in ledger['cash_entries']) == Decimal(ledger['cash'])
    assert ledger['cash'] == '100000.005'


def test_reason_only_fee_override_cannot_mark_estimate_actual(store):
    a = account(store)
    with pytest.raises(ValueError, match='费用数值'):
        store.record_trade(a['id'], trade(store, a, fees_override={'reason': '没有实际费用金额'}))
    assert len(store.ledger(a['id'])['entries']) == 1


def test_empty_unknown_baseline_does_not_taint_new_cost(store):
    a = account(store, holdings=[{'symbol': 'SZ:002354', 'quantity': '0', 'cost_price': None}])
    result = store.record_trade(a['id'], trade(store, a))
    assert result['entry']['total_cost_after'] == '10005.10'
    assert store.account(a['id'])['ledger']['positions']['SZ:002354']['cost_basis'] == 'trade_cost'


def test_reversing_correction_preserves_later_metadata(store):
    a = account(store)
    corrected = store.correct_snapshot(a['id'], {'operation_id': 'metadata-correction-1', 'expected_version': 1, 'reason': '受控综合校正', 'cash': '99999', 'holdings': [], 'name': '校正时名称'})
    latest = store.account(a['id'])
    edited = store.save_account_metadata_operation(a['id'], {'name': '后来保存的名称', 'preferences': {**latest['preferences'], 'horizon': '后来保存的周期'}, 'expected_version': 2}, 'later-metadata-1')
    reversed_entry = store.reverse_last_entry(a['id'], corrected['entry_id'], {'operation_id': 'metadata-reversal-1', 'expected_version': edited['version'], 'reason': '仅撤销末笔账务，不覆盖后续元信息'})
    after = store.account(a['id'])
    assert after['name'] == '后来保存的名称'
    assert after['preferences']['horizon'] == '后来保存的周期'
    assert after['cash'] == '100000'
    assert reversed_entry['entry']['metadata_reversal_policy'] == 'restore_only_without_later_account_version'


def test_reversing_immediate_combined_correction_restores_metadata(store):
    a = account(store)
    corrected = store.correct_snapshot(a['id'], {'operation_id': 'immediate-correction-1', 'expected_version': 1, 'reason': '受控综合校正', 'cash': '99999', 'holdings': [], 'name': '校正时名称'})
    store.reverse_last_entry(a['id'], corrected['entry_id'], {'operation_id': 'immediate-reversal-1', 'expected_version': 2, 'reason': '立即撤销'})
    assert store.account(a['id'])['name'] == a['name']


def test_tax_only_override_keeps_unconfirmed_commission_estimated(store):
    a = account(store)
    result = store.record_trade(a['id'], trade(store, a, fees_override={'transfer': '0.09', 'reason': '只核实过户费，佣金协议未知'}))
    assert result['fees']['estimated_only'] is True
    assert result['fees']['actual_override_components'] == ['transfer']
    fixed = store.adjust_trade_fees(a['id'], result['entry_id'], {'operation_id': 'tax-only-fee-fix', 'expected_version': 2, 'fees': {'transfer': '0.10'}, 'reason': '只更正过户费'})
    assert fixed['fees']['estimated_only'] is True
    assert fixed['fees']['actual_override_components'] == ['transfer']
    b = account(store)
    actual = store.record_trade(b['id'], trade(store, b, fees_override={'commission': '2.50', 'reason': '佣金已由交割单核实'}))
    changed_tax = store.adjust_trade_fees(b['id'], actual['entry_id'], {'operation_id': 'actual-commission-tax-fix', 'expected_version': 2, 'fees': {'transfer': '0.09'}, 'reason': '过户费用更正'})
    assert changed_tax['fees']['estimated_only'] is False
    assert changed_tax['fees']['actual_override_components'] == ['commission', 'transfer']
