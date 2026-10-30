"""Disposable production API, explicit vault/catalog doubles; never uses a real key."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1]).resolve()
os.environ.update(APP_DB_PATH=str(directory / 'ninth.sqlite3'), APP_MODE='live', PYTHON_DOTENV_DISABLED='1',
                  MODEL_KEY_ENV='NINTH_BROWSER_UNUSED_KEY', MODEL_NAME='', MODEL_PROTOCOL='responses',
                  MODEL_BASE_URL='https://unconfigured.example.test/v1', REASONING_EFFORT='medium')
from app.main import create_app
from app.domain import AccountInput
from tests.helpers_sixth import FixtureCredentials
import httpx
import uvicorn
app = create_app(os.environ['APP_DB_PATH'], mode='live', worker=False)
store = app.state.store
store._credential_store = FixtureCredentials()
actual = store.discover_models
async def discovery(provider_id, **kwargs):
    provider = store.provider(provider_id)
    status = 401 if 'authfail' in provider['base_url'] else 200
    kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(status, json={'data': [{'id': 'catalog/observed-A'}, {'id': 'catalog/observed-B'}]}))
    return await actual(provider_id, **kwargs)
store.discover_models = discovery
account = store.save_account(AccountInput(name='第九轮临时交互账户', cash='20000', holdings=[]))
@app.get('/__fixture/reopen')
async def reopen():
    from app.store import Store
    reopened = Store(store.path)
    return {'providers': reopened.list_providers(), 'roles': reopened.list_agent_roles(), 'readiness': reopened.configuration_readiness(account['id'])}
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
print(json.dumps({'port': listener.getsockname()[1], 'account_id': account['id']}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[listener])
