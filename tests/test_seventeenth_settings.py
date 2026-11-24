"""Real settings/SQLite contracts for nullable count policies; no model HTTP."""
import json

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.domain import AccountInput
from app.main import Settings, create_app
from app.store import Store
from tests.test_fourth_recovery import checkpoint_fixture, store


COUNT_FIELDS = ('max_model_calls', 'max_agent_turns', 'synthesis_max_calls')


@pytest.mark.parametrize('field', COUNT_FIELDS)
@pytest.mark.parametrize('value', [None, '', '  '])
def test_settings_support_explicit_unlimited(field, value):
    parsed = Settings(**{field: value}).model_dump(exclude_unset=True)
    assert parsed == {field: None}
    assert json.loads(json.dumps(parsed, allow_nan=False))[field] is None


@pytest.mark.parametrize('field', COUNT_FIELDS)
@pytest.mark.parametrize('value', [True, False, float('inf'), float('nan'), -1, 0, 2.5])
def test_non_finite_and_non_count_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_real_settings_roundtrip_and_frozen_snapshot_preserve_null(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    app = create_app(tmp_path / 'settings.sqlite3', mode='live', worker=False)
    store = app.state.store
    store.set_meta('settings', {**store.settings(), 'max_model_calls': 88,
        'max_agent_turns': 24, 'synthesis_max_calls': 6, 'quote_provider': 'manual', 'debounce_seconds': 0})
    with TestClient(app) as client:
        before = client.get('/api/settings').json()
        assert [before[k] for k in COUNT_FIELDS] == [88, 24, 6]
        changed = client.put('/api/settings', json={'max_model_calls': None})
        assert changed.status_code == 200
        assert [changed.json()[k] for k in COUNT_FIELDS] == [None, 24, 6]
        assert client.put('/api/settings', json={k: None for k in COUNT_FIELDS}).status_code == 200
        fetched = client.get('/api/settings').json()
        assert all(fetched[k] is None for k in COUNT_FIELDS)
        assert client.put('/api/settings', json=fetched).status_code == 200
        account = store.save_account(AccountInput(name='Unbounded fixture', cash='100', holdings=[], research_scope='cash_only'))
        job = store.claim_jobs(account_id=account['id'])[0]
        assert all(job['config_snapshot'][k] is None for k in COUNT_FIELDS)
        assert client.put('/api/settings', json={'max_model_calls': 50, 'max_agent_turns': 20,
            'synthesis_max_calls': 5}).status_code == 200
        assert all(store.job(job['id'])['config_snapshot'][k] is None for k in COUNT_FIELDS)
    reopened = Store(store.path)
    assert [reopened.settings()[k] for k in COUNT_FIELDS] == [50, 20, 5]


def test_unlimited_explicit_recovery_is_not_capped_at_two_and_success_is_terminal(store):
    store.set_meta('settings', {**store.settings(), **{key: None for key in COUNT_FIELDS}})
    account, original, context, _ = checkpoint_fixture(store)
    for attempt in range(1, 5):
        store.submit_task(account['id'], parent_job_id=original['id'], research_scope='cash_only')
        retry = store.claim_jobs(account_id=account['id'])[0]
        cp = store.reusable_synthesis_checkpoint(retry, context, continue_research=True)
        assert cp['resume_attempt'] == attempt
        assert cp['checkpoint_job_id'] == original['id']
        store.set_job_state(retry['id'], 'failed', 'controlled recovery failure')
        store = Store(store.path)
    diagnostic = store.checkpoint_recovery_diagnostic(store.job(original['id']), context)
    assert diagnostic['eligible']
    # Committed successful publication, not a magic attempt count, consumes it.
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='completed', result=? WHERE id=?",
            (json.dumps({'synthesis_checkpoint_job_id': original['id']}), retry['id']))
        assert db.execute('SELECT resume_count FROM synthesis_checkpoints WHERE job_id=?',
                          (original['id'],)).fetchone()[0] == 4
    store.submit_task(account['id'], parent_job_id=original['id'], research_scope='cash_only')
    retry = store.claim_jobs(account_id=account['id'])[0]
    assert store.reusable_synthesis_checkpoint(retry, context, continue_research=True) is None
    assert 'checkpoint_already_delivered' in store.checkpoint_recovery_diagnostic(
        store.job(original['id']), context)['reasons']
