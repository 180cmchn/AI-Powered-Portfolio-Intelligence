"""Real FastAPI/SQLite, bounded fixture-only material and event changes.
No worker, external data requests or user configuration; prices are arithmetic samples.
"""
from delivery_helpers import publish_fixture
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory / 'thirteenth.sqlite3'), APP_MODE='live',
                  PYTHON_DOTENV_DISABLED='1', MODEL_NAME='', MODEL_KEY_ENV='THIRTEENTH_UNUSED_KEY',
                  MODEL_BASE_URL='https://fixture.invalid/v1')
from app import clock
clock._utc_now = lambda: datetime.fromisoformat('2026-11-02T10:00:00+08:00')
from app.main import create_app
from app.domain import AccountInput, now
from tests.helpers_sixth import configure_test_team
import uvicorn

app = create_app(os.environ['APP_DB_PATH'], mode='live', worker=False)
store = app.state.store
store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0, 'quote_provider': 'manual'})
configure_test_team(store, {**store.settings(), 'model_name': 'explicit-thirteenth-browser-fixture'})
for sid, name, kind in [('SH:518880', '华安黄金ETF', 'etf'), ('SZ:002354', '天娱数科', 'stock'),
                        ('SZ:159599', '东财中证芯片产业ETF', 'etf')]:
    store.put_cache('securities', sid, {'symbol': sid, 'name': name, 'kind': kind, 'status': 'ok',
                                      'identity_status': 'confirmed', 'fetched_at': now(),
                                      'source': 'explicit-browser-directory-fixture'})
account = store.save_account(AccountInput(name='第十三轮工程 A', cash='100000', holdings=[]))
other = store.save_account(AccountInput(name='第十三轮隔离 B', cash='200000', holdings=[]))
for job in store.claim_jobs(10):
    publish_fixture(store,job['id'], {'report': '工程报告，不是模型生成结果。'}, None)
sequence = 0

@app.post('/__fixture/material')
async def material(body: dict):
    global sequence
    sequence += 1
    return store.save_material({'scope': 'public', 'classification': body['classification'],
                                'title': f'明确工程材料 {sequence}', 'access': 'full',
                                'url': f'https://example.test/thirteenth/{sequence}',
                                'text': f'非投资事实。工程材料 {sequence}。'})

@app.post('/__fixture/touch')
async def touch():
    job = store.jobs(account['id'])[0]
    store.event(job['id'], {'type': 'progress', 'message': '受控轮询更新'})
    return {'ok': True}

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
print(json.dumps({'port': listener.getsockname()[1], 'account_id': account['id'],
                  'other_id': other['id']}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])
