"""Round-two deterministic regression/integration checks. No real model claims."""
import asyncio
from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from app.domain import AccountInput, TZ, now
from app.service import ResearchTools, Service
from app.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'round2.sqlite3')
    s.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    return s


def account(store, name='account', quantity='10'):
    return store.save_account(AccountInput(name=name, holdings=[{'symbol':'SH:600519','quantity':quantity}]))


def publish(store, job, **extra):
    covered, offset = [], 0
    while offset is not None:
        page = store.task_demands(job['id'], job['account_id'], limit=50, offset=offset)
        covered.extend(row['id'] for row in page['items'])
        offset = page['next_offset']
    assert store.publish(job['id'], {'report':'deterministic report', 'covered_demand_ids': covered, **extra}, None)


@pytest.mark.asyncio
async def test_whole_trading_day_scan_and_account_demands_no_starvation(store):
    accounts = [account(store, str(n)) for n in range(4)]
    service = Service(store)
    async def noop(*args, **kw):
        return {}
    service.refresh_quotes = noop
    stamps = [datetime(2026,10,22,h,0,tzinfo=TZ)+timedelta(minutes=m)
              for h, minutes in ((9, range(30,150)), (13, range(120))) for m in minutes]
    for stamp in stamps:
        await service.scheduler_tick(stamp)
        await service.scheduler_tick(stamp)  # same-bucket idempotency
        global_run = store.claim_global()
        if global_run:
            store.finish_global(global_run['id'], 'completed', {'report':'shared fixture'})
        store.dispatch_demands()
        for job in store.claim_jobs(2):
            publish(store, job)
    for job in store.claim_jobs(10):
        publish(store, job)
    demands = store.demands(limit=1000)
    scans = [d for d in demands if d['kind']=='candidate_scan']
    assert len(scans) == 48
    assert all(d['state']=='completed' for d in scans)
    for a in accounts:
        scheduled = [d for d in demands if d['account_id']==a['id'] and d['kind']=='scheduled']
        assert len(scheduled) == 16
        assert all(d['state']=='completed' for d in scheduled)
    assert len(store.global_runs(limit=100)) == 48  # shared once, not per account
    await service.stop()


@pytest.mark.asyncio
async def test_busy_deep_research_deferred_demands_survive_restart(store):
    a = account(store)
    active = store.claim_jobs()[0]
    service = Service(store)
    async def noop(*args, **kw):
        return {}
    service.refresh_quotes = noop
    for minute in range(0,31,5):
        await service.scheduler_tick(datetime(2026,10,22,10,minute,tzinfo=TZ))
    assert store.job(active['id'])['state']=='running'
    deferred = store.demands(a['id'])
    assert len(deferred)==3 and all(d['state']=='pending' for d in deferred)
    assert all('running' in d['detail'] for d in deferred)
    publish(store, active)
    reopened = Store(store.path)
    reopened.recover()
    reopened.dispatch_demands()
    later = reopened.claim_jobs()[0]
    assert later['id'] != active['id'] and 'scheduled' in later['reason']
    publish(reopened, later)
    assert all(d['state']=='completed' for d in reopened.demands(a['id']))
    assert len({d['job_id'] for d in reopened.demands(a['id'])})==1
    await service.stop()


def test_waiting_config_resume_only_latest_snapshot_and_no_job_churn(store, monkeypatch):
    a = account(store)
    old = store.claim_jobs()[0]
    store.set_job_state(old['id'], 'waiting_config')
    for i in range(30):
        store.request_analysis(a['id'],'scheduled',f'schedule:{i}')
    assert len(store.jobs(a['id']))==1
    changed = store.save_account(AccountInput(name='new',holdings=[]), a['id'])
    current = store.claim_jobs()[0]
    store.set_job_state(current['id'], 'waiting_config')
    store.recover(config_ready=False)
    assert store.job(current['id'])['state']=='waiting_config'
    store.set_meta('settings', {**store.settings(), 'model_name':'fixture-name','model_key_env':'ROUND2_TEST_KEY'})
    monkeypatch.setenv('ROUND2_TEST_KEY','not-a-real-key')
    from helpers_sixth import configure_test_team
    configure_test_team(store, store.settings())  # Current product config selects a fixture-discovered role binding.
    store.recover(config_ready=Service(store).model_configured())
    assert store.job(old['id'])['state']=='superseded'
    assert store.claim_jobs()[0]['id']==changed['latest_job_id']


@pytest.mark.asyncio
async def test_material_indexes_ranges_search_privacy_and_reversions(store):
    a, b = account(store), account(store,'other')
    tools = ResearchTools(store, store.job(a['latest_job_id']), {})
    for i in range(35):
        store.save_material({'url':f'https://example.test/{i}', 'scope':'public',
                             'content':'x'*20000+'关键事实'+str(i)+'y'*20000,'access':'full_text'})
    index = await tools.list_materials(limit=10)
    assert len(index)==10 and len(json.dumps(index))<30000
    assert all('content' not in item and 'text' not in item for item in index)
    matches = await tools.search_materials('关键事实2')
    assert matches and matches[0]['match_offset']==20000
    part = await tools.read_material_range(matches[0]['id'],start=20000,length=12)
    assert part['text'].startswith('关键事实2') and part['truncated']
    private = await tools.save_material({'content':'private account logic'})
    other = ResearchTools(store,store.job(b['latest_job_id']),{})
    with pytest.raises(KeyError):
        await other.get_material(private['id'])
    original = {'url':'https://example.test/correction','scope':'public','content':'income 10','published_at':'2026-01-01'}
    one=store.save_material(original)
    two=store.save_material({**original,'content':'income 8'})
    again=store.save_material({**original,'content':'income 8'})
    three=store.save_material(original)
    assert [one['version'],two['version'],three['version']]==[1,2,3]
    assert again['_created'] is False and two['previous_id']==one['id']
    assert two['diff']['before']=='income 10' and two['diff']['after']=='income 8'
    assert store.material(one['id'])['content']=='income 10'


@pytest.mark.asyncio
async def test_news_same_url_and_published_at_corrected_body_triggers_review(store, monkeypatch):
    from app import providers
    a = account(store)
    store.set_meta('settings', {**store.settings(), 'news_queries': ['company']})
    async def forbidden(*args, **kwargs):
        raise AssertionError('independent search must never run')
    monkeypatch.setattr(providers, 'search', forbidden)
    service = Service(store)
    first = await service.news_scan()
    assert first['state'] == 'queued'
    assert (await service.news_scan())['demand_id'] == first['demand_id']
    for content in ['old source', 'corrected source']:
        material = store.save_material({'scope': 'public', 'url': 'https://example.test/report',
            'content': content, 'access': 'full_text', 'published_at': '2026-10-22'}, notify=True)
        service.notify_material_changes()
        assert material['id'] in store.pending_material_notifications()
        service.route_material_changes('controlled-public-report', {'information_impacts': [{
            'material_id': material['id'], 'status': 'relevant', 'market_wide': True,
            'explanation': 'Controlled explicit market-wide relevance'}]}, [material['id']])
    assert material['version'] == 2 and material['previous_id']
    assert len([d for d in store.demands(a['id']) if d['kind'] == 'public_information']) == 2


def suggestion(expiry):
    return {'security_id':'SH:600519','action':'add_candidate','basis':'known fixture fact','material_ids':[],
            'price_conditions':[{'field':'price','operator':'between','lower':'9','upper':'11'}],
            'agent_conditions':['盈利逻辑保持成立'],'valid_until':expiry}


def test_recommendation_price_expiry_and_input_change(store):
    stamp=datetime.now(TZ)
    a=account(store)
    job=store.claim_jobs()[0]
    publish(store, job, suggestions=[suggestion((stamp+timedelta(hours=1)).isoformat())])
    rid=store.recommendations(a['id'])[0]['id']
    quote={'SH:600519':{'price':'10','status':'ok','as_of':stamp.isoformat()}}
    store.review_recommendations(quote,stamp)
    assert store.recommendations(a['id'])[0]['state']=='triggered'
    assert len(store.recommendation_events(rid))==2
    store.review_recommendations(quote,stamp)
    assert len(store.recommendation_events(rid))==2
    store.review_recommendations(quote,stamp+timedelta(hours=2))
    assert store.recommendations(a['id'])[0]['state']=='expired'
    follow=store.claim_jobs()[0]
    publish(store,follow,suggestions=[suggestion((stamp+timedelta(days=1)).isoformat())])
    store.save_account(AccountInput(name='changed',holdings=[]),a['id'])
    assert store.recommendations(a['id'])[0]['state']=='needs_review'
    assert store.claim_jobs()[0]['input_version']==2


def test_stale_quotes_cannot_trigger_and_source_updates_are_not_sentiment(store):
    a=account(store)
    publish(store,store.claim_jobs()[0],suggestions=[suggestion(None)])
    store.review_recommendations({'SH:600519':{'price':'10','status':'stale','as_of':now()}})
    assert store.recommendations(a['id'])[0]['state']=='watching'
    store.review_recommendations(material_ids=['corrected-material-id'])
    r=store.recommendations(a['id'])[0]
    assert r['state']=='needs_review' and r['action']=='add_candidate'
    assert 'corrected-material-id' in r['review_reason']


@pytest.mark.asyncio
async def test_quote_reuse_and_preparation_timeout(store, monkeypatch):
    from app.providers import MarketProvider
    count=[]
    async def quote(self, s):
        count.append(s)
        return {'symbol':s,'price':'10','status':'ok','as_of':now(),'fetched_at':now()}
    monkeypatch.setattr(MarketProvider,'quote',quote)
    svc=Service(store)
    await asyncio.gather(svc.refresh_quotes(['SH:600519']),svc.refresh_quotes(['SH:600519']))
    assert count==['SH:600519']
    a=account(store)
    store.set_meta('settings',{**store.settings(),'quote_refresh_timeout_seconds':0.01})
    async def hanging(*args):
        await asyncio.Event().wait()
    async def runner(context,*args):
        return {'report':'research completed using cache'}
    svc.refresh_quotes=hanging
    svc.runner=runner
    await asyncio.wait_for(svc.execute(store.claim_jobs()[0]),1)
    assert store.job(a['latest_job_id'])['state']=='completed'
    assert any(e['type']=='quote_timeout' for e in store.job(a['latest_job_id'],events=True)['events'])


def test_generated_public_research_and_account_fit_are_isolated(store):
    a,b=account(store),account(store,'b','100')
    shared={'kind':'candidate','security_id':'SH:600519','title':'shared','reason':'public change','findings':[{'fact':'x'}]}
    store.save_research_records([shared],'global')
    store.save_research_records([{**shared,'account_fit':'A concentrated'}],'a',a['id'])
    assert any(r.get('account_fit')=='A concentrated' for r in store.research_records(a['id']))
    assert not any(r.get('account_fit') for r in store.research_records(b['id']))
    assert store.research_records(b['id']) == []  # Sixth iteration shares source data, not generated public findings.
    assert store.research_records()[0]['scope'] == 'public'  # Public history remains viewable.


def test_schema1_upgrade_preserves_ids_and_creates_snapshot(store):
    a=account(store)
    m=store.save_material({'content':'old text','scope':'public'})
    with store.connect() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute('DROP TABLE material_catalog')
    upgraded=Store(store.path)
    assert upgraded.meta('schema_version')==6
    assert upgraded.account(a['id'])['version']==1
    assert upgraded.material(m['id'])['content']=='old text'
    assert store.path.with_name(store.path.name+'.before-v2.bak').exists()
    assert upgraded.material_index()[0]['id']==m['id']


def test_expired_report_without_suggestions_schedules_review(store):
    a=account(store)
    publish(store,store.claim_jobs()[0],valid_until=(datetime.now(TZ)-timedelta(seconds=1)).isoformat())
    store.review_recommendations()
    assert store.analyses(a['id'])[0]['status']=='expired'
    assert store.account(a['id'])['analysis_state']=='pending'
    assert len([d for d in store.demands(a['id']) if d['kind']=='report_expired'])==1
    store.review_recommendations()
    assert len([d for d in store.demands(a['id']) if d['kind']=='report_expired'])==1


def test_same_price_condition_new_report_no_infinite_reanalysis(store):
    a=account(store)
    quote={'SH:600519':{'price':'10','status':'ok','as_of':now()}}
    publish(store,store.claim_jobs()[0],suggestions=[suggestion(None)])
    store.review_recommendations(quote)
    follow=store.claim_jobs()[0]
    publish(store,follow,suggestions=[suggestion(None)])
    store.review_recommendations(quote)
    assert store.recommendations(a['id'])[0]['state']=='triggered'
    assert not store.claim_jobs()
    quote['SH:600519']['price']='12'
    store.review_recommendations(quote)
    assert store.recommendations(a['id'])[0]['state']=='watching'
    quote['SH:600519']['price']='10'
    store.review_recommendations(quote)
    assert store.claim_jobs()


def test_malformed_tracking_metadata_keeps_report_and_does_not_break_worker(store):
    a=account(store)
    publish(store,store.claim_jobs()[0],suggestions=[{**suggestion(None),'price_conditions':['bad condition']}])
    store.review_recommendations({'SH:600519':{'price':'10','status':'ok','as_of':now()}})
    assert store.recommendations(a['id'])[0]['state']=='needs_review'
    assert store.analyses(a['id'])[0]['report']=='deterministic report'


@pytest.mark.asyncio
async def test_material_save_cannot_claim_other_accounts_private_id(store):
    a,b=account(store),account(store,'B')
    ta=ResearchTools(store,store.job(a['latest_job_id']),{})
    tb=ResearchTools(store,store.job(b['latest_job_id']),{})
    private=await ta.save_material({'content':'private A'})
    with pytest.raises(KeyError):
        await tb.save_material({'id':private['id'],'content':'try to relink'})
    with pytest.raises(KeyError):
        await tb.get_material(private['id'])


def test_document_hash_not_only_excerpt_detects_later_page_correction(store):
    base={'url':'https://example.test/report.pdf','access':'excerpt','scope':'public','text':'identical first page', 'document_id':'full-byte-hash-v1'}
    one=store.save_material(base, observation_id=store.begin_material_observation(base['url']))
    two=store.save_material({**base,'document_id':'full-byte-hash-v2'}, observation_id=store.begin_material_observation(base['url']))
    assert two['version']==2 and two['previous_id']==one['id']


@pytest.mark.asyncio
async def test_evening_news_scan_independent_of_market_and_cost_bounded(store,monkeypatch):
    store.set_meta('settings',{**store.settings(),'search_provider':'tavily','news_queries':['announcements']})
    svc=Service(store)
    calls=[]
    async def scan():
        calls.append('search attempt')
        raise RuntimeError('source unavailable')
    svc.news_scan=scan
    for _ in range(3):
        await svc.scheduler_tick(datetime(2026,10,24,21,1,tzinfo=TZ))  # weekend evening
        await asyncio.sleep(0)
    assert calls==['search attempt']
    assert store.meta('last_news_scan')['errors']==['RuntimeError']
    assert not store.demands()
    await svc.stop()


def test_repeated_invalid_or_past_report_expiry_does_not_loop(store):
    a=account(store)
    publish(store,store.claim_jobs()[0],valid_until='2020-01-01T00:00:00+08:00')
    store.review_recommendations()
    correction=store.claim_jobs()[0]
    publish(store,correction,valid_until='not-a-date')
    store.review_recommendations()
    assert not store.claim_jobs()
    assert store.account(a['id'])['analysis_state']=='expired'
    assert all(r['status']=='expired' for r in store.analyses(a['id']))


def test_source_update_notification_survives_interruption_before_dispatch(store):
    a=account(store)
    material=store.save_material({'scope':'public','url':'https://example.test/new','content':'correction'},notify=True)
    # Simulate a crash between persistence and notification, then a repeated fetch.
    reopened=Store(store.path)
    assert not reopened.save_material({'scope':'public','url':'https://example.test/new','content':'correction'},notify=True)['_created']
    svc=Service(reopened)
    svc.notify_material_changes()
    assert reopened.pending_material_notifications() == [material['id']]
    svc.route_material_changes('controlled-public-report', {'information_impacts': [{
        'material_id': material['id'], 'status': 'relevant', 'market_wide': True,
        'explanation': 'Controlled explicit market-wide relevance'}]}, [material['id']])
    assert not reopened.pending_material_notifications()
    assert any(material['id'] in d['reason'] for d in reopened.demands(a['id']))
    before=len(reopened.demands())
    svc.notify_material_changes()
    assert len(reopened.demands())==before


def test_global_recovery_has_bounded_attempts(store):
    store.request_analysis(None,'candidate_scan','scan:1')
    one=store.claim_global()
    assert one['attempts']==1
    store.recover()
    two=store.claim_global()
    assert two['attempts']==2
    store.recover()
    assert store.claim_global() is None
    assert store.demands()[0]['state']=='failed'


@pytest.mark.asyncio
async def test_global_runner_reused_by_two_accounts_with_independent_results(store):
    a,b=account(store,'a','10'),account(store,'b','100')
    contexts=[]
    async def runner(context,cfg,tools,emit):
        contexts.append(context)
        if context.get('scope')=='global':
            return {'report':'shared protocol fixture','research_records':[{'kind':'candidate','security_id':'SH:600519','title':'candidate','reason':'shared evidence'}]}
        return {'report':context['account']['holdings'][0]['quantity'], 'research_records':[{'kind':'candidate','security_id':'SH:600519','title':'fit','reason':'different allocation','account_fit':context['account']['name']}]}
    svc=Service(store,runner=runner)
    store.request_analysis(None,'candidate_scan','global:fixture')
    await svc.execute_global(store.claim_global())
    for job in store.claim_jobs():
        await svc.execute(job)
    assert len([c for c in contexts if c.get('scope')=='global'])==1
    assert all(c['shared_research'] == [] for c in contexts if c.get('account'))
    assert store.analyses(a['id'])[0]['report']=='10'
    assert store.analyses(b['id'])[0]['report']=='100'
    assert not any(r.get('account_fit')=='a' for r in store.research_records(b['id']))


def test_global_prose_report_stays_reusable_without_optional_records(store):
    store.request_analysis(None,'candidate_scan','global:prose')
    run=store.claim_global()
    store.finish_global(run['id'],'completed',{'report':'公共研究结论并非原始事实','model_calls':1})
    record=store.research_records()[0]
    material=store.material(record['material_ids'][0])
    assert material['classification']=='agent_analysis'
    assert material['content']=='公共研究结论并非原始事实'
    assert store.global_run(run['id'])['result']['report_material_id']==material['id']


@pytest.mark.asyncio
async def test_same_source_distinct_extractions_are_not_false_corrections(store):
    a=account(store)
    tools=ResearchTools(store,store.job(a['latest_job_id']),{})
    first=await tools.save_material({'url':'https://example.test/report','classification':'fact','content':'收入100'})
    second=await tools.save_material({'url':'https://example.test/report','classification':'fact','content':'利润10'})
    assert first['version']==second['version']==1
    assert first['id']!=second['id'] and second['previous_id'] is None
