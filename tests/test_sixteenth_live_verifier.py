"""Verifier accounting must not turn unrelated task failure into acceptance."""
from app.store import Store
from scripts.verify_sixteenth_live import observations, event_outcomes, pending_work


def test_observations_use_real_schema_and_keep_unknown_cost(tmp_path):
    store = Store(tmp_path / 'accounting.sqlite3')
    account = store.save_account({'name': 'fixture', 'cash': '100', 'holdings': []})
    store.set_job_state(account['latest_job_id'], 'failed', 'controlled failure', error_code='fixture_failure')
    snapshot = observations(store)
    assert snapshot['jobs'][0]['state'] == 'failed'
    assert snapshot['jobs'][0]['error_code'] == 'fixture_failure'
    assert snapshot['http_attempts'] == 0 and snapshot['known_fee'] is None
    assert event_outcomes(snapshot)['status'] == 'fail'


def test_successful_account_does_not_hide_failed_public_task():
    snapshot = {'jobs': [{'id': 'a', 'account_id': 'a', 'state': 'completed'}],
                'global_runs': [{'id': 'public', 'state': 'failed'}], 'demands': []}
    assert not pending_work(snapshot)['busy']
    outcome = event_outcomes(snapshot)
    assert outcome['status'] == 'fail' and outcome['failed_tasks'][0]['id'] == 'public'


def test_blocked_account_keeps_its_demand_visible_without_failing_it():
    job = {'id': 'b', 'account_id': 'b', 'state': 'waiting_config'}
    demand = {'id': 1, 'account_id': 'b', 'state': 'pending'}
    snapshot = {'jobs': [job], 'global_runs': [], 'demands': [demand]}
    work = pending_work(snapshot)
    assert work['blocked'] == [job] and work['deferred'] == [demand]
    assert event_outcomes(snapshot)['status'] == 'incomplete'
    assert job['state'] == 'waiting_config' and demand['state'] == 'pending'
