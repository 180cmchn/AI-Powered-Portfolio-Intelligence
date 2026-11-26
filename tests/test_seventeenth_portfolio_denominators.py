"""Synthetic ledger and real calculation/context controls, without provider I/O.

These tests establish denominator arithmetic and delivered inputs, not whether
an external model will adopt the guidance or whether a purchase is desirable.
"""
from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput, metrics
from app.model import ModelClient
from app.service import ResearchTools, Service
from app.store import Store
from tests.delivery_helpers import delivery_arguments
from tests.helpers_sixth import configure_test_team
from tests.test_ninth_calculation_context import assert_hot_closeout, config, output, seed


STOCK='SZ:002354'
GOLD='SH:518880'
OTHER='SZ:159278'


@pytest.fixture
def portfolio(tmp_path,frozen_clock):
    frozen_clock.set('2026-11-06T10:00:00+08:00')
    store=Store(tmp_path/'denominators.sqlite3',mode='live')
    store.set_meta('settings',{**store.settings(),'debounce_seconds':0,'quote_provider':'manual',
        'scheduled_research_enabled':False,'market_refresh_enabled':False,'forecast_evaluation_enabled':False})
    for symbol,kind,price in [(STOCK,'stock','8.41'),(GOLD,'etf','7.676'),(OTHER,'etf','1.000')]:
        store.put_cache('securities',symbol,{'symbol':symbol,'name':'controlled '+symbol,'kind':kind,
            'identity_status':'confirmed','status':'ok','source':'synthetic directory'})
        store.put_cache('quotes',symbol,{'symbol':symbol,'price':price,'status':'ok',
            'as_of':'2026-11-06T10:00:00+08:00','source':'synthetic saved prices'})
    account=store.save_account(AccountInput(name='Controlled denominator account',cash='85579.90',
        holdings=[{'symbol':STOCK,'quantity':'1000','cost_price':'7.01667'},
                  {'symbol':GOLD,'quantity':'500','cost_price':'8'}]))
    return store,account


def position_bytes(store,account_id):
    with store.connect() as db:
        return {r['symbol']:r['body'] for r in db.execute('SELECT symbol,body FROM ledger_positions WHERE account_id=?',(account_id,))}


def buy(store,account_id,*,symbol=OTHER,quantity='10000',price='1.000',zero_fee=False):
    args={'operation_id':'controlled-buy','expected_version':store.account(account_id)['version'],
        'fee_plan_version':store.fee_plan(account_id)['version'],'symbol':symbol,'side':'buy',
        'quantity':quantity,'price':price,'traded_at':'2026-11-06T10:00:00+08:00','mode':'simulated'}
    if zero_fee:
        args['fees_override']={'commission':'0','transfer':'0','stamp':'0','reason':'Explicit zero-fee arithmetic control, not a real fee schedule'}
    preview=store.preview_trade(account_id,args)
    receipt=store.record_trade(account_id,args)
    assert preview['cash_after']==receipt['cash_after']
    assert preview['total_cost_after']==receipt['entry']['total_cost_after']
    return receipt


def measures(store,account_id,quotes=None):
    return metrics(store.account(account_id),quotes if quotes is not None else store.cache('quotes'),store.cache('securities'))


def stock_item(values):
    return next(p for p in values['positions'] if p['symbol']==STOCK)


async def percentage(tools,numerator,denominator):
    result=await tools.calculate('percent',[numerator,denominator])
    assert result['inputs']==[numerator,denominator]
    return Decimal(result['result'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fee,stock_price,expected_securities,expected_assets,weight_direction',[
    ('0.00','8.41','22248.00','97827.90','down'),
    ('5.00','8.41','22248.00','97822.90','down'),
    ('5.00','33.64','47478.00','123052.90','up'),
])
async def test_cash_buy_other_security_separates_value_and_two_denominators(
        portfolio,fee,stock_price,expected_securities,expected_assets,weight_direction):
    store,account=portfolio;account_id=account['id']
    before=measures(store,account_id);positions_before=position_bytes(store,account_id)
    assert (stock_item(before)['market_value'],before['securities_value'],before['total_assets'])==('8410.00','12248.00','97827.90')
    receipt=buy(store,account_id,zero_fee=fee=='0.00')
    quotes=deepcopy(store.cache('quotes'));quotes[STOCK]['price']=stock_price
    after=measures(store,account_id,quotes)
    tools=ResearchTools(store,store.job(receipt['research']['job_id']),store.settings())
    before_book=await percentage(tools,stock_item(before)['market_value'],before['securities_value'])
    after_book=await percentage(tools,stock_item(after)['market_value'],after['securities_value'])
    before_assets=await percentage(tools,stock_item(before)['market_value'],before['total_assets'])
    after_assets=await percentage(tools,stock_item(after)['market_value'],after['total_assets'])
    assert (after['securities_value'],after['total_assets'])==(expected_securities,expected_assets)
    assert receipt['fees']['total']==fee
    assert Decimal(after['cash'])==Decimal(before['cash'])-Decimal('10000')-Decimal(fee)
    assert (after_book<before_book) if weight_direction=='down' else (after_book>before_book)
    assert stock_item(after)['weight_pct']==format(after_book.quantize(Decimal('.01')),'f')
    assert before['weight_basis']==after['weight_basis']=='已录入持仓证券市值（不含现金）'
    if stock_price=='8.41':
        assert stock_item(after)['market_value']==stock_item(before)['market_value']
        assert after_assets==before_assets if fee=='0.00' else after_assets>before_assets
    else:
        assert stock_item(after)['market_value']=='33640.00' and after_assets>before_assets
    # Another security's purchase cannot rewrite original cost/lots. Quote
    # revaluation changes market value, never the recorded remaining cost.
    current_positions=position_bytes(store,account_id)
    assert all(current_positions[symbol]==body for symbol,body in positions_before.items())
    assert stock_item(after)['total_cost']==stock_item(before)['total_cost']=='7016.67000'
    assert Decimal(next(p for p in after['positions'] if p['symbol']==OTHER)['total_cost'])==Decimal('10000')+Decimal(fee)


@pytest.mark.asyncio
async def test_buying_same_security_is_opposite_control_not_automatic_diversification(portfolio):
    store,account=portfolio;before=measures(store,account['id']);old=position_bytes(store,account['id'])
    receipt=buy(store,account['id'],symbol=STOCK,quantity='1000',price='8.41',zero_fee=True)
    after=measures(store,account['id']);tools=ResearchTools(store,store.job(receipt['research']['job_id']),store.settings())
    assert stock_item(after)['market_value']=='16820.00'
    assert after['securities_value']=='20658.00' and after['total_assets']==before['total_assets']
    assert await percentage(tools,'16820.00','20658.00')>await percentage(tools,'8410.00','12248.00')
    assert await percentage(tools,'16820.00',after['total_assets'])>await percentage(tools,'8410.00',before['total_assets'])
    assert Decimal(stock_item(after)['total_cost'])==Decimal(stock_item(before)['total_cost'])+Decimal('8410')
    assert position_bytes(store,account['id'])[GOLD]==old[GOLD]


@pytest.mark.parametrize('missing',['cash','price'])
def test_unknown_denominator_does_not_become_zero_or_complete_account_weight(portfolio,missing):
    store,account=portfolio;account=store.account(account['id']);quotes=store.cache('quotes')
    if missing=='cash':account={**account,'cash':None}
    else:quotes={key:value for key,value in quotes.items() if key!=GOLD}
    result=metrics(account,quotes,store.cache('securities'))
    assert result['total_assets'] is None
    if missing=='cash':
        assert result['securities_value']=='12248.00' and stock_item(result)['weight_pct']=='68.66'
    else:
        assert result['securities_value'] is None and stock_item(result)['weight_pct'] is None
        assert result['known_priced_value']=='8410.00' and result['missing_prices']==[GOLD]


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['responses','chat_completions'])
async def test_single_security_production_payload_keeps_account_denominator_and_cost(portfolio,monkeypatch,protocol):
    store,account=portfolio;receipt=buy(store,account['id']);account=store.account(account['id'])
    cfg={**store.settings(),**config(monkeypatch,model_protocol=protocol,max_agent_turns=3,
        model_responses_stream=False,model_chat_stream=False)}
    store.set_meta('settings',cfg);configure_test_team(store,cfg)
    selected=store.submit_task(account['id'],target=STOCK,operation_id='target-stock',research_scope='securities')
    job=next(j for j in store.claim_jobs(account_id=account['id']) if j['id']==selected['id'])
    prior_positions=position_bytes(store,account['id']);payloads=[];handler_errors=[]
    def check_payload(request):
        payload=json.loads(request.content);payloads.append(payload);visible=seed(payload)
        instructions=payload.get('instructions') or '\n'.join(
            item['content'] for item in payload.get('messages',[]) if item.get('role')=='system')
        assert '单证券研究范围不改变账户权重分母' in instructions
        assert '占比下降本身不证明经济风险已分散' in instructions
        assert visible['target_security']['symbol']==STOCK
        assert visible['metrics']['securities_value']=='22248.00'
        assert visible['metrics']['total_assets']=='97822.90'
        assert stock_item(visible['metrics'])['weight_pct']=='37.80'
        assert stock_item(visible['metrics'])['total_cost']=='7016.67000'
        assert {p['symbol'] for p in visible['metrics']['positions']}=={STOCK,GOLD,OTHER}
        if len(payloads)==1:
            return output(protocol,[('calculate',{'operation':'percent','values':['8410.00','22248.00']}),
                ('calculate',{'operation':'percent','values':['8410.00','97822.90']})])
        assert len(payloads)==2
        assert_hot_closeout(payload,payloads[-2],'finish_analysis')
        assert all(t.get('type')!='web_search' for t in payload['tools'])
        calculations={tuple(item['inputs']):item['result'] for item in visible['calculation_summaries']}
        assert Decimal(calculations[('8410.00','22248.00')])==Decimal('8410')/Decimal('22248')*100
        assert Decimal(calculations[('8410.00','97822.90')])==Decimal('8410')/Decimal('97822.90')*100
        return output(protocol,[('finish_analysis',delivery_arguments(
            '受控结论：单证券范围仍用账户证券市值22248元作分母，原股证券账占比37.80%；原股市值8410元，原成本未变。费用已计入总资产分母97822.90元。是否分散经济风险仍需独立判断。',
            scope=(STOCK,),covered_demand_ids=[]))])
    def handler(request):
        try:return check_payload(request)
        except Exception as exc:
            handler_errors.append(repr(exc));raise
    monkeypatch.setattr(agents,'ModelClient',lambda config,before_request=None:ModelClient(config,before_request,httpx.MockTransport(handler)))
    await Service(store).execute(job)
    assert not handler_errors,handler_errors
    saved=store.job(job['id']);assert saved['state']=='completed',saved.get('error')
    report=next(r for r in store.analyses(account['id']) if r['job_id']==job['id'])
    assert report['result']['scope']==[STOCK]
    assert report['result']['metrics']['securities_value']=='22248.00'
    assert position_bytes(store,account['id'])==prior_positions
    assert store.account(account['id'])['cash']==receipt['cash_after']
    assert len(payloads)==2
