"""Production Runtime/Store lookups; controlled HTTP and clock, no model calls."""
import asyncio
import copy
import json

import httpx
import pytest

from app import agents
from app.calculation_context import signature
from app.calendar import SHANGHAI
from app.model import ModelClient, ToolCall
from app.research_progress import tool_round_signature
from app.service import ResearchTools
from app.store import Store
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output


def call(name, arguments=None):
    return ToolCall('controlled-id', name, arguments or {})


def lookup_round():
    return [
        (call('list_business_demands'), {'items': [{'id': 'd', 'state': 'assigned', 'updated_at': 'business-event'}]}),
        (call('list_forecast_targets'), {'id': 'batch', 'delivered_at': 'read-1', 'deadline': 'deadline',
            'items': [{'id': 'target', 'target_date': 'target-day', 'baseline': {'price': '10', 'as_of': 'price-time'},
                       'preparation': {'ready_at': 'ready-time'}}]}),
        (call('list_recommendations'), {'items': [{'id': 'r', 'updated_at': 'refresh-1', 'revision': 1,
            'state': 'watching', 'observed_price': '10', 'price_observed_at': 'quote-time',
            'reviewed_at': 'review-time', 'basis': 'visible basis', 'source': {'updated_at': 'source-time'}}]}),
        (call('list_agent_roles'), {'team_id': 'team', 'roles': [{'role_id': 'manager', 'version': 1,
            'resolved': {'model_id': 'fixture', 'reasoning_effort': 'xhigh'}}]}),
    ]


def test_only_confirmed_receipt_paths_and_call_order_are_normalized_without_mutation():
    original = lookup_round()
    changed = copy.deepcopy(original)
    changed[1][1]['delivered_at'] = 'read-2'
    changed[2][1]['items'][0]['updated_at'] = 'refresh-2'
    changed.reverse()
    before = copy.deepcopy(changed)
    assert tool_round_signature(original) == tool_round_signature(changed)
    assert changed == before
    assert tool_round_signature(original) != tool_round_signature([*original, original[0]])


@pytest.mark.parametrize('index,path,value', [
    (0, ['items', 0, 'state'], 'completed'),
    (0, ['items', 0, 'updated_at'], 'new-business-event'),
    (1, ['id'], 'different-batch'),
    (1, ['deadline'], 'later-deadline'),
    (1, ['items', 0, 'id'], 'different-target'),
    (1, ['items', 0, 'target_date'], 'different-target-day'),
    (1, ['items', 0, 'baseline', 'price'], '11'),
    (1, ['items', 0, 'baseline', 'as_of'], 'new-price-time'),
    (1, ['items', 0, 'preparation', 'ready_at'], 'new-ready-time'),
    (2, ['items', 0, 'state'], 'triggered'),
    (2, ['items', 0, 'observed_price'], '11'),
    (2, ['items', 0, 'price_observed_at'], 'new-quote-time'),
    (2, ['items', 0, 'reviewed_at'], 'new-review-time'),
    (2, ['items', 0, 'revision'], 2),
    (2, ['items', 0, 'basis'], 'new visible basis'),
    (2, ['items', 0, 'source', 'updated_at'], 'new-source-time'),
    (3, ['team_id'], 'different-team'),
    (3, ['roles', 0, 'resolved', 'reasoning_effort'], 'high'),
])
def test_business_identity_content_prices_and_other_times_remain_progress(index, path, value):
    original = lookup_round()
    changed = copy.deepcopy(original)
    target = changed[index][1]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert tool_round_signature(original) != tool_round_signature(changed)


def test_pagination_row_order_and_non_lookup_rounds_remain_strict():
    original = lookup_round()
    changed = copy.deepcopy(original)
    changed[1][0].arguments['offset'] = 10
    assert tool_round_signature(original) != tool_round_signature(changed)
    rows = [(call('list_recommendations'), {'items': [{'id': 'a'}, {'id': 'b'}]})]
    assert tool_round_signature(rows) != tool_round_signature([(rows[0][0], {'items': list(reversed(rows[0][1]['items']))})])
    mixed = [*original, (call('calculate', {'operation': 'sum', 'values': [1, 2]}), {'result': '3'})]
    strict = signature('tool_round', {}, [(c.name, c.arguments, value) for c, value in mixed])
    assert tool_round_signature(mixed) == strict
    assert tool_round_signature(mixed) != tool_round_signature(list(reversed(mixed)))
    changed = copy.deepcopy(mixed)
    changed[1][1]['delivered_at'] = 'read-2'
    assert tool_round_signature(mixed) != tool_round_signature(changed)


def store_fixture(tmp_path, monkeypatch, frozen_clock, protocol):
    frozen_clock.set('2026-11-06T16:00:00+08:00')
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps({'coverage': {'start': '2026-11-01', 'end': '2026-12-31'},
        'source': 'controlled fixture calendar', 'holidays': []}))
    monkeypatch.setenv('STOCK_CALENDAR_PATH', str(calendar))
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None, max_agent_turns=None,
        synthesis_max_calls=None, max_context_chars=10000, analysis_timeout_seconds=60,
        agent_timeout_seconds=40, model_request_hard_timeout_seconds=2,
        synthesis_request_hard_timeout_seconds=2)
    store = Store(tmp_path / 'lookups.sqlite3')
    account = store.save_account({'name': 'controlled lookup fixture', 'cash': '1000',
        'holdings': [{'symbol': 'SH:600519', 'quantity': '1', 'cost_price': '100'}]})
    job = store.job(account['latest_job_id'])
    batch = store.create_forecast_batch(account['id'], kind='test',
        at=frozen_clock.instant.astimezone(SHANGHAI), calendar_path=calendar)
    store.bind_forecast_job(batch['id'], job['id'])
    with store.connect() as db:
        store._save_suggestions(db, job, delivery_arguments('Controlled previous suggestion.', suggestions=[{
            'security_id': 'SH:600519', 'action': 'watch', 'basis': 'Controlled unchanged basis',
            'price_conditions': [{'operator': 'gte', 'value': '110'}]}]))
        db.execute('''INSERT INTO research_demands
            (event_key,account_id,input_version,kind,reason,state,job_id,detail,created_at,updated_at)
            VALUES(?,?,?,'recommendation_review','controlled reason','assigned',?,'unchanged',?,?)''',
            ('controlled-demand', account['id'], account['version'], job['id'],
             frozen_clock.instant.isoformat(), frozen_clock.instant.isoformat()))
    context = {'job_id': job['id'], 'account': account, 'input_version': account['version']}
    return cfg, store, ResearchTools(store, job, cfg), context


LOOKUPS = [('list_business_demands', {'limit': 30, 'offset': 0}),
           ('list_forecast_targets', {'limit': 30, 'offset': 0}),
           ('list_recommendations', {'view': 'active', 'limit': 30, 'offset': 0}),
           ('list_agent_roles', {})]


def tool_names(payload):
    return {tool.get('name') or tool.get('function', {}).get('name') for tool in payload['tools']}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_three_semantically_identical_lookup_rounds_force_final_after_real_compaction(
        tmp_path, monkeypatch, frozen_clock, protocol):
    cfg, store, tools, context = store_fixture(tmp_path, monkeypatch, frozen_clock, protocol)
    requests, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        number = len(requests)
        if number == 4:
            assert tool_names(payload) == {'finish_analysis'}
            return output(protocol, [('finish_analysis', delivery_arguments('三轮查阅无新增业务事实，提交有限结果与未知。', covered_demand_ids=[]))])
        assert number < 4 and tool_names(payload) != {'finish_analysis'}
        frozen_clock.advance(seconds=1)
        store.review_recommendations({}, frozen_clock.instant.astimezone(SHANGHAI))
        orders = ([0, 1, 2, 3], [3, 1, 0, 2], [2, 0, 3, 1])
        return output(protocol, [LOOKUPS[index] for index in orders[number - 1]])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 4
    assert result['context_compactions'] > 0
    no_progress = [event for event in events if event['type'] == 'research_no_progress']
    assert len(no_progress) == 1 and no_progress[0]['payload']['identical_tool_rounds'] == 3
    read_results = [event['payload'] for event in events if event['type'] == 'tool_result']
    forecast_stamps = [item['result']['delivered_at'] for item in read_results if item['tool'] == 'list_forecast_targets']
    recommendation_stamps = [item['result']['items'][0]['updated_at'] for item in read_results if item['tool'] == 'list_recommendations']
    assert len(set(forecast_stamps)) == len(set(recommendation_stamps)) == 3
    # Stamps remain in real tool-result messages and events; normalization only
    # affects the no-progress comparison, never model-visible evidence.
    assert all(stamp in json.dumps(requests, ensure_ascii=False) for stamp in forecast_stamps)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_new_business_content_continues_beyond_three_lookup_rounds(
        tmp_path, monkeypatch, frozen_clock, protocol):
    cfg, store, tools, context = store_fixture(tmp_path, monkeypatch, frozen_clock, protocol)
    requests, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        number = len(requests)
        assert tool_names(payload) != {'finish_analysis'}
        if number == 6:
            return output(protocol, [('finish_analysis', delivery_arguments('业务内容持续变化，取得五次新事实后自然提交。', covered_demand_ids=[]))])
        frozen_clock.advance(seconds=1)
        store.review_recommendations({}, frozen_clock.instant.astimezone(SHANGHAI))
        with store.connect() as db:
            db.execute("UPDATE research_demands SET detail=? WHERE event_key='controlled-demand'", (f'new evidence {number}',))
        return output(protocol, LOOKUPS[number % 4:] + LOOKUPS[:number % 4])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 6
    assert not any(event['type'] == 'research_no_progress' for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_actual_live_child_wait_remains_useful_in_both_protocols(monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None, max_agent_turns=None,
        synthesis_max_calls=None, analysis_timeout_seconds=60, agent_timeout_seconds=40,
        model_request_hard_timeout_seconds=20, synthesis_request_hard_timeout_seconds=20)
    entered, release = asyncio.Event(), asyncio.Event()
    requests, events = [], []
    child = None
    async def handler(request):
        payload = json.loads(request.content)
        history = payload.get('input', payload.get('messages'))
        first = next(json.loads(item['content']) for item in history if item.get('role') == 'user')
        if first.get('task'):
            entered.set()
            await release.wait()
            return output(protocol, [('submit_report', {'summary': 'Actual live child completed the local fixture.'})])
        requests.append(payload)
        assert tool_names(payload) != {'finish_analysis'}
        if len(requests) <= 4:
            assert not child.task_handle.done()
            if len(requests) == 4:
                release.set()
                await asyncio.wait_for(child.task_handle, 2)
            return output(protocol, [('wait_agents', {'agent_ids': [child.id], 'timeout_seconds': 0})])
        assert len(requests) == 5 and child.status == 'completed'
        return output(protocol, [('finish_analysis', delivery_arguments('等待本次真实活跃句柄完成后自然交付。'))])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime({'account': {'id': 'controlled', 'version': 1}}, cfg, object(), emit)
    try:
        created = await runtime._spawn({'task': 'controlled live child'})
        child = runtime.children[created['agent_id']]
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.run()
        assert len(requests) == 5
        assert not any(event['type'] == 'research_no_progress' for event in events)
    finally:
        release.set()
        await runtime.close()
