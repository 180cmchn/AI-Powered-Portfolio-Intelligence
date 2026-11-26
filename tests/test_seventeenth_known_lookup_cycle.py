"""Known lookup cycles with controlled clocks, tools and model transport."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import agents
from app.materials import index_of
from app.model import ModelClient
from app.research_progress import KnownLookupTracker, native_material_identity
from app.store import Store
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import assert_hot_closeout, config, forced_function, output
from tests.test_seventeenth_lookup_progress import LOOKUPS, call, lookup_round, store_fixture, tool_names


DEMANDS, FORECASTS, RECOMMENDATIONS, ROLES = LOOKUPS
MATERIALS = ('list_materials', {'limit': 20, 'offset': 0})
SEARCH = ('search_materials', {'query': '年报', 'limit': 20, 'offset': 0})
BUSINESS_PAGES = [DEMANDS, RECOMMENDATIONS]
DISCOVERY_PAGES = [FORECASTS, MATERIALS, ROLES]
FINAL = [('finish_analysis', delivery_arguments('基于本轮已可见的证据提交，未解决事项保持未知。', covered_demand_ids=[]))]
WARNING = 'research_lookup_repeat_warning'
BOUNDARY = 'progress_boundary'


def history(payload):
    return payload.get('input', payload.get('messages'))


def user_messages(payload, key):
    return [value[key] for message in history(payload) if message.get('role') == 'user'
            and isinstance((value := json.loads(message['content'])), dict) and key in value]


def digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=True).encode()).hexdigest()


def model_transport(monkeypatch, handler, appended=None):
    def factory(cfg, before_request=None):
        client = ModelClient(cfg, before_request, httpx.MockTransport(handler))
        if appended is not None:
            add_user = client.add_user

            def record_append(session, value):
                if WARNING not in value and BOUNDARY not in value:
                    return add_user(session, value)
                count, prefix = len(session.history), digest(session.history)
                assert session.history[-1].get('type') == 'function_call_output' or session.history[-1].get('role') == 'tool'
                add_user(session, value)
                assert len(session.history) == count + 1
                assert digest(session.history[:count]) == prefix
                assert json.loads(session.history[-1]['content']) == value
                appended.append(value)

            client.add_user = record_append
        return client

    monkeypatch.setattr(agents, 'ModelClient', factory)


def event_payloads(events, kind):
    return [event['payload'] for event in events if event['type'] == kind]


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_alternating_known_pages_warn_then_close_out(tmp_path, monkeypatch, frozen_clock, protocol):
    cfg, store, tools, context = store_fixture(tmp_path, monkeypatch, frozen_clock, protocol)
    # Keep the research prefix in one epoch; final projection remains intentional.
    cfg['max_context_chars'] = 240_000
    requests, events, appended = [], [], []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        index = len(requests) - 1
        if index == 4:
            assert_hot_closeout(payload, requests[-2], 'finish_analysis')
            return output(protocol, FINAL)
        assert index < 4
        assert {'get_material', 'read_material_range', 'calculate', 'finish_analysis'} <= tool_names(payload)
        if index == 3:
            assert len(user_messages(payload, WARNING)) == 1, 'first all-known round must warn before closeout'
            assert not event_payloads(events, 'research_no_progress')
            before_warning = history(requests[index - 1])
            assert digest(history(payload)[:len(before_warning)]) == digest(before_warning)
        else:
            assert not user_messages(payload, WARNING)
        frozen_clock.advance(seconds=1)
        store.review_recommendations({}, frozen_clock.instant)
        return output(protocol, BUSINESS_PAGES if index % 2 == 0 else DISCOVERY_PAGES)

    async def emit(event):
        events.append(event)

    model_transport(monkeypatch, handler, appended)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 5
    assert result['submission']['action'] == 'finish_analysis'
    assert [next(iter(value)) for value in appended] == [WARNING, BOUNDARY]
    warnings = event_payloads(events, WARNING)
    assert len(warnings) == 1
    assert warnings[0]['known_lookup_rounds'] == 1
    assert warnings[0]['action'] == 'warn_before_closeout'
    assert event_payloads(events, 'research_no_progress') == [{
        'known_lookup_rounds': 2, 'reason': 'repeated_known_lookups', 'action': 'submit_existing_evidence'}]


@pytest.mark.asyncio
async def test_recommendation_detail_timestamp_matches_persisted_review_refresh(tmp_path, monkeypatch, frozen_clock):
    _, store, tools, _ = store_fixture(tmp_path, monkeypatch, frozen_clock, 'responses')
    row = (await tools.list_recommendations(view='active', limit=30, offset=0))['items'][0]
    detail = await tools.get_recommendation(row['id'])
    assert detail['updated_at'] == row['updated_at']
    frozen_clock.advance(seconds=1)
    assert await tools.get_recommendation(row['id']) == detail
    store.review_recommendations({}, frozen_clock.instant)
    refreshed = await tools.get_recommendation(row['id'])
    after_row = (await tools.list_recommendations(view='active', limit=30, offset=0))['items'][0]
    assert refreshed['updated_at'] == after_row['updated_at'] != detail['updated_at']
    assert {k: v for k, v in refreshed.items() if k != 'updated_at'} == {
        k: v for k, v in detail.items() if k != 'updated_at'}
    detail_call = call('get_recommendation', {'recommendation_id': row['id']})
    tracker = KnownLookupTracker()
    before = copy.deepcopy(refreshed)
    assert not tracker.observe([(detail_call, detail)])
    assert tracker.observe([(detail_call, refreshed)])
    assert refreshed == before


def test_tracker_compares_against_pre_round_cache_and_seeds_mixed_successes():
    tracker = KnownLookupTracker()
    index = (call('list_materials'), {'items': [{'id': 'm'}]})
    new_page = (call('list_materials', {'offset': 1}), {'items': []})
    original = (call('get_material', {'material_id': 'm'}), {'content': 'original text'})
    assert not tracker.observe([])
    assert not tracker.observe([index, index])
    assert tracker.observe([index, index])
    assert not tracker.observe([new_page, original])
    assert tracker.observe([new_page])
    assert not tracker.observe([index, original])
    assert not tracker.observe([(index[0], {'items': []}), index])
    assert tracker.observe([index])


@pytest.mark.parametrize('name', ['get_material', 'read_material_range', 'recall_context', 'calculate',
    'query_market_rows', 'market_indicators', 'save_material', 'spawn_research', 'send_message',
    'wait_agents', 'agent_status', 'finish_analysis', 'submit_report'])
def test_non_lookup_tools_do_not_become_known(name):
    tracker = KnownLookupTracker()
    result = [(call(name), {'same': 'result'})]
    assert not tracker.observe(result)
    assert not tracker.observe(result)


@pytest.mark.parametrize('name,args,change', [
    ('list_materials', {'limit': 2, 'offset': 0}, {'offset': 2}),
    ('list_forecast_targets', {'limit': 30, 'offset': 0}, {'limit': 50}),
    ('search_materials', {'query': '年报', 'limit': 2}, {'query': '年报 '}),
    ('list_recommendations', {'view': 'active', 'limit': 30}, {'view': 'history'}),
    ('get_recommendation', {'recommendation_id': 'old'}, {'recommendation_id': 'new'}),
])
def test_tracker_keeps_exact_arguments_even_when_page_content_matches(name, args, change):
    tracker = KnownLookupTracker()
    same_output = {'items': []}
    assert not tracker.observe([(call(name, args), same_output)])
    assert tracker.observe([(call(name, dict(reversed(list(args.items())))), same_output)])
    assert not tracker.observe([(call(name, {**args, **change}), same_output)])


@pytest.mark.parametrize('index,path,value', [
    (0, ('items', 0, 'state'), 'completed'),
    (0, ('items', 0, 'updated_at'), 'new-business-event'),
    (1, ('items', 0, 'baseline', 'as_of'), 'new-baseline-time'),
    (2, ('items', 0, 'state'), 'triggered'),
    (2, ('items', 0, 'revision'), 2),
    (2, ('items', 0, 'observed_price'), '11'),
    (2, ('items', 0, 'price_observed_at'), 'new-quote-time'),
    (2, ('items', 0, 'reviewed_at'), 'new-review-time'),
    (2, ('items', 0, 'source', 'updated_at'), 'new-source-time'),
])
def test_tracker_detects_business_output_changes(index, path, value):
    initial = lookup_round()
    changed = copy.deepcopy(initial)
    field = changed[index][1]
    for key in path[:-1]:
        field = field[key]
    field[path[-1]] = value
    tracker = KnownLookupTracker()
    assert not tracker.observe(initial)
    assert tracker.observe(initial)
    assert not tracker.observe(changed)
    assert tracker.observe(changed)
    assert not tracker.observe(initial)  # Last result, not a set of historic states.


def test_comparison_normalizes_only_refresh_fields_without_mutating_outputs():
    original = lookup_round()
    refreshed = copy.deepcopy(original)
    refreshed[1][1]['delivered_at'] = 'another-receipt'
    refreshed[2][1]['items'][0]['updated_at'] = 'another-refresh'
    snapshots = copy.deepcopy((original, refreshed))
    tracker = KnownLookupTracker()
    assert not tracker.observe(original)
    assert tracker.observe(list(reversed(refreshed)))
    assert (original, refreshed) == snapshots


def test_complete_fingerprint_includes_order_long_index_tail_and_errors():
    tracker = KnownLookupTracker()
    query = call('search_materials', {'query': '材料'})
    first = {'id': 'a', 'excerpt': 'x' * 16000}
    second = {'id': 'b'}
    assert not tracker.observe([(query, {'items': [first, second]})])
    changed_tail = {'id': 'c'}
    assert not tracker.observe([(query, {'items': [first, changed_tail]})])
    assert not tracker.observe([(query, {'items': [changed_tail, first]})])
    failed = {'failed': True, 'error': 'fixture failure', 'error_code': 'query_error'}
    assert not tracker.observe([(query, failed), (call('calculate'), {'result': '3'})])
    assert not tracker.observe([(query, failed)])
    assert tracker.observe([(query, failed)])
    assert not tracker.observe([(query, {**failed, 'error_code': 'different_error'})])


def test_tracker_evicts_oldest_entry_at_128_and_hits_update_recency():
    pages = [(call('list_materials', {'offset': index, 'limit': 1}), {'items': []}) for index in range(129)]
    tracker = KnownLookupTracker(max_entries=128)
    assert not tracker.observe(pages[:128])
    assert not tracker.observe(pages[128:])
    assert not tracker.observe(pages[:1])  # Revisit of the evicted first key.

    tracker = KnownLookupTracker(max_entries=128)
    assert not tracker.observe(pages[:128])
    assert tracker.observe(pages[:1])
    assert not tracker.observe(pages[128:])
    assert tracker.observe(pages[:1])
    assert not tracker.observe(pages[1:2])  # The second key is now oldest.


def local_runtime(monkeypatch, handler, events, *, protocol='responses', native_search=False):
    cfg = config(monkeypatch, model_protocol=protocol, model_native_search=native_search,
        max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None, max_context_chars=240_000,
        analysis_timeout_seconds=60, agent_timeout_seconds=40, model_request_hard_timeout_seconds=2,
        synthesis_request_hard_timeout_seconds=2)
    model_transport(monkeypatch, handler)
    tools = SimpleNamespace(
        list_business_demands=AsyncMock(return_value={'items': [{'id': 'd', 'state': 'assigned'}]}),
        list_forecast_targets=AsyncMock(return_value={'items': [{'id': 'f'}]}),
        list_recommendations=AsyncMock(return_value={'items': [{'id': 'r', 'revision': 1}]}),
        list_materials=AsyncMock(return_value=[{'id': 'm'}]),
        search_materials=AsyncMock(return_value=[{'id': 'm', 'excerpt': 'index match'}]),
        get_material=AsyncMock(return_value={'id': 'm', 'content': 'visible original text'}),
        read_material_range=AsyncMock(return_value={'id': 'm', 'content': 'visible selected range'}),
        calculate=AsyncMock(return_value={'result': '3'}),
    )

    async def emit(event):
        events.append(event)

    return agents.ResearchRuntime({'account': {'id': 'fixture', 'version': 1}}, cfg, tools, emit), tools


@pytest.mark.asyncio
@pytest.mark.parametrize('new_calls,change', [
    ([('get_material', {'material_id': 'm'})], None),
    ([('read_material_range', {'material_id': 'm', 'start': 7, 'length': 10})], None),
    ([('list_materials', {'limit': 20, 'offset': 20})], None),
    ([('search_materials', {**SEARCH[1], 'query': '半年报'})], None),
    ([('calculate', {'operation': 'sum', 'values': [1, 2]})], None),
    (BUSINESS_PAGES, ('list_recommendations', {'items': [{'id': 'r', 'revision': 2}]})),
    (BUSINESS_PAGES, ('list_business_demands', {'items': [{'id': 'd', 'state': 'completed'}]})),
    (DISCOVERY_PAGES, ('list_materials', [{'id': 'm'}, {'id': 'new-material'}])),
], ids=['original', 'range', 'new-page', 'new-term', 'calculate', 'revision', 'demand-state', 'new-material'])
async def test_progress_after_warning_resets_streak_and_allows_submission(monkeypatch, new_calls, change):
    events, requests = [], []
    discovery = [*DISCOVERY_PAGES, SEARCH]
    script = [BUSINESS_PAGES, discovery, BUSINESS_PAGES, new_calls, discovery, FINAL]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert {'calculate', 'finish_analysis'} <= tool_names(payload)
        assert len(event_payloads(events, WARNING)) == [0, 0, 0, 1, 1, 2][index]
        if index == 3 and change is not None:
            name, value = change
            getattr(tools, name).return_value = value
        return output('responses', script[index])

    runtime, tools = local_runtime(monkeypatch, handler, events)
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(script)
    assert result['submission']['action'] == 'finish_analysis'
    assert not event_payloads(events, 'research_no_progress')
    assert not any(isinstance(event['result'], dict) and event['result'].get('failed')
                   for event in event_payloads(events, 'tool_result'))


@pytest.mark.asyncio
@pytest.mark.parametrize('progress', ['native_materials', 'child_event'])
async def test_new_sources_and_child_events_exempt_a_known_lookup_round(monkeypatch, progress):
    events, requests = [], []
    material = {'id': 'native-source', 'title': 'New local fixture source'}
    script = [BUSINESS_PAGES, DISCOVERY_PAGES, BUSINESS_PAGES, DISCOVERY_PAGES, BUSINESS_PAGES, FINAL]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert 'calculate' in tool_names(payload)
        assert len(event_payloads(events, WARNING)) == [0, 0, 0, 1, 1, 2][index]
        if index == 2 and progress == 'child_event':
            await runtime._publish('agent_progress', 'finished-child', {'summary': 'new child finding'})
        if index == 3 and progress == 'child_event':
            assert user_messages(payload, 'research_events')[0][0]['payload']['summary'] == 'new child finding'
        if index == 4 and progress == 'native_materials':
            assert user_messages(payload, 'native_source_materials') == [[material]]
        return output('responses', script[index])

    runtime, _ = local_runtime(monkeypatch, handler, events)
    if progress == 'native_materials':
        monkeypatch.setattr(runtime, '_record_native', AsyncMock(side_effect=lambda *_: [material] if len(requests) == 4 else []))
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(script)
    assert not event_payloads(events, 'research_no_progress')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'running'])
async def test_active_children_exempt_but_do_not_prevent_learning_known_pages(monkeypatch, status):
    events, requests = [], []
    script = [BUSINESS_PAGES, DISCOVERY_PAGES, BUSINESS_PAGES, DISCOVERY_PAGES, BUSINESS_PAGES, FINAL]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert 'calculate' in tool_names(payload)
        assert len(event_payloads(events, WARNING)) == int(index == 5)
        if index == 4:
            child.status = 'completed'
        return output('responses', script[index])

    runtime, _ = local_runtime(monkeypatch, handler, events)
    child = agents.AgentState('fixture-child', 'fixture', 'fixture', runtime.client.session('child', {}), status=status)
    runtime.children[child.id] = child
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(script)
    assert not event_payloads(events, 'research_no_progress')


@pytest.mark.asyncio
@pytest.mark.parametrize('signal', ['native_materials', 'active_child', 'child_event'])
async def test_identical_known_pages_are_not_closeout_trimmed_by_live_progress(monkeypatch, signal):
    events, requests = [], []
    material = {'id': 'native-source', 'title': 'New local fixture source'}
    # Three strict repeats of the same business pages, then explicit submission.
    script = [BUSINESS_PAGES, BUSINESS_PAGES, BUSINESS_PAGES, FINAL]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert 'calculate' in tool_names(payload), 'live progress must not trim tools to submission only'
        # The warning may legitimately fire on the one round that has no live
        # progress yet; the legacy guard must never add a closeout boundary.
        assert len(event_payloads(events, WARNING)) <= int(index >= 2)
        assert not event_payloads(events, 'research_no_progress')
        assert not user_messages(payload, BOUNDARY)
        if signal == 'child_event' and index == 1:
            await runtime._publish('agent_progress', 'finished-child', {'summary': 'new child finding'})
        if signal == 'child_event' and index == 2:
            assert user_messages(payload, 'research_events')[0][0]['payload']['summary'] == 'new child finding'
        if signal == 'native_materials' and index == 3:
            assert user_messages(payload, 'native_source_materials') == [[material]]
        return output('responses', script[index])

    runtime, _ = local_runtime(monkeypatch, handler, events)
    if signal == 'native_materials':
        monkeypatch.setattr(runtime, '_record_native',
            AsyncMock(side_effect=lambda *_: [material] if len(requests) == 3 else []))
    if signal == 'active_child':
        child = agents.AgentState('fixture-child', 'fixture', 'fixture', runtime.client.session('child', {}), status='running')
        runtime.children[child.id] = child
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(script)
    assert result['submission']['action'] == 'finish_analysis'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['wait_agents', 'agent_status'])
async def test_legitimate_wait_is_not_repeated_failed_work(monkeypatch, name):
    events, requests = [], []
    waiting = asyncio.Event()

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert 'calculate' in tool_names(payload)
        if len(requests) == 5:
            waiting.set()
            await child.task_handle
            return output('responses', FINAL)
        assert len(requests) < 5 and not child.task_handle.done()
        args = {'agent_ids': [child.id], 'timeout_seconds': 0} if name == 'wait_agents' else {'agent_id': child.id}
        return output('responses', [(name, args)])

    runtime, _ = local_runtime(monkeypatch, handler, events)
    child = agents.AgentState('fixture-child', 'fixture', 'fixture', runtime.client.session('child', {}), status='running')
    child.task_handle = asyncio.create_task(waiting.wait())
    runtime.children[child.id] = child
    try:
        result = await runtime.run()
        assert result['model_calls'] == len(requests) == 5
        assert not event_payloads(events, WARNING)
        assert not event_payloads(events, 'research_no_progress')
    finally:
        waiting.set()
        await child.task_handle


@pytest.mark.asyncio
async def test_child_inbox_delivery_resets_warning_streak(monkeypatch):
    events, requests = [], []
    message = 'new local instruction for the running child'
    report = [('submit_report', {'summary': 'Completed the follow-up evidence check.'})]
    script = [[MATERIALS], [SEARCH], [MATERIALS], [SEARCH], [MATERIALS], report]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert {'list_materials', 'submit_report'} <= tool_names(payload)
        assert len(event_payloads(events, WARNING)) == [0, 0, 0, 1, 1, 2][index]
        if index == 2:
            child_id, _ = runtime.request_owner.get()
            await runtime._dispatch('manager', 'send_message', {'agent_id': child_id, 'message': message})
        if index == 3:
            assert user_messages(payload, 'manager_messages') == [[message]]
        return output('responses', script[index])

    runtime, _ = local_runtime(monkeypatch, handler, events)
    try:
        child = runtime.children[(await runtime._spawn({'task': 'Controlled inbox check'}))['agent_id']]
        await asyncio.wait_for(child.task_handle, 2)
        assert child.status == 'completed', child.error
        assert len(requests) == len(script)
        assert not event_payloads(events, 'research_no_progress')
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_child_follow_up_resets_cache_and_separate_sessions_do_not_share_it(monkeypatch):
    events, request_counts, requests_by_round = [], {}, {}
    report = [('submit_report', {'summary': 'Bounded child lookup results.'})]
    script = [[MATERIALS], [SEARCH], [MATERIALS], [SEARCH], report]

    async def handler(request):
        payload = json.loads(request.content)
        child_id, _ = runtime.request_owner.get()
        key = (child_id, runtime.children[child_id].round)
        index = request_counts.get(key, 0)
        request_counts[key] = index + 1
        requests_by_round.setdefault(key, []).append(payload)
        assert index < len(script)
        if index == 4:
            assert_hot_closeout(payload, requests_by_round[key][-2], 'submit_report')
        else:
            assert {'list_materials', 'submit_report'} <= tool_names(payload)
        return output('responses', script[index])

    runtime, _ = local_runtime(monkeypatch, handler, events)

    async def verify_round(child, since):
        await asyncio.wait_for(child.task_handle, 2)
        assert child.status == 'completed', child.error
        assert request_counts[(child.id, child.round)] == len(script)
        assert [warning['known_lookup_rounds'] for warning in event_payloads(events[since:], WARNING)] == [1]
        assert [close['reason'] for close in event_payloads(events[since:], 'research_no_progress')] == ['repeated_known_lookups']

    try:
        first = runtime.children[(await runtime._spawn({'task': 'First controlled child'}))['agent_id']]
        await verify_round(first, 0)
        old_session = first.session
        since = len(events)
        resumed = await runtime._dispatch('manager', 'follow_up', {'agent_id': first.id, 'task': 'Review the same pages again'})
        assert resumed['round'] == 2 and first.session is old_session
        await verify_round(first, since)
        since = len(events)
        second = runtime.children[(await runtime._spawn({'task': 'Independent controlled child'}))['agent_id']]
        await verify_round(second, since)
        assert second.session is not first.session
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    {'error': 'fixture failure'}, {'failed': True}, {'ok': False},
    {'status': 'failed'}, {'status': 'error'}, {'status': 'FAILED'}, {'status': ' error '},
], ids=['error', 'failed', 'ok-false', 'status-failed', 'status-error', 'status-uppercase', 'status-whitespace'])
@pytest.mark.parametrize('pattern', ['identical', 'alternating', 'alternating-with-success'])
@pytest.mark.parametrize('child_status', ['queued', 'running'])
async def test_failed_lookups_close_out_with_unrelated_live_child(monkeypatch, failure, pattern, child_status):
    events, requests = [], []
    a = ('get_recommendation', {'recommendation_id': 'a'})
    b = ('get_recommendation', {'recommendation_id': 'b'})
    alternating = pattern != 'identical'
    if alternating:
        # The extra successful lookup must be counted in the warning payload.
        page_a = [MATERIALS, a] if pattern == 'alternating-with-success' else [a]
        rounds = [page_a, [b], page_a, [b]]
        closeout = {'known_lookup_rounds': 2, 'reason': 'repeated_known_lookups',
            'action': 'submit_existing_evidence'}
    else:
        rounds = [[a], [a], [a]]
        closeout = {'identical_tool_rounds': 3, 'action': 'submit_existing_evidence'}

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert child.status == child_status
        if index == len(rounds):
            assert_hot_closeout(payload, requests[-2], 'finish_analysis')
            assert event_payloads(events, 'research_no_progress') == [closeout]
            return output('responses', FINAL)
        assert {'get_recommendation', 'finish_analysis'} <= tool_names(payload)
        assert not event_payloads(events, 'research_no_progress')
        if alternating:
            assert len(event_payloads(events, WARNING)) == int(index == 3)
        return output('responses', rounds[index])

    runtime, tools = local_runtime(monkeypatch, handler, events)
    tools.get_recommendation = AsyncMock(return_value=failure)
    child = agents.AgentState('unrelated-child', 'fixture', 'fixture',
        runtime.client.session('child', {}), status=child_status)
    runtime.children[child.id] = child
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(rounds) + 1
    assert result['submission']['action'] == 'finish_analysis'
    assert tools.get_recommendation.await_count == len(rounds)
    if alternating:
        assert event_payloads(events, WARNING) == [{'known_lookup_rounds': 1,
            'lookup_count': len(rounds[2]), 'action': 'warn_before_closeout'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['failed', 'error', 'exception'],
    ids=['status-failed', 'status-error', 'tool-exception'])
@pytest.mark.parametrize('protocol,progress', [
    ('responses', 'native_materials'),
    ('responses', 'child_event'),
    ('chat_completions', 'child_event'),
], ids=['responses-native', 'responses-child', 'chat-child'])
async def test_delivered_progress_prevents_identical_failed_lookup_closeout(monkeypatch, failure, protocol, progress):
    events, requests = [], []
    lookup = ('get_recommendation', {'recommendation_id': 'a'})
    material = {'id': 'native-source', 'title': 'New local fixture source'}
    finding = {'summary': 'new child finding'}
    native = progress == 'native_materials'

    async def handler(request):
        index = len(requests)
        requests.append(json.loads(request.content))
        assert index < 4
        if index == 1 and not native:
            # The manager delivers this event at the start of round three.
            await runtime._publish('agent_progress', 'finished-child', finding)
        if index == 3:
            return output(protocol, FINAL)
        response = output(protocol, [lookup])
        if index == 2 and native:
            body = response.json()
            body['output'].insert(0, {'type': 'web_search_call', 'id': 'ws_fixture', 'status': 'completed',
                'action': {'type': 'search', 'query': 'fixture source',
                           'sources': [{'url': 'https://example.com/fixture-source'}]}})
            return httpx.Response(200, json=body)
        return response

    runtime, tools = local_runtime(monkeypatch, handler, events, protocol=protocol, native_search=native)
    if native:
        tools.record_native_search = AsyncMock(return_value=[material])
    if failure == 'exception':
        tools.get_recommendation = AsyncMock(side_effect=RuntimeError('fixture failure'))
        failed_result = {'error': 'fixture failure', 'failed': True, 'error_code': 'invalid_tool_arguments'}
    else:
        failed_result = {'status': failure}
        tools.get_recommendation = AsyncMock(return_value=failed_result)

    result = await runtime.run()
    assert result['model_calls'] == len(requests) == 4
    assert result['submission']['action'] == 'finish_analysis'
    assert [event['result'] for event in event_payloads(events, 'tool_result')
            if event['tool'] == 'get_recommendation'] == [failed_result] * 3
    if native:
        assert [event['materials'] for event in event_payloads(events, 'native_search')] == [[material]]
    else:
        assert not user_messages(requests[1], 'research_events')
        assert [event['payload'] for batch in user_messages(requests[2], 'research_events')
                for event in batch] == [finding]
    assert {'get_material', 'calculate', 'finish_analysis'} <= tool_names(requests[3])
    assert not event_payloads(events, 'research_no_progress')
    assert not user_messages(requests[3], BOUNDARY)
    if native:
        assert user_messages(requests[3], 'native_source_materials') == [[material]]


@pytest.mark.asyncio
@pytest.mark.parametrize('pages,new_source,closeout', [
    pytest.param(['a'] * 3, None, {'identical_tool_rounds': 3}, id='identical-duplicate-source'),
    pytest.param(['a', 'b', 'a', 'b'], None,
        {'known_lookup_rounds': 2, 'reason': 'repeated_known_lookups'}, id='alternating-duplicate-source'),
    pytest.param(['a'] * 3, 'url', None, id='third-round-new-url'),
    pytest.param(['a'] * 3, 'content', None, id='third-round-new-content'),
])
async def test_native_source_novelty_during_failed_lookup_cycles(
        tmp_path, monkeypatch, frozen_clock, pages, new_source, closeout):
    cfg, store, tools, context = store_fixture(tmp_path, monkeypatch, frozen_clock, 'responses')
    cfg.update(model_native_search=True, max_context_chars=240_000)
    failed_lookup = AsyncMock(return_value={'status': 'failed'})
    monkeypatch.setattr(tools, 'get_recommendation', failed_lookup)
    requests, events = [], []

    async def handler(request):
        index = len(requests)
        requests.append(json.loads(request.content))
        if index == len(pages):
            return output('responses', FINAL)
        frozen_clock.advance(seconds=1)
        source = {'url': 'https://example.com/fixture-source', 'snippet': 'Original visible excerpt.'}
        if index == 2 and new_source == 'url':
            source['url'] = 'https://example.com/another-source'
        if index == 2 and new_source == 'content':
            source['snippet'] = 'Revised visible excerpt for the same URL.'
        body = output('responses', [('get_recommendation', {'recommendation_id': pages[index]})]).json()
        body['output'].insert(0, {'type': 'web_search_call', 'id': f'ws_fixture_{index}', 'status': 'completed',
            'action': {'type': 'search', 'query': 'fixture source', 'sources': [source]}})
        return httpx.Response(200, json=body)

    async def emit(event):
        events.append(event)

    model_transport(monkeypatch, handler)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == len(pages) + 1
    assert result['submission']['action'] == 'finish_analysis'
    assert [entry.args[0] for entry in failed_lookup.await_args_list] == pages
    assert [event['result'] for event in event_payloads(events, 'tool_result')
            if event['tool'] == 'get_recommendation'] == [{'status': 'failed'}] * len(pages)

    batches = [event['materials'] for event in event_payloads(events, 'native_search')]
    assert [len(batch) for batch in batches] == [1] * len(pages)
    materials = [batch[0] for batch in batches]
    identities = [(item['id'], item['document_version_id'], item['content_hash']) for item in materials]
    assert all(identities[0]) and identities[1] == identities[0]
    assert store.material_count() == (2 if new_source else 1)
    for index, payload in enumerate(requests[1:-1], start=1):
        assert user_messages(payload, 'native_source_materials') == batches[:index]

    final_tools = tool_names(requests[-1])
    no_progress = event_payloads(events, 'research_no_progress')
    if closeout is not None:
        assert identities == [identities[0]] * len(pages)
        assert no_progress == [{**closeout, 'action': 'submit_existing_evidence'}]
        assert_hot_closeout(requests[-1], requests[-2], 'finish_analysis')
        assert event_payloads(events, WARNING) == [{'known_lookup_rounds': 1, 'lookup_count': 1,
                                                   'action': 'warn_before_closeout'}]
        assert len(user_messages(requests[-2], WARNING)) == 1
    else:
        assert materials[2]['id'] != materials[0]['id']
        if new_source == 'content':
            assert materials[2]['url'] == materials[0]['url']
            assert materials[2]['content_hash'] != materials[0]['content_hash']
        else:
            assert materials[2]['url'] != materials[0]['url']
        assert not no_progress
        assert {'get_recommendation', 'calculate', 'finish_analysis'} <= final_tools
        assert user_messages(requests[-1], 'native_source_materials') == batches


@pytest.mark.asyncio
@pytest.mark.parametrize('pages,new_source', [
    pytest.param('aaaaaa', None, id='identical-failures'),
    pytest.param('ababab', None, id='alternating-failures'),
    pytest.param('aaaa', 'content', id='third-round-new-content'),
    pytest.param('abab', 'url', id='third-round-new-url'),
])
async def test_native_source_return_cycles_during_failed_lookups(
        tmp_path, monkeypatch, frozen_clock, pages, new_source):
    cfg, store, tools, context = store_fixture(tmp_path, monkeypatch, frozen_clock, 'responses')
    cfg.update(model_native_search=True, max_context_chars=240_000)
    requests, events = [], []
    excerpts = 'ABCC' if new_source == 'content' else 'ABABAB'

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        # Without the guard the model can continue through all six scripted lookups.
        if forced_function(payload) == 'finish_analysis' or index == len(pages):
            return output('responses', FINAL)
        assert {'get_recommendation', 'calculate', 'finish_analysis'} <= tool_names(payload)
        frozen_clock.advance(seconds=1)
        url = 'https://example.com/native-alt' if index == 2 and new_source == 'url' else 'https://example.com/native'
        source = {'url': url, 'snippet': f'Visible excerpt {excerpts[index]}.'}
        body = output('responses', [('get_recommendation', {'recommendation_id': f'missing-{pages[index]}'})]).json()
        body['output'].insert(0, {'type': 'web_search_call', 'id': f'ws_cycle_{index}', 'status': 'completed',
            'action': {'type': 'search', 'query': 'fixture source', 'sources': [source]}})
        return httpx.Response(200, json=body)

    async def emit(event):
        events.append(event)

    model_transport(monkeypatch, handler)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['model_calls'] == len(requests) == 5
    assert result['submission']['action'] == 'finish_analysis'
    failures = [event['result'] for event in event_payloads(events, 'tool_result')
                if event['tool'] == 'get_recommendation']
    assert len(failures) == 4 and all(failure['failed'] for failure in failures)

    batches = [event['materials'] for event in event_payloads(events, 'native_search')]
    assert [len(batch) for batch in batches] == [1] * 4
    first, second, third, fourth = (batch[0] for batch in batches)
    assert [batch[0]['summary'] for batch in batches] == [f'Visible excerpt {letter}.' for letter in excerpts[:4]]
    assert len({batch[0]['id'] for batch in batches}) == store.material_count() == (3 if new_source else 4)
    assert first['content_hash'] != second['content_hash']
    for index, payload in enumerate(requests[1:-1], start=1):
        assert user_messages(payload, 'native_source_materials') == batches[:index]

    closeouts = event_payloads(events, 'research_no_progress')
    assert event_payloads(events, WARNING) == [{'known_lookup_rounds': 1, 'lookup_count': 1,
                                               'action': 'warn_before_closeout'}]
    assert all(not user_messages(payload, WARNING) for payload in requests[:3])
    if new_source:
        if new_source == 'content':
            assert third['url'] == first['url'] == second['url']
            assert third['content_hash'] not in {first['content_hash'], second['content_hash']}
            assert fourth['id'] == third['id']
        else:
            assert third['url'] != first['url']
            assert third['content_hash'] == first['content_hash']
            assert fourth['id'] == second['id']
        assert not user_messages(requests[3], WARNING)
        assert len(user_messages(requests[4], WARNING)) == 1
        assert user_messages(requests[4], 'native_source_materials') == batches
        assert {'get_recommendation', 'calculate', 'finish_analysis'} <= tool_names(requests[4])
        assert not closeouts
    else:
        assert third['url'] == first['url'] and third['content_hash'] == first['content_hash']
        assert len(user_messages(requests[3], WARNING)) == 1
        expected = ({'identical_tool_rounds': 3} if pages[0] == pages[1] else
                    {'known_lookup_rounds': 2, 'reason': 'repeated_known_lookups'})
        assert closeouts == [{**expected, 'action': 'submit_existing_evidence'}]
        assert [event['type'] for event in events if event['type'] in {WARNING, 'research_no_progress'}] == [
            WARNING, 'research_no_progress']
        assert_hot_closeout(requests[4], requests[3], 'finish_analysis')


def test_stored_native_material_identity_ignores_return_version(tmp_path, frozen_clock):
    store = Store(tmp_path / 'native-identity.sqlite3')
    url = 'https://example.com/native'

    def save_index(source_url, excerpt):
        frozen_clock.advance(seconds=1)
        return index_of(store.save_material({'url': source_url, 'scope': 'public',
            'representation': 'native_search_source', 'access': 'search_excerpt', 'content': excerpt}))

    first_a, first_b, returned_a, returned_b = (save_index(url, excerpt) for excerpt in 'ABAB')
    other_a = save_index(url + '-alt', 'A')

    def differing_fields(first, returned):
        return {key for key in first.keys() | returned.keys()
                if key not in first or key not in returned or first[key] != returned[key]}

    version_fields = {'id', 'document_version_id', 'document_version', 'version', 'previous_id',
                      'change_summary', 'changed_at', 'fetched_at'}
    assert differing_fields(first_a, returned_a) == version_fields | {'change_kind'}
    assert differing_fields(first_b, returned_b) == version_fields
    assert native_material_identity(first_a) == native_material_identity(returned_a)
    assert native_material_identity(first_b) == native_material_identity(returned_b)
    assert native_material_identity(first_a) != native_material_identity(first_b)
    assert native_material_identity(first_a) != native_material_identity(other_a)


def test_native_material_identity_ignores_nested_delivery_timestamps():
    stable = {'source': {'url': 'https://example.com/native', 'title': '年报'},
              'pages': [{'page': 1, 'published_at': '2026-10-30'}]}
    original = copy.deepcopy(stable)
    original['source'].update(fetched_at='f1', retrieved_at='r1')
    original['pages'][0].update(delivered_at='d1', updated_at='u1')
    refreshed = copy.deepcopy(original)
    refreshed['source'].update(fetched_at='f2', retrieved_at='r2')
    refreshed['pages'][0].update(delivered_at='d2', updated_at='u2')
    snapshots = copy.deepcopy((original, refreshed))
    identity = native_material_identity([original])
    assert identity == native_material_identity([refreshed])
    assert identity == json.dumps([stable], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    assert (original, refreshed) == snapshots
    refreshed['pages'][0]['published_at'] = '2026-11-01'
    assert identity != native_material_identity([refreshed])


@pytest.mark.parametrize('status', ['failed', 'error'])
def test_tracker_preserves_success_after_mixed_round_status_failure(status):
    tracker = KnownLookupTracker()
    query = call('get_recommendation', {'recommendation_id': 'r'})
    success = (query, {'status': 'active'})
    failed = (query, {'status': status})
    calculation = (call('calculate'), {'result': '3'})
    assert not tracker.observe([success])
    assert not tracker.observe([failed, calculation])
    assert tracker.observe([success])


@pytest.mark.asyncio
@pytest.mark.parametrize('child_status', ['queued', 'running'])
async def test_active_recommendation_status_remains_exempt_with_live_child(monkeypatch, child_status):
    events, requests = [], []
    lookup = ('get_recommendation', {'recommendation_id': 'active'})
    script = [[lookup], [lookup], [lookup], FINAL]

    async def handler(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert child.status == child_status
        assert {'get_recommendation', 'finish_analysis'} <= tool_names(payload)
        return output('responses', script[index])

    runtime, tools = local_runtime(monkeypatch, handler, events)
    tools.get_recommendation = AsyncMock(return_value={'status': 'active'})
    child = agents.AgentState('unrelated-child', 'fixture', 'fixture',
        runtime.client.session('child', {}), status=child_status)
    runtime.children[child.id] = child
    result = await runtime.run()
    assert result['model_calls'] == len(requests) == len(script)
    assert tools.get_recommendation.await_count == 3
    assert not event_payloads(events, WARNING)
    assert not event_payloads(events, 'research_no_progress')
