"""Non-billable isolated production-HTTP evidence; usable against a git archive.

Run with PYTHONPATH pointing at the production checkout/archive to compare.
All account inputs below are synthetic arithmetic samples, not market prices.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-label', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='guanlan-thirteenth-ledger-') as directory:
        directory = Path(directory)
        calendar = directory / 'calendar.json'
        calendar.write_text(json.dumps({'coverage': {'start': '2026-10-28', 'end': '2026-11-06'},
            'trading_dates': ['2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06'], 'source': 'controlled arithmetic fixture'}))
        os.environ.update(APP_DB_PATH=str(directory / 'module.sqlite3'), APP_MODE='live', MODEL_NAME='',
            MODEL_KEY_ENV='GUANLAN_THIRTEENTH_UNCONFIGURED_KEY', SEARCH_PROVIDER='disabled', STOCK_CALENDAR_PATH=str(calendar))
        from fastapi.testclient import TestClient
        from app import clock, ledger as ledger_module, ledger_api
        from app.main import create_app
        clock._utc_now = lambda: datetime(2026, 11, 2, 4, tzinfo=timezone.utc)
        app = create_app(directory / 'evidence.sqlite3', worker=False)
        store = app.state.store
        store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
            'scheduled_research_enabled': False, 'market_refresh_enabled': False, 'forecast_evaluation_enabled': False})
        store.put_cache('securities', 'SZ:002354', {'symbol': 'SZ:002354', 'name': '受控模拟股票',
            'kind': 'stock', 'status': 'ok', 'identity_status': 'confirmed', 'source': 'controlled fixture'})
        evidence = {'source_label': args.source_label, 'timezone': 'Asia/Shanghai',
            'test_clock': '2026-11-02T12:00:00+08:00', 'worker': False, 'model_requests': 0,
            'scope': 'Production FastAPI TestClient, resolver, ledger and isolated SQLite; synthetic arithmetic only',
            'source_sha256': {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in (ledger_module, ledger_api)}, 'cases': []}
        with TestClient(app) as client:
            for quantities in (['1000'], ['100', '200']):
                a = client.post('/api/accounts', json={'name': '模拟算术', 'cash': '100000', 'holdings': []}).json()
                base = '/api/accounts/' + a['id']
                for i, quantity in enumerate(quantities):
                    current = client.get(base).json()
                    result = client.post(base + '/trades', json={'operation_id': 'evidence-buy-' + str(i),
                        'expected_version': current['version'], 'fee_plan_version': 1, 'symbol': '002354',
                        'side': 'buy', 'quantity': quantity, 'price': '10', 'mode': 'simulated', 'traded_at': '2026-10-29T10:00:00+08:00'})
                    if result.status_code != 201:
                        raise RuntimeError(result.text)
                before = client.get(base).json()
                before_ledger = client.get(base + '/ledger').json()
                response = client.post(base + '/corrections', json={'operation_id': 'evidence-correction',
                    'expected_version': before['version'], 'cash': '99000', 'holdings': before['holdings'],
                    'reason': '仅改变现金的受控模拟算术样本'})
                after = client.get(base).json()
                after_ledger = client.get(base + '/ledger').json()
                evidence['cases'].append({'quantities': quantities, 'status': response.status_code,
                    'error': response.json() if response.status_code != 201 else None,
                    'returned_holdings': before['holdings'],
                    'before_cash': before['cash'], 'after_cash': after['cash'],
                    'before_position': before_ledger['positions'][0], 'after_position': after_ledger['positions'][0],
                    'ledger_facts_identical': before['ledger']['positions'] == after['ledger']['positions'],
                    'before_fees': before_ledger['summary']['total_fees'], 'after_fees': after_ledger['summary']['total_fees'],
                    'before_realized': before_ledger['summary']['realized_pnl'], 'after_realized': after_ledger['summary']['realized_pnl']})
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'source': args.source_label, 'cases': [{k: c[k] for k in ('quantities', 'status', 'ledger_facts_identical')} for c in evidence['cases']]}, ensure_ascii=False))


if __name__ == '__main__':
    run()
