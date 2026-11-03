"""Different return and moving-average periods must survive evidence projection."""
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest

from app.calculation_context import projection
from app.service import ResearchTools
from app.store import Store


@pytest.mark.asyncio
async def test_return_uses_selected_dates_not_moving_average_window(tmp_path):
    store = Store(tmp_path/'periods.sqlite3')
    account = store.save_account({'name':'controlled periods', 'cash':'10000', 'holdings':[]})
    tools = ResearchTools(store, store.job(account['latest_job_id']), {'max_tool_result_chars':14000})
    rows = [{'time':str(date(2026,1,1)+timedelta(days=i)), 'close':str(100+i)} for i in range(60)]
    material = store.save_material({'scope':'public', 'access':'structured_data',
        'content':json.dumps({'symbol':'SZ:159278', 'source':'controlled fixture',
            'status':'ok', 'interval':'daily', 'adjustment':'none', 'rows':rows})})
    full = await tools.market_indicators(material['id'], window=20)
    short = await tools.market_indicators(material['id'], start=rows[-21]['time'], window=20)
    assert Decimal(full['return']) == Decimal('0.59')
    assert Decimal(short['return']) == Decimal(159)/Decimal(139)-1
    assert full['return'] != short['return']
    assert full['moving_average'] == short['moving_average'] == '149.5'
    assert full['return_period'] == {'start':rows[0]['time'], 'end':rows[-1]['time'],
        'return_intervals':59}
    assert short['return_period'] == {'start':rows[-21]['time'], 'end':rows[-1]['time'],
        'return_intervals':20}
    assert full['moving_average_period'] == short['moving_average_period'] == {
        'start':rows[-20]['time'], 'end':rows[-1]['time'], 'window':20}
    for result in (full, short):
        summary = projection('market_indicators', result)
        assert summary['return_period'] == result['return_period']
        assert summary['moving_average_period'] == result['moving_average_period']
        assert summary['return'] == result['return']
        assert 'input_closes' not in summary
    insufficient = await tools.market_indicators(material['id'], start=rows[-3]['time'], window=20)
    assert insufficient['moving_average'] is None
    assert insufficient['moving_average_period']['start'] is None
    assert insufficient['return_period']['return_intervals'] == 2
