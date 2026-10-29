"""Production Store/Service regressions with explicit clocks, HTTP/key fixtures."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.calendar import SHANGHAI
from app.forecasts import scoring_rule
from app.model import ModelError
from app.service import Service
from app.store import Store
from helpers_sixth import configure_test_team

AT = datetime(2026, 10, 28, 16, tzinfo=SHANGHAI)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import app.forecasts as forecasts
    import app.store as storage
    monkeypatch.setattr(forecasts, '_local', lambda at=None: AT if at is None else at)
    monkeypatch.setattr(storage, 'now', lambda: AT.isoformat())
    store = Store(tmp_path / 'identity.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0})
    configure_test_team(store, {'model_name': 'model-A', 'reasoning_effort': 'high'})
    account = store.save_account({'name': 'A', 'cash': '10000', 'holdings': [{'symbol': 'SZ:159278', 'quantity': '10000'}]})
    # due_at intentionally uses the real wall clock; make the controlled job due.
    with store.connect() as db:
        db.execute("UPDATE jobs SET due_at=?", (AT.isoformat(),))
    batch = store.create_forecast_batch(account['id'], kind='test', at=AT)
    store.bind_forecast_job(batch['id'], account['latest_job_id'])
    job = store.claim_jobs(account_id=account['id'])[0]
    return store, account, batch, job


def counts(store, model=None, team=None):
    filters = {'kind': 'test', 'manager_model': model, 'team_id': team}
    page, stats = store.forecasts_page(**filters), store.forecast_statistics(**filters)
    assert page['total'] == stats['expected']
    return page, stats


def report(direction='up'):
    return {'report': 'explicit protocol fixture', 'forecasts': [
        {'symbol': 'SZ:159278', 'horizon': h, 'direction': direction, 'reason': 'fixture evidence'} for h in [1, 5, 20]]}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['before_client', 'credential_unavailable', 'first_request', 'publication'])
async def test_preflight_and_request_failures_retain_three_samples(setup, monkeypatch, failure):
    store, account, batch, job = setup
    attempts = store.forecast_attempts(batch_id=batch['id'])
    assert len(attempts) == 1 and attempts[0]['manager_model'] == 'model-A'
    assert not attempts[0]['request_observed']
    team = attempts[0]['team_id']
    service = Service(store)
    async def no_quotes(symbols): return None
    monkeypatch.setattr(service, 'refresh_quotes', no_quotes)
    if failure == 'before_client':
        monkeypatch.setattr(store, 'snapshot', lambda *a: (_ for _ in ()).throw(RuntimeError('before construction')))
    elif failure == 'credential_unavailable':
        store._credential_store.values.clear()
    else:
        async def run(context, cfg, tools, emit):
            await emit({'type': 'model_error' if failure == 'first_request' else 'model_usage', 'agent_id': 'manager',
                'payload': {'requests': [{'model': 'model-A', 'agent_id': 'manager', 'reasoning_effort': 'high',
                    'error': 'RemoteProtocolError' if failure == 'first_request' else None}]}})
            if failure == 'first_request':
                raise ModelError('controlled first request failed')
            return report()
        service.runner = run
        if failure == 'publication':
            monkeypatch.setattr(store, 'publish', lambda *a: (_ for _ in ()).throw(RuntimeError('publish failed')))
    await service.execute(job)
    all_page, _ = counts(store)
    page, stats = counts(store, 'model-A', team)
    assert all_page['total'] == page['total'] == 3
    assert sum(g['evaluated'] for g in stats['groups']) == 0
    assert all(g['accuracy'] is None for g in stats['groups'])
    attempt = store.forecast_attempts(batch_id=batch['id'])[0]
    assert attempt['request_observed'] == (failure in ('first_request', 'publication'))
    assert not attempt['publication_succeeded']
    assert attempt['state'] == ('waiting_config' if failure == 'credential_unavailable' else 'failed')
    assert 'sixth-protocol-fixture-secret' not in json.dumps(attempt)
    for task in list(service.maintenance):
        task.cancel()


@pytest.mark.parametrize('new_model', ['model-A', 'model-B'])
def test_retry_keeps_attempt_ownership_and_one_sample_denominator(setup, new_model):
    store, account, batch, job = setup
    old = store.forecast_attempts(batch_id=batch['id'])[0]
    store.set_job_state(job['id'], 'failed', 'controlled preflight')
    configure_test_team(store, {'model_name': new_model, 'reasoning_effort': 'max'})
    retry = store.enqueue(account['id'], 'retry')
    # Queued retries have not frozen new configuration yet. Retain the prior
    # attempt's sample attribution even when current settings already use B.
    assert counts(store, 'model-A')[0]['total'] == 3
    assert counts(store, team=old['team_id'])[0]['total'] == 3
    assert counts(store, '__unknown__')[0]['total'] == 0
    if new_model == 'model-B':
        assert counts(store, 'model-B')[0]['total'] == 0
    with store.connect() as db:
        db.execute('UPDATE jobs SET due_at=? WHERE id=?', (AT.isoformat(), retry['id']))
    current = store.claim_jobs(account_id=account['id'])[0]
    assert store.publish(current['id'], report(), AT.isoformat())
    assert counts(store)[0]['total'] == 3
    assert store.forecast_statistics(kind='test')['attempt_statistics']['total'] == 2
    assert counts(store, new_model)[0]['total'] == 3
    history = store.forecast_attempts(batch_id=batch['id'])
    assert len(history) == 2
    assert history[0]['team_id'] == old['team_id'] and history[0]['state'] == 'failed'
    assert history[0]['manager_model'] == 'model-A' and history[1]['manager_model'] == new_model
    assert history[1]['publication_succeeded']
    assert store.forecast_statistics(kind='test', manager_model='model-A')['attempt_statistics']['total'] == (2 if new_model == 'model-A' else 1)
    if new_model == 'model-B':
        assert counts(store, 'model-A')[0]['total'] == 0  # sample belongs to actual publisher
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE forecast_execution_attempts SET manager_model='renamed' WHERE id=?", (history[0]['id'],))


def test_same_job_reclaim_records_each_frozen_configuration(setup):
    store, account, batch, job = setup
    store.set_job_state(job['id'], 'failed', 'preflight')
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='queued',due_at=? WHERE id=?", (AT.isoformat(), job['id']))
    configure_test_team(store, {'model_name': 'model-B'})
    store.claim_jobs(account_id=account['id'])
    attempts = store.forecast_attempts(job_id=job['id'])
    assert [a['attempt'] for a in attempts] == [1, 2]
    assert [a['manager_model'] for a in attempts] == ['model-A', 'model-B']
    assert counts(store, 'model-B')[0]['total'] == 3


def test_legacy_unknown_does_not_adopt_current_model(tmp_path):
    store = Store(tmp_path / 'unknown.sqlite3')
    account = store.save_account({'name': 'unknown', 'holdings': [{'symbol': 'SZ:159278', 'quantity': '1'}]})
    batch = store.create_forecast_batch(account['id'], kind='test', at=AT)
    configure_test_team(store, {'model_name': 'new-current-model'})
    page = store.forecasts_page(kind='test', manager_model='__unknown__')
    assert page['total'] == 3 and {r['attribution_status'] for r in page['items']} == {'configuration_unknown'}
    assert store.forecasts_page(kind='test', manager_model='new-current-model')['total'] == 0


def test_stable_rules_exploration_keeps_all_and_never_selects_winner(setup):
    store, account, _, initial = setup
    assert store.publish(initial['id'], report(), AT.isoformat())
    other = store.save_account({'name': 'B', 'holdings': [{'symbol': 'SZ:159278', 'quantity': '1'}]})
    b1 = store.create_forecast_batch(account['id'], kind='exploratory', at=AT)
    b2 = store.create_forecast_batch(account['id'], kind='exploratory', at=AT + timedelta(minutes=5))
    b3 = store.create_forecast_batch(other['id'], kind='exploratory', at=AT)
    b4 = store.create_forecast_batch(account['id'], kind='exploratory', at=AT + timedelta(days=1))
    assert len({b['id'] for b in [b1,b2,b3,b4]}) == 4
    assert len({b['rule_version'] for b in [b1,b2,b3,b4]}) == 1
    stats = store.forecast_statistics(kind='exploratory', account_id=account['id'])
    assert stats['expected'] == 9 and len(stats['groups']) == 3
    assert all(g['expected'] == 3 for g in stats['groups'])
    # Opposite outcomes cannot select a convenient record. All remain unpaired.
    for batch, direction in [(b1,'up'), (b2,'down'), (b3,'up')]:
        job = store.enqueue(batch['account_id'])
        store.bind_forecast_job(batch['id'], job['id'])
        with store.connect() as db:
            store.publish_forecasts(db, job, report(direction), 'fixture-report', published_at=AT)
    compare = store.forecast_comparison([account['id'],other['id']], kind='exploratory')
    assert compare['pairing_status'] == 'not_directly_pairable' and compare['overlap_expected'] == 0
    assert compare['unmatched_counts'] == {account['id']: 9, other['id']: 3}
    assert store.forecasts_page(kind='exploratory')['total'] == 12
    store.save_forecast_settings(account['id'], {'threshold':'0.01'})
    changed = store.create_forecast_batch(account['id'], kind='exploratory', at=AT)
    assert changed['rule_version'] != b1['rule_version']


def test_historical_rule_projection_preserves_ledger_and_unknown_rules(setup):
    store, account, _, _ = setup
    batches = [store.create_forecast_batch(account['id'],kind='exploratory',at=AT) for _ in range(3)]
    with store.connect() as db:
        for i, b in enumerate(batches):
            db.execute('DELETE FROM forecast_rule_projections WHERE batch_id=?',(b['id'],))
            db.execute('UPDATE forecast_batches SET rule_version=? WHERE id=?', ('legacy-random:' + str(i),b['id']))
        db.execute("UPDATE forecast_batches SET rule='{}' WHERE id=?", (batches[-1]['id'],))
        store.migrate_forecast_identity(db)
        store.migrate_forecast_identity(db)
        rows = list(db.execute('SELECT * FROM forecast_rule_projections WHERE original_rule_version LIKE ?',('legacy-random:%',)))
    assert rows[0]['scoring_rule_id'] == rows[1]['scoring_rule_id']
    assert rows[2]['scoring_rule_id'] is None and rows[2]['status'] == 'unknown'
    assert store.forecasts_page(kind='exploratory')['total'] == 9
    assert {r['original_rule_version'] for r in store.forecasts_page(kind='exploratory')['items']} == {'legacy-random:0','legacy-random:1','legacy-random:2'}


def test_database_page_is_bounded_and_stable_and_aggregates_revisions(setup, monkeypatch):
    store, account, batch, job = setup
    store.publish(job['id'], report(), AT.isoformat())
    traces=[]
    original=store._forecast_rows
    def bounded(db, filters, limit=None, offset=0):
        assert limit == 2
        db.set_trace_callback(traces.append)
        return original(db,filters,limit,offset)
    monkeypatch.setattr(store,'_forecast_rows',bounded)
    first=store.forecasts_page(kind='test',limit=2)
    second=store.forecasts_page(kind='test',limit=2,offset=first['next_offset'])
    assert first['total'] == second['total'] == 3
    assert len({r['id'] for r in first['items'] + second['items']}) == 3
    assert any('LIMIT 2 OFFSET 2' in q for q in traces)
    # SQL statistics must not call the materializing row/list path.
    stats=store.forecast_statistics(kind='test')
    assert stats['expected'] == 3 and stats['attempt_statistics']['total'] == 1


def test_v6_database_migration_keeps_immutable_predictions_and_backup(setup):
    store, account, batch, job = setup
    assert store.publish(job['id'], report(), AT.isoformat())
    with store.connect() as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM forecasts ORDER BY id')]
        # Recreate only the old batch-table shape in this isolated fixture.
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='forecast_batches'").fetchone()[0]
        sql = sql.replace('forecast_batches', 'batches_v6_fixture', 1)
        sql = sql.rstrip().removesuffix(')') + ', UNIQUE(account_id,kind,base_date,rule_version))'
        db.execute(sql)
        db.execute('INSERT INTO batches_v6_fixture SELECT * FROM forecast_batches')
        db.execute('DROP TABLE forecast_batches')
        db.execute('ALTER TABLE batches_v6_fixture RENAME TO forecast_batches')
        db.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        db.execute('DELETE FROM forecast_rule_projections')
    restored = Store(store.path)
    Store(store.path)  # repeated migration is idempotent
    assert store.path.with_name(store.path.name + '.before-v7.bak').exists()
    with restored.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM forecasts ORDER BY id')] == before
        assert db.execute('SELECT count(*) FROM forecast_rule_projections').fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE forecasts SET direction='down'")
    first=restored.create_forecast_batch(account['id'],kind='exploratory',at=AT)
    second=restored.create_forecast_batch(account['id'],kind='exploratory',at=AT)
    assert first['id'] != second['id'] and first['rule_version'] == second['rule_version']
    assert restored.create_forecast_batch(account['id'],kind='test',at=AT)['id'] == batch['id']


def test_rule_labels_and_decimal_format_do_not_split_scoring_identity(setup):
    store, account, batch, job = setup
    original=scoring_rule(batch['rule'])[0]
    store.save_forecast_settings(account['id'],{'labels':['one','five','twenty'],'threshold':'0.0050'})
    second=store.create_forecast_batch(account['id'],kind='exploratory',at=AT)
    assert second['scoring_rule_id'] == original
    assert second['rule']['labels'] == ['one','five','twenty']
    store.save_forecast_settings(account['id'],{'deadline_time':'18:01'})
    third=store.create_forecast_batch(account['id'],kind='exploratory',at=AT)
    assert third['scoring_rule_id'] != original


def test_formal_direction_requires_each_symbols_exact_baseline(setup):
    store, account, _, original_job = setup
    # An existing successful test report prevents formal retry reconciliation.
    assert store.publish(original_job['id'], report(), AT.isoformat())
    baseline={'SZ:159278':{'status':'ready','symbol':'SZ:159278','base_date':'2026-10-28',
        'target_date':'2026-10-28','base_price':'1.10','source':'controlled-history','price_policy':'same_source_pair_v1'}}
    batch=store.create_forecast_batch(account['id'],kind='formal',at=AT,
        symbols=['SZ:159278','SH:510300'],baseline_evidence=baseline)
    job=store.enqueue(account['id'],'formal baseline fixture')
    store.bind_forecast_job(batch['id'],job['id'])
    with store.connect() as db:
        db.execute('UPDATE jobs SET due_at=? WHERE id=?',(AT.isoformat(),job['id']))
    current=store.claim_jobs(account_id=account['id'])[0]
    result=report()
    result['forecasts'] += [{'symbol':'SH:510300','horizon':h,'direction':'up','reason':'fixture missing price'} for h in [1,5,20]]
    assert store.publish(current['id'],result,AT.isoformat())
    page=store.forecasts_page(kind='formal')
    assert page['total']==6
    assert len([r for r in page['items'] if r['forecast_id']])==3
    assert {r['state'] for r in page['items'] if r['symbol']=='SH:510300'}=={'waiting_data'}
    stats=store.forecast_statistics(kind='formal')
    assert all(g['predicted']==1 and g['expected']==2 and g['coverage']==0.5 for g in stats['groups'])
    # Filtering by a nonexistent symbol/rule removes execution attempts too.
    assert store.forecast_statistics(kind='formal',symbol='SZ:000001')['attempt_statistics']['total']==0
    assert store.forecast_statistics(kind='formal',rule_version='no-such-rule')['attempt_statistics']['total']==0


@pytest.mark.parametrize('changes',[{'base_date':'2026-10-27'},{'symbol':'SH:510300'},{'target_date':'2026-10-29'},
    {'status':'waiting_data'},{'base_price':'NaN'},{'price_policy':'other'},{'source':None}])
def test_formal_does_not_accept_mismatched_baseline(setup,changes):
    store,account,_,job=setup
    baseline={'status':'ready','symbol':'SZ:159278','base_date':'2026-10-28','target_date':'2026-10-28',
        'base_price':'1','source':'synthetic','price_policy':'same_source_pair_v1',**changes}
    batch=store.create_forecast_batch(account['id'],kind='formal',at=AT,baseline_evidence={'SZ:159278':baseline})
    # New job is independent because it is not yet associated with a batch.
    assert store.publish(job['id'],report(),AT.isoformat())
    new=store.enqueue(account['id'],'bad baseline fixture')
    store.bind_forecast_job(batch['id'],new['id'])
    with store.connect() as db:
        assert store.publish_forecasts(db,new,report(),'fixture',published_at=AT)==[]
    assert {r['state'] for r in store.forecasts_page(kind='formal')['items']}=={'waiting_data'}


def test_batch_list_summary_does_not_transfer_attempt_bodies(setup):
    store,account,batch,job=setup
    summary=store.forecast_batches(summary=True)[0]
    assert summary['attempt_count']==1 and summary['manager_model']=='model-A'
    assert summary['coverage']=={'uncovered':3}
    assert not {'team_snapshot','baseline_evidence','attempts'} & set(summary)
    assert store.forecast_batches()[0]['attempts'][0]['team_snapshot']['roles']


@pytest.mark.parametrize('old_state', ['running', 'waiting_config'])
def test_replaced_execution_closes_attempt_without_reassigning_model(setup, old_state):
    store,account,batch,job=setup
    if old_state=='waiting_config':
        store.set_job_state(job['id'],old_state,'fixture credential unavailable')
    before=store.forecast_attempts(job_id=job['id'])[0]
    configure_test_team(store,{'model_name':'model-B'})
    replacement=store.enqueue(account['id'],'retry',delay=60)
    old=store.forecast_attempts(job_id=job['id'])[0]
    assert old['state']=='superseded'
    assert old['manager_model']=='model-A' and old['team_id']==before['team_id']
    assert store.job(replacement['id'])['state']=='queued'
    assert counts(store,'model-A')[0]['total']==3
    assert counts(store,'model-B')[0]['total']==0
    assert store.forecast_statistics(kind='test',manager_model='model-A')['attempt_statistics']['states'][0]['state']=='superseded'


def test_restart_closes_interrupted_attempt_before_new_configuration_claim(setup):
    store,account,batch,job=setup
    before=store.forecast_attempts(job_id=job['id'])[0]
    store.recover(config_ready=True)
    old=store.forecast_attempts(job_id=job['id'])[0]
    assert old['state']=='interrupted' and old['team_id']==before['team_id']
    assert counts(store,'model-A')[0]['total']==3
    configure_test_team(store,{'model_name':'model-B'})
    current=store.claim_jobs(account_id=account['id'])[0]
    assert current['id']==job['id'] and current['attempts']==2
    attempts=store.forecast_attempts(job_id=job['id'])
    assert [(r['manager_model'],r['state']) for r in attempts]==[('model-A','interrupted'),('model-B','planned')]
    assert counts(store,'model-B')[0]['total']==3
