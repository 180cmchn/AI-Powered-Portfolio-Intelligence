"""Temporary production stack with explicit HTTP fixtures, zero paid calls."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory/'fourteenth.sqlite3'), APP_MODE='live',
    PYTHON_DOTENV_DISABLED='1', MODEL_NAME='', MODEL_KEY_ENV='FOURTEENTH_UNUSED',
    MODEL_BASE_URL='https://fixture.invalid/v1')
import httpx
import uvicorn
from app import agents, clock
from app.domain import AccountInput
from app.main import create_app
from app.model import ModelClient
from tests.helpers_sixth import configure_test_team
from tests.test_fourteenth_chain import final, call, prose, response

app = create_app(os.environ['APP_DB_PATH'], mode='live', worker=False)
store = app.state.store
cfg = {**store.settings(), 'model_name': 'fourteenth-browser-controlled',
    'model_base_url': 'https://fixture.invalid/v1', 'model_protocol': 'responses',
    'model_native_search': False, 'model_responses_stream': False, 'model_retries': 0,
    'quote_provider': 'manual', 'debounce_seconds': 0, 'max_model_calls': 2,
    'max_agent_turns': 2, 'analysis_timeout_seconds': 10,
    'model_request_hard_timeout_seconds': 2, 'synthesis_request_hard_timeout_seconds': 2}
store.set_meta('settings', cfg)
configure_test_team(store, cfg)
requests = []
state = {'fail': False, 'structured': False, 'structured_requests': 0}
def handler(request):
    payload = json.loads(request.content)
    requests.append(payload)
    if state['structured']:
        state['structured_requests'] += 1
        # First submission contains the exact scope defect. The same Agent
        # must receive field feedback and correct it before Store publishes.
        target = 'SZ:002354' if state['structured_requests'] == 1 else 'SZ:159599'
        if state['structured_requests'] == 2:
            assert 'forecasts[0].security_id' in json.dumps(payload, ensure_ascii=False)
        return response(call('finish_analysis', final(
            report='受控夹具：仅覆盖159599；观察条件变化，另一个证券尚未研究。',
            completion='limited', scope=['SZ:159599'], decision_summary='受控夹具：保持观察。',
            limitations=['002354尚未研究。'],
            suggestions=[{'security_id': 'SZ:159599', 'action': 'watch', 'basis': '受控测试依据'}],
            forecasts=[{'security_id': target, 'horizon': 1, 'direction': 'up', 'reason': '受控测试方向，非真实模型判断'}])))
    if state['fail']:
        return response(prose())
    return response(call('finish_analysis', final(completion='limited',
        report='保留现金100元，证券专题尚未开展；明确资金用途后再确定投入比例。',
        limitations=['证券专题尚未开展；当前判断仅适用于现金安排。'])))
transport = httpx.MockTransport(handler)
agents.ModelClient = lambda config, before_request=None: ModelClient(config, before_request, transport)
account = store.save_account(AccountInput(name='第十四轮受控交付', cash='100', holdings=[], research_scope='cash_only'))
async def prepare():
    first = store.claim_jobs(account_id=account['id'])[0]
    await app.state.service.execute(first)
    assert store.job(first['id'])['state'] == 'completed', store.job(first['id']).get('error')
    old = store.analyses(account['id'])[0]
    state['fail'] = True
    store.submit_task(account['id'], research_scope='cash_only')
    second = store.claim_jobs(account_id=account['id'])[0]
    await app.state.service.execute(second)
    assert store.job(second['id'])['state'] == 'failed'
    assert store.analyses(account['id'])[0]['status'] == 'current'
    return old, second
old, failed = asyncio.run(prepare())
# Known trading window, without real market calls; a missing baseline is a
# publication outcome, not a static model-contract error.
clock._utc_now = lambda: datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
state.update(fail=False, structured=True)
structured_account = store.save_account(AccountInput(name='第十五轮受控逐项回执', cash='1000',
    holdings=[{'symbol': sid, 'quantity': '1'} for sid in ('SZ:159599', 'SZ:002354')]))
batch = store.create_forecast_batch(structured_account['id'], symbols=['SZ:159599', 'SZ:002354'])
store.bind_forecast_job(batch['id'], structured_account['latest_job_id'])
structured_job = store.claim_jobs(account_id=structured_account['id'])[0]
asyncio.run(app.state.service.execute(structured_job))
assert store.job(structured_job['id'])['state'] == 'completed', store.job(structured_job['id']).get('error')
structured_report = store.analyses(structured_account['id'])[0]
assert state['structured_requests'] == 2
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
print(json.dumps({'port': listener.getsockname()[1], 'account_id': account['id'],
    'report_id': old['id'], 'failed_job_id': failed['id'], 'model_fixture_http_requests': len(requests),
    'paid_model_requests': 0, 'structured_account_id': structured_account['id'],
    'structured_job_id': structured_job['id'], 'structured_report_id': structured_report['id']}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])
