"""Production FastAPI/SQLite and real events; explicit non-model test controls.
No worker, external network or user database/credentials.
"""
from delivery_helpers import publish_fixture
import json, os, socket, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
directory = Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory/'twelfth.sqlite3'), APP_MODE='live', PYTHON_DOTENV_DISABLED='1', MODEL_NAME='', MODEL_KEY_ENV='TWELFTH_UNUSED_KEY', MODEL_BASE_URL='https://fixture.invalid/v1')
from app.main import create_app
from app.domain import AccountInput, now
from tests.helpers_sixth import configure_test_team
import uvicorn
app=create_app(os.environ['APP_DB_PATH'], mode='live', worker=False)
store=app.state.store
store.set_meta('settings',{**store.settings(),'debounce_seconds':0,'quote_provider':'manual'})
configure_test_team(store,{**store.settings(),'reasoning_effort':'max','model_name':'explicit-twelfth-browser-fixture'})
for sid,name,kind in [('SH:518880','华安黄金ETF','etf'),('SZ:002354','天娱数科','stock'),('SZ:159599','东财中证芯片产业ETF','etf')]:
    store.put_cache('securities',sid,{'symbol':sid,'name':name,'kind':kind,'status':'ok','identity_status':'confirmed','fetched_at':now(),'source':'explicit-browser-directory-fixture'})
account=store.save_account(AccountInput(name='第十二轮受控 A',cash='100000',holdings=[]))
other=store.save_account(AccountInput(name='第十二轮隔离 B',cash='200000',holdings=[]))
material=store.save_material({'scope':'public','title':'明确标识的工程长材料','url':'https://example.test/twelfth/material','access':'full','text':'\n'.join(f'材料段 {n} '+ '工程材料，非投资事实。'*40 for n in range(160)),'classification':'unclassified'})
for claimed in store.claim_jobs(10):
    publish_fixture(store,claimed['id'],{'report':'## 工程报告，非真实模型结果\n\n'+'\n\n'.join(f'受控报告段 {n}：阅读期间更新应稳定。' for n in range(40)),'material_ids':[material['id']]},None)
report_id=store.analyses(account['id'])[0]['id']
job=store.enqueue(account['id'],'manual')
store.claim_jobs(10,account_id=account['id'])
for label in ['A','B']:
    store.event(job['id'],{'type':'progress','message':f'受控长事件 {label}','payload':{'test_double':True,'lines':[f'长行 {n:03} '+'horizontal-text-'*80 for n in range(180)]}})
for n in range(3):store.event(job['id'],{'type':'progress','message':f'初始普通事件 {n}','payload':{'test_double':True,'n':n}})
sequence=0
@app.post('/__fixture/touch')
async def touch(body:dict):
    global sequence
    for _ in range(min(int(body.get('count',1)),250)):
        sequence+=1
        store.event(job['id'],{'type':'progress','message':f'受控追加事件 {sequence}','payload':{'test_double':True,'sequence':sequence}})
    with store.connect() as db:db.execute('UPDATE jobs SET stage=? WHERE id=?',('synthesizing' if sequence%2 else 'researching',job['id']))
    return {'sequence':sequence,'events':store.events_since(job['id'],limit=500)}
@app.post('/__fixture/insert-above')
async def insert_above():
    newer=store.submit_task(account['id'],target='SH:518880',origin={'page':'market','action':'analyze-symbol'})
    return newer
@app.post('/__fixture/height/{job_id}')
async def height(job_id:str, body:dict):
    count=max(0,min(300,int(body.get('repeat',100))))
    with store.connect() as db:
        db.execute('UPDATE jobs SET error=? WHERE id=?',('受控上方任务说明高度变化。'*count,job_id))
    return {'ok':True}
@app.post('/__fixture/overview-insert')
async def overview_insert():
    return store.submit_task(account['id'],target='SZ:159599',origin={'page':'market','action':'analyze-symbol'})
@app.post('/__fixture/dialog-batches')
async def dialog_batches():
    from datetime import datetime
    from app.domain import TZ
    batches=[]
    for minute in (1,2):
        batch=store.create_forecast_batch(account['id'],at=datetime(2026,10,30,16,minute,tzinfo=TZ),kind='exploratory',symbols=['SH:518880'])
        batches.append(batch['id'])
    return batches
@app.post('/__fixture/new-report')
async def new_report():
    newer=store.enqueue(other['id'],'manual')
    store.claim_jobs(10,account_id=other['id'])
    # This fixture deliberately publishes to A without changing the long-running
    # reading task: normal store publish on a separate security-scoped task.
    newer=store.submit_task(account['id'],target='SZ:002354',origin={'page':'market','action':'analyze-symbol'})
    store.claim_jobs(10,account_id=account['id'])
    publish_fixture(store,newer['id'],{'report':'新工程报告，不是模型结论。','material_ids':[material['id']]},None)
    return store.job(newer['id'])
@app.get('/__fixture/audit')
async def audit():
    with store.connect() as db:
        return {table:db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ['ledger_entries','cash_entries','research_demands']}
listener=socket.socket();listener.bind(('127.0.0.1',0))
print(json.dumps({'port':listener.getsockname()[1],'account_id':account['id'],'other_id':other['id'],'job_id':job['id'],'report_id':report_id,'material_id':material['id']}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[listener])
