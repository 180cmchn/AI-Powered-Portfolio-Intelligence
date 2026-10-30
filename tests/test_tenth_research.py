"""Controlled I/O, real stores/tools. These tests are not real-model reports."""
import asyncio
from datetime import datetime, timedelta
import json
import socket

import httpx
import pytest

from app import providers
from app.service import ResearchTools
from app.store import Store
from app.research_cache import reusable_history


@pytest.fixture(autouse=True)
def clear_caches():
    providers._MARKET_CACHE.clear()
    providers._DOCUMENT_CACHE.clear()
    yield
    providers._MARKET_CACHE.clear()
    providers._DOCUMENT_CACHE.clear()


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize('identity,method,args', [('sz159278','history',{'symbol':'SZ:159278'}),
    ('sh518880','history',{'symbol':'SH:518880'}), ('sh000300','benchmark_history',{'benchmark':'CSI300'})])
async def test_daily_network_fallback_is_whole_snapshot_with_distinct_source(monkeypatch, identity, method, args):
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == 'push2his.eastmoney.com':
            raise httpx.RemoteProtocolError('remote closed', request=request)
        assert request.url.host == 'web.ifzq.gtimg.cn'
        assert request.url.params['param'].startswith(identity + ',day,')
        return httpx.Response(200, json={'code':0, 'data':{identity:{'day':[['2026-10-29','10','11','12','9','123']]}}})
    transport(monkeypatch, handler)
    result = await getattr(providers.MarketProvider({}), method)(**args, start='2026-10-01',end='2026-10-30')
    assert result['status'] == 'ok' and result['source'] == 'tencent:unadjusted-daily'
    assert result['primary_failure']['failure_kind'] == 'remote_protocol'
    assert result['rows'] == [{'time':'2026-10-29','open':'10','close':'11','high':'12','low':'9','volume':'123','amount':None}]
    assert result['adjustment'] == ('index' if method == 'benchmark_history' else 'none')
    assert calls == ['push2his.eastmoney.com','web.ifzq.gtimg.cn']


async def test_adjusted_feed_never_silently_falls_back_to_other_adjustment(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.host)
        raise httpx.RemoteProtocolError('remote closed',request=request)
    transport(monkeypatch, handler)
    result = await providers.MarketProvider({}).history('SZ:159278',adjust='qfq')
    assert result['status'] == 'unavailable' and result['rows'] == []
    assert calls == ['push2his.eastmoney.com']


@pytest.mark.parametrize('response_identity,field', [('sh159278','day'),('sz159278','qfqday')])
async def test_fallback_rejects_wrong_identity_or_adjusted_field(monkeypatch,response_identity,field):
    def handler(request):
        if request.url.host == 'push2his.eastmoney.com':
            raise httpx.ConnectError('failure',request=request)
        return httpx.Response(200,json={'code':0,'data':{response_identity:{field:[['2026-10-29','1','1','1','1','1']]}}})
    transport(monkeypatch,handler)
    result = await providers.MarketProvider({}).history('SZ:159278')
    assert result['status']=='unavailable' and not result['rows']


async def test_endpoint_failure_memory_is_task_local_and_has_finite_retry(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request.url.host)
        raise httpx.RemoteProtocolError('failure',request=request)
    transport(monkeypatch,handler)
    config={'_research_failure_memory':{}}
    market=providers.MarketProvider(config)
    await market.history('SZ:159278')
    await market.history('SH:518880')
    assert len(calls)==2 # one primary and one fallback, not four
    for v in config['_research_failure_memory'].values(): v['at']-=31
    await market.history('SH:562500')
    assert len(calls)==4
    for v in config['_research_failure_memory'].values(): v['at']-=31
    await market.history('SH:688825')
    assert len(calls)==4
    await providers.MarketProvider({'_research_failure_memory':{}}).history('SH:688825')
    assert len(calls)==6


async def test_synthetic_dns_uses_validated_public_doh_and_pinned_tls(monkeypatch):
    async def resolve(url):
        error=providers.DocumentAccessError('non_public_dns','address_validation','synthetic','diagnose')
        error.addresses=['198.18.0.40'];error.hostname='issuer.example'
        raise error
    monkeypatch.setattr(providers,'_resolve_public_url',resolve)
    seen=[]
    def handler(request):
        seen.append(request.url.host)
        if request.url.host=='1.1.1.1':
            assert request.url.params['name']=='issuer.example'
            return httpx.Response(200,json={'Status':0,'Question':[{'name':'issuer.example','type':1}], 'Answer':[{'type':1,'data':'93.184.216.34'}]})
        assert request.url.host=='93.184.216.34'
        assert request.headers['Host']=='issuer.example'
        assert request.extensions['sni_hostname']=='issuer.example'
        return httpx.Response(200,headers={'content-type':'text/html'},text='<article>实际公开正文</article>')
    transport(monkeypatch,handler)
    result=await providers.read_url('https://issuer.example/disclosure',{})
    assert result['access']=='full_text' and '实际公开正文' in result['text']
    assert result['network_diagnostics'][0]['private_address_used'] is False
    assert seen==['1.1.1.1','93.184.216.34']


@pytest.mark.parametrize('addresses,host', [(['127.0.0.1'],'issuer.example'),(['192.168.1.2'],'issuer.example'),
    (['169.254.169.254'],'issuer.example'),(['198.18.0.40','127.0.0.1'],'issuer.example'),(['198.18.0.40'],'198.18.0.40')])
async def test_private_mixed_or_literal_dns_never_uses_fallback(monkeypatch,addresses,host):
    async def resolve(url):
        error=providers.DocumentAccessError('non_public_dns','address_validation','blocked','diagnose')
        error.addresses=addresses;error.hostname=host
        raise error
    async def forbidden(*a,**kw): raise AssertionError('must not resolve through DoH')
    monkeypatch.setattr(providers,'_resolve_public_url',resolve)
    monkeypatch.setattr(providers,'_public_dns_fallback',forbidden)
    result=await providers.read_url('https://issuer.example/report',{})
    assert result['access']=='failed' and result['error_kind']=='non_public_dns'


@pytest.mark.parametrize('answer', ['127.0.0.1','192.168.0.1','198.18.0.40','not-an-address'])
async def test_doh_does_not_legalize_non_public_answers(monkeypatch,answer):
    transport(monkeypatch,lambda request:httpx.Response(200,json={'Status':0,'Question':[{'name':'issuer.example','type':1}], 'Answer':[{'type':1,'data':answer}]}))
    with pytest.raises(ValueError): await providers._public_dns_fallback('issuer.example',{})


async def test_dns_failure_memory_preserves_stage_and_explicit_refresh(monkeypatch):
    calls=[]
    async def fetch(url,config):
        calls.append(url)
        raise providers.DocumentAccessError('public_dns_fallback_failed','dns','failure','diagnose')
    monkeypatch.setattr(providers,'_fetch_public',fetch)
    config={'_research_failure_memory':{}}
    first=await providers.read_url('https://issuer.example/a.pdf',config)
    second=await providers.read_url('https://issuer.example/b.pdf',config)
    assert len(calls)==1 and second['failure_stage']=='dns' and second['returned_chars']==0
    third=await providers.read_url('https://issuer.example/b.pdf',config,refresh=True)
    assert len(calls)==2 and third['access']=='failed'
    assert first['error_kind']==second['error_kind']==third['error_kind']


def save_history(store, *, adjustment='none', symbol='SZ:159278', scope='public', representation=None):
    stamp=datetime.now(providers.SHANGHAI)
    day=(stamp-timedelta(days=1)).strftime('%Y%m%d')
    body={'symbol':symbol, 'interval':'daily','adjustment':adjustment,'status':'ok','source':'eastmoney:public-website',
        'requested_range':{'start':day,'end':day},'fetched_at':stamp.isoformat(), 'as_of':(stamp-timedelta(days=1)).date().isoformat(),
        'rows':[{'time':(stamp-timedelta(days=1)).date().isoformat(),'open':'1','close':'2','high':'2','low':'1'}]}
    material=store.save_material({'source_title':'history','symbol':symbol,'access':'structured_data','scope':scope,
        'representation':representation,'content':json.dumps(body)})
    return material,{'symbol':symbol,'start':day,'end':day,'adjust':adjustment}


def test_saved_raw_cache_identity_adjustment_and_privacy(tmp_path):
    store=Store(tmp_path/'test.sqlite3')
    material,args=save_history(store)
    found=reusable_history(store,'history',args,{})
    assert found[0]['id']==material['id'] and found[1]['cache_stale']
    assert reusable_history(store,'history',{**args,'symbol':'SH:518880'},{}) is None
    assert reusable_history(store,'history',{**args,'adjust':'qfq'},{}) is None
    assert reusable_history(store,'history',args,{'research_provider':'tushare'}) is None
    private,private_args=save_history(store,symbol='SH:562500',scope='account')
    assert reusable_history(store,'history',private_args,{}) is None
    extracted,extracted_args=save_history(store,symbol='SH:688825',representation='agent_extraction')
    assert reusable_history(store,'history',extracted_args,{}) is None


async def test_real_tool_reuses_saved_snapshot_after_refresh_failure_with_freshness(tmp_path,monkeypatch):
    store=Store(tmp_path/'test.sqlite3');material,args=save_history(store)
    async def failed(self,**kw):
        return {'status':'unavailable','symbol':kw['symbol'],'error':'network failed','error_kind':'remote_protocol','failure_stage':'market_data','rows':[]}
    monkeypatch.setattr(providers.MarketProvider,'history',failed)
    tools=ResearchTools(store,{'id':'real-tools','account_id':None},{})
    result=await tools.history(**args)
    assert result['material_id']==material['id'] and result['cache_reused_after_failure']
    assert result['rows'][0]['close']=='2' and result['refresh_failure']['error_kind']=='remote_protocol'
    assert result['cache_fetched_at']==json.loads(material['content'])['fetched_at']
    assert store.material(material['id'])['content']==material['content']


async def test_parallel_consumers_count_one_failed_http_attempt_in_memory(monkeypatch):
    calls=[]
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.01)
        raise httpx.RemoteProtocolError('failure',request=request)
    transport(monkeypatch,handler)
    config={'_research_failure_memory':{}}
    results=await asyncio.gather(*[providers._market_json('https://push2his.eastmoney.com/test',config) for _ in range(8)],return_exceptions=True)
    assert all(isinstance(v,httpx.RemoteProtocolError) for v in results)
    assert len(calls)==1 and config['_research_failure_memory'][('market','https://push2his.eastmoney.com/test')]['attempts']==1


async def test_new_source_classification_is_separate_from_access_and_legacy_retained(tmp_path):
    store=Store(tmp_path/'test.sqlite3');tools=ResearchTools(store,{'id':'test','account_id':None},{})
    rows=await tools.record_native_search('manager',[],[{'url':'https://issuer.example/a'},{'url':'https://issuer.example/b','snippet':'actual excerpt'}],[])
    assert [r['access'] for r in rows]==['source_link','search_excerpt']
    assert all(r['classification']=='unclassified' and r['source_type']=='native_search_source' for r in rows)
    old=store.save_material({'classification':'uncertain','url':'https://issuer.example/old','content':'old'})
    assert store.save_material(old)['classification']=='uncertain'
    rumor=await tools.save_material({'classification':'rumor','content':'explicit claim','material_ids':[rows[0]['id']]})
    assert rumor['classification']=='rumor' and rumor['source_type']=='model_extraction'


def test_exact_builtin_prompt_upgrade_preserves_effort_custom_and_frozen(tmp_path):
    from pathlib import Path
    store=Store(tmp_path/'test.sqlite3')
    prior=(Path(__file__).with_name('fixtures')/'ninth-manager-prompt.txt').read_text()
    with store.connect() as db:
        role=json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role['system_prompt']=prior; role['default_binding']={'reasoning_effort':'max'}
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'",(json.dumps(role),))
        db.execute("DELETE FROM meta WHERE key='tenth_role_migration'")
        store.migrate_tenth_roles(db)
        updated=json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        assert '本轮结论' in updated['system_prompt'] and updated['default_binding']['reasoning_effort']=='max'
        assert updated['version']==role['version']+1 and updated['prompt_version']==role['prompt_version']+1
        version=updated['version'];store.migrate_tenth_roles(db)
        assert json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])['version']==version
        updated['system_prompt']='custom';updated['custom']=True
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'",(json.dumps(updated),))
        db.execute("DELETE FROM meta WHERE key='tenth_role_migration'");store.migrate_tenth_roles(db)
        assert json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])['system_prompt']=='custom'
