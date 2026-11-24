"""Conditional final schema remains exact; controlled recovery sends no HTTP on tampering."""
import copy
import json

import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient
from app.store import Store
from scripts import verify_fifteenth_recovery as recovery
from tests.test_fifteenth_business import prepared
from tests.test_fifteenth_recovery import parent, prep, runargs, install_model
from tests.delivery_helpers import delivery_arguments, final_result, runner_result


def test_positive_runner_declares_empty_without_repairing_missing_negative_helpers():
    body = {'report': 'controlled explicit runner fixture'}
    assert runner_result(body, {})['covered_demand_ids'] == []
    assert 'covered_demand_ids' not in body
    for value in ([7], None, False):
        assert runner_result({**body, 'covered_demand_ids': value}, {})['covered_demand_ids'] == value
    assert 'covered_demand_ids' not in delivery_arguments(body['report'])
    assert 'covered_demand_ids' not in final_result(body)


def test_recovery_schema_condition_uses_current_frozen_store_scope_and_preserves_base(tmp_path):
    store = Store(tmp_path / 'schema.sqlite3')
    account = store.save_account(AccountInput(name='controlled schema', cash='10'))
    job = store.job(account['latest_job_id'])
    original = copy.deepcopy(next(t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis'))
    digest = recovery.business.digest(original)
    schema, required = recovery.expected_final_schema(store.path, job['id'], digest)
    assert schema == original and required is False
    store.request_analysis(account['id'], 'ledger_update', 'controlled-schema-demand')
    store.task_demands(job['id'], account['id'])
    schema, required = recovery.expected_final_schema(store.path, job['id'], digest)
    expected = copy.deepcopy(original)
    expected['parameters']['required'].append('covered_demand_ids')
    assert schema == expected and required is True
    assert next(t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis') == original
    with pytest.raises(ValueError, match='Frozen production final schema changed'):
        recovery.expected_final_schema(store.path, job['id'], 'different-frozen-schema')
    with pytest.raises(ValueError, match='Actual recovery task'):
        recovery.expected_final_schema(store.path, None, digest)
    with pytest.raises(KeyError, match='任务不存在'):
        recovery.expected_final_schema(store.path, 'absent-task', digest)


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['remove_declaration', 'remove_property', 'extra_tool'])
async def test_recovery_rejects_changed_required_fields_or_tool_permissions_before_http(parent, monkeypatch, tamper):
    prep(parent)
    actual_complete = ModelClient.complete
    mutated = []
    async def changed(self, session, tools, **kwargs):
        altered = copy.deepcopy(tools)
        assert len(altered) == 1 and altered[0]['name'] == 'finish_analysis'
        assert 'covered_demand_ids' in altered[0]['parameters']['required']
        if tamper == 'remove_declaration':
            altered[0]['parameters']['required'].remove('covered_demand_ids')
        elif tamper == 'remove_property':
            altered[0]['parameters']['properties'].pop('limitations')
        else:
            altered.append(copy.deepcopy(next(t for t in agents.RESEARCH_TOOLS if t['name'] == 'calculate')))
        mutated.append(tamper)
        return await actual_complete(self, session, altered, **kwargs)
    monkeypatch.setattr(ModelClient, 'complete', changed)
    def forbidden(request):
        pytest.fail('Altered recovery schema must stop before any model HTTP')
    install_model(monkeypatch, forbidden)
    assert await recovery.run_prepared(runargs(parent)) == 2
    summary = recovery.read(parent.output / 'execution/summary.json')
    assert mutated == [tamper]
    assert summary['budget']['reserved_attempts'] == 0
    assert not summary['published'] and summary['execution_stopped']
    with recovery.ReadOnlyStore(parent.output / 'execution/evaluation.sqlite3').connect() as db:
        events = [json.loads(row[0]) for row in db.execute('SELECT body FROM events WHERE job_id=?', (summary['job_id'],))]
    error = next(e['payload']['error'] for e in events if e['type'] == 'model_error')
    assert 'Complete production final schema must remain intact' in error
