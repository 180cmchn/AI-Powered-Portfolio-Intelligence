"""Isolated production configuration and evidence accounting; no real requests."""
import copy
from datetime import datetime
import json
from pathlib import Path

import pytest

from app.calendar import market_state
from app.domain import AccountInput
from app.store import Store
from scripts.verify_seventeenth_live import (
    configure_isolated, COUNT_FIELDS, formal_window_plan, observations, recovery_progress,
)
from tests.helpers_sixth import configure_test_team


@pytest.mark.parametrize('output_limit', [None, 4000])
def test_isolation_copies_selected_effective_models_without_daily_business_or_effort_override(tmp_path, monkeypatch, output_limit):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    source = Store(tmp_path / 'daily.sqlite3')
    config = {**source.settings(), 'model_name': 'controlled-selected-model', 'reasoning_effort': 'max',
        'max_model_calls': 88, 'max_agent_turns': 32, 'synthesis_max_calls': 7, 'max_output_tokens': output_limit,
        'max_job_attempts': 3, 'model_network_route': 'direct', 'news_queries': ['private unrelated query']}
    source.set_meta('settings', config)
    provider = configure_test_team(source, config)
    account = source.save_account(AccountInput(name='DO_NOT_COPY_DAILY_NAME', cash='1234567', holdings=[],
        preferences={'horizon': 'DO_NOT_COPY_DAILY_PREFERENCES'}))
    source.set_account_agent_override(account['id'], 'manager', {'reasoning_effort': 'xhigh'})
    custom = source.list_agent_roles()[0]
    source.update_agent_definition(custom['role_id'], {'system_prompt': 'preserve exact customized role prompt'})
    with source.connect() as db:
        before = list(db.iterdump())
        # Unreferenced supplier config must not leak into the validation DB.
        db.execute('INSERT INTO model_providers VALUES(?,?,?)', ('unused-provider', 1,
            json.dumps({'id': 'unused-provider', 'private_note': 'DO_NOT_COPY_UNUSED_PROVIDER'})))
    isolated, actual = configure_isolated(source.path, tmp_path / 'isolated.sqlite3', account['id'])
    assert all(actual[key] is None for key in COUNT_FIELDS)
    assert actual['max_output_tokens'] == output_limit
    assert actual['max_job_attempts'] == 3 and actual['model_network_route'] == 'direct'
    assert actual['news_queries'] == []
    assert not isolated.accounts()
    with isolated.connect() as db:
        dump = '\n'.join(db.iterdump())
        roles = {row['role_id']: json.loads(row['body']) for row in db.execute('SELECT * FROM agent_definitions')}
        assert [row[0] for row in db.execute('SELECT id FROM model_providers')] == [provider['id']]
        for table in ('jobs', 'analyses', 'materials', 'account_agent_overrides'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    assert 'DO_NOT_COPY_' not in dump
    assert roles['manager']['default_binding']['reasoning_effort'] == 'xhigh'
    assert roles['market_researcher']['default_binding']['reasoning_effort'] == 'max'
    assert roles[custom['role_id']]['system_prompt'] == 'preserve exact customized role prompt'
    with source.connect() as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]) == config
        assert db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 1


@pytest.mark.parametrize('at,seconds,reason', [
    ('2026-11-06T14:00:00+08:00', 18000, None),
    ('2026-11-06T14:00:00+08:00', 3600, 'configured_wall_deadline_does_not_cover_full_window'),
    ('2026-11-06T15:30:00+08:00', 18000, 'original_window_already_started'),
    ('2026-11-06T19:00:00+08:00', 18000, 'original_window_already_started'),
    ('2026-11-07T14:00:00+08:00', 18000, 'calendar_not_confirmed_trading_day'),
])
def test_formal_window_uses_original_deadline_without_backdating(at, seconds, reason):
    from app.forecasts import DEFAULT_RULE
    rule = copy.deepcopy(DEFAULT_RULE)
    result = formal_window_plan(market_state(datetime.fromisoformat(at)), seconds, rule)
    assert result['reason'] == reason
    assert result['deadline'].endswith('18:00:00+08:00')
    assert result['rule']['horizons'] == [1, 5, 20]
    assert rule == DEFAULT_RULE


def test_request_accounting_deduplicates_only_local_identity_and_retains_failure(tmp_path):
    store = Store(tmp_path / 'accounting.sqlite3')
    account = store.save_account(AccountInput(name='Independent fixture', holdings=[]))
    request = {'local_request_id': 'exact-local-request', 'protocol': 'responses',
        'session_id': 'same-session', 'agent_id': 'manager', 'cache_epoch': 0,
        'usage': {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 50}}}
    store.event(account['latest_job_id'], {'type': 'model_usage', 'payload': {'requests': [request]}})
    store.event(account['latest_job_id'], {'type': 'model_error', 'payload': {'requests': [request,
        {'local_request_id': 'failed-request', 'error': 'ReadError', 'session_id': 'same-session'}]}})
    store.set_job_state(account['latest_job_id'], 'failed', 'controlled failure')
    result = observations(store)
    assert result['http_attempts'] == 2 and result['model_usage']['failed_requests'] == 1
    assert result['model_usage']['cache_summary']['weighted_read_ratio'] == .5
    assert result['model_usage']['cache_summary']['coverage'] == .5
    assert result['known_fee'] is None


def test_recovery_progress_ignores_new_identity_and_time_but_detects_actual_new_evidence():
    checkpoint = {'reports': [{'report_id': 'old', 'agent_id': 'child', 'summary': 'Completed work',
        'source_task_id': 'original', 'created_at': 'then', 'material_ids': ['original-evidence']}],
        'calculations': [{'calculation_id': 'one', 'arguments': {'values': [1, 2]}, 'summary': {'result': '3'}}]}
    restored = copy.deepcopy(checkpoint)
    restored['reports'][0].update(report_id='new', agent_id='restored', source_task_id='recovery', created_at='later')
    restored['calculations'][0]['calculation_id'] = 'recovered-one'
    assert recovery_progress(checkpoint) == recovery_progress(restored)
    restored['reports'][0]['material_ids'].append('new-evidence')
    assert recovery_progress(checkpoint) != recovery_progress(restored)


def test_recovery_progress_deduplicates_semantic_work_without_sorting_arithmetic_operands():
    checkpoint = {'reports': [{'summary': 'Same completed report', 'record_id': 'archive-1',
        'agent_id': 'child-1', 'session_id': 'session-1', 'research_round': 1}],
        'calculations': [{'tool': 'calculate', 'arguments': {'operation': 'subtract', 'values': [9, 2]},
                          'summary': {'result': '7'}, 'record_id': 'calculation-1'}],
        'open_questions': ['Missing fact'],
        'unresolved_tasks': [{'task': 'Read new disclosure', 'agent_id': 'child-1', 'round': 1,
                              'pending_messages': ['Follow up']} ]}
    repeated = copy.deepcopy(checkpoint)
    repeated['reports'][0].update(record_id='archive-2', agent_id='child-2', session_id='session-2', research_round=2)
    repeated['calculations'][0]['record_id'] = 'calculation-2'
    repeated['unresolved_tasks'][0].update(agent_id='child-2', round=2)
    repeated['unresolved_tasks'][0]['pending_messages'] *= 2
    for field in ('reports', 'calculations', 'open_questions', 'unresolved_tasks'):
        repeated[field] *= 3
    assert recovery_progress(repeated) == recovery_progress(checkpoint)
    repeated['calculations'][0]['arguments']['values'] = [2, 9]
    assert recovery_progress(repeated) != recovery_progress(checkpoint)


def test_isolated_copy_rechecks_exact_old_builtin_prompt_after_fresh_store_migrations(tmp_path):
    source = Store(tmp_path / 'old-prompts.sqlite3')
    configure_test_team(source, {**source.settings(), 'model_name': 'controlled-model', 'reasoning_effort': 'max'})
    account = source.save_account(AccountInput(name='Prompt migration reference', holdings=[]))
    old = Path('tests/fixtures/sixteenth-manager-prompt.txt').read_text()
    with source.connect() as db:
        role = json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role.update(system_prompt=old, custom=False)
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (json.dumps(role),))
    isolated, _ = configure_isolated(source.path, tmp_path / 'new-prompts.sqlite3', account['id'])
    with isolated.connect() as db:
        actual = json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
    assert actual['system_prompt'] == Path('app/prompts/manager.txt').read_text()
    assert actual['default_binding'] == role['default_binding']
    with source.connect() as db:
        assert json.loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])['system_prompt'] == old
