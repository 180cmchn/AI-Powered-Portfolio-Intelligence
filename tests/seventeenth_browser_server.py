"""Production HTTP/UI fixture, isolated SQLite and no worker/model network."""
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
directory = Path(sys.argv[1]).resolve()
os.environ.update(PYTHON_DOTENV_DISABLED='1', MODEL_NAME='', MODEL_KEY_ENV='SEVENTEENTH_UNUSED_KEY',
                  MODEL_BASE_URL='https://fixture.invalid/v1')
from app.main import create_app
from app.domain import AccountInput
from app.model import ModelClient
from tests.delivery_helpers import publish_fixture
import uvicorn

app = create_app(directory / 'browser.sqlite3', mode='live', worker=False)
store = app.state.store
store.set_meta('settings', {**store.settings(), 'quote_provider': 'manual', 'debounce_seconds': 0,
    'max_model_calls': None, 'max_agent_turns': None, 'synthesis_max_calls': None})
account = store.save_account(AccountInput(name='第十七轮页面工程夹具', cash='100000', holdings=[], research_scope='cash_only'))
job = store.claim_jobs(account_id=account['id'])[0]
requests = []
for index, (session, inp, cached) in enumerate([
    ('manager-session', 10000, 8000), ('manager-session', 10000, 9000),
    ('child-session', 5000, 0), ('child-session', None, None),
]):
    requests.append({'local_request_id': f'fixture-request-{index}',
        'session_id': session, 'agent_id': 'manager' if session == 'manager-session' else 'child-instance',
        'cache_epoch': 0, 'stage': 'research' if index < 3 else 'synthesis', 'protocol': 'responses',
        'duration_ms': 1000 + index, 'model': 'fixture-no-network', 'reasoning_effort': 'xhigh',
        'prefix_diagnostic': {'change_reason': 'first' if index in (0, 2) else 'append_only'},
        'usage': {'input_tokens': 2000} if inp is None else {'input_tokens': inp, 'output_tokens': 100,
            'input_tokens_details': {'cached_tokens': cached}}})
requests[-1]['error'] = 'controlled_failure_without_cache_usage'
summary = ModelClient.usage_summary(requests)
publish_fixture(store, job['id'], {'report': '仅验证页面控件与缓存用量显示的工程报告。',
    'model_usage': summary, 'model_calls': 4}, None)
store.event(job['id'], {'type': 'model_usage', 'agent_id': 'manager',
    'payload': {'requests': requests, 'session': summary}})

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
print(json.dumps({'port': listener.getsockname()[1], 'account_id': account['id'], 'job_id': job['id']}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])
