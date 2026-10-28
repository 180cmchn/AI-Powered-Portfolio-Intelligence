"""Disposable browser fixtures. No real credentials, upstream data, or model calls."""
import os, sys, json, socket, asyncio
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.update(PYTHON_DOTENV_DISABLED='1',APP_DB_PATH=sys.argv[1],APP_MODE='live',MODEL_NAME='',MODEL_KEY_ENV='SIXTH_BROWSER_UNUSED',MODEL_BASE_URL='https://fixture.invalid/v1',MODEL_PROTOCOL='responses')
from app.main import create_app
from tests.helpers_sixth import configure_test_team
from app.domain import AccountInput,TZ
from datetime import datetime
import httpx,uvicorn
app=create_app(sys.argv[1],worker=False)
store=app.state.store
cfg={'debounce_seconds':0,'quote_provider':'manual','scheduled_research_enabled':False,'model_native_search':False,'model_name':'fixture/research-a','model_base_url':'https://fixture.invalid/v1','reasoning_effort':'medium'}
store.set_meta('settings',cfg)
provider=configure_test_team(store,cfg)
original=store.discover_models
async def discover(provider_id, **kwargs):
    def response(req):
        if req.url.host=='unsupported.invalid': return httpx.Response(404,json={'error':'fixture unsupported'})
        return httpx.Response(200,json={'data':[{'id':'fixture/research-a'},{'id':'fixture/research-b'}]})
    kwargs['transport']=httpx.MockTransport(response)
    return await original(provider_id,**kwargs)
store.discover_models=discover
asyncio.run(discover(provider['id'],force=True))
if not store.accounts():
    a=store.save_account(AccountInput(name='第六轮受控账户 A',cash='10000',holdings=[{'symbol':'SZ:159278','quantity':'10000'}]))
    b=store.save_account(AccountInput(name='第六轮受控账户 B',cash='20000',holdings=[]))
else:a,b=store.accounts()[:2]
for j in store.claim_jobs(20):store.publish(j['id'],{'report':'仅用于浏览器布局与交互验收的确定性夹具'},None)
# Fixed historical test-only sample; never enters formal statistics.
if not store.forecast_batches(a['id']):
    at=datetime(2026,10,12,16,tzinfo=TZ)
    batch=store.create_forecast_batch(a['id'],at=at,kind='test')
    job=store.enqueue(a['id'],'fixture_test')
    store.bind_forecast_job(batch['id'],job['id'])
    job=store.claim_jobs(account_id=a['id'])[0]
    with store.connect() as db:
        ids=store.publish_forecasts(db,job,{'report':'历史夹具，不是真实前瞻预测','forecasts':[{'security_id':'SZ:159278','horizon':h,'direction':'up' if h!=5 else 'abstain','reason':'工程夹具'} for h in [1,5,20]]},'browser-fixture-report',published_at=at)
    store.set_job_state(job['id'],'completed')
    store.record_forecast_evaluation(ids[0],{'symbol':'SZ:159278','base_date':'2026-10-12','target_date':'2026-10-13','status':'ready','price_policy':'same_source_pair_v1','base_price':'1','target_price':'1.01','source':'controlled-history-fixture','data_version':'fixture-v1','basis':'raw_close'},at=datetime(2026,10,13,16,tzinfo=TZ))
listener=socket.socket();listener.bind(('127.0.0.1',int(sys.argv[2]) if len(sys.argv)>2 else 0))
print(json.dumps({'port':listener.getsockname()[1],'account_a':a['id'],'account_b':b['id'],'provider_id':provider['id']}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[listener])
