"""Seventh iteration: source-stream recovery and bounded transport diagnostics."""
from copy import deepcopy
import json

import httpx
import pytest

from app.domain import AccountInput
from app.model import ModelClient, ModelError
from app.store import Store


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path / 'seventh-recovery.sqlite3')
    store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0})
    account = store.save_account(AccountInput(name='Recovery', holdings=[], research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    context = {'account': account, 'universe_version': store.universe_snapshot()['universe_version'],
               'metrics': {'positions': []}}
    return store, account, job, context


def source(store, version, url='https://issuer.example/report', **kwargs):
    token = kwargs.pop('token', None) or store.begin_material_observation(url)
    return store.save_material({'scope': 'public', 'url': url, 'document_id': version,
        'text': 'visible ' + version, 'access': 'full_text', 'truncated': False, **kwargs}, observation_id=token)


def deliver(case, material, actor='child', **kwargs):
    store, account, job, _ = case
    return store.record_material_delivery(job['id'], account['id'], actor, 'get_material',
        {**store.material_range(material['id']), **kwargs})


def report(materials, receipts, actor='child', handled=False):
    result = {'agent_id': actor, 'summary': 'Visible findings',
        'material_ids': [m['id'] for m in materials], 'material_delivery_ids': receipts,
        'model_config': {'model_name': 'original-research-model'}}
    if handled:
        result['material_updates'] = [{'latest_material_id': materials[-1]['id'],
            'prior_material_ids': [m['id'] for m in materials[:-1]], 'status': 'handled',
            'summary': 'Compared original and corrected text; findings use the correction.'}]
    return result


def save(case, reports, manager=None):
    store, _, job, context = case
    body = {'reports': reports, 'material_ids': list(dict.fromkeys(
        [mid for r in reports for mid in r.get('material_ids', [])] + (manager or {}).get('material_ids', []))),
        'manager_summary': manager or {}}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job['id'], 'failed', 'controlled synthesis transport failure')
    return store.checkpoint_status(job['id'])


def test_only_old_rejected_only_latest_allowed_and_comparison_allowed(case):
    store, _, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    old_reads, new_reads = deliver(case, old), deliver(case, new)
    save(case, [report([old], old_reads)])
    assert store.reusable_synthesis_checkpoint(job, context) is None
    save(case, [report([new], new_reads)])
    assert store.reusable_synthesis_checkpoint(job, context)
    save(case, [report([old, new], old_reads + new_reads, handled=True)])
    resumed = store.reusable_synthesis_checkpoint(job, context)
    assert resumed and resumed['recovery_state'] == 'synthesis_ready'
    assert resumed['reports'][0]['model_config']['model_name'] == 'original-research-model'
    assert {item['read_document_version'] for item in resumed['verified_material_versions'].values()} == {1, 2}


@pytest.mark.parametrize('proof', ['id_only', 'index', 'context_excerpt', 'wrong_actor', 'read_after_report'])
def test_new_version_not_actually_delivered_to_report_actor_is_not_coverage(case, proof):
    store, account, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    receipts = deliver(case, old)
    if proof == 'index':
        assert store.record_material_delivery(job['id'], account['id'], 'child', 'list_materials', new) == []
    elif proof == 'context_excerpt':
        assert deliver(case, new, truncated_for_context=True, context_excerpt='partial JSON') == []
    elif proof == 'wrong_actor':
        receipts += deliver(case, new, actor='other-child')
    elif proof == 'read_after_report':
        deliver(case, new)  # Available in the task, absent from the report's receipt snapshot.
    save(case, [report([old, new], receipts, handled=True)])
    assert store.reusable_synthesis_checkpoint(job, context) is None
    assert store.checkpoint_status(job['id'])['recovery_state'] == 'needs_research'


def test_latest_unretrieved_id_alone_is_not_a_read(case):
    store, _, job, context = case
    new = source(store, 'v2')
    save(case, [report([new], [])])
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_old_child_report_requires_visible_update_or_manager_supersession(case):
    store, _, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    old_report = report([old], deliver(case, old))
    # The manager compares the actually delivered old specialist result with
    # its direct correction read; naming an unread historical ID is insufficient.
    old_report.update(report_id='fixture-old-specialist', role_id='filing_researcher', research_round=1)
    store.record_report_delivery(job['id'], job['account_id'], old_report, old_report, channel='unit-visible-report')
    reads = deliver(case, new, 'manager')
    manager = report([new], reads, 'manager')
    save(case, [old_report], manager)
    assert store.reusable_synthesis_checkpoint(job, context) is None
    manager.update(material_ids=[old['id'], new['id']], material_updates=[{
        'latest_material_id': new['id'], 'prior_material_ids': [old['id']], 'status': 'handled',
        'summary': 'The previous child claim is superseded by the retrieved correction.',
        'supersedes_agent_ids': ['child']}])
    save(case, [old_report], manager)
    assert store.reusable_synthesis_checkpoint(job, context)


def test_latest_child_round_can_process_history_and_preserves_prior_round(case):
    store, _, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    r1 = report([old], deliver(case, old))
    r2 = report([old, new], r1['material_delivery_ids'] + deliver(case, new), handled=True)
    save(case, [r1, r2])
    resumed = store.reusable_synthesis_checkpoint(job, context)
    assert len(resumed['reports']) == 2


@pytest.mark.parametrize('mutation', ['v3', 'rollback', 'uncovered_source', 'parent_chain', 'other_account'])
def test_stream_and_scope_changes_do_not_bypass_guards(case, mutation):
    store, _, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    reports = [report([old, new], deliver(case, old) + deliver(case, new), handled=True)]
    if mutation == 'uncovered_source':
        stale = source(store, 's1', 'https://other.example/source')
        source(store, 's2', 'https://other.example/source')
        reports.append(report([stale], deliver(case, stale, 'other-child'), 'other-child'))
    save(case, reports)
    if mutation == 'v3':
        source(store, 'v3')
    elif mutation == 'rollback':
        assert source(store, 'v1')['document_version'] == 3
    elif mutation == 'parent_chain':
        # Simulate corruption/legacy mutable parent metadata; guard still catches it.
        with store.connect() as db:
            body = json.loads(db.execute('SELECT body FROM materials WHERE id=?', (old['id'],)).fetchone()[0])
            body['source_material_ids'] = ['unexpected-parent']
            db.execute('UPDATE materials SET body=? WHERE id=?', (json.dumps(body), old['id']))
    elif mutation == 'other_account':
        other = store.save_account(AccountInput(name='Other', holdings=[]))
        job = store.claim_jobs(account_id=other['id'])[0]
        context = {**context, 'account': other}
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_same_version_fragments_preserve_actual_range_and_config_relation(case):
    store, _, job, context = case
    token = store.begin_material_observation('https://issuer.example/report')
    first = source(store, 'v1', token=token, text='first page', returned_range={'start_page': 1})
    second = source(store, 'v1', token=token, text='second page', returned_range={'start_page': 2})
    receipts = deliver(case, first, start=2, end=5, text='rst') + deliver(case, second)
    save(case, [report([first, second], receipts)])
    resumed = store.reusable_synthesis_checkpoint(job, {**context, 'model_config': {'model_name': 'new-main-model'}})
    assert resumed['configuration_relation']['changed']
    assert resumed['reports'][0]['model_config']['model_name'] == 'original-research-model'
    assert resumed['material_deliveries'][0]['body']['delivered_range']['start'] == 2
    assert store.reusable_synthesis_checkpoint(job, context)['resume_attempt'] == 2
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_pending_visible_correction_is_research_required(case):
    store, _, job, context = case
    old, new = source(store, 'v1'), source(store, 'v2')
    item = report([old, new], deliver(case, old) + deliver(case, new), handled=True)
    item['material_updates'][0]['status'] = 'needs_review'
    save(case, [item])
    assert store.reusable_synthesis_checkpoint(job, context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['headers', 'body', 'parse'])
async def test_transport_diagnostics_keep_phase_and_safe_request_id(phase):
    async def handler(request):
        trace = request.extensions['trace']
        await trace('connection.connect_tcp.complete', {'sensitive': 'never recorded'})
        await trace('http11.receive_response_headers.started', {})
        if phase != 'headers':
            await trace('http11.receive_response_headers.complete', {'return_value': (b'HTTP/1.1', 200, b'OK',
                [(b'x-request-id', b'provider-id'), (b'authorization', b'secret-that-must-not-be-logged')])})
            await trace('http11.receive_response_body.started', {})
        if phase == 'parse':
            return httpx.Response(200, text='not json', headers={'x-request-id': 'provider-id'})
        raise httpx.RemoteProtocolError('Server disconnected without sending a response.' if phase == 'headers'
                                       else 'peer closed connection without sending complete message body', request=request)
    cfg = {'model_name': 'fixture', 'model_base_url': 'http://localhost/v1', 'model_allow_no_key': True,
        'model_retries': 0, 'model_native_search': False}
    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    session = client.session('fixture', {})
    session.phase = 'synthesis'
    with pytest.raises(ModelError):
        await client.complete(session, [])
    observation = client.requests[0]
    assert observation['stage'] == 'synthesis'
    assert observation['transport_phase'] == {'headers': 'receiving_headers', 'body': 'receiving_body',
                                               'parse': 'local_response_parsing'}[phase]
    assert observation['response_headers_received'] == (phase != 'headers')
    assert observation['request_id'] == (None if phase == 'headers' else 'provider-id')
    assert 'secret-that-must-not-be-logged' not in json.dumps(observation)
    await client.aclose()


@pytest.mark.asyncio
async def test_production_runtime_comparison_failure_resumes_only_synthesis(case, monkeypatch):
    from app import agents
    from app.service import Service
    from tests.helpers_sixth import configure_test_team
    store, account, original, _ = case
    monkeypatch.setenv('SEVENTH_RECOVERY_KEY', 'controlled-not-real')
    cfg = {'model_name': 'recovery-fixture', 'model_base_url': 'https://model.example/v1',
        'model_key_env': 'SEVENTH_RECOVERY_KEY', 'model_protocol': 'responses', 'model_native_search': False,
        'quote_provider': 'manual', 'debounce_seconds': 0, 'model_retries': 0,
        'model_request_timeout_seconds': 2, 'synthesis_request_timeout_seconds': 2,
        'agent_timeout_seconds': 5, 'analysis_timeout_seconds': 8}
    store.set_meta('settings', cfg)
    configure_test_team(store, cfg)
    # Claim a fresh configured job, leaving the already claimed setup task terminal.
    store.set_job_state(original['id'], 'failed', 'fixture setup')
    store.submit_task(account['id'], research_scope='cash_only')
    job = store.claim_jobs(account_id=account['id'])[0]
    old, new = source(store, 'v1'), source(store, 'v2')
    stage = {'requests': 0, 'resumed': 0}
    research_contexts = []

    def persisted_checkpoint():
        with store.connect() as db:
            return json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?',
                                         (job['id'],)).fetchone()[0])

    def output(*calls):
        return httpx.Response(200, json={'id': 'controlled-response', 'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': str(index), 'name': name, 'arguments': json.dumps(args)}
            for index, (name, args) in enumerate(calls)]})

    async def handler(request):
        payload = json.loads(request.content)
        initial = json.loads(payload['input'][0]['content'])
        checkpoint = initial.get('synthesis_checkpoint')
        if checkpoint and checkpoint.get('checkpoint_job_id'):
            stage['resumed'] += 1
            persisted = persisted_checkpoint()
            # Audit proofs remain durable, while synthesis sees the actual
            # comparison and its provenance without replaying receipt bodies.
            assert not {'source_coverage', 'verified_material_versions', 'material_deliveries',
                        'material_guard_version', 'covered_event_scope',
                        'latest_event_scope_at_save'} & checkpoint.keys()
            assert checkpoint['unresolved_source_coverage'] == []
            assert checkpoint['manager_summary'] == persisted['manager_summary']
            assert checkpoint['model_config'] == persisted['model_config']
            assert set(checkpoint['material_ids']) == {old['id'], new['id']}
            assert checkpoint['recovery_state'] == 'synthesis_ready'
            assert all(item.get('name') not in {'read', 'spawn_research'} for item in payload['tools'])
            return output(('finish_analysis', {'report': 'Recovered visible comparison',
                'material_ids': [old['id'], new['id']]}))
        stage['requests'] += 1
        if stage['requests'] == 1:
            research_contexts.append(initial)
            return output(('get_material', {'material_id': old['id']}), ('get_material', {'material_id': new['id']}))
        if stage['requests'] == 2:
            return output(('prepare_synthesis', {'summary': 'Compared historical and corrected source text.',
                'material_ids': [old['id'], new['id']], 'material_updates': [{
                    'latest_material_id': new['id'], 'prior_material_ids': [old['id']], 'status': 'handled',
                    'summary': 'The corrected text supersedes the old statement; old text retained for comparison.'}]}))
        raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    service = Service(store)
    await service.execute(job)
    assert store.job(job['id'])['state'] == 'failed'
    assert store.checkpoint_status(job['id'])['recovery_state'] == 'synthesis_ready'
    persisted = persisted_checkpoint()
    assert persisted['material_guard_version'] == 3
    assert len(persisted['source_coverage']) == 1
    coverage = next(iter(persisted['source_coverage'].values()))
    assert coverage['status'] == 'covered' and coverage['handled_by'] == ['manager']
    assert coverage['historical_material_ids'] == [old['id']]
    assert coverage['latest_material_ids'] == [new['id']]
    assert coverage['delivery_ids'] and not coverage['unresolved_report_agents']
    verified = persisted['verified_material_versions']
    assert set(verified) == {old['id'], new['id']}
    assert {entry['read_document_version'] for entry in verified.values()} == {1, 2}
    assert store.checkpoint_material_guard(list(verified), account['id']) == verified
    assert {entry['body']['read_document_version'] for entry in persisted['material_deliveries']} == {1, 2}
    store.submit_task(account['id'], research_scope='cash_only', parent_job_id=job['id'])
    retry = store.claim_jobs(account_id=account['id'])[0]
    await service.execute(retry)
    assert store.job(retry['id'])['state'] == 'completed'
    assert stage == {'requests': 3, 'resumed': 1}
    report = store.analyses(account['id'])[0]['result']
    assert report['synthesis_recovered']
    assert persisted_checkpoint() == persisted
    source(store, 'v3')
    assert store.reusable_synthesis_checkpoint(retry, research_contexts[0]) is None


def test_visible_authored_extraction_keeps_source_read_proof(case):
    store, account, job, context = case
    current = source(store, 'v1')
    source_receipts = deliver(case, current)
    authored = {'content': 'Visible extracted finding', 'material_ids': [current['id']]}
    extracted = store.save_material({**authored, 'scope': 'account', 'representation': 'agent_extraction'},
                                   job['id'], account['id'])
    receipts = store.record_material_delivery(job['id'], account['id'], 'child', 'save_material',
                                              {'id': extracted['id']}, authored=authored)
    assert receipts
    save(case, [report([extracted], source_receipts + receipts)])
    assert store.reusable_synthesis_checkpoint(job, context)
    source(store, 'v2')
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_saving_existing_original_id_does_not_forge_original_read(case):
    store, account, job, context = case
    current = source(store, 'v1')
    assert store.record_material_delivery(job['id'], account['id'], 'child', 'save_material',
        {'id': current['id']}, authored={'id': current['id'], 'content': current['text']}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('origin_status', [None, 200])
async def test_proxy_connect_success_is_not_origin_response_evidence(origin_status):
    from types import SimpleNamespace
    async def handler(request):
        trace = request.extensions['trace']
        await trace('connection.connect_tcp.complete', {})
        await trace('http11.send_request_headers.started', {'request': SimpleNamespace(method=b'CONNECT')})
        await trace('http11.receive_response_headers.complete', {'return_value': (
            b'HTTP/1.1', 200, b'Connection established', [(b'x-request-id', b'proxy-only-id')])})
        await trace('http11.receive_response_body.complete', {})
        await trace('proxy.start_tls.started', {})
        await trace('proxy.start_tls.complete', {})
        await trace('http11.send_request_headers.started', {'request': SimpleNamespace(method=b'POST')})
        await trace('http11.receive_response_headers.started', {})
        if origin_status is None:
            await trace('http11.receive_response_headers.failed', {})
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)
        await trace('http11.receive_response_headers.complete', {'return_value': (
            b'HTTP/1.1', origin_status, b'OK', [(b'x-request-id', b'origin-only-id')])})
        return httpx.Response(origin_status, json={'id': 'controlled', 'status': 'completed', 'output': []},
                              headers={'x-request-id': 'origin-only-id'})
    cfg = {'model_name': 'fixture', 'model_base_url': 'http://localhost/v1', 'model_allow_no_key': True,
           'model_retries': 0, 'model_native_search': False}
    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    session = client.session('fixture', {})
    if origin_status is None:
        with pytest.raises(ModelError):
            await client._post('responses', {'model': 'fixture'}, session)
    else:
        await client._post('responses', {'model': 'fixture'}, session)
    observation = client.requests[0]
    assert observation['proxy_tunnel'] == {'observed': True, 'status_code': 200,
        'response_headers_received': True, 'established': True}
    assert observation['status_code'] == origin_status
    assert observation['request_accepted'] == (True if origin_status else None)
    assert observation['response_headers_received'] is bool(origin_status)
    assert observation['response_body_received'] is bool(origin_status)
    assert observation['request_id'] == ('origin-only-id' if origin_status else None)
    assert 'proxy-only-id' not in json.dumps(observation)
    assert any(event['scope'] == 'proxy_tunnel' for event in observation['transport_events'])
    assert observation['transport_phase'] == ('response_received' if origin_status else 'receiving_headers')
    await client.aclose()
