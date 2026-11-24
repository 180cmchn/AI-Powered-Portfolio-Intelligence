"""Real Store migration of the stopped seventeenth defaults; no model calls."""
import hashlib
import json
from pathlib import Path

import pytest

from app.agent_config import (PROMPTS, SEVENTEENTH_MARKET_PREVIOUS_PROMPTS,
                              SEVENTEENTH_PREVIOUS_PROMPTS)
from app.store import Store, dumps, loads
from tests.test_sixth_config import account, response_transport, store, supplier


MARKER = 'seventeenth_market_semantics_prompt_migration'
FIXTURES = Path(__file__).with_name('fixtures')


def roles(store):
    with store.connect() as db:
        return {row['role_id']: loads(row['body']) for row in
                db.execute('SELECT role_id,body FROM agent_definitions')}


def old_prompts():
    values = json.loads((FIXTURES / 'seventeenth-pre-semantics-prompts.json').read_text())
    assert {key: hashlib.sha256(value.encode()).hexdigest() for key, value in values.items()} == SEVENTEENTH_MARKET_PREVIOUS_PROMPTS
    return values


def seed_roles(store, values):
    with store.connect() as db:
        for role_id, role in values.items():
            db.execute('UPDATE agent_definitions SET version=?,body=? WHERE role_id=?',
                       (role['version'], dumps(role), role_id))
        db.execute('DELETE FROM meta WHERE key=?', (MARKER,))


@pytest.mark.asyncio
async def test_real_store_upgrades_stopped_defaults_and_tool_contract_without_rebinding(store):
    provider = await supplier(store, transport=response_transport({'data': [
        {'id': 'grok-4.7'}, {'id': 'grok-4.6'}]}))
    store.update_agent_definition('manager', {'default_binding': {
        'provider_id': provider['id'], 'model_id': 'grok-4.7', 'reasoning_effort': 'max'}})
    store.update_agent_definition('etf_researcher', {'default_binding': {
        'provider_id': provider['id'], 'model_id': 'grok-4.6', 'reasoning_effort': 'high'}})
    account_id = account(store)
    store.set_account_agent_override(account_id, 'market_researcher', {'reasoning_effort': 'max'})
    config = store.settings()
    config.update(max_model_calls=None, synthesis_max_calls=None, analysis_timeout_seconds=3600)
    store.set_meta('settings', config)
    before = roles(store)
    for role_id, prompt in old_prompts().items():
        before[role_id]['system_prompt'] = prompt
    seed_roles(store, before)
    with store.connect() as db:
        frozen = store.task_config_snapshot(db, account_id)
        db.execute('UPDATE jobs SET config_snapshot=? WHERE account_id=?', (dumps(frozen), account_id))
        frozen_rows = db.execute('SELECT id,config_snapshot FROM jobs WHERE account_id=?', (account_id,)).fetchall()
        assert frozen_rows
        jobs_before = [tuple(row) for row in frozen_rows]
        bindings_before = [tuple(row) for row in db.execute('SELECT * FROM account_agent_overrides')]
    catalog_before = store.meta('agent_catalog_version')
    reopened = Store(store.path)
    after = roles(reopened)
    for role_id, prior in before.items():
        expected = (PROMPTS / (role_id + '.txt')).read_text()
        if role_id != 'manager':
            expected += '\n\n' + (PROMPTS / 'researcher.txt').read_text()
        assert after[role_id]['system_prompt'] == expected
        assert after[role_id]['prompt_version'] == prior['prompt_version'] + 1
        assert after[role_id]['tool_version'] == prior['tool_version'] + 1
        assert after[role_id]['version'] == prior['version'] + 1
        for key in set(prior) - {'system_prompt', 'prompt_version', 'tool_version', 'version', 'updated_at'}:
            assert after[role_id][key] == prior[key]
    with reopened.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT id,config_snapshot FROM jobs WHERE account_id=?', (account_id,))] == jobs_before
        assert [tuple(row) for row in db.execute('SELECT * FROM account_agent_overrides')] == bindings_before
    assert reopened.settings() == config
    assert reopened.meta('agent_catalog_version') == catalog_before + 1
    migration = reopened.meta(MARKER)
    assert set(migration['updated_prompt_roles']) == set(before)
    assert set(migration['updated_tool_roles']) == set(before)
    assert roles(Store(store.path)) == after


def test_real_store_retains_custom_prompts_and_only_versions_tools_actually_allowed(tmp_path):
    store = Store(tmp_path / 'custom.sqlite3')
    store.update_agent_definition('custom_market', {'name': 'custom market',
        'responsibility': 'test customization', 'system_prompt': 'Do not replace this prompt.',
        'tools': ['query_market_rows', 'submit_report']})
    store.update_agent_definition('custom_notes', {'name': 'custom notes',
        'responsibility': 'test customization', 'system_prompt': 'Only notes.', 'tools': ['calculate', 'submit_report']})
    before = roles(store)
    before['market_researcher']['system_prompt'] = old_prompts()['market_researcher'] + '\n用户保留的精确说明。'
    before['filing_researcher']['system_prompt'] = old_prompts()['filing_researcher']
    before['filing_researcher']['custom'] = True
    before['portfolio_researcher']['system_prompt'] = old_prompts()['portfolio_researcher']
    before['portfolio_researcher']['tools'] = ['calculate', 'submit_report']
    seed_roles(store, before)
    reopened = Store(store.path)
    after = roles(reopened)
    for role_id in ['market_researcher', 'filing_researcher', 'custom_market', 'custom_notes']:
        assert after[role_id]['system_prompt'] == before[role_id]['system_prompt']
        assert after[role_id]['prompt_version'] == before[role_id]['prompt_version']
    for role_id, prior in before.items():
        uses_market = bool(set(prior['tools']) & {'query_market_rows', 'market_indicators'})
        assert after[role_id]['tool_version'] == prior['tool_version'] + int(uses_market)
        assert after[role_id]['tools'] == prior['tools']
        assert after[role_id]['default_binding'] == prior['default_binding']
    assert after['custom_notes'] == before['custom_notes']
    assert after['portfolio_researcher']['prompt_version'] == before['portfolio_researcher']['prompt_version'] + 1
    assert 'portfolio_researcher' not in reopened.meta(MARKER)['updated_tool_roles']
    assert roles(Store(store.path)) == after


@pytest.mark.parametrize('prior_migration_recorded', [True, False])
def test_sixteenth_manager_is_exactly_recognized_with_either_prior_marker_state(tmp_path, prior_migration_recorded):
    store = Store(tmp_path / 'sixteenth.sqlite3')
    values = roles(store)
    old = (FIXTURES / 'sixteenth-manager-prompt.txt').read_text()
    assert hashlib.sha256(old.encode()).hexdigest() == SEVENTEENTH_PREVIOUS_PROMPTS['manager']
    values['manager']['system_prompt'] = old
    seed_roles(store, values)
    if not prior_migration_recorded:
        with store.connect() as db:
            db.execute("DELETE FROM meta WHERE key='seventeenth_content_prompt_migration'")
    reopened = Store(store.path)
    current = roles(reopened)['manager']
    assert current['system_prompt'] == (PROMPTS / 'manager.txt').read_text()
    assert current['prompt_version'] == values['manager']['prompt_version'] + 1
    assert current['tool_version'] == values['manager']['tool_version'] + 1
    assert current['default_binding'] == values['manager']['default_binding']
    assert roles(Store(store.path)) == roles(reopened)
