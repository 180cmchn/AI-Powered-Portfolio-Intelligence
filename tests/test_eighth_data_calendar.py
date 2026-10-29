"""Production tools/SQLite, controlled prices and HTTP. Not real market research."""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
import os
import subprocess
import sys

import httpx
import pytest
from app.calendar import SHANGHAI, market_state
from app.providers import quote_time_state
from app.service import ResearchTools
from app.store import Store
from app import providers


def account(store):
    return store.save_account({'name':'isolated','cash':'10000','holdings':[{'symbol':'SZ:159278','quantity':'10000'}], 'preferences':{}})


def clock(monkeypatch, stamp):
    current = [stamp]
    for module in ('app.store', 'app.forecasts', 'app.forecast_windows'):
        monkeypatch.setattr(module+'.now', lambda: current[0].isoformat())
    return current


def calendar(path, first, last, overrides=None):
    path.write_text(json.dumps({'coverage':{'start':first,'end':last},'holidays':[], 'overrides':overrides or {}, 'source':'verified-fixture-exchange','version':'unchanged-label'}))


def test_old_unknown_rechecked_after_newer_record_and_revision_history(tmp_path,monkeypatch):
    at=clock(monkeypatch,datetime(2026,10,28,10,tzinfo=SHANGHAI))
    p=tmp_path/'calendar.json';calendar(p,'2026-10-29','2026-12-31')
    s=Store(tmp_path/'db');a=account(s);s.save_forecast_settings(a['id'],{'enabled':True})
    at[0]=at[0].replace(hour=19);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    first=s.forecast_window_runs()['items'][0]
    assert first['status']=='calendar_unknown'
    at[0]+=timedelta(days=1);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    assert s.forecast_window_runs()['items'][0]['status']=='missed'
    calendar(p,'2026-10-01','2026-12-31');at[0]+=timedelta(days=1)
    s=Store(s.path);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    old=next(x for x in s.forecast_window_runs()['items'] if x['planned_date']=='2026-10-28')
    assert old['status']=='missed' and old['discovered_at']==first['discovered_at']
    assert old['detail']['gap_discovered_at']==first['detail']['gap_discovered_at']
    assert old['revisions'][0]['previous_status']=='calendar_unknown'
    assert old['revisions'][0]['reason']=='calendar_reconciled'
    count=old['revision_count'];s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    assert next(x for x in s.forecast_window_runs()['items'] if x['planned_date']=='2026-10-28')['revision_count']==count
    assert s.forecasts_page()['total']==0
    # A temporary closure correction changes history even with the same version label.
    calendar(p,'2026-10-01','2026-12-31',{'2026-10-28':False});s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    old=next(x for x in s.forecast_window_runs()['items'] if x['planned_date']=='2026-10-28')
    assert old['status']=='non_trading_day' and old['revision_count']==count+1
    assert s.forecasts_page()['total']==0


def test_long_unknown_backlog_bounded_restart_and_unknown_history(tmp_path,monkeypatch):
    at=clock(monkeypatch,datetime(2026,12,20,19,tzinfo=SHANGHAI))
    p=tmp_path/'calendar.json';calendar(p,'2026-12-31','2026-12-31')
    s=Store(tmp_path/'db');a=account(s);s.save_forecast_settings(a['id'],{'enabled':True})
    # No enabled interval at initial day's already elapsed window.
    at[0]=datetime(2027,2,20,19,tzinfo=SHANGHAI);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    assert s.forecast_window_statistics()['states']['calendar_unknown']>32
    calendar(p,'2026-12-01','2027-12-31');s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    assert s.forecast_window_statistics()['states'].get('calendar_unknown',0)>0
    for _ in range(3):
        s=Store(s.path);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    assert s.forecast_window_statistics()['states'].get('calendar_unknown',0)==0
    old=next(x for x in s.forecast_window_runs(limit=200)['items'] if x['planned_date']=='2026-12-20')
    assert old['status']=='non_trading_day' # Sunday; no guesses outside coverage.
    assert s.forecasts_page()['total']==0


def test_calendar_fill_does_not_invent_old_enabled_configuration(tmp_path,monkeypatch):
    at=clock(monkeypatch,datetime(2026,10,28,19,tzinfo=SHANGHAI))
    p=tmp_path/'calendar.json';calendar(p,'2026-10-29','2026-12-31')
    s=Store(tmp_path/'db');a=account(s);s.save_forecast_settings(a['id'],{'enabled':True})
    s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    calendar(p,'2026-10-01','2026-12-31');at[0]+=timedelta(days=2);s.reconcile_forecast_windows(at=at[0],calendar_path=p)
    old=next(x for x in s.forecast_window_runs()['items'] if x['planned_date']=='2026-10-28')
    assert old['status']=='outside_enabled_window' and old['expected_count'] is None
    assert s.forecasts_page()['total']==0


@pytest.mark.parametrize('hour,minute,session,continuous,closed',[(9,29,'pre_open',False,False),(9,30,'morning',True,False),(11,29,'morning',True,False),(11,30,'lunch_break',False,False),(12,59,'lunch_break',False,False),(13,0,'afternoon',True,False),(14,59,'afternoon',True,False),(15,0,'after_close',False,True),(15,30,'after_close',False,True),(18,0,'after_close',False,True)])
def test_session_semantics_compatibility(hour,minute,session,continuous,closed):
    at=datetime(2026,10,29,hour,minute,tzinfo=SHANGHAI);s=market_state(at)
    assert s['session']==session and s['is_continuous_trading']==continuous and s['has_closed_today']==closed
    assert s['phase']==(session if continuous else 'closed')
    quote=quote_time_state(at.isoformat(),{},stamp=at)
    assert quote['fresh_for_triggers']==continuous and quote['market_session']==session
    assert quote['quote_kind']!='last_close' or closed


def test_unknown_calendar_is_not_nontrading_and_never_trigger(tmp_path):
    p=tmp_path/'calendar';calendar(p,'2026-10-01','2026-10-28')
    at=datetime(2026,10,29,13,tzinfo=SHANGHAI)
    s=market_state(at,p);q=quote_time_state(at.isoformat(),{'calendar_path':str(p)},stamp=at)
    assert s['trading_day'] is None and s['session']=='unknown' and s['has_closed_today'] is None
    assert not q['fresh_for_triggers']


def fixture_rows(n=300):
    return [{'time':(date(2025,1,1)+timedelta(days=i)).isoformat(),'open':str(i+100),'close':str(i+101),
             'high':str(i+102),'low':str(i+99),'volume':'100000','amount':'1000000'} for i in range(n)]


@pytest.mark.asyncio
async def test_long_market_snapshot_latest_exact_page_and_reproducible_alignment(tmp_path,monkeypatch):
    rows=fixture_rows()
    async def history(self,**kwargs):
        return {'symbol':kwargs['symbol'],'status':'ok','rows':rows,'source':'fixture','fetched_at':'2026-10-29T12:00:00+08:00',
                'as_of':rows[-1]['time'],'interval':'daily','adjustment':'none','truncated':False}
    monkeypatch.setattr(providers.MarketProvider,'history',history)
    s=Store(tmp_path/'db');a=account(s);job=s.job(a['latest_job_id']);t=ResearchTools(s,job,{'max_tool_result_chars':6000})
    first=await t.history('SZ:159278',limit=1000)
    mid=first['material_id'];assert len(s.material(mid)['content'])>16000
    assert first['rows'][0]['time']==rows[-1]['time'] and len(json.dumps(first,ensure_ascii=False))<=6000
    assert first['has_more'] and first['row_count']>0
    seen=list(first['rows']);page=first
    while page['has_more']:
        page=await t.query_market_rows(mid,offset=page['next_offset'],limit=100)
        seen.extend(page['rows'])
    assert len(seen)==300 and len({r['time'] for r in seen})==300
    exact=await t.query_market_rows(mid,start=rows[100]['time'],end=rows[100]['time'],fields=['close'])
    assert exact['rows']==[{'time':rows[100]['time'],'close':rows[100]['close']}]
    missing=await t.query_market_rows(mid,start='2026-10-29',end='2026-10-29')
    assert missing['rows']==[] and missing['missing_exact_date']=='2026-10-29'
    other={**history.__dict__,'benchmark':'CSI300','status':'ok','rows':[{'time':r['time'],'close':str(100+i)} for i,r in enumerate(rows[-21:]) if i!=5],
           'source':'fixture','interval':'daily','adjustment':'index'}
    b=s.save_material({'content':json.dumps(other),'access':'structured_data','scope':'public','source':'fixture'})
    result=await t.market_indicators(mid,start=rows[-21]['time'],end=rows[-1]['time'],window=20,benchmark_material_id=b['id'])
    assert result['comparison_status']=='ok' and result['aligned_samples']==20
    assert Decimal(result['relative_return'])==Decimal(rows[-1]['close'])/Decimal(rows[-21]['close'])-Decimal(120)/Decimal(100)
    assert result['comparison_input_closes'][5][0]==rows[-15]['time']
    assert Decimal(result['moving_average'])==sum(Decimal(r['close']) for r in rows[-20:])/20


@pytest.mark.asyncio
async def test_market_permissions_adjustment_and_calculation_bounds(tmp_path):
    s=Store(tmp_path/'db');a=account(s);t=ResearchTools(s,s.job(a['latest_job_id']),{'max_tool_result_chars':6000})
    def saved(adjust='none',scope='public',account_id=None):
        return s.save_material({'content':json.dumps({'rows':fixture_rows(),'interval':'daily','adjustment':adjust,'symbol':'SZ:159278'}),
                                'access':'structured_data','scope':scope},account_id=account_id)
    m=saved();b=saved('qfq')
    limited=await t.market_indicators(m['id']);assert limited['reason']=='range_too_large'
    res=await t.market_indicators(m['id'],start=fixture_rows()[-10]['time'],benchmark_material_id=b['id'])
    assert res['comparison_status']=='incompatible_adjustment_or_interval'
    with pytest.raises(ValueError):await t.query_market_rows(m['id'],symbol='SH:510300')
    private=saved(scope='account',account_id='different-account')
    with pytest.raises(KeyError):await t.query_market_rows(private['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['denied','nonpdf','parse','dns','empty','page'])
async def test_document_failure_stage_is_actionable_and_never_fulltext(monkeypatch,kind):
    from test_provider_documents import pdf_bytes
    providers._DOCUMENT_CACHE.clear()
    async def fetch(url,config):
        if kind=='denied':
            raise httpx.HTTPStatusError('secret response',request=httpx.Request('GET',url),response=httpx.Response(403))
        if kind=='dns':
            raise providers.DocumentAccessError('non_public_dns','address_validation','研究工具不能访问本机、内网或保留地址','核对DNS')
        if kind=='nonpdf':return b'<article>download <a href="/actual.pdf">attachment</a></article>','text/html',url
        if kind=='parse':return b'%PDF-broken', 'application/pdf',url
        return pdf_bytes(1,blank_page=1 if kind=='empty' else None),'application/pdf',url
    monkeypatch.setattr(providers,'_fetch_public',fetch)
    r=await providers.read_url('https://issuer.example/report.pdf',{},page=3 if kind=='page' else 1)
    expected={'denied':'access_denied','nonpdf':'non_pdf_response','parse':'extraction_failed','dns':'non_public_dns','empty':'no_extractable_text','page':'page_out_of_range'}
    assert r['access']=='failed' and r['text'] is None and r['returned_chars']==0
    assert r['error_kind']==expected[kind] and r['failure_stage'] and r['next_action']
    assert 'secret response' not in json.dumps(r)
    if kind=='nonpdf':assert r['attachments'][0]['url']=='https://issuer.example/actual.pdf'


def test_import_main_never_initializes_database_and_start_provenance(tmp_path):
    path=tmp_path/'must-not-exist.sqlite3'
    env={**os.environ,'APP_DB_PATH':str(path),'APP_MODE':'live'}
    subprocess.run([sys.executable,'-c','import app.main; assert app.main.app._application is None'],env=env,check=True)
    assert not path.exists()
    from app.main import create_app
    app=create_app(tmp_path/'independent.sqlite3',worker=False)
    v=app.state.runtime_version
    assert v['schema_version']==8 and len(v['source_sha256_at_start'])==64 and v['started_at']


def test_forward_cursor_drains_over_128_days_despite_today_already_recorded(tmp_path, monkeypatch):
    at = clock(monkeypatch, datetime(2026, 1, 2, 10, tzinfo=SHANGHAI))
    p = tmp_path / 'calendar.json'; calendar(p, '2026-01-01', '2027-12-31')
    store = Store(tmp_path / 'bounded.sqlite3'); a = account(store)
    store.save_forecast_settings(a['id'], {'enabled': True})
    at[0] = datetime(2027, 2, 1, 19, tzinfo=SHANGHAI)
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    first = store.forecast_window_runs(limit=200)
    assert first['total'] == 129  # 128 forward dates plus today's independent observation.
    assert first['items'][0]['planned_date'] == '2027-02-01'
    for _ in range(3):
        store = Store(store.path)
        store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    total_days = (at[0].date() - date(2026, 1, 2)).days + 1
    assert store.forecast_window_runs()['total'] == total_days
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecast_window_runs WHERE planned_date BETWEEN '2026-05-10' AND '2026-12-31'").fetchone()[0] == 236
    assert store.forecasts_page()['total'] == 0


def test_unresolved_old_window_obeys_backoff_and_retains_first_gap(tmp_path, monkeypatch):
    at = clock(monkeypatch, datetime(2026, 10, 28, 10, tzinfo=SHANGHAI))
    p = tmp_path / 'calendar.json'; calendar(p, '2026-10-29', '2026-12-31')
    store = Store(tmp_path / 'backoff.sqlite3'); a = account(store)
    store.save_forecast_settings(a['id'], {'enabled': True})
    at[0] = at[0].replace(hour=19)
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    at[0] += timedelta(days=2)
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    with store.connect() as db:
        before = dict(db.execute("SELECT * FROM forecast_window_checks WHERE planned_date='2026-10-28'").fetchone())
    for _ in range(3):
        store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    with store.connect() as db:
        after = dict(db.execute("SELECT * FROM forecast_window_checks WHERE planned_date='2026-10-28'").fetchone())
    assert after == before
    at[0] = datetime.fromisoformat(before['next_check'])
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    with store.connect() as db:
        retried = dict(db.execute("SELECT * FROM forecast_window_checks WHERE planned_date='2026-10-28'").fetchone())
    assert retried['attempts'] == before['attempts'] + 1
    old = next(r for r in store.forecast_window_runs()['items'] if r['planned_date'] == '2026-10-28')
    assert old['revision_count'] == 1 and old['status'] == 'calendar_unknown'


def test_existing_batch_calendar_conflict_retains_ledger_and_execution_fact(tmp_path, monkeypatch):
    at = clock(monkeypatch, datetime(2026, 10, 28, 16, tzinfo=SHANGHAI))
    p = tmp_path / 'calendar.json'; calendar(p, '2026-10-01', '2026-12-31')
    store = Store(tmp_path / 'conflict.sqlite3'); a = account(store)
    store.save_forecast_settings(a['id'], {'enabled': True})
    batch = store.create_forecast_batch(a['id'], at=at[0], calendar_path=p)
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    initial = store.forecast_window_runs()['items'][0]
    assert initial['status'] == 'batch_created'
    calendar(p, '2026-10-01', '2026-12-31', {'2026-10-28': False})
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    conflict = store.forecast_window_runs()['items'][0]
    assert conflict['status'] == 'calendar_conflict'
    assert conflict['detail']['batch_execution_status'] == 'batch_created'
    assert conflict['batch_id'] == batch['id'] and conflict['expected_count'] == 3
    assert conflict['discovered_at'] == initial['discovered_at']
    assert conflict['revisions'][0]['previous_status'] == 'batch_created'
    stats = store.forecast_window_statistics()
    assert stats['calendar_conflict_windows'] == 1 and stats['known_expected_windows'] == 0
    assert store.forecasts_page()['total'] == 3
    calendar(p, '2026-10-29', '2026-12-31')
    store.reconcile_forecast_windows(at=at[0], calendar_path=p)
    unknown = store.forecast_window_runs()['items'][0]
    assert unknown['status'] == 'calendar_unknown' and unknown['batch_id'] == batch['id']
    assert unknown['expected_count'] == 3 and store.forecasts_page()['total'] == 3


def test_schema8_backup_idempotent_preserves_custom_role_and_immutable_ledger(tmp_path):
    import sqlite3
    s=Store(tmp_path/'upgrade.sqlite3');a=account(s)
    role=s.list_agent_roles()[0]
    custom_tools=['read','finish_analysis'] if role['role_id']=='manager' else ['read','submit_report']
    s.update_agent_definition(role['role_id'],{'system_prompt':'User-authored unchanged instructions', 'tools':custom_tools})
    before_role=next(r for r in s.list_agent_roles() if r['role_id']==role['role_id'])
    with s.connect() as db:
        db.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
        db.execute("DELETE FROM meta WHERE key='eighth_role_migration'")
    upgraded=Store(s.path)
    backup=s.path.with_name(s.path.name+'.before-v8.bak')
    assert backup.exists() and upgraded.meta('schema_version')==8
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='7'
        assert db.execute('SELECT id FROM accounts').fetchone()[0]==a['id']
    assert next(r for r in upgraded.list_agent_roles() if r['role_id']==role['role_id'])==before_role
    raw=backup.read_bytes();Store(s.path)
    assert backup.read_bytes()==raw
    with upgraded.connect() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='forecasts_immutable'").fetchone()[0]==1
