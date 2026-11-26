"""Exact market facts from saved rows; no live provider or model calls.

The two real-value counterexamples were re-read from the isolated iteration-17
materials, not a model report. Public source: eastmoney:public-website, none
adjustment. Original full-content SHA256 values:
518880 / 0a1105643aa0403c8458abe5d1d7a231:
b43a79a7abfcdb0f174447e60eaee2a86b76e050879a3ff223009923339ce305
002354 / 82eb99f0cb22491197b51513919bdc59:
afdb0b69ededc091700b677900045874df8a23bd88d5e98f2d495027a1154c25
Only the necessary dates and public prices are reproduced below.
"""
import copy
from decimal import Decimal
import json

import httpx
import pytest

from app import agents
from app.calculation_context import projection
from app.context import recall
from app.model import ModelClient
from app.service import ResearchTools
from app.store import Store
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import assert_hot_closeout, config, forced_function, output, seed, setup
from tests.test_seventeenth_child_final_evidence import wire_tool_exchanges


GOLD_CLOSES = [('2026-11-02', '7.941'), ('2026-11-03', '7.989'),
               ('2026-11-04', '8.229'), ('2026-11-05', '8.420')]
ZERO_UPPER = {'time': '2026-10-26', 'open': '8.83', 'high': '8.83', 'low': '8.13', 'close': '8.15'}
CANDLE_FIELDS = ['signed_body', 'real_body', 'upper_shadow', 'lower_shadow']


def saved(store, closes=None, *, rows=None, adjustment='none', symbol='SH:518880'):
    rows = rows if rows is not None else [{'time': day, 'close': close} for day, close in closes]
    return store.save_material({'scope': 'public', 'access': 'structured_data',
        'source_identifier': 'seventeenth-shape:' + symbol,
        'content': json.dumps({'symbol': symbol, 'source': 'controlled saved-price regression',
            'status': 'ok', 'interval': 'daily', 'adjustment': adjustment, 'rows': rows})})


@pytest.fixture
def tools(tmp_path):
    store = Store(tmp_path / 'shape.sqlite3')
    account = store.save_account({'name': 'isolated shape', 'cash': '0', 'holdings': []})
    return ResearchTools(store, store.job(account['latest_job_id']), {'max_tool_result_chars': 6000})


@pytest.mark.asyncio
async def test_actual_four_closes_are_three_rising_intervals_with_actual_dates(tools):
    material = saved(tools.store, GOLD_CLOSES)
    result = await tools.market_indicators(material['id'], window=4)
    streak = result['tail_close_streak']
    assert result['valid_samples'] == 4 and result['return_period']['return_intervals'] == 3
    assert streak == {'status': 'ok', 'direction': 'up', 'change_intervals': 3,
        'baseline_date': '2026-11-02', 'first_change_date': '2026-11-03', 'end_date': '2026-11-05',
        'boundary': 'selection_start', 'preceding_date': None, 'left_censored': True,
        'calendar_continuity': 'unverified'}
    average = sum(Decimal(value) for _, value in GOLD_CLOSES) / 4
    assert Decimal(result['moving_average']) == average
    assert Decimal(result['close_to_ma_change_pct']) == (Decimal('8.420') / average - 1) * 100
    summary = projection('market_indicators', result)
    assert summary['tail_close_streak'] == streak
    assert summary['close_to_ma_change_pct'] == result['close_to_ma_change_pct']
    assert summary['units']['close_to_ma_change_pct'] == 'percent_change'
    assert 'input_closes' not in summary
    summary['tail_close_streak']['change_intervals'] = 999
    assert streak['change_intervals'] == 3


@pytest.mark.asyncio
async def test_actual_open_equals_high_has_zero_upper_shadow_and_raw_material_is_unchanged(tools):
    material = saved(tools.store, rows=[ZERO_UPPER], symbol='SZ:002354')
    result = await tools.query_market_rows(material['id'], start='2026-10-26', end='2026-10-26',
        fields=['open', 'high', 'low', 'close', *CANDLE_FIELDS])
    row = result['rows'][0]
    assert {key: Decimal(row[key]) for key in CANDLE_FIELDS} == {
        'signed_body': Decimal('-.68'), 'real_body': Decimal('.68'),
        'upper_shadow': Decimal('0'), 'lower_shadow': Decimal('.02')}
    assert row['candle_status'] == 'ok' and row['candle_reason'] is None
    assert result['derived_field_methods']['upper_shadow'] == 'high-max(open,close)'
    default = await tools.query_market_rows(material['id'])
    assert not any(key in default['rows'][0] for key in CANDLE_FIELDS)
    assert 'derived_field_methods' not in default
    assert tools.store.material(material['id'])['content'] == material['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('closes,direction,count,baseline,boundary', [
    (['12', '10', '11', '12'], 'up', 2, '2026-11-03', 'direction_change'),
    (['10', '12', '11', '10'], 'down', 2, '2026-11-03', 'direction_change'),
    (['10', '12', '12', '12'], 'flat', 2, '2026-11-03', 'direction_change'),
    (['10', '11', '12', '13'], 'up', 3, '2026-11-02', 'selection_start'),
])
async def test_tail_direction_counts_intervals_and_stops_at_direction_boundary(tools, closes, direction, count, baseline, boundary):
    material = saved(tools.store, [(day, value) for (day, _), value in zip(GOLD_CLOSES, closes)])
    result = await tools.market_indicators(material['id'], window=2)
    streak = result['tail_close_streak']
    assert (streak['direction'], streak['change_intervals'], streak['baseline_date'], streak['boundary']) == (
        direction, count, baseline, boundary)
    assert streak['left_censored'] is (boundary == 'selection_start')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, 'NaN', 'Infinity', '0', '-1', 'not-a-price'])
async def test_missing_or_invalid_middle_close_does_not_join_intervals_or_fill_zero(tools, invalid):
    material = saved(tools.store, [('2026-10-30', '10'), ('2026-11-02', invalid),
        ('2026-11-03', '11'), ('2026-11-04', '12'), ('2026-11-05', '13')])
    result = await tools.market_indicators(material['id'], window=2)
    assert result['status'] == 'insufficient_data' and 'return' not in result
    assert result['invalid_close_dates'] == ['2026-11-02']
    assert result['input_closes'][1] == ['2026-11-02', None]
    streak = result['tail_close_streak']
    assert streak['change_intervals'] == 2 and streak['baseline_date'] == '2026-11-03'
    assert streak['preceding_date'] == '2026-11-02'
    assert streak['boundary'] == 'missing_or_invalid_close' and streak['left_censored']
    assert result['close_to_ma_change_pct'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('closes,count,boundary', [
    ([], 0, 'no_rows'), ([('2026-11-05', '10')], 0, 'selection_start'),
    ([('2026-11-04', '10'), ('2026-11-05', None)], None, 'missing_or_invalid_close')])
async def test_empty_single_and_missing_latest_observation_are_explicit(tools, closes, count, boundary):
    material = saved(tools.store, closes)
    result = await tools.market_indicators(material['id'])
    assert result['status'] == 'insufficient_data'
    assert result['tail_close_streak']['change_intervals'] == count
    assert result['tail_close_streak']['direction'] is None
    assert result['tail_close_streak']['boundary'] == boundary
    assert result['close_to_ma_change_pct'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('adjustment,multiplier', [('none', '1'), ('qfq', '0.5'), ('hfq', '2')])
async def test_same_adjustment_scaling_keeps_interval_and_relative_ma_meaning(tools, adjustment, multiplier):
    factor = Decimal(multiplier)
    material = saved(tools.store, [(day, str(Decimal(value)*factor)) for day, value in GOLD_CLOSES], adjustment=adjustment)
    result = await tools.market_indicators(material['id'], window=4)
    assert result['tail_close_streak']['change_intervals'] == 3
    assert Decimal(result['close_to_ma_change_pct']) == (Decimal('8.420')/(Decimal('32.579')/4)-1)*100


@pytest.mark.asyncio
async def test_unknown_and_mixed_adjustments_have_explicit_boundaries(tools):
    unknown = saved(tools.store, GOLD_CLOSES, adjustment='unknown')
    result = await tools.market_indicators(unknown['id'], window=4)
    assert result['tail_close_streak']['status'] == 'unverified_adjustment'
    assert result['tail_close_streak']['change_intervals'] is None
    rows = [{'time': day, 'close': close} for day, close in GOLD_CLOSES]
    rows[1]['adjustment'] = 'qfq'
    mixed = saved(tools.store, rows=rows)
    result = await tools.market_indicators(mixed['id'], window=4)
    assert result['status'] == 'insufficient_data' and 'moving_average' not in result
    assert result['incompatible_adjustment_dates'] == ['2026-11-03']
    assert result['tail_close_streak']['change_intervals'] == 1
    assert result['tail_close_streak']['baseline_date'] == '2026-11-04'
    assert result['tail_close_streak']['boundary'] == 'incompatible_adjustment'


@pytest.mark.asyncio
async def test_date_selection_and_calendar_gap_are_not_extra_observations(tools):
    material = saved(tools.store, [('2026-10-30', '10'), ('2026-11-02', '11'), ('2026-11-05', '12')])
    result = await tools.market_indicators(material['id'], start='2026-11-02', end='2026-11-06', window=3)
    assert result['tail_close_streak']['change_intervals'] == 1
    assert result['tail_close_streak']['baseline_date'] == '2026-11-02'
    assert result['tail_close_streak']['end_date'] == '2026-11-05'
    assert result['tail_close_streak']['calendar_continuity'] == 'unverified'
    assert result['moving_average'] is None and result['close_to_ma_change_pct'] is None
    assert result['input_closes'] == [['2026-11-02', '11'], ['2026-11-05', '12']]


@pytest.mark.asyncio
@pytest.mark.parametrize('updates,status,reason', [
    ({'high': None}, 'unavailable', 'missing_ohlc:high'),
    ({'low': 'NaN'}, 'invalid', 'nonfinite_or_nonpositive_ohlc'),
    ({'open': '0'}, 'invalid', 'nonfinite_or_nonpositive_ohlc'),
    ({'high': '8.82'}, 'invalid', 'inconsistent_ohlc_bounds'),
    ({'low': '8.16'}, 'invalid', 'inconsistent_ohlc_bounds'),
])
async def test_ohlc_missing_or_contradictory_values_are_null_with_reason(tools, updates, status, reason):
    material = saved(tools.store, rows=[{**ZERO_UPPER, **updates}])
    result = await tools.query_market_rows(material['id'], fields=CANDLE_FIELDS)
    row = result['rows'][0]
    assert all(row[field] is None for field in CANDLE_FIELDS)
    assert row['candle_status'] == status and row['candle_reason'] == reason
    assert set(result['missing_fields']) == set(CANDLE_FIELDS)


@pytest.mark.asyncio
async def test_geometry_projection_keeps_each_returned_value_and_pagination_without_raw_ohlc(tools):
    material = saved(tools.store, rows=[{**ZERO_UPPER, 'time': f'2026-10-{day}'} for day in (26, 27, 28)])
    result = await tools.query_market_rows(material['id'], fields=['open', 'close', *CANDLE_FIELDS], limit=2)
    summary = projection('query_market_rows', result)
    assert summary['row_count'] == 2 and len(summary['candle_geometry']) == 2
    assert summary['has_more'] and summary['next_offset'] == 2
    assert summary['truncated'] and summary['stored_rows'] == 3
    assert summary['requested_range'] == result['requested_range']
    assert summary['material_identity'] == result['material_identity']
    assert summary['derived_field_methods'] == result['derived_field_methods']
    for row, geometric in zip(result['rows'], summary['candle_geometry']):
        assert all(geometric[field] == row[field] for field in ['time', *CANDLE_FIELDS, 'candle_status', 'candle_reason'])
        assert 'open' not in geometric and 'close' not in geometric
    summary['candle_geometry'][0]['upper_shadow'] = '999'
    assert result['rows'][0]['upper_shadow'] == '0.00'
    default = await tools.query_market_rows(material['id'])
    assert projection('query_market_rows', default) is None
    single = await tools.query_market_rows(material['id'], fields=['upper_shadow'], limit=1)
    single_summary = projection('query_market_rows', single)
    assert 'real_body' not in single_summary['candle_geometry'][0]
    assert single_summary['units'] == {'upper_shadow': 'source OHLC price unit'}


@pytest.mark.asyncio
async def test_exact_facts_survive_production_runtime_in_final_model_payload(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=4)
    store, research_tools, context = setup(tmp_path, cfg)
    gold = saved(store, GOLD_CLOSES)
    equity = saved(store, rows=[ZERO_UPPER], symbol='SZ:002354')
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(copy.deepcopy(payload))
        if len(payloads) == 1:
            return output('responses', [
                ('market_indicators', {'material_id': gold['id'], 'window': 4}),
                ('query_market_rows', {'material_id': equity['id'], 'start': '2026-10-26',
                    'end': '2026-10-26', 'fields': CANDLE_FIELDS})])
        if len(payloads) == 2:
            assert forced_function(payload) != 'finish_analysis'
            return output('responses', [('calculate', {'operation': 'subtract', 'values': ['8.83', '8.83']})])
        visible = seed(payload)
        summary = next(item for item in visible['calculation_summaries'] if item['tool'] == 'market_indicators')
        assert summary['tail_close_streak']['change_intervals'] == 3
        assert summary['tail_close_streak']['first_change_date'] == '2026-11-03'
        assert summary['close_to_ma_change_pct'] is not None
        geometry = next(item for item in visible['calculation_summaries'] if item['tool'] == 'query_market_rows')
        assert Decimal(geometry['candle_geometry'][0]['upper_shadow']) == 0
        assert geometry['candle_geometry'][0]['time'] == '2026-10-26'
        assert geometry['candle_geometry'][0]['candle_status'] == 'ok'
        assert 'open' not in geometry['candle_geometry'][0]
        calculations = wire_tool_exchanges(payload, 'calculate')
        assert len(calculations) == 1
        arguments, calculation = calculations[0]
        assert arguments == {'operation': 'subtract', 'values': ['8.83', '8.83']}
        assert 'inputs' in calculation, calculation
        assert calculation['inputs'] == ['8.83', '8.83']
        assert Decimal(calculation['result']) == 0
        assert_hot_closeout(payload, payloads[-2], 'finish_analysis')
        return output('responses', [('finish_analysis', delivery_arguments(report='三个已存收盘上涨间隔；开盘等于最高价，上影为零。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    result = await agents.run_research(context, cfg, research_tools, emit)
    assert result['model_calls'] == 3


@pytest.mark.asyncio
async def test_geometry_checkpoint_restores_exact_values_scope_and_full_input_archive(tmp_path, monkeypatch):
    cfg = config(monkeypatch)
    store, research_tools, context = setup(tmp_path, cfg)
    material = saved(store, rows=[ZERO_UPPER, {**ZERO_UPPER, 'time': '2026-10-27'}])
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, research_tools, emit)
    try:
        await runtime._dispatch('manager', 'query_market_rows', {'material_id': material['id'],
            'fields': ['open', 'close', *CANDLE_FIELDS], 'order': 'asc', 'limit': 1})
        await runtime._dispatch('manager', 'query_market_rows', {'material_id': material['id'], 'fields': ['close']})
        assert len(runtime.calculations) == 1
        checkpoint = await runtime._save_checkpoint({'summary': '保存真实几何值与分页范围，不增加形态判断。'})
    finally:
        await runtime.close()
    # A legacy saved result can identify requested fields from output.fields;
    # it must not require reconstructed or guessed tool arguments.
    checkpoint['calculations'][0].pop('arguments')
    original = copy.deepcopy(checkpoint)
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        summaries = seed(payload)['calculation_summaries']
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary['tool'] == 'query_market_rows' and summary['source_task_id'] == context['job_id']
        assert summary['row_count'] == 1 and summary['has_more'] and summary['next_offset'] == 1
        assert summary['candle_geometry'][0]['time'] == '2026-10-26'
        assert Decimal(summary['candle_geometry'][0]['upper_shadow']) == 0
        assert Decimal(summary['candle_geometry'][0]['real_body']) == Decimal('.68')
        assert {tool['name'] for tool in payload['tools']} == {'finish_analysis'}
        return output('responses', [('finish_analysis', delivery_arguments(report='按已保存的实际日期与数值汇总。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    resumed = agents.ResearchRuntime({**context, 'job_id': 'resumed-shape', 'synthesis_checkpoint': checkpoint},
        cfg, research_tools, emit)
    try:
        summary = resumed.calculations[0]['summary']
        archived = recall(resumed.manager_session, record_id=summary['full_inputs']['record_id'], length=12000)
        raw = json.loads(archived['text'])['output']
        assert raw['rows'][0]['open'] == '8.83' and raw['rows'][0]['close'] == '8.15'
        assert raw['has_more'] and raw['next_offset'] == 1
        resumed.calls = cfg['max_model_calls']-2
        resumed.synthesis_started_calls = resumed.calls
        result = await resumed.run()
        assert result['synthesis_recovered'] and len(payloads) == 1
        assert checkpoint == original
    finally:
        await resumed.close()
