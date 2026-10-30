"""Real HTTP/SQLite UI regression, with explicit directory/model-result doubles.

No external model worker, network research, credentials or daily database.
"""
import json
import os
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory / 'tenth.sqlite3'), APP_MODE='live', PYTHON_DOTENV_DISABLED='1',
                  MODEL_NAME='', MODEL_KEY_ENV='TENTH_UNUSED_KEY', MODEL_BASE_URL='https://fixture.invalid/v1',
                  MODEL_PROTOCOL='responses', REASONING_EFFORT='max')
from app.main import create_app
from app.domain import AccountInput, now
from tests.helpers_sixth import configure_test_team
import uvicorn

app = create_app(os.environ['APP_DB_PATH'], mode='live', worker=False)
store = app.state.store
store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0, 'quote_provider':'manual'})
configure_test_team(store, {**store.settings(), 'reasoning_effort':'max', 'model_name':'explicit-browser-fixture'})
for sid, name, kind in [('SH:518880','黄金ETF','etf'), ('SH:688825','长鑫科技','stock'),
                         ('SH:562500','机器人ETF','etf'), ('SZ:159278','创业板人工智能ETF','etf'),
                         ('SZ:000001','平安银行','stock')]:
    store.put_cache('securities', sid, {'symbol':sid,'name':name,'kind':kind,'status':'ok',
        'identity_status':'confirmed','fetched_at':now(),'source':'explicit-browser-directory-fixture'})
account = store.save_account(AccountInput(name='第十轮虚拟 A', cash='10000', holdings=[{'symbol':'SZ:159278','quantity':'10000'}]))
other = store.save_account(AccountInput(name='第十轮现金 B', cash='20000', holdings=[]))
ids=[]
for index in range(100):
    material = store.save_material({'scope':'public','title':f'受控来源 {index}',
        'url':f'https://example.test/evidence/{index}', 'access':'source_link', 'representation':'native_search_source',
        'classification':'unclassified','classification_origin':'unclassified_default'})
    ids.append(material['id'])
rumor = store.save_material({'scope':'public','title':'明确分类的传闻样例','url':'https://example.test/rumor',
    'access':'search_excerpt','text':'这是测试传闻条目，非真实事件','classification':'rumor'})
for job in store.claim_jobs(10):
    store.save_synthesis_checkpoint(job, {'reports':[{'summary':'受控子研究成果','agent_id':'fixture-child','material_ids':[ids[0]]}],
        'material_ids':[ids[0]], 'open_questions':['受控公开正文仍未取得']},
        {'account':store.account(job['account_id']), 'universe_version':store.universe_snapshot()['universe_version']})
    store.publish(job['id'], {'report':'## 本轮结论\n\n受控浏览器报告，非真实投资建议：继续观察。\n\n' + '\n\n'.join(f'第 {i} 段测试长报告：账户现金 10000 元；数量仅用于界面回归。' for i in range(40)),
        'material_ids': ids, 'suggestions':[]}, None)

@app.post('/__fixture/merged-retry/{account_id}')
async def merged_retry(account_id:str):
    failed=store.submit_task(account_id,target='SH:518880',origin={'page':'market','action':'analyze-symbol'})
    store.set_job_state(failed['id'],'failed','受控旧任务失败')
    queued=store.submit_task(account_id,target='SH:518880',origin={'page':'market','action':'analyze-symbol'})
    merged=store.submit_task(account_id,target='SH:518880',origin={'page':'agents','action':'retry-job'},parent_job_id=failed['id'])
    assert merged['id']==queued['id'] and merged['merged']
    return {'failed_id':failed['id'],'job':merged}

@app.post('/__fixture/history/{account_id}')
async def history(account_id:str):
    for index in range(12):
        job=store.enqueue(account_id, 'manual')
        store.claim_jobs(10,account_id=account_id)
        store.event(job['id'], {'type':'progress','message':f'弹窗可读取事件 {index}'})
        store.publish(job['id'], {'report':f'受控历史分页报告 {index}'},None)
    return store.job(job['id'])

@app.post('/__fixture/merged-demands/{account_id}')
async def merged_demands(account_id:str):
    job=store.submit_task(account_id,target='SH:518880',origin={'page':'market','action':'analyze-symbol'})
    store.request_analysis(account_id,'close_summary',event_key=f"close:{account_id}:2026-10-30",reason='受控收盘需求')
    store.request_analysis(account_id,'formal_forecast',event_key=f"forecast:{account_id}:2026-10-30",reason='受控正式批次需求')
    return store.job(job['id'])

@app.post('/__fixture/touch')
async def touch():
    store.save_material({'scope':'public','title':'轮询新增受控材料','url':'https://example.test/touch', 'access':'source_link'})
    return {'ok':True}

@app.post('/__fixture/claim/{account_id}')
async def claim(account_id:str):
    return store.claim_jobs(10, account_id=account_id)

@app.post('/__fixture/complete/{job_id}')
async def complete(job_id:str):
    job = store.job(job_id)
    if job['state'] == 'queued':
        store.claim_jobs(10, account_id=job['account_id'])
    store.publish(job_id, {'report':'## 本轮结论\n\n受控更新报告：继续观察。持仓变更已体现在冻结输入；这是测试替身。', 'material_ids':ids, 'research_delivery':{'child_total':0,'child_completed':0,'child_incomplete':[], 'recovered_partial_research':True, 'recovered_unresolved_tasks':[{'agent_id':'fixture-original-child','title':'受控原行情专题缺口','status':'failed','error':'受控原专题超时','pending_messages':[]}]}}, None)
    return store.job(job_id)

@app.post('/__fixture/fail/{job_id}')
async def fail(job_id:str):
    store.set_job_state(job_id, 'failed', '受控汇总超时；未发布最终报告')
    return store.job(job_id)

@app.post('/__fixture/waiting-config/{job_id}')
async def waiting_config(job_id:str):
    store.set_job_state(job_id, 'waiting_config', '受控配置等待')
    return store.job(job_id)

listener=socket.socket()
listener.bind(('127.0.0.1',0))
print(json.dumps({'port':listener.getsockname()[1],'account_id':account['id'],'other_id':other['id'],
    'report_id':store.analyses(account['id'])[0]['id'],'material_id':ids[0], 'rumor_id':rumor['id']}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[listener])
