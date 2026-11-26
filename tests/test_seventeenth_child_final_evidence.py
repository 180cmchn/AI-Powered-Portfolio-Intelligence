"""Offline production final-request boundaries, never model-quality assertions."""
import copy
import json
import time
from pathlib import Path

import httpx
import pytest

from app import agents
from app.context import archive, compact
from app.model import ModelClient, ToolCall
from tests.test_seventeenth_model_cache import config, response
from tests.test_ninth_calculation_context import output


FINANCIAL = json.loads(Path(__file__).with_name('fixtures').joinpath('seventeenth-financial-signs.json').read_text())
BUSINESS = '2026H1 原表：数据流量行业 1,081,822,142.43 元，占98.91%；电竞游戏行业 11,206,704.12 元，占1.02%。'


def add_tool(client, session, name, value, identity, arguments=None):
    args = arguments or {}
    if client.protocol == 'responses':
        session.history.append({'type': 'function_call', 'name': name, 'call_id': identity,
                                'arguments': json.dumps(args), 'status': 'completed'})
    else:
        session.history.append({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}],
            'reasoning_content': 'HIDDEN-CHAT-REASONING'})
    call = ToolCall(identity, name, args)
    session.pending_calls.append(call)
    client.add_results(session, [(call, value)])
    return call


async def make_runtime(monkeypatch, handler, protocol='responses', **extra):
    cfg = config(protocol, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None,
                 max_context_chars=80000, max_tool_result_chars=12000,
                 agent_timeout_seconds=60, analysis_timeout_seconds=120, **extra)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    events = []
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime({'task_id': 'child-final-local-fixture'}, cfg, object(), emit)
    monkeypatch.setattr(runtime, '_launch', lambda child: None)
    child_id = (await runtime._spawn({'task': '核对财报期间与业务结构'}))['agent_id']
    child = runtime.children[child_id]
    child.status = 'running'
    child.session.deadlines = {'child_budget_exhausted': time.monotonic() + 60}
    return runtime, child, events


def payload_history(payload):
    return payload.get('input', payload.get('messages'))


def projected_items(payload):
    values = []
    for message in payload_history(payload):
        if message.get('role') == 'user':
            try:
                value = json.loads(message.get('content', '{}'))
            except (ValueError, TypeError):
                continue
            values.extend(value.get('delivered_research_evidence', []))
    return values


def wire_tool_exchanges(payload, name):
    arguments, exchanges = {}, []
    for message in payload_history(payload):
        if message.get('type') == 'function_call' and message.get('name') == name:
            arguments[message['call_id']] = json.loads(message['arguments'])
        for call in message.get('tool_calls') or []:
            if call['function']['name'] == name:
                arguments[call['id']] = json.loads(call['function']['arguments'])
        if message.get('type') == 'function_call_output' or message.get('role') == 'tool':
            identity = message.get('call_id', message.get('tool_call_id'))
            if identity in arguments:
                exchanges.append((arguments.pop(identity), json.loads(message.get('output', message.get('content')))))
    return exchanges


def assert_private_protocol_only(payload, *markers):
    # Same-endpoint continuation may carry opaque provider fields, never prose,
    # function arguments/results or newly generated user-context messages.
    visible = [{key: value for key, value in item.items() if key not in {'reasoning_content', 'encrypted_content'}
                or not (item.get('role') == 'assistant' and key == 'reasoning_content'
                        or item.get('type') == 'reasoning' and key == 'encrypted_content')}
               for item in payload_history(payload)]
    assert all(marker not in json.dumps(visible) for marker in markers)


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('boundary', ['normal', 'fallback'])
async def test_compacted_child_final_http_keeps_own_visible_financial_and_business_facts(monkeypatch, protocol, boundary):
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(200, json=response(protocol))
        wire = json.dumps(payload, ensure_ascii=False)
        assert all(sign in wire for sign in ['-2.04%', '19.11%', '17.57%', '98.91%', '1.02%'])
        assert payload['tools'] == payloads[0]['tools']
        assert 'UNREAD-RAW-TAIL' not in wire and 'OTHER-CHILD-ORIGINAL' not in wire
        assert 'OPAQUE-DO-NOT-LOG' not in wire and 'HIDDEN-CHAT-REASONING' not in wire
        assert 'MANAGER-ONLY-ORIGINAL' not in wire
        items = projected_items(payload)
        saved = next(item['result'] for item in items if item['tool'] == 'save_material')
        assert saved['classification'] == 'agent_extraction'
        assert saved['text'] == '模型提取，非独立原始事实：营收下降，利润上升。'
        assert sum(item['result'] == original for item in items) == 1
        assert child.session.deadlines == deadlines and child.session.phase == 'research'
        assert child.session.native_search is True
        # First final result deliberately violates the atomic terminal contract.
        if len(payloads) == 2:
            return output(protocol, [('submit_report', {'summary': '未提交候选'}),
                                     ('submit_report', {'summary': '另一未提交候选'})])
        assert payload_history(payload)[:len(payload_history(payloads[1]))] == payload_history(payloads[1])
        return output(protocol, [('submit_report', {'summary': '本地控制夹具：证据可见，不证明外部模型采用。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler, protocol)
    client, session = runtime.client, child.session
    original = {'url': FINANCIAL['source']['url'], 'material_id': 'own-q3', 'text': FINANCIAL['excerpt'],
                'returned_range': {'start_page': 2, 'end_page': 2}}
    try:
        add_tool(client, session, 'read', original, 'finance')
        add_tool(client, session, 'read', original, 'finance-repeat')
        add_tool(client, session, 'get_material', {'id': 'own-h1', 'text': BUSINESS}, 'business')
        add_tool(client, session, 'save_material', {'id': 'own-extraction', 'classification': 'agent_extraction',
            'text': '模型提取，非独立原始事实：营收下降，利润上升。'}, 'extraction')
        add_tool(client, session, 'read', {'text': 'PADDING ' * 3000 + 'UNREAD-RAW-TAIL'}, 'oversized')
        archive(runtime.manager_session, {'role': 'tool', 'content': 'MANAGER-ONLY-ORIGINAL'}, 'conversation')
        other = client.session('other child', {})
        archive(other, {'role': 'tool', 'content': 'OTHER-CHILD-ORIGINAL'}, 'conversation')
        runtime._pin_runtime_status(session, child.id, None, False)
        token = runtime.request_owner.set((child.id, False))
        try:
            await client.complete(session, agents.RESEARCH_TOOLS + agents.CHILD_TOOLS)
        finally:
            runtime.request_owner.reset(token)
        assert '-2.04%' in json.dumps(payloads[0], ensure_ascii=False)
        # Real generic compaction after acceptance retires the source messages.
        for i in range(9):
            client.add_user(session, {'discovery_index': i, 'text': 'index-only ' * 900})
        assert compact(session, runtime.config['max_context_chars'])
        # Ordinary production compaction now keeps the owned source facts,
        # before either deadline branch enters submit-only context.
        assert '-2.04%' in json.dumps(session.history, ensure_ascii=False)
        deadlines = copy.deepcopy(session.deadlines)
        if boundary == 'normal':
            child.deadline_closing = True
        else:
            checks = 0
            def expires_between_prepare_and_http(agent_id):
                nonlocal checks
                if agent_id != child.id:
                    return False
                checks += 1
                return checks > 1
            monkeypatch.setattr(runtime, '_child_closeout', expires_between_prepare_and_http)
        result = await runtime._loop(child.id, session, manager=False)
        assert result['submission_corrections'] == 1
        assert len(payloads) == 3
        projected = [event for event in events if event['type'] == 'child_final_context_projected']
        assert len(projected) == 1
        assert projected[0]['payload']['reason'] == ('bounded_final_request' if boundary == 'normal' else 'concurrent_budget_closeout')
        assert projected[0]['payload']['after_chars'] < runtime.config['max_context_chars'] * .74
        assert runtime.client.requests[-1]['prefix_diagnostic']['history_is_prefix']
    finally:
        await runtime.close()


async def test_child_projection_rejects_pending_boundary_without_mutating_session(monkeypatch):
    runtime, child, _ = await make_runtime(monkeypatch, lambda r: httpx.Response(200, json=response()))
    try:
        child.session.pending_calls.append(ToolCall('pending', 'read', {'url': 'https://fixture.invalid'}))
        previous = copy.deepcopy(child.session.history)
        epoch = child.session.cache_epoch
        with pytest.raises(ValueError, match='所有工具调用'):
            await runtime._enter_child_final_context(child.session, child.id, reason='test')
        assert child.session.history == previous and child.session.cache_epoch == epoch
    finally:
        await runtime.close()


async def test_child_projection_bounded_omissions_keep_whole_values_and_unread_archive_closed(monkeypatch):
    payloads = []
    async def handler(request):
        payloads.append(json.loads(request.content))
        return output('responses', [('submit_report', {'summary': '部分已读结果未纳入本次有限上下文。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler)
    try:
        for i in range(40):
            add_tool(runtime.client, child.session, 'read', {'material_id': str(i),
                'text': f'BEGIN-{i} ' + ('bounded-visible ' * 250) + f' END-{i}'}, f'read-{i}')
        archive(child.session, {'text': 'UNREAD-ARCHIVE-ONLY'}, 'tool_result')
        child.deadline_closing = True
        await runtime._loop(child.id, child.session, manager=False)
        wire = json.dumps(payloads[0], ensure_ascii=False)
        assert 'UNREAD-ARCHIVE-ONLY' not in wire
        assert len(json.dumps(payload_history(payloads[0]), ensure_ascii=False)) < runtime.config['max_context_chars'] * .78
        for item in projected_items(payloads[0]):
            i = item['result']['material_id']
            assert item['result']['text'].startswith(f'BEGIN-{i}')
            assert item['result']['text'].endswith(f'END-{i}')
        event = next(e for e in events if e['type'] == 'child_final_context_projected')
        assert event['payload']['omitted_outputs'] > 0
        assert event['payload']['included_outputs'] + event['payload']['omitted_outputs'] == 40
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_ordinary_prefix_unchanged_and_first_invalid_terminal_can_enter_child_final(monkeypatch, protocol):
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(200, json=response(protocol))
        if len(payloads) == 2:
            assert payload_history(payload)[:len(payload_history(payloads[0]))] == payload_history(payloads[0])
            return httpx.Response(200, json=response(protocol))
        if len(payloads) == 3:
            return output(protocol, [('submit_report', {'summary': '非法混批候选'}),
                                     ('get_material', {'material_id': 'must-not-read'})])
        wire = json.dumps(payload, ensure_ascii=False)
        assert '-2.04%' in wire and '98.91%' in wire
        assert_private_protocol_only(payload, 'OPAQUE-DO-NOT-LOG', 'NEVER_LOG_PRIVATE_REASONING')
        assert payload['tools'] == payloads[2]['tools']
        assert payload_history(payload)[:len(payload_history(payloads[2]))] == payload_history(payloads[2])
        assert 'invalid_terminal_submission' in wire
        assert wire_tool_exchanges(payload, 'get_material') == [({'material_id': 'must-not-read'},
            wire_tool_exchanges(payload, 'submit_report')[0][1])]
        assert wire_tool_exchanges(payload, 'get_material')[0][1]['error_code'] == 'invalid_terminal_submission'
        return output(protocol, [('submit_report', {'summary': '修正提交，未知仍未知。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler, protocol)
    try:
        add_tool(runtime.client, child.session, 'read', {'text': FINANCIAL['excerpt'] + '\n' + BUSINESS}, 'own-source')
        token = runtime.request_owner.set((child.id, False))
        try:
            await runtime.client.complete(child.session, agents.RESEARCH_TOOLS)
            runtime.client.add_user(child.session, {'task_update': '普通研究继续，前缀不能改写'})
            await runtime.client.complete(child.session, agents.RESEARCH_TOOLS)
        finally:
            runtime.request_owner.reset(token)
        assert child.session.cache_epoch == 0
        result = await runtime._loop(child.id, child.session, manager=False)
        assert result['submission_corrections'] == 1
        assert len(payloads) == 4 and child.session.cache_epoch == 0
        projected, = [e['payload'] for e in events if e['type'] == 'child_final_context_projected']
        assert projected['mode'] == 'tail_append' and projected['history_preserved']
        assert 'OPAQUE-DO-NOT-LOG' not in json.dumps([events, child.session.archive, result])
        assert 'NEVER_LOG_PRIVATE_REASONING' not in json.dumps([events, child.session.archive, result])
    finally:
        await runtime.close()


async def test_last_completed_result_is_first_delivered_only_after_final_http_acceptance(monkeypatch):
    from types import SimpleNamespace
    receipts = []
    class ReceiptStore:
        def record_material_delivery(self, job, account, agent, tool, value, **kwargs):
            receipts.append((agent, tool, copy.deepcopy(value)))
            return [len(receipts)]
        def prepare_pending_tool_output(self, *args):
            return None
    async def handler(request):
        assert receipts == []
        item = next(item for item in projected_items(json.loads(request.content)) if item['tool'] == 'read')
        assert item['visibility'] == 'completed_tool_output_not_previously_delivered'
        assert item['result'] == original
        return output('responses', [('submit_report', {'summary': '刚完成的原文在本次提交前首次交付。'})])
    runtime, child, _ = await make_runtime(monkeypatch, handler)
    original = {'material_id': 'just-completed', 'text': FINANCIAL['excerpt']}
    try:
        runtime.tools = SimpleNamespace(store=ReceiptStore())
        call = add_tool(runtime.client, child.session, 'read', original, 'latest')
        child.session._pending_material_deliveries = [{'call': call,
            'message': copy.deepcopy(child.session.history[-1]),
            'proof': {'tool': 'read', 'result': copy.deepcopy(original)}}]
        child.deadline_closing = True
        result = await runtime._loop(child.id, child.session, manager=False)
        assert receipts == [(child.id, 'read', original)]
        assert result['material_delivery_ids'] == [1]
    finally:
        await runtime.close()


def capture_save_result(monkeypatch, client):
    captured = []
    add_results = client.add_results

    def capture(session, results):
        captured.extend(copy.deepcopy(value) for call, value in results if call.name == 'save_material')
        add_results(session, results)

    monkeypatch.setattr(client, 'add_results', capture)
    return captured


def assert_saved_extraction_on_wire(payload, material, captured, save_fails, reproject):
    if reproject:
        saved, = [item for item in projected_items(payload) if item['tool'] == 'save_material']
        assert saved['authored_material'] == material
        assert saved['authored_material_classification'] == 'agent_extraction'
        assert saved['save_result_status'] == ('failed' if save_fails else 'saved')
        assert 'NEVER_LOG_PRIVATE_REASONING' not in json.dumps(payload)
        result = saved['result']
    else:
        (arguments, result), = wire_tool_exchanges(payload, 'save_material')
        assert arguments == {'material': material}
        assert not any(item['tool'] == 'save_material' for item in projected_items(payload))
        assert_private_protocol_only(payload, 'NEVER_LOG_PRIVATE_REASONING')
    assert captured == [result]
    if save_fails:
        assert result['failed'] is True and result['error'] and 'id' not in result
    else:
        assert result['id'] and len(result['summary']) == 400
        assert result['representation'] == 'agent_extraction'
    return result


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('save_fails', [False, True])
@pytest.mark.parametrize('reproject', [False, True])
async def test_production_save_material_index_keeps_own_authored_tail_without_promoting_failure(tmp_path, monkeypatch, protocol, save_fails, reproject):
    from tests.test_ninth_calculation_context import setup
    payloads = []
    material = {'claim': '已读业务提取前文。' * 100 + 'OWN-AUTHORED-TAIL',
                'financial_metrics': copy.deepcopy(FINANCIAL['expected_financial_metrics'])}
    if save_fails:
        material['id'] = 'inaccessible-material'
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            child.deadline_closing = True
            if reproject:
                for _ in range(9):
                    runtime.client.add_user(child.session, {'context_padding': 'visible ' * 1000})
            return output(protocol, [('save_material', {'material': material})])
        result = assert_saved_extraction_on_wire(payload, material, captured, save_fails, reproject)
        assert '-2.04' in json.dumps(material, ensure_ascii=False)
        assert 'OWN-AUTHORED-TAIL' not in json.dumps(result, ensure_ascii=False)
        if not save_fails:
            assert 'financial_metrics' not in result and result['financial_metrics_count'] == 3
        return output(protocol, [('submit_report', {'summary': '模型提取保持身份，保存状态按实际结果保留。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler, protocol)
    _, tools, context = setup(tmp_path, runtime.config)
    runtime.tools = tools
    runtime.task_id = context['job_id']
    runtime.account_id = context['account']['id']
    captured = capture_save_result(monkeypatch, runtime.client)
    try:
        await runtime._loop(child.id, child.session, manager=False)
        assert len(payloads) == 2
        transition, = [e['payload'] for e in events if e['type'] == 'child_final_context_projected']
        assert transition['mode'] == ('reproject' if reproject else 'tail_append')
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('rejection', ['duplicate_submission', 'candidate_draft'])
async def test_required_correction_compaction_preserves_selected_evidence_and_exact_calculations(monkeypatch, protocol, rejection):
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        wire = json.dumps(payload, ensure_ascii=False)
        assert 'RETAIN-ORIGINAL-AFTER-COMPACTION' in wire
        first = next(json.loads(message['content']) for message in payload_history(payload)
                     if message.get('role') == 'user')
        summary = first['calculation_summaries'][0]
        assert summary['inputs'] == ['1093814390.25', '547112843.14']
        assert summary['result'] == '546701547.11'
        assert len(json.dumps(payload_history(payload), ensure_ascii=False)) < runtime.config['max_context_chars']
        if len(payloads) == 1:
            if rejection == 'duplicate_submission':
                return output(protocol, [('submit_report', {'summary': 'candidate' * 1300}),
                                         ('submit_report', {'summary': 'duplicate' * 1300})])
            answer = response(protocol)
            if protocol == 'responses':
                answer['output'] = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'draft-only ' * 2200}]}]
            else:
                answer['choices'][0]['message']['content'] = 'draft-only ' * 2200
            return httpx.Response(200, json=answer)
        assert first['runtime_status'].get('submission_feedback')
        if rejection == 'candidate_draft':
            assert first['runtime_status']['candidate_draft']['truncated'] is True
        return output(protocol, [('submit_report', {'summary': '协议修正；此前精确证据仍可见。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler, protocol)
    try:
        for i in range(30):
            add_tool(runtime.client, child.session, 'read', {'text': str(i) + ' padding' * 450}, f'fill-{i}')
        add_tool(runtime.client, child.session, 'read', {'url': 'https://public.fixture/q3',
            'text': 'Report preface ' * 90 + ' RETAIN-ORIGINAL-AFTER-COMPACTION ' + FINANCIAL['excerpt']}, 'source')
        runtime._remember_calculation(child.id, 'calculate', {'operation': 'subtract',
            'values': ['1093814390.25', '547112843.14']}, {'operation': 'subtract',
            'inputs': ['1093814390.25', '547112843.14'], 'result': '546701547.11'})
        child.deadline_closing = True
        deadlines = copy.deepcopy(child.session.deadlines)
        await runtime._loop(child.id, child.session, manager=False)
        assert len(payloads) == 2 and child.session.compactions == 1
        assert runtime.client.requests[-1]['prefix_diagnostic']['change_reason'] == 'compression'
        assert child.session.deadlines == deadlines
        assert len([e for e in events if e['type'] == 'child_final_context_projected']) == 1
    finally:
        await runtime.close()


async def test_followup_round_appends_research_resumption_then_selects_new_final_epoch(monkeypatch):
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return output('responses', [('submit_report', {'summary': '第一轮已提交。'})])
        if len(payloads) == 2:
            names = [tool.get('name') for tool in payload['tools']]
            assert 'read' in names and len(names) > 1
            assert child.session.native_search is True
            assert 'child_research_resumed' in json.dumps(payload)
            assert payload_history(payload)[:len(payload_history(payloads[0]))] == payload_history(payloads[0])
            child.deadline_closing = True
            return output('responses', [('report_progress', {'summary': '后续研究保留新问题。'})])
        assert 'NEW-ROUND-OWN-EVIDENCE' in json.dumps(payload)
        return output('responses', [('submit_report', {'summary': '第二轮已有新增证据。'})])
    runtime, child, events = await make_runtime(monkeypatch, handler)
    try:
        add_tool(runtime.client, child.session, 'read', {'text': FINANCIAL['excerpt']}, 'old')
        child.deadline_closing = True
        await runtime._loop(child.id, child.session, manager=False)
        child.round += 1
        child.deadline_closing = False
        runtime.client.add_user(child.session, {'follow_up': '新一轮核对'})
        add_tool(runtime.client, child.session, 'read', {'text': 'NEW-ROUND-OWN-EVIDENCE'}, 'new')
        await runtime._loop(child.id, child.session, manager=False)
        assert len(payloads) == 3
        assert len([e for e in events if e['type'] == 'child_final_context_projected']) == 2
    finally:
        await runtime.close()
