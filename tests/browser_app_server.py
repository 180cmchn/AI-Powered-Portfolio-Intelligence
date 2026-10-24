"""Production FastAPI + temporary SQLite fixture server for browser integration.

No worker, provider, or model is started. All fixture claims are explicitly marked.
The parent supplies a new temporary directory; importing app.main also uses that DB.
"""
import json
import os
from pathlib import Path
import socket
import sys
from datetime import datetime

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
directory = Path(sys.argv[1]).resolve()
os.environ['APP_DB_PATH'] = str(directory / 'browser.sqlite3')
os.environ['APP_MODE'] = 'demo'
os.environ['PYTHON_DOTENV_DISABLED'] = '1'

from app.main import create_app
from app.domain import AccountInput, TZ
import uvicorn

app = create_app(os.environ['APP_DB_PATH'], mode='demo', worker=False)
store = app.state.store
store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual',
                           'search_provider': 'disabled', 'model_name': '',
                           'model_key_env': 'GUANLAN_BROWSER_TEST_UNUSED_KEY'})
store.save_universe({'watchlist': ['SH:600519'], 'candidates': []})
old = store.claim_global()
store.save_universe({'watchlist': ['SH:600519'], 'candidates': ['SZ:000001']})
current = store.claim_global()
store.finish_global(current['id'], 'completed', {'report': '受控当前范围公共研究',
    'research_records': [{'kind': 'market', 'title': '浏览器公共研究', 'summary': '当前范围结论'}]})
store.finish_global(old['id'], 'completed', {'report': '受控历史范围公共研究',
    'research_records': [{'kind': 'market', 'title': '浏览器公共研究', 'summary': '历史范围结论'}]})
account = store.save_account(AccountInput(name='受控浏览器账户 A', holdings=[{'symbol': 'SH:600519', 'quantity': '10'}]))
other = store.save_account(AccountInput(name='受控浏览器账户 B', holdings=[]))
material = store.save_material({'scope': 'public', 'title': '受控选页材料', 'text': '受控已保存 PDF 选页正文',
    'url': 'https://example.test/selected.pdf', 'access': 'excerpt', 'document_id': 'f'*64,
    'page_count': 32, 'requested_range': {'start_page': 25, 'end_page': 26},
    'returned_range': {'start_page': 25, 'end_page': 26}, 'truncated': True})
private = store.save_material({'scope': 'account', 'text': '账户 B 私有原文不得显示',
                              'title': '账户 B 私有资料标题', 'access': 'summary'}, account_id=other['id'])
unknown = 'd'*32

def suggestion(symbol, index):
    return {'security_id': symbol, 'action': 'watch', 'basis': f'受控建议 {symbol} {index}',
            'price_conditions': [{'operator': 'gte', 'value': '110'}], 'material_ids': [material['id']]}

for job in store.claim_jobs(10):
    result = {'report': '受控初始报告'}
    if job['account_id'] == account['id']:
        result['suggestions'] = [suggestion('SH:600519', 0)]
    assert store.publish(job['id'], result, None)
for index in range(105):
    job = store.enqueue(account['id'], 'manual_security:SZ:000001')
    claimed = next(j for j in store.claim_jobs(10) if j['id'] == job['id'])
    assert store.publish(claimed['id'], {'report': f'受控替代报告 {index}',
        'suggestions': [suggestion('SZ:000001', index)]}, None)
job = store.enqueue(account['id'], 'manual')
claimed = next(j for j in store.claim_jobs(10) if j['id'] == job['id'])
assert store.publish(claimed['id'], {'report': '受控待复核报告 https://example.test/source-link',
    'material_ids': [material['id'], private['id'], unknown]}, None)
stamp = datetime.now(TZ)
store.review_recommendations({symbol: {'status': 'ok', 'price': '120', 'as_of': stamp.isoformat()}
                              for symbol in ['SH:600519', 'SZ:000001']}, stamp)
# Complete one real queued review without a structured resolution to expose explicit retry.
for job in store.claim_jobs(10):
    assert store.publish(job['id'], {'report': '受控复核尚无最终判断 https://example.test/source-link',
        'material_ids': [material['id'], private['id'], unknown]}, None)
coverage = store.recommendations_page(account['id'], view='active', limit=10)
assert coverage['total'] == 2 and coverage['counts']['history'] == 104
assert all(item['review_status'] == 'needs_review' and item['price_state'] == 'met' for item in coverage['items'])
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
port = listener.getsockname()[1]
print(json.dumps({'port': port, 'account_id': account['id'], 'other_id': other['id'],
                  'material_id': material['id'], 'private_id': private['id'], 'unknown_id': unknown,
                  'old_run_id': old['id'], 'universe_version': store.universe_snapshot()['universe_version']}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])
