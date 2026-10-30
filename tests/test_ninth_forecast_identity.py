"""Sample attribution regressions through production Store/Service with controlled models."""
import json
import sqlite3
from datetime import timedelta

import pytest

from app.store import Store
from helpers_sixth import configure_test_team
from test_eighth_forecast_preparation import env, A, B, Prices, tick, claim, service_for


@pytest.mark.asyncio
@pytest.mark.parametrize('model,effort,endpoint', [
    ('model-B', 'max', 'https://fixture.invalid/v1'),
    ('protocol-fixture', 'medium', 'https://fixture.invalid/v1'),
    ('protocol-fixture', 'max', 'https://fixture.invalid/v1'),
    ('protocol-fixture', 'medium', 'https://second-fixture.invalid/v1'),
])
async def test_partial_publication_retry_preflight_failure_uses_frozen_sample_identity(env, monkeypatch, model, effort, endpoint):
    store, account, clock = env
    await tick(store, clock, Prices())
    first = claim(store, clock, account)
    initial = {'report': 'controlled first security only', 'forecasts': [
        {'symbol': A, 'horizon': h, 'direction': 'up', 'reason': 'controlled'} for h in (1, 5, 20)]}
    assert store.publish(first['id'], initial, clock[0].isoformat())
    before = {r['id']: dict(r) for r in store.forecasts_page()['items'] if r['symbol'] == A}
    old = store.forecast_attempts(job_id=first['id'])[0]
    if endpoint != 'https://fixture.invalid/v1':
        store._fixture_provider_id = None  # distinct supplier, same catalog model name
        store._fixture_effective_key = None
    configure_test_team(store, {'model_name': model, 'reasoning_effort': effort, 'model_base_url': endpoint})
    retry = store.enqueue(account['id'], 'retry')
    # Merely queueing a retry neither rewrites identity nor erases a failure.
    assert {r['execution_attempt_id'] for r in store.forecasts_page()['items'] if r['symbol'] == B} == {old['id']}
    current = claim(store, clock, account)
    assert current['id'] == retry['id']
    monkeypatch.setattr(store, 'snapshot', lambda *args: (_ for _ in ()).throw(RuntimeError('controlled preflight failure')))
    async def never_called(*args): raise AssertionError('must fail before model invocation')
    service = service_for(store, monkeypatch, never_called)
    await service.execute(current)
    attempt = store.forecast_attempts(job_id=current['id'])[0]
    assert attempt['state'] == 'failed' and not attempt['request_observed']
    rows = store.forecasts_page()['items']
    assert len(rows) == 6 and store.forecast_statistics()['expected'] == 6
    for row in rows:
        if row['symbol'] == A:
            assert row == before[row['id']]
        else:
            assert row['manager_model'] == model and row['manager_effort'] == effort
            assert row['team_id'] == attempt['team_id'] and row['execution_attempt_id'] == attempt['id']
            assert row['state'] == 'failed' and row['forecast_id'] is None
    assert store.forecasts_page(team_id=attempt['team_id'], symbol=B)['total'] == 3
    assert store.forecast_statistics()['attempt_statistics']['total'] == 2
    if endpoint != 'https://fixture.invalid/v1' or effort != 'medium' or model != 'protocol-fixture':
        assert attempt['team_id'] != old['team_id']
        summary = store.forecast_batches(summary=True)[0]
        assert summary['attribution_status'] == 'mixed' and summary['identity_count'] == 2
        assert summary['manager_model'] is None
        assert store.forecast_batches()[0]['sample_attribution']['identity_count'] == 2
    assert all(group['evaluated'] == 0 for group in store.forecast_statistics()['groups'])


@pytest.mark.asyncio
async def test_retry_precise_horizon_scope_queue_failure_and_claim_time_configuration(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    first = claim(store, clock, account)
    store.set_job_state(first['id'], 'failed', 'old failure')
    old = store.forecast_attempts(job_id=first['id'])[0]
    configure_test_team(store, {'model_name': 'queued-model'})
    retry = store.enqueue(account['id'], 'retry')
    batch = store.forecast_batches()[0]
    store.bind_forecast_job(batch['id'], retry['id'], symbols=[B], horizons=[5])
    assert all(r['state'] == 'failed' for r in store.forecasts_page()['items'])
    assert {r['execution_attempt_id'] for r in store.forecasts_page()['items']} == {old['id']}
    configure_test_team(store, {'model_name': 'claimed-model', 'reasoning_effort': 'xhigh'})
    current = claim(store, clock, account)
    context = store.forecast_job_context(current['id'])
    assert {(i['symbol'], i['horizon']) for i in context['items'] if i['direction_eligible']} == {(B, 5)}
    store.set_job_state(current['id'], 'failed', 'new scoped failure')
    rows = store.forecasts_page()['items']
    selected = [r for r in rows if r['manager_model'] == 'claimed-model']
    assert len(selected) == 1 and (selected[0]['symbol'], selected[0]['horizon']) == (B, 5)
    assert sum(r['execution_attempt_id'] == old['id'] for r in rows) == 5
    assert store.forecasts_page(manager_model='queued-model')['total'] == 0
    assert store.forecast_statistics()['expected'] == 6
    # Restart and a cancelled retry preserve the last claimed identity.
    store = Store(store.path)
    assert store.forecasts_page(manager_model='claimed-model')['total'] == 1
    cancelled = store.enqueue(account['id'], 'retry')
    store.set_job_state(cancelled['id'], 'cancelled', 'controlled cancel before claim')
    assert store.forecasts_page(manager_model='claimed-model')['total'] == 1
    assert store.forecast_statistics()['attempt_statistics']['total'] == 2


@pytest.mark.asyncio
async def test_schema8_migration_only_uses_proven_single_attempt_scopes(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    current = claim(store, clock, account)
    old = store.forecast_attempts(job_id=current['id'])[0]
    with store.connect() as db:
        db.execute('DELETE FROM forecast_sample_attempts')
        db.execute('DELETE FROM forecast_research_scope_samples')
        db.execute("DELETE FROM meta WHERE key='forecast_sample_identity_v9'")
        db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    store = Store(store.path)
    assert store.meta('schema_version') == 9
    assert store.forecasts_page(team_id=old['team_id'])['total'] == 6
    backup = store.path.with_name(store.path.name + '.before-v9.bak')
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '8'
    Store(store.path)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM forecast_sample_attempts').fetchone()[0] == 6
        db.execute('DELETE FROM forecast_sample_attempts')
        db.execute('DELETE FROM forecast_research_scope_samples')
        db.execute('DELETE FROM forecast_research_scopes')
        db.execute("DELETE FROM meta WHERE key='forecast_sample_identity_v9'")
    store = Store(store.path)
    assert store.forecasts_page(manager_model='__unknown__')['total'] == 6
    assert store.forecast_attempts(job_id=current['id'])[0]['manager_model'] == old['manager_model']


@pytest.mark.asyncio
async def test_limited_first_attempt_failure_does_not_mark_unassigned_targets_failed(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    batch = store.forecast_batches()[0]
    store.bind_forecast_job(batch['id'], batch['job_id'], symbols=[B], horizons=[5])
    current = claim(store, clock, account)
    store.set_job_state(current['id'], 'failed', 'controlled single-target failure')
    for row in store.forecasts_page()['items']:
        if (row['symbol'], row['horizon']) == (B, 5):
            assert row['state'] == 'failed' and row['execution_attempt_id']
        else:
            assert row['state'] == 'uncovered' and row['execution_attempt_id'] is None
    assert store.forecast_statistics(symbol=A)['attempt_statistics']['total'] == 0
    assert store.forecast_statistics(symbol=B, horizon=5)['attempt_statistics']['total'] == 1
    assert store.forecast_statistics(symbol=B, horizon=1)['attempt_statistics']['total'] == 0


@pytest.mark.asyncio
async def test_partial_history_migration_keeps_published_attempt_and_retry_scope(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    first = claim(store, clock, account)
    assert store.publish(first['id'], {'report': 'first publication', 'forecasts': [
        {'symbol': A, 'horizon': h, 'direction': 'abstain', 'reason': 'uncertainty'} for h in (1, 5, 20)]}, clock[0].isoformat())
    old_id = store.forecast_attempts(job_id=first['id'])[0]['id']
    configure_test_team(store, {'model_name': 'retry-B'})
    store.enqueue(account['id'], 'retry')
    retry = claim(store, clock, account)
    store.set_job_state(retry['id'], 'failed', 'controlled failure')
    new_id = store.forecast_attempts(job_id=retry['id'])[0]['id']
    with store.connect() as db:
        db.execute('DELETE FROM forecast_sample_attempts')
        db.execute('DELETE FROM forecast_research_scope_samples')
        db.execute("DELETE FROM meta WHERE key='forecast_sample_identity_v9'")
    store = Store(store.path)
    for row in store.forecasts_page()['items']:
        assert row['execution_attempt_id'] == (old_id if row['symbol'] == A else new_id)
        if row['symbol'] == A:
            assert row['direction'] == 'abstain' and row['published_at'] == clock[0].isoformat()
    with store.connect() as db:
        # No retrospective assignment of already-published A to retry B.
        assert db.execute('SELECT count(*) FROM forecast_sample_attempts WHERE attempt_id=?', (new_id,)).fetchone()[0] == 3
