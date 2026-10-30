"""Production app, isolated SQLite, explicit directory/results; no model worker."""
import json, os, socket, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
directory=Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory/'eleventh.sqlite3'),APP_MODE='live',PYTHON_DOTENV_DISABLED='1',MODEL_NAME='',MODEL_KEY_ENV='ELEVENTH_UNUSED_KEY',MODEL_BASE_URL='https://fixture.invalid/v1')
from app.main import create_app
from app.domain import AccountInput, now
from tests.helpers_sixth import configure_test_team
import uvicorn
app=create_app(os.environ['APP_DB_PATH'],mode='live',worker=False)
store=app.state.store
store.set_meta('settings',{**store.settings(),'debounce_seconds':0,'quote_provider':'manual'})
configure_test_team(store,{**store.settings(),'reasoning_effort':'max','model_name':'explicit-browser-fixture'})
for sid,name,kind in [('SH:518880','华安黄金ETF','etf'),('SZ:002354','天娱数科','stock'),('SZ:159599','东财中证芯片产业ETF','etf')]:
    store.put_cache('securities',sid,{'symbol':sid,'name':name,'kind':kind,'status':'ok','identity_status':'confirmed','fetched_at':now(),'source':'explicit-browser-directory-fixture'})
long_prompt='\n'.join(f'受控长提示行 {i:03d} '+('横向阅读验证 ' * 15) for i in range(150))
role=store.list_agent_roles()[0]
store.update_agent_definition(role['role_id'],{'system_prompt':long_prompt})
account=store.save_account(AccountInput(name='第十一轮模拟 A',cash='100000',holdings=[]))
other=store.save_account(AccountInput(name='第十一轮隔离 B',cash='200000',holdings=[]))
for job in store.claim_jobs(10):
    store.publish(job['id'],{'report':'受控浏览器报告，不是模型生成结果。'},None)
@app.post('/__fixture/touch')
async def touch():
    job=store.jobs(account['id'])[0]
    store.event(job['id'],{'type':'progress','message':'受控轮询事件变化'})
    return {'ok':True}
@app.post('/__fixture/role-change')
async def role_change():
    role=store.list_agent_roles()[0]
    return store.update_agent_definition(role['role_id'],{'system_prompt':'已保存的新版本提示\n'+long_prompt})
listener=socket.socket();listener.bind(('127.0.0.1',0))
print(json.dumps({'port':listener.getsockname()[1],'account_id':account['id'],'other_id':other['id'],'role_id':role['role_id'],'long_prompt':long_prompt}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[listener])
