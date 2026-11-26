"""The standalone recommendation card receives the same sale basis as the report.

The ledger, frozen team, Service, Runtime and ModelClient are real. The two
protocols use scripted HTTP responses to test delivery and persistence.
"""
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app import agents
from app.agent_config import PROMPTS, SEVENTEENTH_CARD_BASIS_PREVIOUS_MANAGER_PROMPT
from app.domain import AccountInput
from app.model import ModelClient
from app.service import Service
from app.store import Store, dumps, loads
from scripts.verify_seventeenth_live import configure_isolated
from tests.context_helpers import effective_context
from tests.delivery_helpers import delivery_arguments
from tests.helpers_sixth import configure_test_team
from tests.test_ninth_calculation_context import assert_hot_closeout, config, output
from tests.test_seventeenth_child_final_evidence import wire_tool_exchanges


MARKER = 'seventeenth_card_basis_prompt_migration'
GOLD = 'SH:518880'
OLD_PROMPT = Path(__file__).with_name('fixtures') / 'seventeenth-b8-manager-before-card-basis.txt'


def roles(store):
    with store.connect() as db:
        return {row['role_id']: loads(row['body']) for row in db.execute('SELECT * FROM agent_definitions')}


def stage_historical_prompt(store, value, *, custom=False):
    """Only the historical migration starting input is constructed directly."""
    with store.connect() as db:
        role = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role.update(system_prompt=value, custom=custom)
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(role),))
        db.execute('DELETE FROM meta WHERE key=?', (MARKER,))


@pytest.mark.parametrize('variant', ['exact_b8', 'custom_flag', 'edited_builtin', 'already_current'])
def test_standalone_basis_migration_changes_only_the_exact_default_once(tmp_path, variant):
    store = Store(tmp_path / 'manager-basis.sqlite3')
    settings = {**store.settings(), 'model_name': 'controlled-basis', 'reasoning_effort': 'xhigh',
                'debounce_seconds': 0}
    store.set_meta('settings', settings)
    configure_test_team(store, settings)
    previous = OLD_PROMPT.read_text()
    assert hashlib.sha256(previous.encode()).hexdigest() == SEVENTEENTH_CARD_BASIS_PREVIOUS_MANAGER_PROMPT
    current = (PROMPTS / 'manager.txt').read_text()
    text = current if variant == 'already_current' else previous
    if variant == 'edited_builtin':
        text += '\n用户独立保存的建议卡文字。'
    stage_historical_prompt(store, text, custom=variant == 'custom_flag')
    before = roles(store)
    catalog = int(store.meta('agent_catalog_version'))
    account = store.save_account(AccountInput(name='Frozen earlier team', cash='100', research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    with store.connect() as db:
        frozen_team = db.execute('SELECT config_snapshot FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
    assert json.loads(frozen_team)['_agent_team']['roles']['manager']['system_prompt'] == text

    reopened = Store(store.path)
    after = roles(reopened)
    upgraded = variant == 'exact_b8'
    assert reopened.meta(MARKER)['updated_prompt_roles'] == (['manager'] if upgraded else [])
    assert int(reopened.meta('agent_catalog_version')) == catalog + int(upgraded)
    assert after['manager']['system_prompt'] == (current if upgraded else text)
    for field in ('version', 'prompt_version'):
        assert after['manager'][field] == before['manager'][field] + int(upgraded)
    for field in set(before['manager']) - {'system_prompt', 'version', 'prompt_version', 'updated_at'}:
        assert after['manager'][field] == before['manager'][field]
    assert {k: v for k, v in before.items() if k != 'manager'} == {k: v for k, v in after.items() if k != 'manager'}
    with reopened.connect() as db:
        assert db.execute('SELECT config_snapshot FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == frozen_team
    again = Store(store.path)
    assert roles(again) == after
    assert again.meta(MARKER) == reopened.meta(MARKER)


@pytest.mark.parametrize('source_current', [False, True])
def test_isolated_copy_inherits_card_basis_migration_from_chosen_source(tmp_path, source_current):
    source = Store(tmp_path / 'synthetic-source.sqlite3')
    configure_test_team(source, {**source.settings(), 'model_name': 'controlled-basis', 'reasoning_effort': 'xhigh'})
    account = source.save_account(AccountInput(name='Synthetic source', cash='100'))
    if not source_current:
        stage_historical_prompt(source, OLD_PROMPT.read_text())
    before = roles(source)
    with source.connect() as db:
        historical_source = list(db.iterdump())
    isolated, _ = configure_isolated(source.path, tmp_path / 'isolated.sqlite3', account['id'])
    actual = roles(isolated)
    manager = actual['manager']
    assert manager['system_prompt'] == (PROMPTS / 'manager.txt').read_text()
    for field in ('version', 'prompt_version'):
        assert manager[field] == before['manager'][field] + int(not source_current)
    for field in ('tool_version', 'tools', 'default_binding'):
        assert manager[field] == before['manager'][field]
    assert {k: v for k, v in actual.items() if k != 'manager'} == {k: v for k, v in before.items() if k != 'manager'}
    if source_current:
        assert isolated.meta(MARKER) == source.meta(MARKER)
    else:
        assert isolated.meta(MARKER)['updated_prompt_roles'] == ['manager']
    assert roles(Store(isolated.path)) == actual
    with source.connect() as db:
        assert list(db.iterdump()) == historical_source


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_non_half_sale_basis_survives_normal_service_and_final_card(tmp_path, monkeypatch, frozen_clock, protocol):
    frozen_clock.set('2026-11-06T10:00:00+08:00')
    store = Store(tmp_path / 'card-sale.sqlite3', mode='live')
    settings = {**store.settings(), **config(monkeypatch, model_protocol=protocol, max_agent_turns=3,
                model_responses_stream=False, model_chat_stream=False), 'quote_provider': 'manual',
                'debounce_seconds': 0, 'scheduled_research_enabled': False,
                'market_refresh_enabled': False, 'forecast_evaluation_enabled': False}
    store.set_meta('settings', settings)
    configure_test_team(store, settings)
    store.put_cache('securities', GOLD, {'symbol': GOLD, 'name': 'Controlled ETF', 'kind': 'etf',
                    'identity_status': 'confirmed', 'status': 'ok', 'source': 'synthetic directory'})
    store.put_cache('quotes', GOLD, {'symbol': GOLD, 'price': '8.42', 'status': 'ok',
                    'as_of': '2026-11-06T10:00:00+08:00', 'source': 'synthetic saved quote'})
    account = store.save_account(AccountInput(name='Controlled recommendations', cash='10000'))
    for side, quantity, price in [('buy', '300', '10'), ('sell', '100', '11')]:
        receipt = store.record_trade(account['id'], {'operation_id': 'controlled-card-' + side,
            'expected_version': store.account(account['id'])['version'], 'fee_plan_version': 1,
            'symbol': GOLD, 'side': side, 'quantity': quantity, 'price': price,
            'traded_at': '2026-11-06T10:00:00+08:00', 'mode': 'simulated'})
    job = store.claim_jobs(account_id=account['id'])[0]
    assert job['id'] == receipt['research']['job_id']
    ledger = store.account(account['id'])['ledger']
    sale = next(row for row in ledger['recent_entries'] if row.get('side') == 'sell')
    relieved = sale['cost_relieved']
    remaining = sale['total_cost_after']
    assert (sale['quantity_before'], sale['quantity_after']) == ('300', '200')
    assert Decimal(relieved).quantize(Decimal('.01')) == Decimal('1001.67')
    assert Decimal(remaining).quantize(Decimal('.01')) == Decimal('2003.33')
    assert (sale['gross'], sale['fee_total'], sale['net_sale_proceeds'], sale['realized_pnl']) == (
        '1100.00', '5.00', '1095.00', '93.33')
    calls = []
    errors = []
    card_basis = ('卖出100份×11元/份，成交额1100.00元，扣卖出费用5.00元后净所得1095.00元；'
                  '减卖出份额结转成本约1001.67元，已实现盈亏约93.33元。剩余200份总成本约2003.33元。')

    def inspect(request):
        payload = json.loads(request.content)
        calls.append(payload)
        instructions = payload.get('instructions') or '\n'.join(
            m['content'] for m in payload.get('messages', []) if m.get('role') == 'system')
        assert '建议卡会单独展示 basis' in instructions
        assert '净卖出所得＝卖出数量×每份成交价－卖出费用' in instructions
        assert '已实现盈亏＝净卖出所得－结转成本' in instructions
        visible = effective_context(payload)
        observed = next(row for row in visible['account']['ledger']['recent_entries'] if row.get('side') == 'sell')
        for key in ('quantity', 'price', 'gross', 'fee_total', 'net_sale_proceeds',
                    'cost_relieved', 'total_cost_after', 'realized_pnl_effect'):
            assert observed[key] == sale[key]
        if len(calls) == 1:
            return output(protocol, [
                ('calculate', {'operation': 'multiply', 'values': [observed['quantity'], observed['price']]}),
                ('calculate', {'operation': 'subtract', 'values': [observed['gross'], observed['fee_total']]}),
                ('calculate', {'operation': 'subtract', 'values': [observed['net_sale_proceeds'], relieved]})])
        assert len(calls) == 2
        assert_hot_closeout(payload, calls[-2], 'finish_analysis')
        assert all(tool.get('type') != 'web_search' for tool in payload['tools'])
        calculations = {tuple(item['inputs']): Decimal(item['result']) for item in visible['calculation_summaries']}
        assert calculations[('100', '11')] == Decimal('1100')
        assert calculations[('1100.00', '5.00')] == Decimal('1095.00')
        assert calculations[('1095.00', relieved)].quantize(Decimal('.01')) == Decimal('93.33')
        suggestion = {'security_id': GOLD, 'action': 'hold', 'basis': card_basis, 'material_ids': [],
                      'agent_conditions': ['下次复核剩余200份及账本可卖日期。']}
        return output(protocol, [('finish_analysis', delivery_arguments(
            '受控账本保留剩余200份。' + card_basis, scope=('account',), suggestions=[suggestion],
            covered_demand_ids=[]))])

    def handler(request):
        try:
            return inspect(request)
        except Exception as error:
            errors.append(repr(error))
            raise

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))
    await Service(store).execute(job)
    assert not errors, errors
    assert len(calls) == 2
    saved = store.job(job['id'])
    assert saved['state'] == 'completed', saved.get('error')
    result = store.analyses(account['id'])[0]['result']
    assert result['suggestions'][0]['basis'] == card_basis
    application = result['delivery_application']['suggestions'][0]
    assert application['status'] == 'applied'
    card = store.recommendation(account['id'], application['recommendation_id'])
    assert card['basis'] == card_basis
    assert card['security_id'] == GOLD and card['action'] == 'hold'
    assert store.account(account['id'])['ledger']['realized_pnl'] == '93.33'


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_disclosure', [False, True])
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_card_holds_shareholder_table_and_separate_governance_evidence(
        tmp_path, monkeypatch, frozen_clock, protocol, explicit_disclosure):
    """Two controlled disclosures can support different standalone card text."""
    from tests.test_fourteenth_chain import setup_app

    frozen_clock.set('2026-11-06T10:00:00+08:00')
    stock = 'SZ:002354'
    table = '合成股东表显示第一大股东甲持股2.93%。'
    disclosure = '另附合成治理披露明确认定甲为控股股东，依据是披露的表决权委托协议。'
    excerpt = table + (disclosure if explicit_disclosure else '')
    basis = ('股东表记载甲持股2.93%。另附治理披露依据表决权委托协议认定甲为控股股东。'
             if explicit_disclosure else '股东表记载甲持股2.93%，现有表内信息下控制关系未知。')
    calls = []
    errors = []

    def inspect(request):
        payload = json.loads(request.content)
        calls.append(payload)
        instructions = payload.get('instructions') or '\n'.join(
            m['content'] for m in payload['messages'] if m.get('role') == 'system')
        assert '股东表中的持股比例只说明表内持股情况' in instructions
        assert '判断控股股东或实际控制人须有相应治理披露或表决权安排等依据' in instructions
        if len(calls) == 1:
            return output(protocol, [('get_material', {'material_id': material['id']})])
        assert len(calls) == 2
        assert_hot_closeout(payload, calls[-2], 'finish_analysis')
        exchanges = wire_tool_exchanges(payload, 'get_material')
        assert len(exchanges) == 1
        arguments, source = exchanges[0]
        assert arguments == {'material_id': material['id']}
        assert source['id'] == material['id']
        assert table in source['text']
        assert (disclosure in source['text']) is explicit_disclosure
        return output(protocol, [('finish_analysis', delivery_arguments(
            '受控持仓观察。' + basis, scope=('account',), covered_demand_ids=[], material_ids=[material['id']],
            suggestions=[{'security_id': stock, 'action': 'hold', 'basis': basis,
                          'material_ids': [material['id']]}]))])

    def handler(request):
        try:
            return inspect(request)
        except Exception as error:
            errors.append(repr(error))
            raise

    app, store, _ = setup_app(tmp_path, monkeypatch, handler, model_protocol=protocol,
                              max_agent_turns=3, model_chat_stream=False)
    store.put_cache('securities', stock, {'symbol': stock, 'name': '受控公司', 'kind': 'stock',
                    'identity_status': 'confirmed', 'status': 'ok', 'source': 'synthetic directory'})
    store.put_cache('quotes', stock, {'symbol': stock, 'price': '10', 'status': 'ok',
                    'as_of': '2026-11-06T10:00:00+08:00', 'source': 'synthetic saved quote'})
    account = store.save_account(AccountInput(name='受控股东表账户', cash='1000',
                                 holdings=[{'symbol': stock, 'quantity': '100', 'cost_price': '10'}]))
    material = store.save_material({'scope': 'public', 'access': 'excerpt', 'source_type': 'external_original',
                                    'source_title': '合成披露夹具', 'content': excerpt})
    job = store.claim_jobs(account_id=account['id'])[0]
    await app.state.service.execute(job)
    assert not errors, errors
    assert len(calls) == 2
    saved = store.job(job['id'])
    assert saved['state'] == 'completed', saved.get('error')
    result = store.analyses(account['id'])[0]['result']
    application = result['delivery_application']['suggestions'][0]
    assert application['status'] == 'applied'
    card = store.recommendation(account['id'], application['recommendation_id'])
    assert card['basis'] == basis and card['material_ids'] == [material['id']]
