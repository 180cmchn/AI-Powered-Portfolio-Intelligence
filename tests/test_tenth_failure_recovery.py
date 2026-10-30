"""Real runtime/store with controlled upstream failures; no live-model claims."""
import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.service import ResearchTools, Service
from tests.test_fourth_recovery import store, install, response, call


@pytest.mark.parametrize('failure',['model','deadline'])
async def test_early_failure_keeps_completed_calculate_then_reuses_without_research(store,monkeypatch,failure):
    account=store.save_account(AccountInput(name='early failure fixture',cash='10000',holdings=[],research_scope='cash_only'))
    manager=next(r for r in store.list_agent_roles() if r['role_id']=='manager')
    store.update_agent_definition('manager',{'default_binding':{**{k:manager['default_binding'][k] for k in ('provider_id','model_id')},'reasoning_effort':'max'}})
    if failure=='deadline':store.set_meta('settings',{**store.settings(),'analysis_timeout_seconds':.2})
    calls=[]
    async def handler(request):
        payload=json.loads(request.content);calls.append(payload)
        assert payload['reasoning']['effort']=='max'
        seed=json.loads(payload['input'][0]['content'])
        if seed.get('synthesis_checkpoint'):
            checkpoint=seed['synthesis_checkpoint']
            assert checkpoint['partial_research'] and checkpoint['interruption']
            assert '有限分析' in json.dumps(payload,ensure_ascii=False)
            assert Decimal(seed['calculation_summaries'][0]['result'])==Decimal('8850')
            assert not any(t.get('name')=='spawn_research' or t['type']=='web_search' for t in payload['tools'])
            return response(call('finish_analysis',{'report':'有限分析：已完成持仓估值计算8850；原专题尚未完成，保留未知。'}))
        if len(calls)==1:return response(call('calculate',{'operation':'multiply','values':['10000','.885']}))
        if failure=='deadline':await asyncio.sleep(2)
        raise httpx.ReadTimeout('controlled early failure',request=request)
    install(monkeypatch,handler)
    service=Service(store);first=store.claim_jobs(account_id=account['id'])[0]
    await service.execute(first)
    assert store.job(first['id'])['state']=='failed' and not store.analyses(account['id'])
    with store.connect() as db:
        body=json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?',(first['id'],)).fetchone()[0])
    assert body['partial_research'] and Decimal(body['calculations'][0]['output']['result'])==Decimal('8850')
    assert body['recovery_state']=='synthesis_ready' and body['manager_summary']=={}
    assert 'controlled early failure' not in json.dumps(body) and 'encrypted_content' not in json.dumps(body)
    store.set_meta('settings',{**store.settings(),'analysis_timeout_seconds':5})
    store.submit_task(account['id'],research_scope='cash_only',parent_job_id=first['id'])
    retry=store.claim_jobs(account_id=account['id'])[0]
    await service.execute(retry)
    assert store.job(retry['id'])['state']=='completed'
    assert store.job(first['id'])['state']=='failed'
    assert len(calls)==3 and store.analyses(account['id'])[0]['result']['synthesis_recovered']
    assert not any(e.get('payload',{}).get('tool')=='calculate' for e in store.job(retry['id'],events=True)['events'] if e.get('type')=='tool_started')


def metadata_checkpoint(store,*,prior_text=False,claim=None):
    account=store.save_account(AccountInput(name='metadata fixture',holdings=[],research_scope='cash_only'))
    job=store.claim_jobs(account_id=account['id'])[0]
    context={'scope':'account','account':account,'universe_version':store.universe_snapshot()['universe_version'],'metrics':{'positions':[]}}
    base={'scope':'public','representation':'native_search_source','source':'model_native_search',
          'url':'https://issuer.example/unchanged-url','classification':'unclassified'}
    old=store.save_material({**base,'source_title':None,'content':'Previously visible text' if prior_text else '',
                            'access':'search_excerpt' if prior_text else 'source_link'},job['id'],account['id'])
    new=store.save_material({**base,'source_title':'https://issuer.example/unchanged-url','content':'','access':'source_link'},job['id'],account['id'])
    for m in (old,new):store.record_material_delivery(job['id'],account['id'],'manager','native_search',
        {'material_id':m['id'],'text':m.get('content')})
    declaration={}
    if claim=='reference':declaration={'material_ids':[old['id']]}
    if claim=='read':declaration={'material_updates':[{'latest_material_id':new['id'],'status':'read'}]}
    body={'phase':'synthesis','task_id':job['id'],'account_id':account['id'],'material_ids':[old['id'],new['id']],
          'reports':[],'manager_summary':declaration}
    store.save_synthesis_checkpoint(job,body,context)
    store.set_job_state(job['id'],'failed','controlled')
    return account,job,context,old,new


@pytest.mark.parametrize('prior_text,claim',[(False,None),(True,None),(False,'reference'),(False,'read')])
def test_unused_link_title_revision_only_does_not_require_fulltext(store,prior_text,claim):
    account,job,context,old,new=metadata_checkpoint(store,prior_text=prior_text,claim=claim)
    status=store.checkpoint_status(job['id'])
    allowed=not prior_text and claim is None
    assert (status['recovery_state']=='synthesis_ready') is allowed
    recovered=store.reusable_synthesis_checkpoint(job,context)
    assert bool(recovered) is allowed


def test_old_metadata_checkpoint_reassessed_without_rewriting_guard_or_record(store):
    account,job,context,old,new=metadata_checkpoint(store)
    with store.connect() as db:
        row=db.execute('SELECT body,guard FROM synthesis_checkpoints WHERE job_id=?',(job['id'],)).fetchone()
        body=json.loads(row['body']);body['recovery_state']='needs_research'
        for v in body['source_coverage'].values():v.update(status='needs_research',reason='historical_comparison_not_processed')
        encoded=json.dumps(body)
        db.execute('UPDATE synthesis_checkpoints SET body=? WHERE job_id=?',(encoded,job['id']))
    status=store.checkpoint_status(job['id'])
    assert status['saved_recovery_state']=='needs_research' and status['recovery_state']=='synthesis_ready'
    with store.connect() as db:
        before=db.execute('SELECT body,guard,resume_count FROM synthesis_checkpoints WHERE job_id=?',(job['id'],)).fetchone()
    assert before['body']==encoded and before['guard']==row['guard'] and before['resume_count']==0
    assert store.reusable_synthesis_checkpoint(job,{**context,'target_security':{'symbol':'SH:518880'}}) is None
    # A new visible body is a new source version and must still invalidate reuse.
    store.save_material({'scope':'public','representation':'native_search_source','url':old['url'],
        'source':'model_native_search','content':'Now there is a real excerpt','access':'search_excerpt'})
    assert store.reusable_synthesis_checkpoint(job,context) is None


async def test_interruption_never_overwrites_more_complete_checkpoint(store):
    account=store.save_account(AccountInput(name='preserve',holdings=[],research_scope='cash_only'))
    job=store.claim_jobs(account_id=account['id'])[0]
    context={'account':account,'job_id':job['id'],'input_version':account['version'],'universe_version':store.universe_snapshot()['universe_version']}
    tools=ResearchTools(store,job,store.settings(),context)
    async def emit(event):pass
    runtime=agents.ResearchRuntime(context,store.settings(),tools,emit)
    try:
        await runtime._dispatch('manager','calculate',{'operation':'sum','values':[1,2]})
        original=await runtime._save_checkpoint({'summary':'A completed manager synthesis declaration','material_ids':[]})
        await runtime._save_interrupted_checkpoint('model_request_or_budget')
        with store.connect() as db:body=json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?',(job['id'],)).fetchone()[0])
        assert body['manager_summary']['summary']==original['manager_summary']['summary']
        assert not body.get('partial_research')
    finally:await runtime.close()


async def test_recovered_report_keeps_prior_child_gaps_separate_from_new_children(store,monkeypatch):
    account=store.save_account(AccountInput(name='recover gaps',holdings=[],research_scope='cash_only'))
    job=store.claim_jobs(account_id=account['id'])[0]
    original={'phase':'synthesis','task_id':'old-task','reports':[],'material_ids':[],'manager_summary':{},
        'partial_research':True,'unresolved_tasks':[{'agent_id':'old-child','title':'incomplete price study','status':'failed','error':'timeout'}]}
    context={'account':account,'job_id':job['id'],'input_version':account['version'],'universe_version':store.universe_snapshot()['universe_version'],
             'synthesis_checkpoint':original}
    async def handler(request):return response(call('finish_analysis',{'report':'有限分析：旧价格专题未完成，保留其影响。'}))
    install(monkeypatch,handler)
    async def emit(event):pass
    result=await agents.run_research(context,store.settings(),ResearchTools(store,job,store.settings(),context),emit)
    delivery=result['research_delivery']
    assert delivery['child_total']==0 and delivery['child_completed']==0 and delivery['child_incomplete']==[]
    assert delivery['recovered_partial_research'] and delivery['recovered_unresolved_tasks']==original['unresolved_tasks']
