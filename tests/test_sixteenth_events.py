"""Controlled multi-event production worker, not real-market stability evidence."""
import asyncio
import json

import httpx
import pytest

from app.domain import AccountInput
from app.service import Service
from app.store import Store
from scripts.verify_sixteenth_live import pending_work
from tests.test_fourteenth_chain import setup_app, final, call, response
from tests.test_fourteenth_deadlines import wait_for_stage
from tests.test_ninth_calculation_context import seed


def test_live_observer_keeps_legal_pending_and_exposes_configuration_deferral():
    snap = {'jobs':[{'id':'blocked-a','account_id':'A','state':'waiting_config'}],
            'global_runs':[{'id':'blocked-global','state':'waiting_config'}],
            'demands':[{'id':1,'account_id':'A','state':'pending'},
                       {'id':2,'account_id':'B','state':'pending'},
                       {'id':3,'account_id':'','state':'pending'}]}
    work = pending_work(snap)
    assert [item['id'] for item in work['pending']] == [2]
    assert [item['id'] for item in work['deferred']] == [1,3]
    assert len(work['blocked']) == 2
    assert all(item['state'] == 'pending' for item in snap['demands'])


@pytest.mark.asyncio
async def test_two_accounts_scope_invalidation_public_event_and_restart_without_feedback_loop(tmp_path, monkeypatch, frozen_clock):
    frozen_clock.set('2026-11-02T14:00:00+08:00')
    entered, release = asyncio.Event(), asyncio.Event()
    old_id = None
    accounts, private, public = {}, {}, {}
    requests, cancellations, per_job = [], [], {}

    async def handler(request):
        payload = json.loads(request.content)
        context = seed(payload)
        job_id = context['job_id']
        account_id = (context.get('account') or {}).get('id')
        wire = json.dumps(payload, ensure_ascii=False)
        requests.append({'job':job_id,'account':account_id,'wire':wire})
        per_job[job_id] = per_job.get(job_id,0) + 1
        if job_id == old_id:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append(job_id)
                raise
        if account_id:
            other = 'B' if account_id == accounts['A']['id'] else 'A'
            assert f'PRIVATE_{other}_REPORT' not in wire
            assert 'PUBLIC_MODEL_VIEW_NOT_PUBLIC_FACT' not in wire
            if account_id == accounts['B']['id']:
                assert 'PRIVATE_A_SOURCE_CONTENT' not in wire
        else:
            assert 'PRIVATE_A_REPORT' not in wire and 'PRIVATE_B_REPORT' not in wire
            assert 'PRIVATE_A_SOURCE_CONTENT' not in wire
            assert context['account'] is None and context['shared_research'] == []

        information = context.get('pending_information', [])
        routed = any(item['kind'] == 'public_information' for item in context.get('business_demands', []))
        if (information or routed) and per_job[job_id] == 1:
            calls = [call('get_material', {'material_id':public['id']}, 'read-public')]
            if account_id == accounts['B']['id']:
                calls.append(call('get_material', {'material_id':private['id']}, 'deny-private'))
            return response(*calls)
        if information or routed:
            assert 'RAW_PUBLIC_EVENT_CONTENT' in wire
        if not account_id:
            extra = {'information_impacts':[{'material_id':item['id'],'status':'relevant',
                'symbols':['SZ:159278'],'explanation':'原始材料与候选证券身份相关，不传递投资观点。'}
                for item in information]}
            text = 'PUBLIC_MODEL_VIEW_NOT_PUBLIC_FACT'
        else:
            extra = {}
            text = 'PRIVATE_A_REPORT' if account_id == accounts['A']['id'] else 'PRIVATE_B_REPORT'
        return response(call('finish_analysis', final(text, scope=['account' if account_id else 'public'],
            covered_demand_ids=[item['id'] for item in context.get('business_demands', [])], **extra)))

    app, store, a = setup_app(tmp_path, monkeypatch, handler,
        scheduled_research_enabled=False, market_refresh_enabled=False, forecast_evaluation_enabled=False,
        model_request_hard_timeout_seconds=30, synthesis_request_hard_timeout_seconds=30,
        analysis_timeout_seconds=60, agent_timeout_seconds=40)
    b = store.save_account(AccountInput(name='受控第二账户', cash='100000',
        holdings=[{'symbol':'SZ:159599','quantity':'100','cost_price':'1'}]))
    accounts.update(A=a, B=b)
    old_id = a['latest_job_id']
    service = app.state.service
    await service.tick()
    old_task = service.active[old_id]
    await wait_for_stage(old_task, entered)
    await service.active[b['latest_job_id']]
    def ledger_contents(storage, account_id):
        return {key:value for key,value in storage.ledger(account_id).items() if key != 'input_version'}
    ledger_before = {acc['id']: ledger_contents(store, acc['id']) for acc in accounts.values()}
    store.save_universe({'watchlist': [], 'candidates':['SZ:159278']})
    assert store.account(a['id'])['version'] == a['version'] + 1
    assert store.account(b['id'])['version'] == b['version'] + 1
    await service.tick()
    with pytest.raises(asyncio.CancelledError, match='input_version_updated'):
        await old_task
    assert cancellations == [old_id]
    assert store.job(old_id)['cancellation']['code'] == 'input_version_updated'

    async def drain(worker, storage):
        # Explicit timeline advances cover the production two-second event
        # debounce. Await real task completions, never race a wall-clock sleep.
        for _ in range(20):
            frozen_clock.advance(seconds=3)
            worker.last_recommendation_check = 0
            await worker.tick()
            tasks = [*worker.active.values(), *worker.maintenance]
            if tasks:
                await asyncio.gather(*tasks)
            with storage.connect() as db:
                busy = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
                busy += db.execute("SELECT count(*) FROM global_runs WHERE state='running'").fetchone()[0]
                pending = db.execute("SELECT count(*) FROM research_demands WHERE state='pending'").fetchone()[0]
            if not busy and not pending:
                return
        raise AssertionError('Legitimate events did not reach a terminal state within the declared scenario')

    await drain(service, store)
    for acc in accounts.values():
        latest = store.analyses(acc['id'])[0]
        assert latest['input_version'] == store.account(acc['id'])['version']
        assert latest['status'] == 'current'
        assert ledger_contents(store, acc['id']) == ledger_before[acc['id']]
    assert store.global_runs()[0]['state'] == 'completed'
    private.update(store.save_material({'scope':'account','url':'https://controlled.example/private-a',
        'text':'PRIVATE_A_SOURCE_CONTENT'}, account_id=a['id']))
    public.update(store.save_material({'scope':'public','url':'https://controlled.example/public-event',
        'text':'RAW_PUBLIC_EVENT_CONTENT'}, notify=True))
    service.notify_material_changes()
    assert store.pending_material_notifications() == [public['id']]
    # Restart before the public event executes; its durable demand and outbox
    # are consumed by the ordinary new Service, with no task allowlist.
    reopened = Store(store.path)
    # The external credential service is the same declared in-memory fixture
    # across simulated process lifetimes; never access the user's keychain.
    reopened._credential_store = store._credential_store
    reopened.recover(config_ready=True)
    restarted = Service(reopened)
    await drain(restarted, reopened)
    assert reopened.pending_material_notifications() == []
    for acc in accounts.values():
        demands = [item for item in reopened.demands(acc['id']) if item['kind'] == 'public_information']
        assert len(demands) == 1 and demands[0]['state'] == 'completed'
        assert len([row for row in requests if row['job'] == demands[0]['job_id']]) == 2
        assert ledger_contents(reopened, acc['id']) == ledger_before[acc['id']]
        assert reopened.analyses(acc['id'])[0]['result']['delivery']['content_assessment'] == 'not_evaluated'
    with reopened.connect() as db:
        assert db.execute("SELECT state FROM information_routes WHERE material_id=?", (public['id'],)).fetchone()[0] == 'routed'
        reports_before = list(db.execute('SELECT id,report FROM analyses ORDER BY id'))
        forecasts_before = list(db.execute('SELECT id FROM forecasts ORDER BY id'))
        generated = [row[0] for row in db.execute("SELECT id FROM materials WHERE json_extract(body,'$.access')='generated_report'")]
    assert generated and not set(generated) & set(reopened.pending_material_notifications())
    request_count = len(requests)
    assert request_count == 11  # Includes the cancelled paid-request boundary.
    with reopened.connect() as db:
        attempts = [attempt for row in db.execute('SELECT body FROM events')
                    for attempt in json.loads(row[0]).get('payload', {}).get('requests', [])]
    assert len(attempts) == request_count
    assert sum(attempt.get('cancelled_by') == 'input_version_updated' for attempt in attempts) == 1
    demand_count = len(reopened.demands())
    for _ in range(3):
        await drain(restarted, reopened)
    reopened_again = Store(store.path)
    reopened_again._credential_store = store._credential_store
    reopened_again.recover(config_ready=True)
    await drain(Service(reopened_again), reopened_again)
    assert len(requests) == request_count
    assert len(reopened_again.demands()) == demand_count
    with reopened_again.connect() as db:
        assert list(db.execute('SELECT id,report FROM analyses ORDER BY id')) == reports_before
        assert list(db.execute('SELECT id FROM forecasts ORDER BY id')) == forecasts_before
