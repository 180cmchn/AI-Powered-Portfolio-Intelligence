"""Synthetic large discovery inventories through the real final HTTP boundary.

No captured account text, real document IDs, credentials or live requests.
"""
import copy
import json

import httpx
import pytest

from app import agents
from app.context import archive, compact, recall
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, seed, setup, source


def source_inventory(index_count, unresolved_count):
    entries = {}
    for status, count in [('index_only', index_count), ('covered', 16), ('needs_research', unresolved_count)]:
        for n in range(count):
            identity = f'{status}-stream-{n:04d}'
            entries[identity] = {'status': status, 'reason': 'discovery only' if status == 'index_only' else 'latest original not processed' if status == 'needs_research' else 'delivered',
                'latest_document_version_id': f'version-{identity}',
                'latest_material_ids': [f'material-{identity}'],
                'historical_material_ids': [f'historical-{identity}'],
                'unresolved_report_agents': ['research-fixture'] if status == 'needs_research' else []}
    return entries


def previous_report():
    # Mirrors a follow-up's large prior report plus repeated structures, rather
    # than a new empty account. The narrative remains a whole visible conclusion.
    return {'id': 'previous-report', 'job_id': 'previous-task', 'input_version': 4,
        'report': 'PREVIOUS-CONCLUSION: known facts, unchanged limits. ' * 100,
        'result': {'research_records': [{'kind': 'prior', 'summary': 'same prior rationale ' * 220} for _ in range(6)],
            'references': [{'material_id': f'old-{n}', 'url': f'https://fixture.invalid/{n}',
                            'range': {'description': 'prior source index ' * 110}} for n in range(9)],
            'forecasts': [{'security_id': f'fixture-{n}', 'reason': 'prior conditional forecast ' * 160} for n in range(4)],
            'suggestions': [{'action': 'hold', 'reason': 'prior position rationale ' * 160} for _ in range(3)],
            'materials': [{'id': f'old-material-{n:04d}'} for n in range(780)]}}


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('shape', ['initial', 'followup'])
async def test_large_manager_inventory_preserves_final_evidence_without_second_compaction(tmp_path, monkeypatch, protocol, shape):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None,
                 max_agent_turns=None, synthesis_max_calls=None, max_context_chars=240000)
    store, tools, context = setup(tmp_path, cfg)
    if shape == 'followup':
        context['previous_analysis'] = previous_report()
    payloads, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        current = seed(payload)
        checkpoint = current['synthesis_checkpoint']
        assert checkpoint['source_coverage_summary']['status_counts']['index_only'] == index_count
        assert checkpoint['source_coverage_summary']['unresolved_count'] == unresolved_count
        assert checkpoint['source_coverage_summary']['unlisted_unresolved_count'] == 0
        assert {item['source_stream'] for item in checkpoint['unresolved_source_coverage']} == {
            f'needs_research-stream-{n:04d}' for n in range(unresolved_count)}
        assert all(item['status'] == 'needs_research' and item['latest_document_version_id']
                   for item in checkpoint['unresolved_source_coverage'])
        assert len(checkpoint['material_ids']) < 780
        assert checkpoint['material_index_summary']['unlisted_ids'] > 0
        assert 'calculations' not in checkpoint
        assert len(current['calculation_summaries']) == count
        assert current['calculation_indexes']['omitted_summary_count'] == 0
        arithmetic = [item for item in current['calculation_summaries'] if item['tool'] == 'calculate']
        assert all(item['inputs_visibility']['status'] == 'complete' and len(item['inputs']) == 2 for item in arithmetic)
        assert all(item['result'] == expected[item['calculation_id']] for item in arithmetic)
        for original in reports:
            retained = next(item for item in checkpoint['reports'] if item['report_id'] == original['report_id'])
            assert retained['summary'] == original['summary'] and retained['findings'] == original['findings']
            assert 'material_delivery_ids' not in retained and retained['material_delivery_count'] == 1000
        assert not current.get('retained_child_reports')
        wire = json.dumps(payload, ensure_ascii=False)
        assert 'DELIVERED-FINAL-FACT' in wire and 'UNREAD-RAW-ARCHIVE' not in wire
        assert len(json.dumps(payload.get('input', payload.get('messages')), ensure_ascii=False)) < cfg['max_context_chars'] * .78
        if shape == 'followup':
            prior = current['previous_analysis']
            assert prior['report'] == context['previous_analysis']['report']
            assert prior['input_version'] == 4 and prior['bounded_history_projection']
            assert prior['full_context']['record_id']
            assert sum(prior['omitted_structure_counts'].values()) > 0
        names = {item.get('name') or item.get('function', {}).get('name') for item in payload['tools']}
        assert names == {'finish_analysis'}
        return output(protocol, [('finish_analysis', delivery_arguments(report='Controlled final payload; not an external model quality verdict.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    index_count, unresolved_count, count = (734, 8, 70) if shape == 'initial' else (70, 2, 39)
    try:
        benchmark = source(store, 'CSI300', 0, benchmark=True)
        for n in range(10):
            material = source(store, 'fixture-asset', n)
            await runtime._dispatch('manager', 'market_indicators', {'material_id': material['id'],
                'benchmark_material_id': benchmark['id']})
        for n in range(count - 10):
            await runtime._dispatch('manager', 'calculate', {'operation': 'subtract', 'values': [str(n) + '.1234', '1.2300']})
        expected = {item['calculation_id']: item['output'].get('result') for item in runtime.calculations}
        reports = []
        for n in range(3):
            child = agents.AgentState(f'research-{n}', f'completed topic {n}', 'fixture',
                runtime.client.session('child fixture', {}), status='completed')
            report = {'report_id': f'completed-report-{n}', 'summary': f'FULL-CHILD-{n}: ' + '已完成的观点及反向事实。' * 600,
                'findings': [{'claim': 'bounded original finding ' * 140}],
                'material_ids': [f'cited-{n}'], 'material_delivery_ids': list(range(n * 1000, (n + 1) * 1000)),
                'open_questions': ['unresolved original scope'], 'research_round': 1}
            child.reports.append(report)
            runtime.children[child.id] = child
            reports.append(copy.deepcopy(report))
        checkpoint = {'phase': 'synthesis', 'task_id': runtime.task_id, 'account_id': runtime.account_id,
            'input_version': runtime.input_version, 'reports': copy.deepcopy(reports),
            'calculations': copy.deepcopy(runtime.calculations), 'recovery_state': 'needs_research',
            'source_coverage': source_inventory(index_count, unresolved_count),
            'material_ids': [f'fixture-material-id-{n:020d}' for n in range(780)],
            'materials': [{'id': f'fixture-material-id-{n:020d}', 'source_title': 'discovery title ' * 20} for n in range(780)]}
        original = copy.deepcopy(checkpoint)
        runtime.synthesis_checkpoint = checkpoint
        runtime.synthesis_only = True
        runtime.context['synthesis_checkpoint'] = agents._checkpoint_context(checkpoint)
        # The legitimate visible output is available; a larger unread archive
        # exists but must never be expanded to fill the reclaimed space.
        session = runtime.manager_session
        from tests.test_seventeenth_child_final_evidence import add_tool
        add_tool(runtime.client, session, 'read', {'text': 'DELIVERED-FINAL-FACT', 'material_id': 'visible-source'}, 'visible')
        archive(session, {'text': 'UNREAD-RAW-ARCHIVE'}, 'tool_result')
        await runtime._enter_synthesis_context(session, reason='bounded_final_request')
        runtime._pin_runtime_status(session, 'manager', None, True)
        before = session.compactions
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(session, [item for item in agents.MANAGER_TOOLS if item['name'] == 'finish_analysis'], tool_choice='finish_analysis')
        finally:
            runtime.request_owner.reset(token)
        assert len(payloads) == 1 and session.compactions == before
        assert not any(event['type'] == 'context_compacted' for event in events)
        assert checkpoint == original
        assert {item['visibility'] for item in runtime.calculation_coverage[-1]['calculations']} == {'summary'}
        # A later explicit compaction reuses one copy of each complete child.
        retained = runtime._retained_runtime_context(session, 'manager', None, True)
        assert not retained.get('retained_child_reports')
        assert len(retained['synthesis_checkpoint']['reports']) == 3
        if shape == 'followup':
            ref = seed(payloads[0])['previous_analysis']['full_context']['record_id']
            page = recall(session, record_id=ref, length=100)
            assert page['total_chars'] > 90000
    finally:
        await runtime.close()


def test_source_inventory_omissions_never_turn_unresolved_into_covered():
    checkpoint = {'phase': 'synthesis', 'source_coverage': source_inventory(734, 8),
                  'reports': [], 'material_ids': []}
    original = copy.deepcopy(checkpoint)
    projected = agents._checkpoint_context(checkpoint, index_budget=1000)
    assert len(projected['unresolved_source_coverage']) < 8
    assert projected['source_coverage_summary']['unresolved_count'] == 8
    assert projected['source_coverage_summary']['unlisted_unresolved_count'] > 0
    assert all(item['status'] == 'needs_research' and item['source_stream']
               for item in projected['unresolved_source_coverage'])
    assert projected['source_coverage_summary']['status_counts'] == {'index_only': 734, 'covered': 16, 'needs_research': 8}
    assert checkpoint == original


def test_checkpoint_declaration_removes_only_internal_receipt_inventories():
    declaration = {'summary': 'Visible manager conclusion.', 'material_ids': ['source-id'],
        'calculation_ids': ['calculation-id'], 'material_delivery_ids': list(range(2000)),
        'report_delivery_ids': list(range(1000)), 'open_questions': ['Unresolved original scope.']}
    checkpoint = {'phase': 'synthesis', 'reports': [], 'manager_summary': declaration}
    before = copy.deepcopy(checkpoint)
    projected = agents._checkpoint_context(checkpoint)['manager_summary']
    assert 'material_delivery_ids' not in projected and 'report_delivery_ids' not in projected
    assert projected['material_delivery_count'] == 2000 and projected['report_delivery_count'] == 1000
    assert projected['material_ids'] == ['source-id'] and projected['calculation_ids'] == ['calculation-id']
    assert projected['summary'] == declaration['summary'] and projected['open_questions'] == declaration['open_questions']
    assert checkpoint == before


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('rejection', ['duplicate_submission', 'candidate_draft'])
async def test_manager_required_correction_compaction_preserves_selected_evidence(tmp_path, monkeypatch, protocol, rejection):
    from tests.test_seventeenth_child_final_evidence import add_tool
    from tests.test_seventeenth_model_cache import response
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=80000,
                 max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    _, tools, context = setup(tmp_path, cfg)
    payloads, events = [], []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        visible = seed(payload)
        assert 'RETAIN-MANAGER-SOURCE' in json.dumps(payload, ensure_ascii=False)
        summary, = visible['calculation_summaries']
        assert summary['inputs'] == ['12.3400', '5.6700'] and summary['result'] == '6.6700'
        assert 'UNREAD-MANAGER-ARCHIVE' not in json.dumps(payload)
        if len(payloads) == 1:
            if rejection == 'duplicate_submission':
                return output(protocol, [('finish_analysis', delivery_arguments(report='candidate ' * 1300)),
                    ('finish_analysis', delivery_arguments(report='duplicate ' * 1300))])
            answer = response(protocol)
            if protocol == 'responses':
                answer['output'] = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'draft-only ' * 2200}]}]
            else:
                answer['choices'][0]['message']['content'] = 'draft-only ' * 2200
            return httpx.Response(200, json=answer)
        assert visible['runtime_status'].get('submission_feedback')
        return output(protocol, [('finish_analysis', delivery_arguments(report='Corrected protocol; retained source and exact operands.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        session = runtime.manager_session
        for n in range(30):
            add_tool(runtime.client, session, 'read', {'text': str(n) + ' visible detail' * 250}, f'padding-{n}')
        add_tool(runtime.client, session, 'read', {'text': 'RETAIN-MANAGER-SOURCE: source limitations remain known.'}, 'source')
        archive(session, {'text': 'UNREAD-MANAGER-ARCHIVE'}, 'tool_result')
        await runtime._dispatch('manager', 'calculate', {'operation': 'subtract', 'values': ['12.3400', '5.6700']})
        runtime.budget_closing = True
        result = await runtime._loop('manager', session, manager=True)
        assert len(payloads) == 2 and result['submission_corrections'] == 1
        assert session.compactions == 1
        assert runtime.client.requests[-1]['prefix_diagnostic']['change_reason'] == 'compression'
        assert len([event for event in events if event['type'] == 'synthesis_context_projected']) == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('save_fails', [False, True])
async def test_manager_own_save_arguments_survive_index_result_without_promoting_failure(tmp_path, monkeypatch, protocol, save_fails):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None,
                 max_agent_turns=None, synthesis_max_calls=None)
    _, tools, context = setup(tmp_path, cfg)
    material = {'claim': 'own extraction; not independent evidence. ' * 100 + 'MANAGER-EXTRACTION-TAIL',
                'material_ids': [], 'uncertainties': ['No original source established by this authored text.']}
    if save_fails:
        material['id'] = 'not-accessible'
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            runtime.budget_closing = True
            return output(protocol, [('save_material', {'material': material})])
        saved = next(item for item in seed(payload)['delivered_research_evidence'] if item['tool'] == 'save_material')
        assert saved['authored_material'] == material
        assert saved['authored_material_classification'] == 'agent_extraction'
        assert saved['save_result_status'] == ('failed' if save_fails else 'saved')
        assert 'MANAGER-EXTRACTION-TAIL' not in json.dumps(saved['result'])
        if not save_fails:
            assert len(saved['result']['summary']) == 400
        return output(protocol, [('finish_analysis', delivery_arguments(report='Authored extraction and save status remain separate.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        await runtime._loop('manager', runtime.manager_session, manager=True)
        assert len(payloads) == 2
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_small_context_reports_are_explicit_excerpts_and_legacy_summary_remains_visible(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=10000,
                 max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    reports = [{'report_id': f'legacy-report-{n}', 'agent_id': f'legacy-child-{n}',
        'summary': 'Known source-derived result. ' * 180,
        'findings': [{'claim': 'Original report fact and limitations.'}], 'material_ids': [],
        'open_questions': ['Still unresolved.']} for n in range(3)]
    legacy = {'tool': 'calculate', 'summary': {'tool': 'calculate', 'operation': 'subtract',
        'result': '6.67', 'status': 'ok', 'source_task_id': 'original-task', 'agent_id': 'original-child'}}
    checkpoint = {'phase': 'synthesis', 'task_id': 'original-task', 'reports': reports,
                  'calculations': [legacy], 'material_ids': []}
    before = copy.deepcopy(checkpoint)
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        current = seed(payload)
        projected = current['synthesis_checkpoint']
        assert projected['reports_bounded_for_context'] is True
        assert len(projected['reports']) == 3 and projected['unlisted_report_count'] == 0
        assert all(item['summary_truncated'] and item['complete_report_ref'] for item in projected['reports'])
        result, = current['calculation_summaries']
        assert result['result'] == '6.67' and result['source_task_id'] == 'original-task'
        assert result['agent_id'] == 'original-child' and 'inputs' not in result
        assert result['inputs_visibility']['status'] == 'unavailable'
        assert 'saved_summary_only' in result['full_inputs']['scope']
        assert all(item['access'] != 'full_report' for item in runtime.request_report_visibility)
        # max_context_chars bounds session history. Chat's separate system
        # prompt is serialized into messages; Responses uses instructions.
        history = [item for item in payload.get('input', payload.get('messages')) if item.get('role') != 'system']
        assert len(json.dumps(history, ensure_ascii=False)) < 10000 * .78
        return output(protocol, [('finish_analysis', delivery_arguments(report='Limited visible report excerpts, no invented operands.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime({**context, 'synthesis_checkpoint': checkpoint}, cfg, tools, emit)
    try:
        assert runtime.calculations[0]['summary_only']
        await runtime._enter_synthesis_context(runtime.manager_session, reason='bounded_final_request')
        runtime._pin_runtime_status(runtime.manager_session, 'manager', None, True)
        token = runtime.request_owner.set(('manager', True))
        try:
            await runtime.client.complete(runtime.manager_session,
                [item for item in agents.MANAGER_TOOLS if item['name'] == 'finish_analysis'])
        finally:
            runtime.request_owner.reset(token)
        assert len(requests) == 1 and runtime.manager_session.compactions == 0
        assert checkpoint == before
        ref = seed(requests[0])['synthesis_checkpoint']['report_context']['record_id']
        assert recall(runtime.manager_session, record_id=ref)['total_chars'] == len(json.dumps(reports, ensure_ascii=False))
    finally:
        await runtime.close()


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_projected_report_receipts_keep_raw_source_lineage_and_identity(tmp_path, monkeypatch, protocol):
    from app.recovery import report_identity
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=1, max_agent_turns=1)
    store, tools, context = setup(tmp_path, cfg)
    material = store.save_material({'scope': 'public', 'url': 'https://fixture.invalid/recovered-source',
        'text': 'Known original fixture source', 'access': 'full_text'})
    ids = store.record_material_delivery(context['job_id'], context['account']['id'], 'child-fixture',
        'get_material', store.material_range(material['id']))
    report = {'report_id': 'recovered-report', 'agent_id': 'child-fixture', 'summary': 'Known finding',
        'material_ids': [material['id']], 'material_delivery_ids': ids, 'research_round': 1,
        'findings': [{'claim': 'Known source fixture'}], 'open_questions': []}
    checkpoint = {'phase': 'synthesis', 'task_id': context['job_id'], 'account_id': context['account']['id'],
        'input_version': context['input_version'], 'reports': [report], 'calculations': []}
    original = copy.deepcopy(checkpoint)
    async def handler(request):
        projected = seed(json.loads(request.content))['synthesis_checkpoint']['reports'][0]
        assert 'material_delivery_ids' not in projected
        assert projected['material_delivery_count'] == len(ids)
        assert projected['material_ids'] == [material['id']]
        return output(protocol, [('finish_analysis', delivery_arguments(report='Known finding and unchanged original source lineage.'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime({**context, 'synthesis_checkpoint': checkpoint}, cfg, tools, emit)
    try:
        await runtime.run()
        receipts = store.checkpoint_report_deliveries(context['job_id'])
        full = [item['body'] for item in receipts if item['body']['access'] == 'full_report']
        assert full
        assert all(item['report_hash'] == report_identity(report) for item in full)
        assert all([entry['id'] for entry in item['source_material_deliveries']] == ids for item in full)
        assert checkpoint == original and runtime.restored_checkpoint == original
    finally:
        await runtime.close()
