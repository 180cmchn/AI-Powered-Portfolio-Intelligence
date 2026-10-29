"""Production API + temporary SQLite, controlled clocks/models; no external services."""
import os
import sys
import json
import socket
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(PYTHON_DOTENV_DISABLED='1', APP_DB_PATH=str(Path(sys.argv[1])/'browser.sqlite3'),
                  APP_MODE='live', MODEL_NAME='', MODEL_KEY_ENV='SEVENTH_UNUSED', MODEL_BASE_URL='https://fixture.invalid/v1')
from app.main import create_app
from app.domain import TZ
from tests.helpers_sixth import configure_test_team
import uvicorn

app = create_app(Path(sys.argv[1])/'browser.sqlite3', worker=False)
store = app.state.store
cfg = {**store.settings(), 'debounce_seconds':0, 'scheduled_research_enabled':True,
       'model_name':'fixture/seventh-A', 'model_base_url':'https://fixture.invalid/v1', 'model_native_search':False}
store.set_meta('settings',cfg)
configure_test_team(store,cfg)
start = datetime(2026,9,1,10,tzinfo=TZ)
with patch('app.store.now', return_value=start.isoformat()), patch('app.forecast_windows.now',return_value=start.isoformat()), patch('app.forecasts.now',return_value=start.isoformat()):
    a=store.save_account({'name':'第七轮工程账户 A','cash':'10000','holdings':[{'symbol':'SZ:159278','quantity':'10000'}]})
    b=store.save_account({'name':'第七轮工程账户 B','cash':'20000','holdings':[{'symbol':'SZ:159278','quantity':'1'}]})
    for aid in (a['id'],b['id']):store.save_forecast_settings(aid,{'enabled':True})
    store.reconcile_forecast_windows(at=start)
for j in store.claim_jobs(20):store.set_job_state(j['id'],'completed')
# Entire window offline: only ledger observations, never reconstructed forecasts.
store.reconcile_forecast_windows(at=datetime(2026,10,28,19,tzinfo=TZ))
at=datetime(2026,10,29,16,tzinfo=TZ)
batch=store.create_forecast_batch(a['id'],at=at,kind='formal')
job=store.enqueue(a['id'],'fixture_preflight_failure')
store.bind_forecast_job(batch['id'],job['id'])
job=store.claim_jobs(account_id=a['id'])[0]
store.set_job_state(job['id'],'failed','受控前置失败；未发出模型请求')
store.expire_forecast_batches(at=datetime(2026,10,29,19,tzinfo=TZ))
store.reconcile_forecast_windows(at=datetime(2026,10,29,19,tzinfo=TZ))
for aid,count in ((a['id'],12),(b['id'],1)):
    for n in range(count):
        stamp=at+timedelta(minutes=n)
        batch=store.create_forecast_batch(aid,at=stamp,kind='exploratory')
        job=store.enqueue(aid,'fixture_exploration')
        store.bind_forecast_job(batch['id'],job['id'])
        job=store.claim_jobs(account_id=aid)[0]
        with store.connect() as db:
            store.publish_forecasts(db,job,{'report':'工程测试记录，不代表真实预测','forecasts':[
                {'symbol':'SZ:159278','horizon':h,'direction':'up' if n%2==0 else 'down','reason':'明确的协议夹具'} for h in (1,5,20)]},'fixture-report',published_at=stamp)
        store.set_job_state(job['id'],'completed')
listener=socket.socket();listener.bind(('127.0.0.1',0))
print(json.dumps({'port':listener.getsockname()[1],'account_a':a['id'],'account_b':b['id']}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[listener])
