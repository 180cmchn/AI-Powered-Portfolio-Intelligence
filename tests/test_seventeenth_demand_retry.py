"""Normal HTTP retry of legacy uncovered work; synthetic Store and model only.

The old report is an explicit pre-scope historical publication fixture: it uses
the production Store contract before any demand page is frozen. No SQL changes
to report, demand or job states simulate a successful modern Runtime submission.
All subsequent execution uses the production Service/Runtime/HTTP lifecycle.
"""
import json

import httpx
import pytest

from app import agents
from app.domain import now
from app.main import create_app
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments, final_result
from tests.helpers_sixth import configure_test_team
from tests.test_ninth_calculation_context import output


def stored_report(store, job_id):
    with store.connect() as db:
        row = db.execute('SELECT report,result FROM analyses WHERE job_id=?', (job_id,)).fetchone()
        return tuple(row)


def accounting_rows(store, account_id):
    """Compare actual persisted accounting/operation rows, not response labels."""
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(
            f'SELECT * FROM {table} WHERE account_id=? ORDER BY rowid', (account_id,))]
            for table in ('ledger_positions', 'ledger_entries', 'cash_entries', 'ledger_operations', 'fee_plans')}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('declare_coverage', [False, True], ids=['explicit-empty', 'explicit-id'])
async def test_legacy_uncovered_ledger_demand_requires_current_version_explicit_retry(
        tmp_path, monkeypatch, frozen_clock, protocol, declare_coverage):
    app = create_app(tmp_path / 'demand-retry.sqlite3', mode='live', worker=False)
    store = app.state.store
    cfg = {**store.settings(), 'quote_provider': 'manual', 'quote_fallback_provider': 'manual',
        'debounce_seconds': 0, 'scheduled_research_enabled': False,
        'market_refresh_enabled': False, 'forecast_evaluation_enabled': False,
        'model_name': 'demand-retry-fixture', 'model_base_url': 'https://fixture.invalid/v1',
        'model_protocol': protocol, 'model_native_search': False,
        'model_responses_stream': False, 'model_retries': 0,
        'analysis_timeout_seconds': 60, 'agent_timeout_seconds': 30,
        'model_request_hard_timeout_seconds': 3, 'synthesis_request_hard_timeout_seconds': 3,
        'max_agent_turns': 8, 'max_model_calls': 12, 'synthesis_max_calls': 2}
    store.set_meta('settings', cfg)
    configure_test_team(store, cfg)
    store.put_cache('securities', 'SH:518880', {'symbol': 'SH:518880',
        'name': 'Controlled gold ETF', 'kind': 'etf', 'status': 'ok',
        'identity_status': 'confirmed', 'fetched_at': now(), 'source': 'offline fixture'})
    store.put_cache('quotes', 'SH:518880', {'symbol': 'SH:518880', 'price': '10',
        'status': 'closed', 'fresh_for_triggers': False, 'as_of': now(),
        'fetched_at': now(), 'source': 'offline fixture'})

    payloads = []
    retry_job_id = None
    old_demand_id = None

    def handler(request):
        payload = json.loads(request.content)
        messages = payload.get('input', payload.get('messages'))
        initial = json.loads(next(m['content'] for m in messages if m.get('role') == 'user'))
        payloads.append((initial['job_id'], payload))
        if initial['job_id'] != retry_job_id:
            # A new account version does not silently claim the old uncovered
            # ledger demand. A report can explicitly leave work uncovered.
            assert old_demand_id not in {d['id'] for d in initial['business_demands']}
            return output(protocol, [('finish_analysis', delivery_arguments(
                report='Controlled current-version report; prior demand not covered.',
                covered_demand_ids=[]))])
        assert initial['input_version'] == current['version']
        assert old_demand_id in {d['id'] for d in initial['business_demands']}
        terminal = next(t.get('function', t) for t in payload['tools']
                        if t.get('name', t.get('function', {}).get('name')) == 'finish_analysis')
        assert 'covered_demand_ids' in terminal['parameters']['required']
        own = [p for task, p in payloads if task == retry_job_id]
        if len(own) == 1:
            return output(protocol, [('list_business_demands', {'limit': 50, 'offset': 0})])
        returned = [json.loads(m.get('output', m.get('content', '{}'))) for m in messages
                    if m.get('type') == 'function_call_output' or m.get('role') == 'tool']
        assert any(old_demand_id in {d['id'] for d in value.get('items', [])}
                   for value in returned if isinstance(value, dict))
        assert store.demands(account_id=account['id'])[0]['state'] == 'assigned'
        return output(protocol, [('finish_analysis', delivery_arguments(
            report=('Controlled retry explicitly addresses the committed simulated trade.'
                    if declare_coverage else 'Controlled retry leaves the ledger demand unanswered.'),
            covered_demand_ids=[old_demand_id] if declare_coverage else []))])

    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url='http://testserver') as api:
        response = await api.post('/api/accounts', json={'name': 'Controlled retry account',
            'cash': '10000', 'holdings': [], 'research_scope': 'cash_only'})
        assert response.status_code == 201, response.text
        account = response.json()
        bought = await api.post(f"/api/accounts/{account['id']}/trades", json={
            'operation_id': 'historical-ledger-buy', 'expected_version': account['version'],
            'fee_plan_version': 1, 'symbol': 'SH:518880', 'side': 'buy', 'quantity': '100',
            'price': '10', 'traded_at': '2026-10-30T10:00:00+08:00', 'mode': 'simulated',
            'note': 'Synthetic historical demand fixture, not a real trade'})
        assert bought.status_code == 201, bought.text
        original = store.claim_jobs(account_id=account['id'])[0]
        assigned = store.demands(job_id=original['id'])
        assert len(assigned) == 1 and assigned[0]['state'] == 'assigned'
        old_demand_id = assigned[0]['id']
        # Historical fixture only: no demand page has yet been frozen. This is
        # the retained legacy Store contract, not a bypass of modern validation.
        with store.connect() as db:
            assert not db.execute('SELECT 1 FROM research_demand_scopes WHERE job_id=?',
                                  (original['id'],)).fetchone()
        legacy = final_result({'report': 'Historical report with no coverage declaration.'})
        assert 'covered_demand_ids' not in legacy
        assert store.publish(original['id'], legacy, None)
        old_report = stored_report(store, original['id'])
        assert 'covered_demand_ids' not in json.loads(old_report[1])
        assert store.demands(job_id=original['id'])[0]['state'] == 'uncovered'
        ledger_before = accounting_rows(store, account['id'])
        trade_before = store.ledger_operation(account['id'], 'historical-ledger-buy')
        accounting_snapshot = store.account(account['id'])

        current = store.account(account['id'])
        changed = await api.patch(f"/api/accounts/{account['id']}", json={
            'name': 'Controlled retry account renamed', 'preferences': current['preferences'],
            'expected_version': current['version']}, headers={'X-Operation-ID': 'metadata-version-change'})
        assert changed.status_code == 200, changed.text
        current = changed.json()
        assert current['version'] == original['input_version'] + 1
        assert store.demands(job_id=original['id'])[0]['state'] == 'uncovered'
        newer = store.claim_jobs(account_id=account['id'])[0]
        await app.state.service.execute(newer)
        assert store.job(newer['id'])['state'] == 'completed', store.job(newer['id']).get('error')
        assert store.demands(job_id=original['id'])[0]['state'] == 'uncovered'
        assert stored_report(store, original['id']) == old_report

        retry_url = f"/api/jobs/{original['id']}/retry"
        retry_body = {'operation_id': 'retry-uncovered-ledger', 'research_scope': 'securities'}
        retried = await api.post(retry_url, json=retry_body)
        assert retried.status_code == 200, retried.text
        receipt = retried.json()
        retry_job_id = receipt['id']
        assert retry_job_id not in {original['id'], newer['id']}
        assert receipt['parent_job_id'] == original['id']
        assert receipt['input_version'] == current['version']
        assert receipt['task_kind'] == 'manual_retry' and receipt['state'] == 'queued'
        reassigned = store.demands(job_id=retry_job_id)
        assert [(d['id'], d['state']) for d in reassigned] == [(old_demand_id, 'assigned')]
        count = len(store.jobs(account['id']))
        assert (await api.post(retry_url, json=retry_body)).json()['id'] == retry_job_id
        assert len(store.jobs(account['id'])) == count
        claimed, = store.claim_jobs(account_id=account['id'])
        assert claimed['id'] == retry_job_id
        await app.state.service.execute(claimed)
        assert store.job(retry_job_id)['state'] == 'completed', store.job(retry_job_id).get('error')
        demand, = store.demands(job_id=retry_job_id)
        assert demand['state'] == ('completed' if declare_coverage else 'uncovered')
        with store.connect() as db:
            assert db.execute('SELECT 1 FROM research_demand_reads WHERE job_id=? AND demand_id=?',
                              (retry_job_id, old_demand_id)).fetchone()
        published = json.loads(stored_report(store, retry_job_id)[1])
        assert published['covered_demand_ids'] == ([old_demand_id] if declare_coverage else [])
        assert published['demand_coverage']['completed'] == int(declare_coverage)
        assert published['demand_coverage']['uncovered'] == int(not declare_coverage)
        assert published['demand_coverage']['unaccepted_claims'] == []
        assert (await api.post(retry_url, json=retry_body)).json()['id'] == retry_job_id
        assert len(store.jobs(account['id'])) == count
        assert store.claim_jobs(account_id=account['id']) == []
        assert stored_report(store, original['id']) == old_report
        assert accounting_rows(store, account['id']) == ledger_before
        assert store.ledger_operation(account['id'], 'historical-ledger-buy') == trade_before
        after = store.account(account['id'])
        assert after['cash'] == accounting_snapshot['cash']
        assert after['holdings'] == accounting_snapshot['holdings']
        assert len(payloads) == 3  # One current report, read then submit on retry.
