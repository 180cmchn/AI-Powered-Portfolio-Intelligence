from tests.delivery_helpers import delivery_arguments
"""Production runtime/store/client regressions; HTTP is controlled, never paid."""
import asyncio
import json
import time
from copy import deepcopy

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient, ModelError
from app.service import ResearchTools, Service
from tests.test_fourth_recovery import store, install, response, call, checkpoint_fixture
from tests.test_eleventh_timeouts import config, event, terminal


@pytest.mark.parametrize('fail_once', [False, True])
async def test_explicit_retry_continues_partial_work_and_delivers_saved_report(store, monkeypatch, fail_once):
    account = store.save_account(AccountInput(name='partial retry', holdings=[], research_scope='cash_only'))
    material = store.save_material({'url': 'https://issuer.example/partial', 'text': 'A saved source body', 'scope': 'public'})
    state = {'spawned': False, 'child_done': False, 'recovery_calls': 0}
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        seed = json.loads(payload['input'][0]['content'])
        saved = seed.get('synthesis_checkpoint')
        if saved:
            state['recovery_calls'] += 1
            assert saved['recovery_mode'] == 'continue_research'
            assert saved['recovery_state'] == 'needs_research'
            assert 'Completed saved child report' in json.dumps(payload)
            assert 'spawn_research' in {t.get('name') for t in payload['tools']}
            assert saved['unresolved_source_coverage']  # A referenced URL is not a source read.
            if state['recovery_calls'] == 1:
                return response(call('get_material', {'material_id': material['id']}))
            if fail_once and state['recovery_calls'] == 2:
                raise httpx.RemoteProtocolError('peer closed connection (incomplete chunked read)', request=request)
            return response(call('finish_analysis', delivery_arguments(**{'report': '有限分析：复用子研究，并补读已保存资料；其他范围未知。',
                'material_ids': [material['id']]})))
        if 'task' in seed:
            state['child_done'] = True
            return response(call('submit_report', {'summary': 'Completed saved child report',
                'material_ids': [material['id']], 'open_questions': ['Source must still be read']}))
        if not state['spawned']:
            state['spawned'] = True
            return response(call('spawn_research', {'role_id': 'filing_researcher', 'task': 'Inspect existing source index'}))
        if not state['child_done']:
            return response(call('wait_agents', {'timeout_seconds': .05}))
        raise httpx.RemoteProtocolError('peer closed connection (incomplete chunked read)', request=request)

    install(monkeypatch, handler)
    service = Service(store)
    first = store.claim_jobs(account_id=account['id'])[0]
    await service.execute(first)
    assert store.job(first['id'])['state'] == 'failed'
    assert store.checkpoint_status(first['id'])['recovery_state'] == 'needs_research'
    assert not store.analyses(account['id'])
    before_requests = len(requests)
    store.submit_task(account['id'], research_scope='cash_only', parent_job_id=first['id'])
    retry = store.claim_jobs(account_id=account['id'])[0]
    await service.execute(retry)
    intermediate = None
    if fail_once:
        intermediate = retry
        assert store.job(retry['id'])['state'] == 'failed'
        with store.connect() as db:
            saved = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (retry['id'],)).fetchone()[0])
        assert saved['reports'][0]['checkpoint_source_job_id'] == first['id']
        assert saved['material_deliveries'] and saved['partial_research']
        assert not saved.get('pending_calls')
        store.submit_task(account['id'], research_scope='cash_only', parent_job_id=retry['id'])
        retry = store.claim_jobs(account_id=account['id'])[0]
        await service.execute(retry)
    assert store.job(retry['id'])['state'] == 'completed', store.job(retry['id']).get('error')
    assert store.job(first['id'])['state'] == 'failed'
    assert len(requests) - before_requests == (3 if fail_once else 2)
    result = store.analyses(account['id'])[0]['result']
    assert result['recovery_mode'] == 'continue_research'
    assert result['research_recovered'] and not result['synthesis_recovered']
    receipts = store.checkpoint_report_deliveries(retry['id'])
    assert receipts and receipts[0]['source_job_id'] == first['id']
    assert receipts[0]['body']['access'] == 'full_report'
    assert receipts[0]['body']['channel'] == 'restored_research'
    assert receipts[0]['body']['source_material_deliveries'] == []  # No fabricated original read.
    events = store.job(retry['id'], events=True)['events']
    if intermediate:
        events += store.job(intermediate['id'], events=True)['events']
    assert not any(e.get('type') == 'agent_created' for e in events)
    assert any(e.get('type') == 'research_resumed' for e in events)
    visible = [e['payload'] for e in events if e.get('type') == 'research_context_delivery']
    assert visible and all(any(r['source_task_id'] == first['id'] and r['access'] == 'full_report'
        for r in item['reports']) for item in visible)
    assert any(e.get('payload', {}).get('tool') == 'get_material' for e in events)
    assert not any(e.get('payload', {}).get('tool') == 'read' for e in events)


def test_partial_continuation_requires_explicit_parent_and_preserves_guards(store):
    account, source_job, context, material = checkpoint_fixture(store)
    with store.connect() as db:
        row = db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (source_job['id'],)).fetchone()
        body = json.loads(row['body'])
        body['manager_summary'] = {'material_ids': [material['id']]}
    store.save_synthesis_checkpoint(source_job, body, context)
    assert store.checkpoint_status(source_job['id'])['recovery_state'] == 'needs_research'
    assert store.reusable_synthesis_checkpoint(source_job, context, continue_research=True) is None
    retry = {**source_job, 'parent_job_id': source_job['id']}
    assert store.reusable_synthesis_checkpoint(retry, context) is None
    changed = deepcopy(context)
    changed['candidate_quotes']['SH:600519']['price'] = '99'
    assert store.reusable_synthesis_checkpoint(retry, changed, continue_research=True) is None
    before = store.checkpoint_status(source_job['id'])['resume_count']
    diagnostic = store.checkpoint_recovery_diagnostic(source_job, context)
    assert diagnostic['continue_eligible'] and not diagnostic['eligible']
    assert store.checkpoint_status(source_job['id'])['resume_count'] == before
    restored = store.reusable_synthesis_checkpoint(retry, context, continue_research=True)
    assert restored['recovery_mode'] == 'continue_research'
    assert restored['reports'] == body['reports']


async def test_closeout_waits_within_budget_and_includes_completed_report(monkeypatch):
    events, waits = [], []
    async def emit(value): events.append(value)
    runtime = agents.ResearchRuntime({'job_id': 'bounded-closeout'}, config(
        analysis_timeout_seconds=120, synthesis_request_hard_timeout_seconds=36,
        agent_timeout_seconds=90), object(), emit)
    child_session = runtime.client.session('child', {})
    child_session.deadlines = {'child_budget_exhausted': time.monotonic() + 70}
    child = agents.AgentState('child', 'child', 'pending work', child_session, status='running')
    runtime.children[child.id] = child
    async def finish():
        await asyncio.sleep(0)
        child.reports.append({'report_id': 'completed-in-closeout', 'summary': 'Paid work completed'})
        child.status = 'completed'
    child.task_handle = asyncio.create_task(finish())
    actual_wait = asyncio.wait
    async def observe_wait(tasks, *, timeout):
        waits.append(timeout)
        return await actual_wait(tasks, timeout=.1)
    monkeypatch.setattr(agents.asyncio, 'wait', observe_wait)
    runtime.deadline = time.monotonic() + 100
    try:
        await runtime._wait_for_closeout()
        assert 50 < waits[0] <= 52  # 100 remaining minus 48 for submission/correction; not fixed 30.
        assert runtime.budget_closing
        await runtime._enter_synthesis_context(runtime.manager_session, reason='bounded_final_request')
        seed = json.loads(runtime.manager_session.history[0]['content'])
        assert seed['synthesis_checkpoint']['reports'][0]['report_id'] == 'completed-in-closeout'
        assert events[0]['payload']['active_child_ids'] == ['child']
        assert events[1]['payload']['completed_report_ids'] == ['completed-in-closeout']
        assert not events[1]['payload']['unfinished_child_ids']
    finally:
        await runtime.close()


async def test_closeout_deadline_does_not_wait_for_unfinished_work(monkeypatch):
    async def emit(value): pass
    runtime = agents.ResearchRuntime({'job_id': 'no-time'}, config(), object(), emit)
    session = runtime.client.session('child', {})
    task = asyncio.create_task(asyncio.Event().wait())
    child = agents.AgentState('child', 'child', 'unfinished', session, status='running', task_handle=task)
    runtime.children['child'] = child
    runtime.handles.add(task)
    runtime.deadline = time.monotonic() - .01
    try:
        await runtime._wait_for_closeout()
        assert not task.done()
        assert runtime.events[-1]['payload']['unfinished_child_ids'] == ['child']
        assert not runtime.events[-1]['payload']['completed_report_ids']
    finally:
        await runtime.close()


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield event('response.output_text.delta', delta='partial visible text')
        yield event('response.function_call_arguments.delta', delta='{"report":')
        try:
            raise OSError('transport cause with private proxy address')
        except OSError as cause:
            raise httpx.RemoteProtocolError('peer closed connection (incomplete chunked read)') from cause


async def test_disconnect_logs_protocol_cause_without_replaying_partial_call(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://private-user:private-secret@127.0.0.1:12345')
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-request-id': 'safe-id'},
            extensions={'http_version': b'HTTP/1.1'}, stream=BrokenStream())
    client = ModelClient(config(), transport=httpx.MockTransport(handler))
    try:
        session = client.session('system', {})
        with pytest.raises(ModelError):
            await client.complete(session, [])
        assert len(client.requests) == 1
        record = client.requests[0]
        assert record['http_version'] == 'HTTP/1.1' and record['request_id'] == 'safe-id'
        assert record['exception_types'] == ['RemoteProtocolError', 'OSError']
        assert record['transport_failure_kind'] == 'incomplete_response_body'
        assert record['environment_proxy_present']
        assert record['retry_suppressed'] == 'accepted_or_sent_request_outcome_unknown'
        assert not session.pending_calls and len(session.history) == 1
        assert not record['response_body_received'] and not record['cost_known']
        assert 'private-secret' not in json.dumps(record) and 'private proxy' not in json.dumps(record)
    finally:
        await client.aclose()
