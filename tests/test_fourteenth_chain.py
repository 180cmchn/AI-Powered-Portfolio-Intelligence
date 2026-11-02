"""Real ModelClient -> Agent -> Service -> Store -> API, only HTTP is scripted.

No model SDK/agent/SQL publication substitute and no paid calls. These tests
prove delivery behavior, not model opinion quality or endpoint compatibility.
"""
import hashlib
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.main import create_app
from app.model import ModelClient
from tests.helpers_sixth import configure_test_team
from scripts.verify_fourteenth_live import acceptance_layers


def test_transport_review_does_not_infer_http_eof_from_a_protocol_terminal():
    summary = {'all_attempts_after_shutdown': [{'requests': [
        {'response_status': 'completed', 'http_body_complete': False},
        {'response_status': 'completed', 'response_body_received': True},
    ]}]}
    transport = acceptance_layers(summary)['transport']
    assert transport['complete_terminals'] == 2 and transport['all_complete']
    assert transport['http_bodies_complete'] == 0
    assert transport['http_body_completion_unknown'] == 1
    assert not transport['all_http_bodies_complete']


def final(report='保留现金100元，暂不投入；有明确用途后再确定投入比例。', **changes):
    return {'report': report, 'completion': 'full', 'scope': ['cash_only'],
            'decision_summary': '保留现金，待明确用途后决定投入比例。', 'limitations': [], **changes}


def call(name, arguments, identity='final'):
    return {'type': 'function_call', 'call_id': identity, 'name': name,
            'arguments': json.dumps(arguments, ensure_ascii=False)}


def response(*items):
    return httpx.Response(200, json={'id': 'fixture', 'status': 'completed', 'output': list(items),
        'usage': {'input_tokens': 10, 'output_tokens': 4}})


def prose(text='正在整理最终报告。'):
    return {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}


def setup_app(tmp_path, monkeypatch, handler, **limits):
    app = create_app(tmp_path/'delivery.sqlite3', mode='live', worker=False)
    store = app.state.store
    config = {**store.settings(), 'model_name': 'fourteenth-controlled',
        'model_base_url': 'https://fixture.invalid/v1', 'model_protocol': 'responses',
        'model_native_search': False, 'model_responses_stream': False, 'model_retries': 0,
        'quote_provider': 'manual', 'debounce_seconds': 0, 'max_model_calls': 4,
        'max_agent_turns': 4, 'analysis_timeout_seconds': 10,
        'agent_timeout_seconds': 5, 'model_request_hard_timeout_seconds': 2,
        'synthesis_request_hard_timeout_seconds': 2, **limits}
    store.set_meta('settings', config)
    configure_test_team(store, config)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
                        ModelClient(cfg, before_request, transport))
    account = store.save_account(AccountInput(name='受控现金账户', cash='100', holdings=[], research_scope='cash_only'))
    return app, store, account


async def execute(app, store, account):
    job = store.claim_jobs(account_id=account['id'])[0]
    await app.state.service.execute(job)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        saved = (await api.get('/api/jobs/'+job['id'])).json()
        reports = (await api.get('/api/accounts/'+account['id']+'/analyses')).json()
    return saved, reports


@pytest.mark.asyncio
async def test_plain_completed_process_message_fails_through_api_without_budget(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response(prose())
    app, store, account = setup_app(tmp_path, monkeypatch, handler, max_model_calls=1, max_agent_turns=1)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'failed'
    assert saved['error_code'] == 'final_submission_missing'
    assert saved['delivery']['status'] == 'incomplete'
    assert not saved['delivery']['contract_verified']
    assert reports == [] and len(requests) == 1
    events = store.job(saved['id'], events=True)['events']
    assert any('正在整理最终报告' in json.dumps(event, ensure_ascii=False) for event in events)
    assert not any(event['type'] == 'published' for event in events)


@pytest.mark.asyncio
async def test_complete_plain_draft_requires_explicit_correction_then_short_cash_report_publishes(tmp_path, monkeypatch):
    requests = []
    draft = '维持100元现金，等资金用途明确后再确定配置。'
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(prose(draft))
        assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
        assert draft in json.dumps(payload, ensure_ascii=False)
        return response(call('finish_analysis', final(draft)))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(requests) == 2 and len(reports) == 1
    result = reports[0]['result']
    assert result['report'] == draft
    assert result['delivery']['status'] == 'full'
    assert result['delivery']['contract_verified']
    assert result['delivery']['content_assessment'] == 'not_evaluated'
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0
    # Response-loss/repeated publication cannot create a second analysis.
    assert store.publish(saved['id'], result, None)
    assert len(store.analyses(account['id'])) == 1
    store.recover(config_ready=True)
    assert store.job(saved['id'])['state'] == 'completed'
    assert store.claim_jobs(account_id=account['id']) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing_fields', 'multiple', 'mixed', 'truncated', 'incomplete_item', 'wrong_tool'])
async def test_invalid_terminal_is_corrected_without_partial_publication(tmp_path, monkeypatch, bad):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) > 1:
            assert not store.analyses(account['id'])
            assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
            return response(call('finish_analysis', final(completion='limited', limitations=['未研究证券配置'])) )
        item = call('finish_analysis', final())
        if bad == 'missing_fields': item = call('finish_analysis', {'report': '候选'})
        if bad == 'truncated': item['arguments'] = '{"report":'
        if bad == 'incomplete_item': item['status'] = 'incomplete'
        if bad == 'wrong_tool': return response(call('save_material', {'material': {'text': '禁止写入'}}, 'save'))
        if bad == 'multiple': return response(item, call('finish_analysis', final(), 'second'))
        if bad == 'mixed': return response(item, call('save_material', {'material': {'text': '禁止写入'}}, 'save'))
        return response(item)
    app, store, account = setup_app(tmp_path, monkeypatch, handler, max_model_calls=2)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(reports) == 1 and len(requests) == 2
    assert reports[0]['result']['delivery']['status'] == 'limited'
    assert not store.material_count()


@pytest.mark.asyncio
async def test_failed_same_version_research_preserves_current_old_report(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response(call('finish_analysis', final())) if len(requests) == 1 else response(prose())
    app, store, account = setup_app(tmp_path, monkeypatch, handler, max_model_calls=1)
    first, before = await execute(app, store, account)
    assert first['state'] == 'completed'
    store.submit_task(account['id'], research_scope='cash_only')
    second, after = await execute(app, store, account)
    assert second['state'] == 'failed'
    assert second['error_code'] == 'final_submission_missing'
    assert [(r['id'], r['status']) for r in after] == [(before[0]['id'], 'current')]
    assert after[0]['report'] == before[0]['report']


def test_content_review_is_never_inferred_from_transport_or_structure():
    body = '保留现金，待明确用途。'
    summary = {'released_job_id': 'j', 'business_jobs_with_http': 1,
        'report_published': True, 'single_report_acceptance_passed': True,
        'final_reports': [{'job_id': 'j', 'status': 'current', 'result': {'report': body,
            'delivery': {'status': 'full', 'contract_verified': True}}}],
        'all_attempts_after_shutdown': [{'requests': [{'stream_terminal': 'response.completed'}]}]}
    layers = acceptance_layers(summary)
    assert layers['transport']['all_complete']
    assert layers['delivery_contract']['satisfied']
    assert layers['content']['accepted'] is None
    assert not layers['overall_live_acceptance_passed']
    review = {'kind': 'manual_content_review', 'reviewer': 'named-developer',
        'reviewed_at': '2026-11-02T12:00:00+08:00', 'report_sha256': hashlib.sha256(body.encode()).hexdigest(),
        'checks': dict.fromkeys(('account_facts', 'answers_scope', 'basis_and_counter_factors', 'conditions', 'limitations'), 'pass')}
    assert acceptance_layers(summary, review)['overall_live_acceptance_passed']
    review['report_sha256'] = 'wrong-body'
    assert acceptance_layers(summary, review)['content']['accepted'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ignored', 'rejected', 'deadline'])
async def test_final_endpoint_failure_remains_visible_and_bounded(tmp_path, monkeypatch, mode):
    import asyncio
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if mode == 'rejected':
            return httpx.Response(400, json={'error': {'message': 'tool_choice function is unsupported', 'param': 'tool_choice'}})
        if mode == 'deadline':
            await asyncio.sleep(.1)
        return response(prose())
    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        max_model_calls=2, max_agent_turns=2,
        **({'model_request_hard_timeout_seconds': .02, 'synthesis_request_hard_timeout_seconds': .02}
           if mode == 'deadline' else {}))
    saved, reports = await execute(app, store, account)
    assert reports == []
    assert saved['state'] == 'failed', saved
    assert len(requests) <= 2
    if mode == 'ignored':
        assert requests[-1]['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
        assert saved['error_code'] == 'final_submission_missing'
    elif mode == 'rejected':
        assert 'tool_choice' in saved.get('error', '') or saved.get('error_code') == 'tool_choice_unsupported'
    else:
        assert '时限' in saved.get('error', '')
    events = store.job(saved['id'], events=True)['events']
    assert any(event['type'] in ('failed', 'configuration_missing') for event in events)


@pytest.mark.asyncio
async def test_single_child_is_material_for_main_not_automatic_account_report(tmp_path, monkeypatch):
    requests = []
    child_text = '受控专题：现金100元，不含证券行情判断。'
    state = {'spawned': False, 'waited': False}
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        seed = json.loads(payload['input'][0]['content'])
        if seed.get('task'):
            return response(call('submit_report', {'summary': child_text, 'open_questions': ['证券专题未研究']}))
        if not state['spawned']:
            state['spawned'] = True
            return response(call('spawn_research', {'role_id': 'portfolio_researcher', 'task': '仅梳理已知现金', 'title': '现金专题'}, 'spawn'))
        if not state['waited']:
            state['waited'] = True
            return response(call('wait_agents', {'timeout_seconds': 1}, 'wait'))
        assert child_text in json.dumps(payload, ensure_ascii=False)
        return response(call('finish_analysis', final(completion='limited', limitations=['证券专题未研究，仅分析已知现金'])))
    app, store, account = setup_app(tmp_path, monkeypatch, handler, max_model_calls=7, max_agent_turns=6)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(reports) == 1 and reports[0]['report'] != child_text
    assert reports[0]['result']['delivery']['status'] == 'limited'
    assert any(child_text in json.dumps(event, ensure_ascii=False)
               for event in store.job(saved['id'], events=True)['events'])


@pytest.mark.asyncio
@pytest.mark.parametrize('ignore_forced', [False, True])
async def test_bounded_final_comparison_uses_identical_visible_input_and_counts_http(tmp_path, monkeypatch, ignore_forced):
    from types import SimpleNamespace
    from app import model
    from app.store import Store
    from scripts import verify_eleventh_live, verify_fourteenth_live
    source = Store(tmp_path/'source.sqlite3', 'live')
    cfg = {**source.settings(), 'model_name': 'fourteenth-compare-fixture',
        'model_base_url': 'https://fixture.invalid/v1', 'model_protocol': 'responses',
        'model_responses_stream': False, 'model_native_search': False,
        'model_retries': 0, 'reasoning_effort': 'max', 'max_model_calls': 4}
    source.set_meta('settings', cfg)
    configure_test_team(source, cfg)
    account = source.save_account(AccountInput(name='模拟来源', cash='100', holdings=[], research_scope='cash_only'))
    original_configure = verify_eleventh_live.configure
    def configure(origin, target, identity):
        saved, settings = original_configure(origin, target, identity)
        saved._credential_store = source._credential_store
        return saved, settings
    monkeypatch.setattr(verify_eleventh_live, 'configure', configure)
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        if ignore_forced and 'tool_choice' in payloads[-1]:
            return response(prose())
        return response(call('finish_analysis', final(scope=['account'])))
    transport = httpx.MockTransport(handler)
    actual = model.ModelClient
    monkeypatch.setattr(model, 'ModelClient', lambda cfg, before_request=None: actual(cfg, before_request, transport))
    package = {'kind': 'controlled_fixture', 'account': {'cash': '100', 'holdings': []},
        'reports': [{'summary': '受控专题'}], 'calculations': [{'tool': 'calculate',
            'summary': {'status': 'ok', 'result': '100'}, 'output': {'large_raw_not_in_final_seed': True}}],
        'materials': [{'id': str(i), 'source_title': '受控来源' * 250, 'access': 'source_link'} for i in range(100)],
        'material_deliveries': [{'private_full_receipt': True}], 'boundary': 'controlled'}
    evidence = tmp_path/'evidence.json'
    evidence.write_text(json.dumps(package))
    output = tmp_path/'comparison'
    args = SimpleNamespace(evidence=evidence, output=output, source=source.path,
        reference_account=account['id'], authorized_http_attempts=2,
        authorization_note='test fixture only; zero paid/network calls')
    assert await verify_fourteenth_live.compare(args) == (2 if ignore_forced else 0)
    summary = json.loads((output/'summary.json').read_text())
    assert len(payloads) == 2
    assert payloads[0]['input'] == payloads[1]['input']
    assert payloads[0]['instructions'] == payloads[1]['instructions']
    assert 'tool_choice' not in payloads[0]
    assert payloads[1]['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
    assert all(p['model'] == 'fourteenth-compare-fixture' and p['reasoning']['effort'] == 'max' for p in payloads)
    assert summary['attempt_budget'] == {'maximum': 2, 'reserved_attempts': 2}
    assert summary['variants'][0]['delivery_contract_satisfied']
    assert summary['variants'][1]['delivery_contract_satisfied'] is (not ignore_forced)
    assert summary['input_characters'] > cfg.get('max_context_chars', 240000) // 3
    expected_seed = verify_fourteenth_live.comparison_seed(package)
    assert json.loads(payloads[1]['input'][0]['content']) == expected_seed
    assert json.loads(payloads[1]['input'][0]['content'])['reports'] == package['reports']
    assert json.loads(payloads[1]['input'][0]['content'])['calculations'][0]['summary'] == package['calculations'][0]['summary']
    assert all(item['actual_request_inputs'][0]['identical_fixed_input']
        and item['actual_request_inputs'][0]['full_reports_visible']
        and item['actual_request_inputs'][0]['calculation_summaries_visible'] for item in summary['variants'])
    assert summary['content_assessment'] == 'not_evaluated'
    assert summary['formal_predictions_written'] == summary['daily_database_writes'] == 0
    assert 'private_full_receipt' not in json.dumps(payloads)
    assert 'large_raw_not_in_final_seed' not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_successful_calculation_remains_visible_in_actual_final_request(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(call('calculate', {'operation': 'sum', 'values': ['60', '40']}, 'sum'))
        if len(requests) == 2:
            return response(call('prepare_synthesis', {'summary': '现金计算已完成，只分析现金安排。'}, 'prepare'))
        seed = json.loads(payload['input'][0]['content'])
        calculations = seed['calculation_summaries']
        assert any(item.get('status') == 'ok' and str(item.get('result')) == '100'
                   for item in calculations)
        assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler, max_model_calls=6, max_agent_turns=4)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(requests) == 3 and len(reports) == 1
    events = store.job(saved['id'], events=True)['events']
    assert any(event['type'] == 'final_calculation_coverage' for event in events)


def test_final_comparison_cli_defaults_to_zero_authorized_calls(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1]/'scripts/verify_fourteenth_live.py'
    result = subprocess.run([sys.executable, str(script), '--run-real', '--output', str(tmp_path/'must-not-exist')],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert 'Default allowance is 0' in result.stderr
    assert not (tmp_path/'must-not-exist').exists()


def test_shared_final_and_full_budget_is_atomic_and_survives_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.model import ModelBudgetExceeded
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    path = tmp_path/'authorized44.sqlite3'
    SharedAttemptBudget.initialize(path)
    def reserve(stage):
        try:
            SharedAttemptBudget(path).reserve(stage)
            return stage
        except ModelBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        done = list(pool.map(reserve, ['final']*12 + ['full']*60))
    assert done.count('final') == 4 and done.count('full') == 40
    assert SharedAttemptBudget(path).status() == {'maximum': 44, 'reserved_attempts': 44,
        'stages': {'final': {'maximum': 4, 'reserved_attempts': 4}, 'full': {'maximum': 40, 'reserved_attempts': 40}}}
    with pytest.raises(FileExistsError):
        SharedAttemptBudget.initialize(path)
    with pytest.raises(ModelBudgetExceeded):
        SharedAttemptBudget(path).reserve('full')


def test_full_run_gate_requires_actual_body_review_and_same_final_source(tmp_path, monkeypatch):
    from scripts import verify_fourteenth_research as full
    monkeypatch.setattr(full, 'source_digest', lambda: 'fixed-source')
    report = '现金100元维持观察；资金用途确定后调整配置。'
    summary = {'source_sha256': 'fixed-source', 'final_source_unchanged': True,
        'final_environment_unchanged': True, 'variants': [{'variant': 'named_finish_analysis',
        'delivery_contract_satisfied': True, 'tool_choice_status': 'observed',
        'tool_calls': [{'name': 'finish_analysis', 'arguments': {'report': report}}]}]}
    summary_path, review_path = tmp_path/'summary.json', tmp_path/'review.json'
    summary_path.write_text(json.dumps(summary))
    review = {'kind': 'manual_content_review', 'reviewer': 'explicit-test-review', 'reviewed_at': '2026-11-02T10:00:00+08:00',
        'report_sha256': hashlib.sha256(report.encode()).hexdigest(), 'checks':
        dict.fromkeys(('account_facts', 'answers_scope', 'basis_and_counter_factors', 'conditions', 'limitations'), 'pass')}
    review_path.write_text(json.dumps(review))
    assert full.require_approved_final(summary_path, review_path)['report_sha256'] == review['report_sha256']
    review['checks']['conditions'] = 'fail'
    review_path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match='manual review'):
        full.require_approved_final(summary_path, review_path)
    review['checks']['conditions'] = 'pass'
    review['report_sha256'] = 'another-body'
    review_path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match='body-bound'):
        full.require_approved_final(summary_path, review_path)
