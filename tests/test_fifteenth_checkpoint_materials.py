"""Durable manager-visible evidence; real API/Service/Runtime, offline HTTP."""
import json
import hashlib
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from app import agents, providers
from app.main import create_app
from app.model import ToolCall
from app.domain import AccountInput
from app.service import ResearchTools
from app.context import recall
from app.recovery import checkpoint_material_evidence_issues, restored_manager_material_evidence
from app.store import dumps
from tests.delivery_helpers import delivery_arguments
from tests.test_fourth_recovery import store, install, response, call


def financial_rows():
    return {'symbol': 'SZ:002354', 'amount_unit': '人民币元', 'source': 'fixture:income',
        'source_url': 'https://issuer.example/income', 'status': 'ok',
        'as_of': '2026-10-29', 'fetched_at': '2026-11-02T15:10:00+08:00',
        'truncated': True, 'coverage_note': '供应商累计利润表；不是原始财报全文。',
        'rows': [dict(report_period=p, report_type='001', revenue=r, parent_net_profit=n)
                 for p, r, n in [('2026-09-30', '1470.25', '50.25'),
                                  ('2026-06-30', '1090.10', '40.10'),
                                  ('2025-09-30', '1500.50', '42.50'),
                                  ('2025-06-30', '980.20', '23.20')]]}


@pytest.mark.asyncio
@pytest.mark.parametrize('arithmetic', [True, False])
async def test_api_interruption_restores_actual_fundamentals_in_final_http(store, monkeypatch, arithmetic):
    async def fundamentals(self, symbol, **kwargs):
        assert symbol == 'SZ:002354'
        return deepcopy(financial_rows())
    monkeypatch.setattr(providers.MarketProvider, 'fundamentals', fundamentals)
    app = create_app(store.path, mode='live', worker=False)
    actual = app.state.store
    actual._credential_store = store._credential_store
    sent = []
    def handler(request):
        payload = json.loads(request.content); sent.append(payload)
        seed = json.loads(payload['input'][0]['content'])
        if seed.get('synthesis_checkpoint'):
            visible = json.dumps(payload, ensure_ascii=False)
            assert '供应商累计利润表' in visible and '2026-09-30' in visible, 'completed financial rows lost in recovery'
            assert '1470.25' in visible and 'SZ:002354' in visible and '人民币元' in visible
            assert 'visible_result_hash' not in visible and 'material_deliveries' not in visible
            final = next(t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis')
            assert [{k:v for k,v in t.items() if k not in {'type','strict'}} for t in payload['tools']] == [final]
            return response(call('finish_analysis', delivery_arguments(report='Controlled limited report from restored visible income rows.')))
        if len(sent) == 1:
            return response(call('fundamentals', {'symbol':'SZ:002354'}))
        if len(sent) == 2 and arithmetic:
            return response(*[{**call('calculate', {'operation':'subtract','values':values}), 'call_id':'calc-'+str(i)}
                for i,values in enumerate([['1470.25','1090.10'],['50.25','40.10'],['1500.50','980.20'],['42.50','23.20']])])
        raise httpx.RemoteProtocolError('controlled interrupted response', request=request)
    install(monkeypatch, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        created = await api.post('/api/accounts', json={'name':'Visible recovery fixture','cash':'10000','holdings':[], 'research_scope':'cash_only'})
        assert created.status_code == 201, created.text
        account = created.json()
        first = actual.claim_jobs(account_id=account['id'])[0]
        await app.state.service.execute(first)
        assert actual.job(first['id'])['state'] == 'failed' and not actual.analyses(account['id']), actual.job(first['id']).get('error')
        with actual.connect() as db:
            saved = dict(db.execute('SELECT body,created_at FROM synthesis_checkpoints WHERE job_id=?', (first['id'],)).fetchone())
        cp = json.loads(saved['body'])
        assert len(cp['calculations']) == (4 if arithmetic else 0) and cp['manager_summary'] == {}
        receipt = next(x for x in cp['material_deliveries'] if x['tool'] == 'fundamentals')
        assert receipt['material_id'] in cp['verified_material_versions']
        assert receipt['material_id'] in cp['material_ids']
        assert receipt['body']['visible_result']['rows'] == financial_rows()['rows']
        assert hashlib.sha256(dumps(receipt['body']['visible_result']).encode()).hexdigest() == receipt['body']['visible_result_hash']
        retried = await api.post('/api/jobs/'+first['id']+'/retry', json={'research_scope':'cash_only'})
        assert retried.status_code == 200, retried.text
        second = actual.claim_jobs(account_id=account['id'])[0]
        await app.state.service.execute(second)
        assert '供应商累计利润表' in json.dumps(sent[-1], ensure_ascii=False), 'completed financial rows lost in recovery HTTP payload'
        assert actual.job(second['id'])['state'] == 'completed', actual.job(second['id']).get('error')
        assert len(sent) == (4 if arithmetic else 3) and actual.analyses(account['id'])[0]['result']['synthesis_recovered']
        with actual.connect() as db:
            after = db.execute('SELECT body,created_at FROM synthesis_checkpoints WHERE job_id=?', (first['id'],)).fetchone()
        assert dict(after) == saved  # Runtime consumption does not rewrite evidence/clock.


def saved_case(store, actor='manager', *, indexed=False):
    account = store.save_account(AccountInput(name='Material boundary', holdings=[], research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    context = {'scope':'account', 'account':account, 'input_version':account['version'],
        'universe_version':store.universe_snapshot()['universe_version'], 'metrics':{'positions':[]},
        'quotes':{'SZ:002354':{'price':'10','as_of':'2026-11-02T15:00:00+08:00','source':'fixture','status':'closed'}}}
    material = store.save_material({'scope':'account', 'url':'https://issuer.example/range',
        'text':'P'*1000+'VISIBLE_RANGE'+'Q'*1000+'NEVER_DELIVERED_SUFFIX', 'access':'full_text'}, job['id'], account['id'])
    visible = store.material_range(material['id'], start=1000, length=13)
    receipts = store.record_material_delivery(job['id'], account['id'], actor, 'read_material_range', visible)
    body = {'phase':'synthesis','task_id':job['id'],'account_id':account['id'],
        'input_version':account['version'],'reports':[],'calculations':[],'manager_summary':{},
        'material_ids':[material['id']] if indexed else []}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job['id'], 'failed', 'controlled interruption')
    return account, job, context, material, visible, receipts


def checkpoint_row(store, job):
    with store.connect() as db:
        return dict(db.execute('SELECT body,guard,created_at,resume_count FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone())


@pytest.mark.asyncio
async def test_partial_visible_material_rebuilds_local_archive_and_bounded_projection(store, monkeypatch):
    account, job, context, material, visible, _ = saved_case(store)
    cp = json.loads(checkpoint_row(store, job)['body'])
    cp['recovery_mode'] = 'synthesis_only'
    # A stale archive capability in audit metadata must never be used as a new
    # session pointer. Only the saved visible object is installed in its archive.
    cp['material_deliveries'][0]['body']['old_record_id'] = 'context-999'
    install(monkeypatch, lambda r: pytest.fail('No HTTP in archive inspection'))
    async def emit(event): pass
    runtime = agents.ResearchRuntime({**context,'job_id':job['id'],'synthesis_checkpoint':cp},
        store.settings(), ResearchTools(store,job,store.settings(),context), emit)
    try:
        index = runtime.context['restored_material_index'][0]
        assert index['retrieve']['record_id'] != 'context-999'
        recovered = json.loads(recall(runtime.manager_session, record_id=index['retrieve']['record_id'])['text'])
        assert recovered['result'] == visible
        assert recovered['result']['truncated'] and recovered['result']['start'] == 1000
        with pytest.raises(ValueError): recall(runtime.manager_session, record_id='context-999')
        await runtime._enter_synthesis_context(runtime.manager_session, reason='bounded_final_request')
        payload = json.dumps(runtime.manager_session.history)
        assert 'VISIBLE_RANGE' in payload and 'NEVER_DELIVERED_SUFFIX' not in payload
        assert 'visible_result_hash' not in payload and 'material_deliveries' not in payload
        assert len(payload) < int(store.settings().get('max_context_chars',240000) * .78)
    finally: await runtime.close()


def test_child_read_and_unread_source_link_are_not_manager_content(store):
    account, job, context, material, _, _ = saved_case(store, actor='unfinished-child')
    body = json.loads(checkpoint_row(store, job)['body'])
    assert material['id'] not in body['material_ids'] and not restored_manager_material_evidence(body)
    link = store.save_material({'scope':'public','url':'https://issuer.example/index',
        'representation':'native_search_source','access':'source_link','content':''},job['id'],account['id'])
    receipts = store.record_material_delivery(job['id'],account['id'],'manager','native_search',{'material_id':link['id'],'visible_excerpt':''})
    assert receipts
    body['material_ids'].append(link['id'])
    store.save_synthesis_checkpoint(job,body,context)
    body = json.loads(checkpoint_row(store, job)['body'])
    assert not restored_manager_material_evidence(body)
    assert {x['status'] for x in body['source_coverage'].values()} == {'index_only'}
    assert 'visible_result' not in next(x for x in body['material_deliveries'] if x['id'] in receipts)['body']


def test_cross_account_and_context_wrapper_never_create_content_receipt(store):
    account, job, context, material, _, _ = saved_case(store)
    other = store.save_account(AccountInput(name='Other material owner', holdings=[], research_scope='cash_only'))
    private = store.save_material({'scope':'account','text':'OTHER_ACCOUNT_PRIVATE','url':'https://issuer.example/private'},account_id=other['id'])
    assert store.record_material_delivery(job['id'], account['id'], 'manager', 'get_material',
        {'material_id':private['id'],'text':'OTHER_ACCOUNT_PRIVATE'}) == []
    assert store.record_material_delivery(job['id'], account['id'], 'manager', 'get_material',
        {'material_id':material['id'],'truncated_for_context':True,'context_excerpt':'visible prefix',
         'record_id':'context-999','total_chars':99999}) == []
    cp = json.loads(checkpoint_row(store,job)['body'])
    assert 'OTHER_ACCOUNT_PRIVATE' not in dumps(restored_manager_material_evidence(cp))


@pytest.mark.parametrize('mutation', ['hash','missing_guard','legacy_body'])
def test_bad_or_legacy_payload_never_backfills_original_material(store, mutation):
    _, job, context, material, _, _ = saved_case(store)
    row = checkpoint_row(store,job); body = json.loads(row['body'])
    proof = body['material_deliveries'][0]['body']
    if mutation == 'hash': proof['visible_result']['text'] += 'UNSAVED_EXTRA'
    else:
        proof.pop('visible_result'); proof.pop('visible_result_version')
        if mutation == 'missing_guard':
            body['material_ids']=[];body['verified_material_versions']={};body['source_coverage']={}
    with store.connect() as db:
        db.execute('UPDATE synthesis_checkpoints SET body=? WHERE job_id=?',(dumps(body),job['id']))
    before = checkpoint_row(store,job)
    assert not restored_manager_material_evidence(body)
    diagnostic = store.checkpoint_recovery_diagnostic(job,context)
    if mutation == 'legacy_body':
        assert diagnostic['eligible']  # Guarded legacy data stays readable; no fabricated body.
    else:
        assert diagnostic['state']=='invalidated' and not diagnostic['eligible']
        assert checkpoint_material_evidence_issues(body)
        assert store.reusable_synthesis_checkpoint({**job,'parent_job_id':job['id']},context,continue_research=True) is None
        assert store.checkpoint_status(job['id'])['recovery_state']=='needs_research'
    assert checkpoint_row(store,job)==before  # No read-time repair, TTL refresh or attempt use.


@pytest.mark.parametrize('change', ['quote','account','ttl','access','new_version'])
def test_manager_material_restore_retains_current_input_guards(store, monkeypatch, change):
    account, job, context, material, _, _ = saved_case(store)
    source_job = deepcopy(job); before = checkpoint_row(store,job)
    if change == 'quote': context['quotes']['SZ:002354']['price']='11'
    elif change == 'account':
        account = store.save_account(AccountInput(id=account['id'],version=account['version'],name='changed',cash='99',holdings=[],research_scope='cash_only'))
        context['account']=account;job=store.job(account['latest_job_id'])
    elif change == 'ttl':
        import app.recovery as recovery
        original = recovery.now_datetime
        monkeypatch.setattr(recovery,'now_datetime',lambda tz: original(tz)+timedelta(hours=2))
    elif change == 'access':
        with store.connect() as db:
            db.execute("UPDATE material_catalog SET account_id='unavailable-owner' WHERE material_id=?",(material['id'],))
    else:
        store.save_material({'scope':'account','url':material['url'],'text':'Corrected new version','access':'full_text'},source_job['id'],source_job['account_id'])
    assert store.reusable_synthesis_checkpoint(job,context) is None
    assert checkpoint_row(store,source_job)==before


def test_delivered_revision_requires_explicit_correction_handling(store):
    account, job, context, material, _, _ = saved_case(store)
    latest = store.save_material({'scope':'account','url':material['url'],'text':'Corrected new version','access':'full_text'},job['id'],account['id'])
    store.record_material_delivery(job['id'],account['id'],'manager','get_material',store.material_range(latest['id']))
    body=json.loads(checkpoint_row(store,job)['body'])
    store.save_synthesis_checkpoint(job,body,context)
    body=json.loads(checkpoint_row(store,job)['body'])
    assert {material['id'],latest['id']} <= set(body['verified_material_versions'])
    assert body['recovery_state']=='needs_research'
    assert any(v['reason']=='historical_comparison_not_processed' for v in body['source_coverage'].values())
    assert store.reusable_synthesis_checkpoint(job,context) is None
    resumed=store.reusable_synthesis_checkpoint({**job,'parent_job_id':job['id']},context,continue_research=True)
    assert resumed['recovery_mode']=='continue_research'
    assert len(restored_manager_material_evidence(resumed))==2


def test_public_material_does_not_make_other_account_checkpoint_reusable(store):
    account, job, context, _, _, _ = saved_case(store)
    public=store.save_material({'scope':'public','url':'https://issuer.example/shared','text':'shared fact'})
    store.record_material_delivery(job['id'],account['id'],'manager','get_material',store.material_range(public['id']))
    store.save_synthesis_checkpoint(job,json.loads(checkpoint_row(store,job)['body']),context)
    other=store.save_account(AccountInput(name='different account',holdings=[],research_scope='cash_only'))
    other_job=store.claim_jobs(account_id=other['id'])[0]
    assert store.material_accessible(public['id'],other['id'])
    before=checkpoint_row(store,job)
    assert store.reusable_synthesis_checkpoint(other_job,{**context,'account':other}) is None
    assert checkpoint_row(store,job)==before


@pytest.mark.asyncio
async def test_many_restored_results_keep_bounded_indexes_and_retrievable_visible_objects(store, monkeypatch):
    account, job, context, _, _, _ = saved_case(store)
    material=store.save_material({'scope':'account','url':'https://issuer.example/long',
        'text':'visible structured range '*1000},job['id'],account['id'])
    for start in range(0,12000,1000):
        store.record_material_delivery(job['id'],account['id'],'manager','read_material_range',
            store.material_range(material['id'],start=start,length=1000))
    body=json.loads(checkpoint_row(store,job)['body'])
    store.save_synthesis_checkpoint(job,body,context)
    cp=json.loads(checkpoint_row(store,job)['body']);cp['recovery_mode']='synthesis_only'
    cfg={**store.settings(),'max_context_chars':20000}
    install(monkeypatch,lambda r:pytest.fail('Offline projection only'))
    async def emit(event):pass
    runtime=agents.ResearchRuntime({**context,'job_id':job['id'],'synthesis_checkpoint':cp},cfg,
        ResearchTools(store,job,cfg,context),emit)
    try:
        index=runtime.context['restored_material_index']
        assert index['truncated_for_context'] and len(runtime.restored_material_evidence)==13
        restored_index=json.loads(recall(runtime.manager_session,record_id=index['record_id'],length=12000)['text'])
        assert len(restored_index)==13
        await runtime._enter_synthesis_context(runtime.manager_session,reason='bounded_final_request')
        assert len(json.dumps(runtime.manager_session.history,ensure_ascii=False)) < 20000*.78
        last=runtime.restored_material_evidence[-1]
        archived=json.loads(recall(runtime.manager_session,record_id=last['record_id'],length=12000)['text'])
        assert archived['result']==last['result']  # Bound the projection, not saved evidence.
    finally:await runtime.close()


@pytest.mark.asyncio
async def test_interrupted_continuation_keeps_new_material_after_current_checkpoint(store, monkeypatch):
    account, original, context, old, _, _ = saved_case(store)
    corrected=store.save_material({'scope':'account','url':old['url'],'text':'Corrected saved source','access':'full_text'},original['id'],account['id'])
    store.record_material_delivery(original['id'],account['id'],'manager','get_material',store.material_range(corrected['id']))
    store.save_synthesis_checkpoint(original,json.loads(checkpoint_row(store,original)['body']),context)
    store.submit_task(account['id'],research_scope='cash_only',parent_job_id=original['id'])
    retry=store.claim_jobs(account_id=account['id'])[0]
    cp=store.reusable_synthesis_checkpoint(retry,context,continue_research=True)
    assert cp['recovery_mode']=='continue_research'
    parent_before=checkpoint_row(store,original)
    install(monkeypatch,lambda r:pytest.fail('No model request in checkpoint lifecycle fixture'))
    async def emit(event):pass
    tools=ResearchTools(store,retry,store.settings(),context)
    runtime=agents.ResearchRuntime({**context,'job_id':retry['id'],'synthesis_checkpoint':cp},store.settings(),tools,emit)
    async def completed_material(label):
        material=store.save_material({'scope':'account','url':'https://issuer.example/'+label,'text':label},retry['id'],account['id'])
        output=await tools.get_material(material['id'])
        runtime.client.add_results(runtime.manager_session,[(ToolCall(label,'get_material',{'material_id':material['id']}),output)])
        visible=json.loads(runtime.manager_session.history[-1]['output'])
        store.record_material_delivery(retry['id'],account['id'],'manager','get_material',visible)
        return material
    try:
        first=await completed_material('first-current-result')
        await runtime._save_checkpoint()
        prior=json.loads(checkpoint_row(store,retry)['body'])
        assert {old['id'],corrected['id'],first['id']} <= set(prior['material_ids'])
        latest=await completed_material('later-completed-result')
        await runtime._save_interrupted_checkpoint('model_request_or_budget')
        saved=json.loads(checkpoint_row(store,retry)['body'])
        assert latest['id'] in saved['material_ids'], 'new completed material lost after an earlier continuation checkpoint'
        assert {old['id'],corrected['id'],first['id']} <= set(saved['material_ids'])
        assert saved['partial_research'] and saved['recovery_state']=='needs_research'
        assert len(restored_manager_material_evidence(saved))==4
        assert checkpoint_row(store,original)==parent_before
    finally:await runtime.close()
