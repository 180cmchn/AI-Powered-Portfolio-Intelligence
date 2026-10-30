"""Runtime/Service protocol fixtures, never real model or fault-recovery evidence."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient, ToolCall
from app.service import ResearchTools, Service
from app.store import Store
from tests.helpers_sixth import configure_test_team


def output(*calls):
    return httpx.Response(200, json={'status': 'completed', 'output': [
        {'type': 'function_call', 'call_id': str(i), 'name': name, 'arguments': json.dumps(args)}
        for i, (name, args) in enumerate(calls)]})


def source(store, version):
    return store.save_material({'scope': 'public', 'url': 'https://issuer.example/delegated',
        'document_id': version, 'text': 'Actual visible source ' + version,
        'access': 'full_text', 'truncated': False},
        observation_id=store.begin_material_observation('https://issuer.example/delegated'))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    store = Store(tmp_path / 'delegation.sqlite3')
    monkeypatch.setenv('EIGHTH_DELEGATION_KEY', 'controlled-protocol-only')
    cfg = {'model_name': 'delegation-fixture', 'model_base_url': 'https://model.example/v1',
        'model_key_env': 'EIGHTH_DELEGATION_KEY', 'model_protocol': 'responses',
        'model_native_search': False, 'quote_provider': 'manual', 'debounce_seconds': 0,
        'model_retries': 0, 'model_request_timeout_seconds': 3,
        'synthesis_request_timeout_seconds': 3, 'max_model_calls': 20,
        'max_agent_turns': 8, 'agent_timeout_seconds': 8, 'analysis_timeout_seconds': 12}
    store.set_meta('settings', cfg)
    configure_test_team(store, cfg)
    account = store.save_account(AccountInput(name='Delegation', holdings=[], research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    context = {'account': account, 'input_version': job['input_version'], 'job_id': job['id'],
        'universe_version': store.universe_snapshot()['universe_version'], 'metrics': {'positions': []}}
    return store, account, job, context


def checkpoint(store, job_id):
    with store.connect() as db:
        row = db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (job_id,)).fetchone()
    return json.loads(row[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('correction', [False, True])
async def test_service_delegation_fault_resumes_without_reread_or_new_child(configured, monkeypatch, correction):
    store, account, job, _ = configured
    materials = [source(store, 'v1')]
    if correction:
        materials.append(source(store, 'v2'))
    mids = [material['id'] for material in materials]
    requests = {'child': 0, 'manager': 0, 'resumed': 0}
    completed = asyncio.Event()
    initial_contexts = []
    service = Service(store)
    original_record = service.record_model_event

    def record(task_id, event):
        original_record(task_id, event)
        if event.get('type') == 'agent_completed' and event.get('agent_id') != 'manager':
            completed.set()
    service.record_model_event = record

    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload['input'][0]['content'])
        resumed = seed.get('synthesis_checkpoint') or {}
        if resumed.get('checkpoint_job_id'):
            requests['resumed'] += 1
            assert resumed['reports'][0]['role_id'] == 'filing_researcher'
            assert resumed['reports'][0]['model_config']['model_name'] == 'delegation-fixture'
            assert resumed['manager_summary']['material_ids'] == mids
            assert not {'read', 'spawn_research'} & {tool.get('name') for tool in payload['tools']}
            # Receiving an old report in this new synthesis session is a new
            # delivery fact, never a fabricated original-material read.
            new_deliveries = store.checkpoint_report_deliveries(seed['job_id'])
            assert any(item['body']['access'] == 'full_report' for item in new_deliveries)
            assert all(item['source_job_id'] == job['id'] for item in new_deliveries)
            assert store.checkpoint_deliveries(seed['job_id']) == []
            return output(('finish_analysis', {'report': '依据披露研究员已交付的专题结果进行有条件汇总。', 'material_ids': mids}))
        if seed.get('parent_task_id'):
            requests['child'] += 1
            if requests['child'] == 1:
                return output(*[('get_material', {'material_id': mid}) for mid in mids])
            result = {'summary': '披露已读取：' + ('更正取代旧版，比较范围已说明。' if correction else '当前只有同一版 v1。'),
                'material_ids': mids, 'findings': [{'fact': '可见事实', 'material_ids': mids}], 'open_questions': []}
            if correction:
                result['material_updates'] = [{'latest_material_id': mids[-1], 'prior_material_ids': mids[:-1],
                    'status': 'handled', 'summary': '已比较 v1 与 v2，采用 v2 并保留旧版差异。'}]
            return output(('submit_report', result))
        requests['manager'] += 1
        if requests['manager'] == 1:
            initial_contexts.append(seed)
            return output(('spawn_research', {'role_id': 'filing_researcher', 'task': '读取资料，说明版本与事实。'}))
        if requests['manager'] == 2:
            await asyncio.wait_for(completed.wait(), 5)
            return output(('agent_status', {}))
        if requests['manager'] == 3:
            return output(('prepare_synthesis', {'summary': '根据已收到的披露专题报告综合，无需重复下载原文。', 'material_ids': mids}))
        raise httpx.RemoteProtocolError('controlled synthesis fault', request=request)

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    await service.execute(job)
    assert store.job(job['id'])['state'] == 'failed'
    saved = checkpoint(store, job['id'])
    assert saved['recovery_state'] == 'synthesis_ready'
    coverage = next(iter(saved['source_coverage'].values()))
    assert coverage['report_delivery_ids'] and 'manager' in coverage['handled_by']
    assert not coverage['unresolved_report_agents']
    originals = store.checkpoint_deliveries(job['id'])
    assert len(originals) == len(materials)
    assert all(item['agent_id'].startswith('research-') for item in originals)
    reports = store.checkpoint_report_deliveries(job['id'])
    assert any(item['body']['access'] == 'full_report' and item['body']['source_material_deliveries'] for item in reports)
    assert all(item['body']['research_round'] == 1 for item in reports)
    store.submit_task(account['id'], research_scope='cash_only', parent_job_id=job['id'])
    retry = store.claim_jobs(account_id=account['id'])[0]
    await service.execute(retry)
    assert store.job(retry['id'])['state'] == 'completed'
    assert requests == {'child': 2, 'manager': 4, 'resumed': 1}
    assert store.analyses(account['id'])[0]['result']['synthesis_recovered']
    assert store.checkpoint_deliveries(job['id']) == originals
    source(store, 'v3')
    assert store.reusable_synthesis_checkpoint(retry, initial_contexts[0]) is None


async def runtime_child(configured, monkeypatch, *, read=True, correction=False, handled=True, long_summary=False):
    store, account, job, context = configured
    materials = [source(store, 'v1')]
    if correction:
        materials.append(source(store, 'v2'))
    mids = [material['id'] for material in materials]
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            if read:
                return output(*[('get_material', {'material_id': mid}) for mid in mids])
            return output(('list_materials', {}))
        result = {'summary': '专题可见结果。' * (700 if long_summary else 1), 'material_ids': mids,
            'findings': [{'fact': '局部结果与缺口', 'material_ids': mids}], 'open_questions': []}
        if correction and handled:
            result['material_updates'] = [{'latest_material_id': mids[-1], 'prior_material_ids': mids[:-1],
                'status': 'handled', 'summary': '处理当前更正。'}]
        return output(('submit_report', result))

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, job['config_snapshot'], ResearchTools(store, job, job['config_snapshot'], context), emit)
    spawned = await runtime._spawn({'role_id': 'filing_researcher', 'task': '读取并交付专题。'})
    child = runtime.children[spawned['agent_id']]
    await child.task_handle
    assert child.status == 'completed'
    return runtime, child, mids


async def deliver_report(runtime):
    result = await runtime._dispatch('manager', 'agent_status', {})
    results = [(ToolCall('actual-status', 'agent_status', {}), result)]
    runtime.client.add_results(runtime.manager_session, results)
    runtime._remember_report_results(results, runtime.manager_session.history[-1:])
    await runtime._use_budget()


@pytest.mark.asyncio
async def test_undelivered_summary_and_round_scope_are_not_direct_reads(configured, monkeypatch):
    store, account, job, context = configured
    runtime, child, mids = await runtime_child(configured, monkeypatch, long_summary=True)
    try:
        declaration = {'summary': '综合专题。', 'material_ids': mids}
        await runtime._save_checkpoint(declaration)
        saved = checkpoint(store, job['id'])
        assert saved['recovery_state'] == 'needs_research'
        assert next(iter(saved['source_coverage'].values()))['reason'] == 'delegated_report_not_delivered'
        # Merely executing status internally is not delivery to the conversation.
        await runtime._dispatch('manager', 'agent_status', {})
        assert store.checkpoint_report_deliveries(job['id']) == []
        runtime._pin_runtime_status(runtime.manager_session, 'manager', 5, False)
        await runtime._use_budget()
        await runtime._save_checkpoint(declaration)
        assert checkpoint(store, job['id'])['recovery_state'] == 'needs_research'
        assert {row['body']['access'] for row in store.checkpoint_report_deliveries(job['id'])} == {'report_summary'}
        assert all(row['body']['summary_truncated'] for row in store.checkpoint_report_deliveries(job['id']))
        await deliver_report(runtime)
        await runtime._save_checkpoint(declaration)
        assert checkpoint(store, job['id'])['recovery_state'] == 'synthesis_ready'
        assert all(row['agent_id'] != 'manager' for row in store.checkpoint_deliveries(job['id']))
        # A new unanswered research round invalidates the earlier-round finding.
        await runtime._dispatch('manager', 'follow_up', {'agent_id': child.id, 'task': '核对新出现的差异。'})
        assert child.round == 2
        await runtime._save_checkpoint(declaration)
        saved = checkpoint(store, job['id'])
        assert saved['recovery_state'] == 'needs_research'
        assert next(iter(saved['source_coverage'].values()))['reason'] == 'research_round_pending'
        other = store.save_account(AccountInput(name='Other', holdings=[]))
        other_job = store.claim_jobs(account_id=other['id'])[0]
        assert store.record_report_delivery(other_job['id'], other['id'], child.reports[0], child.reports[0],
            source_job_id=job['id'], channel='cross_account_rejected') is None
        assert store.reusable_synthesis_checkpoint(other_job, {**context, 'account': other}) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['index_only', 'unhandled_correction'])
async def test_delegation_does_not_upgrade_unread_or_unhandled_sources(configured, monkeypatch, case):
    store, _, job, _ = configured
    runtime, child, mids = await runtime_child(configured, monkeypatch,
        read=case != 'index_only', correction=case == 'unhandled_correction', handled=False)
    try:
        await deliver_report(runtime)
        await runtime._save_checkpoint({'summary': '收到报告仍有来源缺口。', 'material_ids': mids})
        saved = checkpoint(store, job['id'])
        assert saved['recovery_state'] == 'needs_research'
        coverage = next(iter(saved['source_coverage'].values()))
        assert not coverage['report_delivery_ids']
        if case == 'index_only':
            assert store.checkpoint_deliveries(job['id']) == []
            assert coverage['reason'] == 'visible_report_source_not_received'
        else:
            assert coverage['reason'] == 'historical_comparison_not_processed'
    finally:
        await runtime.close()


def test_formal_batch_guard_is_frozen_but_readiness_is_refreshable(configured):
    store, _, _, context = configured
    batch = {'id': 'formal-1', 'kind': 'formal', 'base_date': '2026-10-29', 'rule_version': 'r1',
        'deadline': '2026-10-29T18:00:00+08:00', 'expected_count': 6,
        'preparation': {'ready': 1}, 'items': [{'state': 'waiting_data'}]}
    first = store.research_guard({**context, 'forecast_batch': batch})
    ready = {**batch, 'preparation': {'ready': 2}, 'items': [{'state': 'ready'}]}
    assert store.research_guard({**context, 'forecast_batch': ready}) == first
    assert store.research_guard({**context, 'forecast_batch': {**batch, 'id': 'formal-2'}}) != first


@pytest.mark.asyncio
async def test_late_completed_report_is_explicitly_delivered_to_new_synthesis_session(configured, monkeypatch):
    store, account, job, context = configured
    runtime, child, mids = await runtime_child(configured, monkeypatch)
    original_report = deepcopy(child.reports[0])
    await runtime.close()
    assert store.checkpoint_report_deliveries(job['id']) == []
    saved = checkpoint(store, job['id'])
    assert saved['manager_summary'] == {}
    assert saved['recovery_state'] == 'synthesis_ready'
    store.set_job_state(job['id'], 'failed', 'controlled manager fault before receiving late child')
    store.enqueue(account['id'], 'manual')
    retry = store.claim_jobs(account_id=account['id'])[0]
    recovered = store.reusable_synthesis_checkpoint(retry, context)
    assert recovered
    calls = []
    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        receipts = store.checkpoint_report_deliveries(retry['id'])
        assert len(receipts) == 1 and receipts[0]['body']['access'] == 'full_report'
        assert receipts[0]['body']['channel'] == 'restored_synthesis'
        assert receipts[0]['body']['report_id'] == original_report['report_id']
        assert receipts[0]['body']['source_agent_id'] == original_report['agent_id']
        assert receipts[0]['body']['model_config'] == original_report['model_config']
        return output(('finish_analysis', {'report': '新汇总会话已收到此前晚到的子报告；保留来源范围。', 'material_ids': mids}))
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    resumed_context = {**context, 'job_id': retry['id'], 'synthesis_checkpoint': recovered}
    result = await agents.run_research(resumed_context, retry['config_snapshot'],
        ResearchTools(store, retry, retry['config_snapshot'], resumed_context), emit)
    assert result['synthesis_recovered'] and len(calls) == 1
    assert store.checkpoint_deliveries(retry['id']) == []
    assert store.checkpoint_report_deliveries(job['id']) == []
    assert checkpoint(store, job['id']) == saved


@pytest.mark.asyncio
async def test_report_and_receipt_ids_in_model_arguments_cannot_forge_coverage(configured, monkeypatch):
    store, _, job, _ = configured
    runtime, child, mids = await runtime_child(configured, monkeypatch, read=False)
    try:
        await runtime._save_checkpoint({'summary': '模型自填收据不代表阅读。', 'material_ids': mids,
            'material_delivery_ids': [123], 'report_delivery_ids': [123]})
        saved = checkpoint(store, job['id'])
        assert 'report_delivery_ids' not in saved['manager_summary']
        assert 'material_delivery_ids' not in saved['manager_summary']
        assert saved['recovery_state'] == 'needs_research'
        assert store.checkpoint_report_deliveries(job['id']) == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_records_only_visible_market_rows_and_calculation_inputs(configured, monkeypatch):
    store, _, job, context = configured
    def market(symbol, days):
        body = {'symbol': symbol, 'interval': 'daily', 'adjustment': 'none', 'status': 'ok',
            'source': 'controlled-market-fixture', 'fetched_at': '2026-01-11T08:00:00+08:00',
            'rows': [{'time': f'2026-01-{day:02}', 'close': str(100 + day)} for day in days]}
        return store.save_material({'scope': 'public', 'url': 'https://prices.example/' + symbol,
            'access': 'structured_data', 'content': json.dumps(body), 'classification': 'fact'})
    asset = market('SZ:159278', range(1, 11))
    benchmark = market('SH:000300', [1, 3, 4, 6, 7, 8, 9, 10])
    calls = []
    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            assert {'query_market_rows', 'market_indicators'} <= {tool.get('name') for tool in payload['tools']}
            return output(('query_market_rows', {'material_id': asset['id'], 'limit': 2, 'fields': ['close']}),
                ('market_indicators', {'material_id': asset['id'], 'benchmark_material_id': benchmark['id'],
                    'start': '2026-01-01', 'end': '2026-01-04', 'window': 3}))
        outputs = [json.loads(item['output']) for item in payload['input'] if item.get('type') == 'function_call_output']
        assert outputs[0]['row_count'] == 2 and outputs[0]['rows'][0]['time'] == '2026-01-10'
        assert outputs[1]['aligned_samples'] == 3
        return output(('submit_report', {'summary': '已读取最新两行及指定区间计算输入，基准按实际日期交集比较。',
            'material_ids': [asset['id'], benchmark['id']]}))
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, job['config_snapshot'], ResearchTools(store, job, job['config_snapshot'], context), emit)
    try:
        spawned = await runtime._spawn({'role_id': 'market_researcher', 'task': '读取指定行情区间并比较基准。'})
        await runtime.children[spawned['agent_id']].task_handle
        assert runtime.children[spawned['agent_id']].status == 'completed'
        receipts = store.checkpoint_deliveries(job['id'])
        assert len(receipts) == 3
        queried = next(item for item in receipts if item['tool'] == 'query_market_rows')
        assert queried['body']['delivered_range']['row_count'] == 2
        assert queried['body']['delivered_range']['returned_range'] == {'start': '2026-01-09', 'end': '2026-01-10'}
        computed = [item for item in receipts if item['tool'] == 'market_indicators']
        assert {item['body']['calculation_input_role']: item['body']['delivered_range']['row_count'] for item in computed} == {
            'asset': 4, 'benchmark_aligned': 3}
        assert all(item['body']['delivery_kind'] == 'calculation_inputs' for item in computed)
        assert all(item['body']['delivered_range']['fields'] == ['time', 'close'] for item in computed)
        assert all(item['body']['parent_visible_result_hash'] for item in computed)
        empty = await runtime.tools.query_market_rows(asset['id'], start='2026-01-31', end='2026-01-31')
        assert empty['rows'] == []
        assert store.record_material_delivery(job['id'], job['account_id'], spawned['agent_id'], 'query_market_rows', empty) == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['partial_range', 'unread_representation', 'unread_extraction_parent'])
async def test_exact_representation_receipts_do_not_require_entire_source(configured, monkeypatch, case):
    store, _, job, context = configured
    url = 'https://issuer.example/same-version-pages'
    token = store.begin_material_observation(url)
    pages = [store.save_material({'scope': 'public', 'url': url, 'document_id': 'v1',
        'text': f'actual content page {n}', 'access': 'full_text', 'truncated': False,
        'returned_range': {'start_page': n, 'end_page': n}}, observation_id=token) for n in (1, 2)]
    assert pages[0]['document_version_id'] == pages[1]['document_version_id']
    count, cited = 0, []
    async def handler(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return output(('read_material_range', {'material_id': pages[0]['id'], 'start': 2, 'length': 3}))
        if count == 2 and case == 'unread_extraction_parent':
            return output(('save_material', {'material': {'content': 'Model extraction cites an unread parent.',
                'material_ids': [pages[1]['id']]}}))
        if case == 'unread_extraction_parent':
            results = [json.loads(item['output']) for item in payload['input'] if item.get('type') == 'function_call_output']
            cited.append(results[-1]['id'])
        else:
            cited.append(pages[0 if case == 'partial_range' else 1]['id'])
        return output(('submit_report', {'summary': 'Visible finding with explicit limited scope.', 'material_ids': cited}))
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, job['config_snapshot'], ResearchTools(store, job, job['config_snapshot'], context), emit)
    try:
        spawned = await runtime._spawn({'role_id': 'filing_researcher', 'task': 'Read a bounded part and report its actual scope.'})
        await runtime.children[spawned['agent_id']].task_handle
        assert runtime.children[spawned['agent_id']].status == 'completed'
        await deliver_report(runtime)
        await runtime._save_checkpoint({'summary': 'Use only the delivered source range.', 'material_ids': cited})
        saved = checkpoint(store, job['id'])
        originals = [item for item in store.checkpoint_deliveries(job['id']) if item['tool'] == 'read_material_range']
        assert [item['material_id'] for item in originals] == [pages[0]['id']]
        assert originals[0]['body']['delivered_range']['start'] == 2
        assert originals[0]['body']['delivered_range']['end'] == 5
        assert originals[0]['body']['delivered_range']['truncated'] is True
        assert saved['recovery_state'] == ('synthesis_ready' if case == 'partial_range' else 'needs_research')
        if case != 'partial_range':
            assert any(item['status'] == 'needs_research' for item in saved['source_coverage'].values())
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_manager_can_combine_two_delivered_specialists_ranges(configured, monkeypatch):
    store, _, job, context = configured
    url = 'https://issuer.example/separate-specialists'
    token = store.begin_material_observation(url)
    pages = [store.save_material({'scope': 'public', 'url': url, 'document_id': 'v1',
        'text': f'visible page {n}', 'access': 'full_text', 'returned_range': {'page': n}}, observation_id=token) for n in (1, 2)]
    counts = {}
    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload['input'][0]['content'])
        page = int(seed['task'])
        counts[page] = counts.get(page, 0) + 1
        if counts[page] == 1:
            return output(('get_material', {'material_id': pages[page]['id']}))
        return output(('submit_report', {'summary': f'Specialist page {page}', 'material_ids': [pages[page]['id']]}))
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, job['config_snapshot'], ResearchTools(store, job, job['config_snapshot'], context), emit)
    try:
        for page in (0, 1):
            await runtime._spawn({'role_id': 'filing_researcher', 'task': str(page)})
        await asyncio.gather(*(child.task_handle for child in runtime.children.values()))
        await deliver_report(runtime)
        await runtime._save_checkpoint({'summary': 'Combine both delivered specialists.', 'material_ids': [page['id'] for page in pages]})
        saved = checkpoint(store, job['id'])
        assert saved['recovery_state'] == 'synthesis_ready'
        assert len(next(iter(saved['source_coverage'].values()))['report_delivery_ids']) == 2
        assert all(item['agent_id'] != 'manager' for item in store.checkpoint_deliveries(job['id']))
    finally:
        await runtime.close()


def test_recovery_ddl_keeps_existing_migration_transaction():
    import sqlite3
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE marker(value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO marker VALUES('must roll back')")
        Store.__new__(Store).migrate_recovery_deliveries(db)
        assert db.in_transaction
        db.rollback()
        assert db.execute('SELECT count(*) FROM marker').fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='research_report_deliveries'").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('real_correction', [False, True])
async def test_model_self_supersession_does_not_clear_unread_current_source(configured, monkeypatch, real_correction):
    from app.model import ModelError
    store, _, job, context = configured
    url = 'https://issuer.example/self-supersession'
    old = None
    if real_correction:
        old = store.save_material({'scope': 'public', 'url': url, 'document_id': 'v0',
            'text': 'actual old content', 'access': 'full_text'}, observation_id=store.begin_material_observation(url))
    token = store.begin_material_observation(url)
    pages = [store.save_material({'scope': 'public', 'url': url, 'document_id': 'v1',
        'text': f'actual content page {n}', 'access': 'full_text', 'truncated': False,
        'returned_range': {'start_page': n, 'end_page': n}}, observation_id=token) for n in (1, 2)]
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return output(*[('get_material', {'material_id': material['id']}) for material in ([old] if old else []) + [pages[0]]])
        if count == 2:
            return output(('prepare_synthesis', {'summary': 'Self supersession cannot establish a read.',
                'material_ids': [p['id'] for p in ([old] if old else []) + pages], 'material_updates': [{
                    'latest_material_id': pages[0]['id'], 'prior_material_ids': [old['id']] if old else [],
                    'status': 'handled', 'summary': 'A model claim cannot remove the missing page.',
                    'supersedes_agent_ids': ['manager']}]}))
        raise httpx.ReadTimeout('controlled synthesis stop', request=request)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, job['config_snapshot'], ResearchTools(store, job, job['config_snapshot'], context), emit)
    with pytest.raises(ModelError):
        await runtime.run()
    saved = checkpoint(store, job['id'])
    assert saved['recovery_state'] == 'needs_research'
    assert 'manager' in next(iter(saved['source_coverage'].values()))['unresolved_report_agents']


@pytest.mark.asyncio
async def test_model_authored_material_echo_is_not_a_report_delivery(configured, monkeypatch):
    store, _, job, _ = configured
    runtime, child, mids = await runtime_child(configured, monkeypatch, long_summary=True)
    try:
        # Even an exact copy of a genuine runtime report, embedded by the model
        # in an authored material, is not a report-channel delivery receipt.
        echoed = await runtime._dispatch('manager', 'save_material', {'material': {
            'content': 'Model-authored echo, not an agent-status result.', 'reports': deepcopy(child.reports)}})
        results = [(ToolCall('echo-report', 'save_material', {}), echoed)]
        runtime.client.add_results(runtime.manager_session, results)
        runtime._remember_report_results(results, runtime.manager_session.history[-1:])
        await runtime._use_budget()
        await runtime._save_checkpoint({'summary': 'Still requires actual report delivery.', 'material_ids': mids})
        assert store.checkpoint_report_deliveries(job['id']) == []
        assert checkpoint(store, job['id'])['recovery_state'] == 'needs_research'
        await deliver_report(runtime)
        await runtime._save_checkpoint({'summary': 'Now the genuine report was delivered.', 'material_ids': mids})
        assert any(item['body']['access'] == 'full_report' for item in store.checkpoint_report_deliveries(job['id']))
        # The authored extraction itself has no original read proof and remains
        # a separate gap; receipt eligibility does not blanket-approve material.
    finally:
        await runtime.close()
