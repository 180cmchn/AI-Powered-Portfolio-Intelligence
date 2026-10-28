"""Controlled ETF snapshots and quotes, not evidence of a live investment report."""
from datetime import datetime
import pytest
from app.domain import AccountInput, TZ, now
from app.providers import quote_time_state
from app.service import Service
from app.store import Store


async def test_etf_snapshot_edit_recomputes_decimal_values_without_cash_deduction(tmp_path, monkeypatch):
    store=Store(tmp_path/'controlled-etf.sqlite3')
    store.set_meta('settings',{'quote_provider':'manual','debounce_seconds':0})
    store.put_cache('quotes','SZ:159278',{'symbol':'SZ:159278','name':'机器人ETF鹏华','price':'0.965',
        'status':'ok','as_of':now(),'source':'controlled-test','fresh_for_triggers':False})
    store.put_cache('securities','SZ:159278',{'symbol':'SZ:159278','name':'机器人ETF鹏华','kind':'etf'})
    async def runner(context, config, tools, emit):
        return {'report':'受控测试报告；不是模型自然投资建议。'}
    service=Service(store,runner=runner)
    async def no_network(*args,**kwargs):return {'updated':0}
    monkeypatch.setattr(service,'refresh_quotes',no_network)
    a=store.save_account(AccountInput(name='A controlled',cash='10000',holdings=[{'symbol':'SZ:159278','quantity':'10000'}]))
    b=store.save_account(AccountInput(name='B controlled',cash='20000'))
    for job in store.claim_jobs(): await service.execute(job)
    old=store.analyses(a['id'])[0]
    before=store.account(b['id'])
    first=store.analysis(old['id'])['result']['metrics']
    assert first['known_priced_value']=='9650.00' and first['total_assets']=='19650.00'
    assert first['positions'][0]['price']=='0.965' and first['positions'][0]['kind']=='etf'
    assert first['unrealized_pnl'] is None
    current=store.account(a['id'])
    edited=store.save_account({**current,'holdings':[{'symbol':'SZ:159278','quantity':'20000'}],
                              'expected_version':current['version']},a['id'])
    assert store.analysis(old['id'])['status']=='stale'
    await service.execute(store.claim_jobs()[0])
    final=store.analyses(a['id'])[0]
    values=store.analysis(final['id'])['result']['metrics']
    assert edited['cash']=='10000' and values['cash']=='10000'
    assert values['known_priced_value']=='19300.00' and values['total_assets']=='29300.00'
    assert values['unrealized_pnl'] is None
    assert final['input_version']==edited['version'] and final['status']=='current'
    assert store.account(b['id'])==before

@pytest.mark.parametrize('source,received,expected',[
    ('2026-10-28T11:29:59+08:00','2026-10-28T11:30:01+08:00',False),
    ('2026-10-28T13:00:02+08:00','2026-10-28T13:00:03+08:00',True),
    ('2026-10-28T13:00:02+08:00','2026-10-28T13:05:03+08:00',False),
    ('2026-10-28T14:00:00+08:00','2026-10-28T13:00:03+08:00',False),
    ('2026-10-28T15:00:00+08:00','2026-10-28T15:00:03+08:00',False),
])
def test_quote_trigger_requires_real_open_session_and_valid_source_time(source,received,expected):
    state=quote_time_state(source,{},stamp=datetime.fromisoformat(received))
    assert state['fresh_for_triggers'] is expected
