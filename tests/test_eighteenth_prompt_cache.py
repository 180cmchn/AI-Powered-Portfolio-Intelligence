"""Offline payload and boundary regressions, not a live cache-hit measurement."""
import hashlib
import json

import httpx
import pytest

from app import agents
from app.context import context_char_limit, serialized
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import output
from tests.test_seventeenth_child_final_evidence import add_tool, make_runtime, payload_history, wire_tool_exchanges
from tests.test_seventeenth_model_cache import config, response


def forced_choice(protocol, name):
    return ({'type': 'function', 'name': name} if protocol == 'responses' else
            {'type': 'function', 'function': {'name': name}})


def tool_hash(payload):
    wire = json.dumps(payload['tools'], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(wire.encode('utf-8')).hexdigest()


@pytest.mark.parametrize('override,expected', [({}, 420_000), ({'max_context_chars': None}, 420_000),
                                               ({'max_context_chars': 120_000}, 120_000)])
async def test_default_none_and_explicit_context_limits_on_wire(override, expected):
    cfg = config()
    cfg.pop('max_context_chars')
    cfg.update(override)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    try:
        text = 'x' * (expected // 3 - 500)
        full = client.session('system', {'original': text})
        bounded = client.session('system', {'original': text + 'y' * 1000})
        await client.complete(full, [])
        await client.complete(bounded, [])
        assert context_char_limit(cfg) == expected
        assert json.loads(sent[0]['input'][0]['content']) == {'original': text}
        retained = json.loads(sent[1]['input'][0]['content'])['initial_context_excerpt']
        assert retained['truncated_for_context'] and retained['record_id'] == bounded.archive[0]['record_id']
        assert retained['total_chars'] == len(serialized({'original': text + 'y' * 1000}))
    finally:
        await client.aclose()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('manager', [False, True])
async def test_hot_final_wire_keeps_tools_and_history_rejects_wrong_function(monkeypatch, protocol, manager):
    payloads, executed = [], []
    terminal = 'finish_analysis' if manager else 'submit_report'
    late = {'report_id': 'late-report', 'summary': 'Late report with exact findings.',
            'findings': [{'claim': 'Precise late fact.'}], 'material_ids': [], 'open_questions': ['Still open.']}

    class Tools:
        async def get_material(self, identity):
            executed.append(identity)
            return {'id': identity, 'text': 'Visible original.'}

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            if manager:
                runtime.budget_closing = True
                child.reports.append(late)
                child.status = 'completed'
            else:
                child.deadline_closing = True
            return output(protocol, [('get_material', {'material_id': 'allowed-during-research'})])
        if len(payloads) == 2:
            return output(protocol, [('get_material', {'material_id': 'must-not-run-in-final'})])
        return output(protocol, [(terminal, delivery_arguments(report='Final after correction.') if manager
                                 else {'summary': 'Final after correction.'})])

    runtime, child, events = await make_runtime(monkeypatch, handler, protocol, model_native_search=True)
    runtime.tools = Tools()
    agent_id, session = ('manager', runtime.manager_session) if manager else (child.id, child.session)
    try:
        result = await runtime._loop(agent_id, session, manager=manager)
        assert executed == ['allowed-during-research']
        assert len(payloads) == 3 and result['submission_corrections'] == 1
        wrong_args, refused = wire_tool_exchanges(payloads[2], 'get_material')[-1]
        assert wrong_args == {'material_id': 'must-not-run-in-final'}
        assert refused['failed'] is True and terminal in refused['error']
        first, closeout, correction = payloads
        assert first.get('tool_choice') is None
        assert first['tools'] == closeout['tools'] == correction['tools']
        assert closeout['tool_choice'] == correction['tool_choice'] == forced_choice(protocol, terminal)
        assert len(first['tools']) > 1 and session.native_search is True
        assert ({'type': 'web_search'} in first['tools']) is (protocol == 'responses')
        assert {r['cache_epoch'] for r in session.requests} == {0}
        for payload, row in zip(payloads, session.requests):
            assert row['prefix_diagnostic']['tools_hash'] == tool_hash(payload)
        for previous, current, row, reason in zip(payloads, payloads[1:], session.requests[1:],
                                                  ['tool_choice_changed', 'append_only']):
            before, after = payload_history(previous), payload_history(current)
            assert after[:len(before)] == before
            diag = row['prefix_diagnostic']
            assert diag['history_is_prefix'] and not diag['epoch_changed'] and diag['change_reason'] == reason
        projection_kind = 'synthesis_context_projected' if manager else 'child_final_context_projected'
        projection, = [e['payload'] for e in events if e['type'] == projection_kind]
        assert projection['mode'] == 'tail_append' and projection['history_preserved']
        updates = [json.loads(item['content']) for item in payload_history(closeout) if item.get('role') == 'user']
        transition, = [item for item in updates if ('synthesis_transition' if manager else 'child_final_transition') in item]
        assert transition['runtime_status']['closing']
        if manager:
            report, = transition['late_completed_child_reports'][0]['reports']
            assert report['report_id'] == late['report_id'] and report['findings'] == late['findings']
            assert report['open_questions'] == late['open_questions']
            assert runtime.synthesis_checkpoint['reports'][0]['report_id'] == late['report_id']
    finally:
        await runtime.close()


@pytest.mark.parametrize('signal', ['event', 'usage', 'incomplete'])
async def test_forced_native_search_is_not_adopted_and_correction_keeps_schema(monkeypatch, signal):
    payloads, recorded = [], []
    terminal = 'submit_report'

    class Tools:
        async def record_native_search(self, *args):
            recorded.append(args)
            return []

    async def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            child.deadline_closing = True
            return output('responses', [('report_progress', {'summary': 'Research before closeout.'})])
        reply = output('responses', [(terminal, {'summary': 'Corrected final.'})]).json()
        if len(payloads) == 2:
            if signal == 'usage':
                reply['server_side_tool_usage'] = {'WEB_SEARCH': 1}
            else:
                reply['output'].insert(0, {'type': 'web_search_call', 'status': 'completed', 'id': 'rejected-native',
                    'action': {'type': 'search', 'sources': [{'url': 'https://unexpected.invalid/search'}]}})
            if signal == 'incomplete':
                reply['status'] = 'incomplete'
        return httpx.Response(200, json=reply)

    runtime, child, events = await make_runtime(monkeypatch, handler, model_native_search=True)
    runtime.tools = Tools()
    try:
        result = await runtime._loop(child.id, child.session, manager=False)
        assert len(payloads) == 3 and result['submission_corrections'] == 1
        assert {'type': 'web_search'} in payloads[0]['tools']
        assert payloads[0]['tools'] == payloads[1]['tools'] == payloads[2]['tools']
        assert payloads[1]['tool_choice'] == payloads[2]['tool_choice'] == forced_choice('responses', terminal)
        assert child.session.requests[1]['tool_choice_status'] == 'unexpected_native_tool'
        assert '收尾阶段不接受原生搜索' in serialized(events)
        assert not recorded and not any(e['type'] == 'native_search' for e in events)
        assert 'unexpected.invalid' not in serialized(payloads[2])
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_compaction_at_eighty_five_percent_preserves_runway_and_stable_continuations(monkeypatch, protocol):
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=response(protocol))

    runtime, child, _ = await make_runtime(monkeypatch, handler, protocol)
    client, session = runtime.client, child.session
    schema = agents.RESEARCH_TOOLS + agents.CHILD_TOOLS
    limit = runtime.config['max_context_chars']
    threshold = int(limit * .85)
    try:
        original = {'id': 'exact-source', 'text': 'Full exact narrative.', 'access': 'full_text'}
        for index in range(12):
            add_tool(client, session, 'get_material', {'id': f'body-{index}', 'text': 'raw ' * 700}, f'read-{index}')
        add_tool(client, session, 'get_material', original, 'exact-source')
        runtime._remember_calculation(child.id, 'calculate', {'operation': 'sum', 'values': ['0.20', '1.31']},
            {'operation': 'sum', 'inputs': ['0.20', '1.31'], 'result': '1.51'})
        runtime._pin_runtime_status(session, child.id, None, False)
        pad = {'role': 'user', 'content': ''}
        session.history.append(pad)
        pad['content'] = 'p' * (threshold - len(serialized(session.history)))
        assert len(serialized(session.history)) == threshold
        token = runtime.request_owner.set((child.id, False))
        try:
            await client.complete(session, schema)
            assert session.compactions == 0
            # The last response crossed the trigger; the next actual send retires it.
            assert len(serialized(session.history)) > threshold
            await client.complete(session, schema)
            history = payload_history(payloads[1])
            if protocol == 'chat_completions':
                assert history[0]['role'] == 'system'
                history = history[1:]  # The session history budget excludes the fixed system prompt.
            retained = history[0]
            assert len(serialized(retained)) <= int(limit * .44)
            assert len(serialized(history)) <= int(limit * .50)
            seed = json.loads(retained['content'])
            assert original in [item['result'] for item in seed['delivered_research_evidence']]
            arithmetic, = seed['calculation_summaries']
            assert arithmetic['inputs'] == ['0.20', '1.31'] and arithmetic['result'] == '1.51'
            for index in range(3):
                client.add_user(session, {'update': 'next ' * 1000, 'sequence': index})
                await client.complete(session, schema)
        finally:
            runtime.request_owner.reset(token)
        assert session.compactions == 1
        assert [r['prefix_diagnostic']['change_reason'] for r in session.requests] == [
            'first', 'compression', 'append_only', 'append_only', 'append_only']
        assert all(r['prefix_diagnostic']['history_is_prefix'] for r in session.requests[2:])
    finally:
        await runtime.close()


@pytest.mark.parametrize('manager', [False, True])
@pytest.mark.parametrize('exceeds_limit', [False, True])
async def test_final_after_ordinary_compaction_restores_omitted_sources(monkeypatch, tmp_path, manager, exceeds_limit):
    from app.context import archive
    from app.domain import AccountInput
    from app.service import ResearchTools
    from app.store import Store
    from tests.test_seventeenth_child_final_evidence import projected_items

    store = Store(tmp_path / 'closeout.sqlite3', mode='live')
    store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='Closeout fixture', cash='100'))
    job, = store.claim_jobs(account_id=account['id'])
    material_ids = [store.save_material({'scope': 'public',
        'text': f'BEGIN-{i} ' + 'original ' * 340 + f' END-{i}'})['id'] for i in range(12)]
    originals = {mid: store.material_range(mid, length=4000) for mid in material_ids}
    first, target = material_ids[:2]
    sent, events, before_final_receipts = [], [], []
    terminal = 'finish_analysis' if manager else 'submit_report'
    cfg = config(max_model_calls=None, max_agent_turns=None, max_context_chars=80000, max_tool_result_chars=12000)
    limit = int(context_char_limit(cfg) * .85)

    def get_materials(ids):
        return output('responses', [('get_material', {'material_id': mid}) for mid in ids])

    async def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return get_materials([first])
        if len(sent) == 2:
            assert wire_tool_exchanges(payload, 'get_material') == [({'material_id': first}, originals[first])]
            return get_materials(material_ids[1:])
        if len(sent) == 3:
            assert payload.get('tool_choice') is None
            pending = {item['call'].arguments['material_id']: item for item in session._pending_material_deliveries}
            assert (pending[target]['proof'] is not None) is manager
            assert [item['material_id'] for item in store.checkpoint_deliveries(job['id'])] == [first]
            return get_materials([first])  # A fresh visible reread must not be projected twice.
        assert len(sent) == 4 and payload['tool_choice'] == forced_choice('responses', terminal)
        assert store.checkpoint_deliveries(job['id']) == before_final_receipts
        return output('responses', [(terminal, delivery_arguments(report='Final from visible evidence.') if manager
                                      else {'summary': 'Final from visible evidence.'})])

    async def emit(event):
        events.append(event)
        if event['type'] != 'context_compacted':
            return
        assert len(sent) == 3 and session.compactions == 1 and session.cache_epoch == 1
        assert session.requests[-1]['prefix_diagnostic']['change_reason'] == 'compression'
        ordinary = projected_items(sent[2])
        kept = {mid for mid, value in originals.items() if any(item['result'] == value for item in ordinary)}
        missing = set(material_ids) - kept
        assert {first, target} <= missing and not wire_tool_exchanges(sent[2], 'get_material')
        seed = json.loads(session.history[0]['content'])
        assert len(missing) == seed['omitted_output_count']
        before_final_receipts.extend(store.checkpoint_deliveries(job['id']))
        assert {item['material_id'] for item in before_final_receipts} == kept | {first}
        pending = {item['call'].arguments['material_id']: item for item in session._pending_material_deliveries}
        assert set(pending) == missing - {first}
        assert (pending[target]['proof'] is not None) is manager
        if exceeds_limit:
            while (runway := limit - len(serialized(session.history))) > 11000:
                client.add_user(session, {'new_work': 'x' * min(8000, runway - 10000)})
        if manager:
            runtime.budget_closing = True
        else:
            child.deadline_closing = True

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    runtime = agents.ResearchRuntime({'task_id': job['id'], 'account': {'id': account['id']}}, cfg,
                                     ResearchTools(store, job, cfg), emit)
    monkeypatch.setattr(runtime, '_launch', lambda child: None)
    client = runtime.client
    try:
        child = runtime.children[(await runtime._spawn({'task': '核对原文'}))['agent_id']]
        agent_id, session = ('manager', runtime.manager_session) if manager else (child.id, child.session)
        archive(session, {'text': 'UNREAD-TOOL-RESULT'}, 'tool_result')
        other = child.session if manager else runtime.manager_session
        add_tool(client, other, 'get_material', {'text': 'OTHER-AGENT-PRIVATE'}, 'private-source')
        for index in range(4):
            client.add_user(session, {'padding': 'research ' * 1000, 'sequence': index})
        result = await runtime._loop(agent_id, session, manager=manager)
        assert len(sent) == 4
        final = sent[-1]
        assert 'UNREAD-TOOL-RESULT' not in serialized(final) and 'OTHER-AGENT-PRIVATE' not in serialized(final)
        kind = 'synthesis_context_projected' if manager else 'child_final_context_projected'
        transition, = [event['payload'] for event in events if event['type'] == kind]
        evidence = [item for item in projected_items(final)
                    if item['tool'] == 'get_material' and not item.get('truncated_for_synthesis')]
        values = [item['result'] for item in evidence]
        assert values and all(value in originals.values() for value in values)
        assert transition['before_chars'] < limit
        ordinary_values = [item['result'] for item in projected_items(sent[2])]
        missing = [value for mid, value in originals.items() if mid != first and value not in ordinary_values]
        expanded = transition['before_chars'] + len(serialized(missing))
        if exceeds_limit:
            assert expanded > limit
            assert transition['mode'] == 'reproject' and transition['history_preserved'] is False
            assert session.cache_epoch == 2 and not session.requests[-1]['prefix_diagnostic']['history_is_prefix']
        else:
            assert expanded < limit - 3000
            assert transition['mode'] == 'tail_append' and transition['history_preserved'] is True
            assert session.cache_epoch == 1 and session.requests[-1]['prefix_diagnostic']['history_is_prefix']
            assert payload_history(final)[:len(payload_history(sent[2]))] == payload_history(sent[2])
            assert len(values) == len(material_ids) - 1 and all(originals[mid] in values for mid in material_ids[1:])
            assert wire_tool_exchanges(final, 'get_material') == [({'material_id': first}, originals[first])]
            pending_evidence, = [item for item in evidence if item['result'] == originals[target]]
            assert pending_evidence['visibility'] == 'completed_tool_output_not_previously_delivered'
            receipts = store.checkpoint_deliveries(job['id'])
            delivered, = [item for item in receipts if item['material_id'] == target]
            assert delivered['agent_id'] == agent_id and delivered['tool'] == 'get_material'
            assert delivered['body']['visible_result'] == originals[target]
            assert delivered['id'] in result['material_delivery_ids']
            assert not any(item['call'].name == 'get_material' for item in session._pending_material_deliveries)
        assert len(serialized(payload_history(final))) <= limit
    finally:
        await runtime.close()


@pytest.mark.parametrize('manager', [False, True])
async def test_hot_final_restores_omitted_calculations_with_owned_provenance(monkeypatch, manager):
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        if len(sent) < 3:
            return httpx.Response(200, json=response())
        return output('responses', [('finish_analysis', delivery_arguments(report='Final arithmetic.'))] if manager
                      else [('submit_report', {'summary': 'Final arithmetic.'})])

    runtime, child, events = await make_runtime(monkeypatch, handler)
    client = runtime.client
    agent_id, session = ('manager', runtime.manager_session) if manager else (child.id, child.session)
    tools = agents.RESEARCH_TOOLS + (agents.MANAGER_TOOLS if manager else agents.CHILD_TOOLS)
    try:
        runtime._pin_runtime_status(session, agent_id, None, False)
        for index in range(8):
            rows = [{'time': f'2026-11-{index + 1:02d}T00:{i:02d}:00+08:00',
                     'signed_body': str(-i), 'real_body': str(i), 'upper_shadow': '0.00', 'lower_shadow': '1.01'}
                    for i in range(46)]
            args = {'fields': ['signed_body', 'real_body', 'upper_shadow', 'lower_shadow']}
            result = {'fields': args['fields'], 'rows': rows}
            add_tool(client, session, 'query_market_rows', result, f'geometry-{index}', args)
            runtime._remember_calculation(agent_id, 'query_market_rows', args, result)
        own = {record['calculation_id']: record['summary']['candle_geometry'] for record in runtime.calculations}
        runtime._remember_calculation(child.id if manager else 'manager', 'calculate',
            {'operation': 'sum', 'values': ['22222222.2']}, {'result': '22222222.2'})
        token = runtime.request_owner.set((agent_id, False))
        try:
            await client.complete(session, tools)
            assert session.compactions == 0
            for index in range(3):
                client.add_user(session, {'padding': 'research ' * 900, 'sequence': index})
            await client.complete(session, tools)
        finally:
            runtime.request_owner.reset(token)
        epoch = session.cache_epoch
        seed = json.loads(session.history[0]['content'])
        assert epoch == session.compactions == 1
        assert seed['calculation_indexes']['omitted_summary_count'] > 0
        if manager:
            runtime.budget_closing = True
        else:
            child.deadline_closing = True
        await runtime._loop(agent_id, session, manager=manager)
        assert session.cache_epoch == epoch and session.requests[-1]['prefix_diagnostic']['history_is_prefix']
        wire = payload_history(sent[-1])
        assert wire[:len(payload_history(sent[1]))] == payload_history(sent[1])
        calculations = [item for message in wire if message.get('role') == 'user'
                        for item in json.loads(message['content']).get('calculation_summaries', [])]
        assert len(calculations) == len(own)
        assert {item['calculation_id']: item['candle_geometry'] for item in calculations} == own
        assert all(item['full_inputs']['session_agent_id'] == agent_id for item in calculations)
        assert '22222222.2' not in serialized(wire)
        coverage, = [event['payload'] for event in events if event['type'] == 'final_calculation_coverage']
        assert {item['calculation_id']: item['visibility'] for item in coverage['calculations']} == dict.fromkeys(own, 'summary')
    finally:
        await runtime.close()


def test_task_list_compact_model_usage_keeps_paired_samples_only(tmp_path):
    from app.domain import AccountInput
    from app.model_usage import compact_usage_summary, usage_summary
    from app.store import Store
    from tests.delivery_helpers import final_result

    def request(search, input_tokens, cached_tokens, *, change=None, epoch=None):
        return {'protocol': 'responses', 'native_search_requested': search, 'duration_ms': 2,
            'usage': {'input_tokens': input_tokens, 'input_tokens_details': {'cached_tokens': cached_tokens}},
            'prefix_diagnostic': {'change_reason': change} if change else None, 'cache_epoch_reason': epoch}

    missing_native_search = request(None, 500, 500)
    missing_native_search.pop('native_search_requested')
    summary = usage_summary([
        request(False, 100, 10, change='first'), request(False, 200, 120, change='append_only'),
        request(False, 100, None, change='append_only'), request(False, 80, 8, change='append_only', epoch='first'),
        request(True, 50, 0, change='compression'), request(True, None, 10, epoch='synthesis'),
        missing_native_search, request(1, 70, 70),
    ])
    assert [row['native_search_requested'] for row in summary['sequence']] == [False] * 4 + [True, True, None, None]
    assert [row['cache_epoch_reason'] for row in summary['sequence']] == [None] * 3 + ['first', None, 'synthesis', None, None]

    def cohort(count, paired, input_tokens=None, read_tokens=None, ratio=None):
        return {'count': count, 'paired': paired, 'reported_input_tokens': input_tokens,
                'cache_read_tokens': read_tokens, 'weighted_read_ratio': ratio}

    expected = {'requests': 8, 'duration_ms': 16, 'failed_requests': 0,
        'cache_summary': summary['cache_summary'], 'normalized_totals': summary['normalized_totals'],
        'prefix_reason_counts': {'first': 1, 'append_only': 3, 'compression': 1, 'synthesis': 1, 'unknown': 2},
        'native_search_breakdown': {'non_search_all': cohort(4, 3, 380, 138, 138 / 380),
            'non_search_cross_turn': cohort(2, 1, 200, 120, .6), 'search_all': cohort(2, 1, 50, 0, 0)}}
    compact = compact_usage_summary(summary)
    assert compact == expected
    assert compact['cache_summary'] is not summary['cache_summary']
    assert compact['normalized_totals']['coverage'] is not summary['normalized_totals']['coverage']
    assert compact_usage_summary(None) is None and compact_usage_summary({'requests': 1}) is None
    assert compact_usage_summary({'cache_summary': {}, 'sequence': [
        {'native_search_requested': False, 'input_tokens': 10, 'cache_read_tokens': 5,
         'request_category': 'first_session_request'},
        {'native_search_requested': True, 'input_tokens': float('inf'), 'cache_read_tokens': 1},
        {'native_search_requested': True, 'input_tokens': 1, 'cache_read_tokens': False},
    ]})['native_search_breakdown'] == {'non_search_all': cohort(1, 1, 10, 5, .5),
        'non_search_cross_turn': cohort(0, 0), 'search_all': cohort(2, 0)}

    store = Store(tmp_path / 'task-usage.sqlite3', mode='live')
    store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='受控用量账户', cash='100',
        holdings=[{'symbol': 'SZ:159599', 'quantity': '1', 'cost_price': '1'}]))
    job, = store.claim_jobs(account_id=account['id'])
    assert store.publish(job['id'], final_result({'report': '缓存摘要测试', 'model_usage': summary}), None)
    listed, = store.jobs(account['id'], summary=True)
    assert 'result' not in listed and 'events' not in listed
    assert listed['model_usage_summary'] == expected
    assert store.job(job['id'])['result']['model_usage'] == summary


@pytest.mark.parametrize('case,request_count', [('nested_only', 1), ('direct_unresolved', 2), ('legacy', 2)])
async def test_reference_feedback_only_counts_direct_unresolved_and_is_once(monkeypatch, case, request_count):
    payloads = []
    nested = {'material_id': 'outer', 'resolution': 'resolved',
              'origin_references': [{'source': 'unmatched', 'resolution': 'unresolved'}]}

    class Tools:
        async def resolve_references(self, arguments):
            direct = {} if case == 'legacy' else {'direct_reference_summary': {'unresolved': int(case == 'direct_unresolved')}}
            return {**arguments, 'references': [nested], 'reference_summary': {'unresolved': 1}, **direct}

    async def handler(request):
        payloads.append(json.loads(request.content))
        return output('responses', [('finish_analysis', delivery_arguments(report='Retain visible evidence and limits.'))])

    runtime, _, events = await make_runtime(monkeypatch, handler)
    runtime.tools = Tools()
    try:
        result = await runtime._loop('manager', runtime.manager_session, manager=True)
        assert result['references'] == [nested] and result['reference_summary']['unresolved'] == 1
        assert len(payloads) == request_count
        outputs = [e['payload']['result'] for e in events if e['type'] == 'tool_result']
        assert sum('reference_feedback' in item for item in outputs) == request_count - 1
        assert outputs[-1]['accepted'] is True
    finally:
        await runtime.close()
