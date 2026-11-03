"""Offline controls for smaller synthesis input and existing parallel research."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from app import agents
from app.agent_config import PROMPTS
from app.model import ModelClient
from app.store import Store, dumps, loads
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, seed, setup


@pytest.mark.asyncio
@pytest.mark.parametrize('metrics_case', ['equal', 'historical_difference', 'type_difference', 'missing_current'])
async def test_final_request_deduplicates_only_identical_metrics_and_keeps_evidence(monkeypatch, metrics_case):
    cfg = config(monkeypatch, max_agent_turns=2)
    current = {'cash': '10000', 'cost_known': True, 'data_as_of': '2026-10-30T15:00:00+08:00'}
    saved = copy.deepcopy(current)
    if metrics_case == 'historical_difference':
        saved['cash'] = '9500'
    elif metrics_case == 'type_difference':
        # Python considers 1 == True; these distinct JSON values must survive.
        saved['cost_known'] = 1
    reports = [{'report_id': 'old-child-report', 'agent_id': 'old-child',
        'role_id': 'market_researcher', 'model_config': {'model_name': 'prior-child-model'},
        'summary': 'Full delivered report. ' * 700,
        'findings': [{'adverse_evidence': 'Known negative return; do not omit.'}],
        'open_questions': ['Source currency remains uncertain.']}]
    checkpoint = {'phase': 'synthesis', 'task_id': 'prior-task', 'account_id': 'A',
        'input_version': 2, 'metrics': saved, 'reports': reports,
        'calculations': [{'tool': 'calculate', 'agent_id': 'old-child', 'calculation_id': 'prior-calc',
            'source_task_id': 'prior-task', 'input_version': 2,
            'arguments': {'operation': 'subtract', 'values': ['100', '125']},
            'output': {'operation': 'subtract', 'inputs': ['100', '125'], 'result': '-25'}}]}
    original = copy.deepcopy(checkpoint)
    context = {'account': {'id': 'A', 'version': 2}, 'input_version': 2,
        'synthesis_checkpoint': checkpoint}
    if metrics_case != 'missing_current':
        context['metrics'] = current
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        visible = seed(payload)
        projected = visible['synthesis_checkpoint']
        if metrics_case == 'equal':
            assert 'metrics' not in projected
            with_duplicate = copy.deepcopy(payload)
            duplicate_seed = seed(with_duplicate)
            duplicate_seed['synthesis_checkpoint']['metrics'] = saved
            next(item for item in with_duplicate['input'] if item.get('role') == 'user')['content'] = json.dumps(duplicate_seed, ensure_ascii=False)
            assert len(json.dumps(payload, ensure_ascii=False)) < len(json.dumps(with_duplicate, ensure_ascii=False))
        else:
            assert projected['metrics'] == saved
        if metrics_case == 'missing_current':
            assert 'metrics' not in visible
        else:
            assert visible['metrics'] == current
        assert projected['reports'] == reports
        calculations = visible['calculation_summaries']
        assert len(calculations) == 1
        assert calculations[0]['result'] == '-25'
        assert calculations[0]['agent_id'] == 'old-child'
        assert calculations[0]['source_task_id'] == 'prior-task'
        assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
        expected = next(tool for tool in agents.MANAGER_TOOLS if tool['name'] == 'finish_analysis')
        assert payload['tools'][0]['parameters'] == expected['parameters']
        return output('responses', [('finish_analysis', delivery_arguments(report='Controlled evidence-preserving final.'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime(context, cfg, object(), emit)
    try:
        result = await runtime._loop('manager', runtime.manager_session, manager=True)
        assert result['submission']['action'] == 'finish_analysis' and len(requests) == 1
        assert checkpoint == original and runtime.synthesis_checkpoint == original
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_independent_children_overlap_and_deliver_full_reports_before_synthesis(tmp_path, monkeypatch):
    cfg = config(monkeypatch, max_model_calls=18, max_agent_turns=12, max_concurrent_agents=2)
    _, tools, context = setup(tmp_path, cfg)
    context['metrics'] = {'cash': '10000'}
    started, child_turns, reports = set(), {}, {}
    both_started = asyncio.Event()
    both_replied = asyncio.Event()
    manager_turns = 0
    events = []

    async def handler(request):
        nonlocal manager_turns
        payload = json.loads(request.content)
        visible = seed(payload)
        identity = visible['runtime_status']['agent_id']
        if identity != 'manager':
            child_turns[identity] = child_turns.get(identity, 0) + 1
            if child_turns[identity] == 1:
                started.add(identity)
                if len(started) == 2:
                    both_started.set()
                # Serial child execution cannot satisfy this barrier.
                await asyncio.wait_for(both_started.wait(), timeout=1)
                return output('responses', [('calculate', {'operation': 'subtract', 'values': [100, 125]})])
            report = {'summary': 'Full independent research for ' + identity,
                'findings': [{'adverse_evidence': 'Negative scenario remains visible.'}],
                'open_questions': ['Conditional judgment; source currency unknown.']}
            reports[identity] = report
            if len(reports) == 2:
                both_replied.set()
            return output('responses', [('submit_report', report)])
        manager_turns += 1
        if manager_turns == 1:
            return output('responses', [('spawn_research', {'task': task}) for task in
                ('Independent security A: evidence and adverse conditions.', 'Independent security B: evidence and adverse conditions.')])
        await asyncio.wait_for(both_replied.wait(), timeout=1)
        if not all(child['status'] == 'completed' for child in visible['runtime_status']['children']):
            return output('responses', [('wait_agents', {'timeout_seconds': .05})])
        if 'synthesis_transition' not in visible:
            return output('responses', [('prepare_synthesis', {'summary': 'Combine delivered research and retain both adverse scenarios.'})])
        assert 'metrics' not in visible['synthesis_checkpoint']
        delivered = visible['synthesis_checkpoint']['reports']
        assert len(delivered) == len(reports) == 2
        for report in delivered:
            expected = reports[report['agent_id']]
            assert all(report[key] == value for key, value in expected.items())
        assert len(visible['calculation_summaries']) == 2
        assert all(item['result'] == '-25' for item in visible['calculation_summaries'])
        return output('responses', [('finish_analysis', delivery_arguments(report='Controlled combined account judgment.'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert both_started.is_set() and len(result['agents']) == 2
    assert all(child['status'] == 'completed' for child in result['agents'])
    assert any(event['type'] == 'synthesis_context_projected' for event in events)


@pytest.mark.parametrize('variant', ['builtin', 'edited_builtin', 'custom_exact'])
def test_synthesis_prompt_migration_runs_after_fourteenth_marker_once_and_preserves_custom(tmp_path, variant):
    path = tmp_path / 'roles.sqlite3'
    store = Store(path)
    old_prompt = (Path(__file__).with_name('fixtures') / 'fifteenth-manager-prompt.txt').read_text()
    assert hashlib.sha256(old_prompt.encode()).hexdigest() == '32ddc797668b30449336648791be23d6f51f87dfc7de38cee2e37c93271d060e'
    with store.connect() as db:
        role = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role['system_prompt'] = old_prompt + ('\nUser-defined scope.' if variant == 'edited_builtin' else '')
        role['custom'] = variant == 'custom_exact'
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(role),))
        assert db.execute("SELECT 1 FROM meta WHERE key='fourteenth_role_migration'").fetchone()
        db.execute("DELETE FROM meta WHERE key='fifteenth_synthesis_prompt_migration'")
        frozen = store.freeze_agent_team(db=db)
    account = store.save_account({'name': 'Migration fixture', 'cash': '10000', 'holdings': []})
    job_id = account['latest_job_id']
    with store.connect() as db:
        db.execute('UPDATE jobs SET config_snapshot=? WHERE id=?', (dumps({'_agent_team': frozen}), job_id))
    updated = Store(path)
    actual = next(item for item in updated.list_agent_roles() if item['role_id'] == 'manager')
    assert actual['default_binding'] == role['default_binding'] and actual['tools'] == role['tools']
    if variant == 'builtin':
        assert actual['system_prompt'] == (PROMPTS / 'manager.txt').read_text()
        assert actual['prompt_version'] == role['prompt_version'] + 1
        assert actual['version'] == role['version'] + 1
    else:
        assert actual['system_prompt'] == role['system_prompt']
        assert actual['prompt_version'] == role['prompt_version'] and actual['version'] == role['version']
        assert actual['prompt_update']['requires_explicit_save']
    assert updated.job(job_id)['config_snapshot']['_agent_team'] == frozen
    assert Store(path).list_agent_roles() == updated.list_agent_roles()


@pytest.mark.parametrize('variant', ['prior_thirteenth', 'prior_fourteenth', 'current', 'edited_builtin', 'custom_exact'])
def test_isolated_copy_migrates_prior_builtin_without_changing_source_or_bindings(tmp_path, monkeypatch, variant):
    from scripts.verify_fourteenth_live import configure_isolated
    monkeypatch.setenv('MODEL_API_KEY', 'controlled-isolation-test-key')
    source = Store(tmp_path / 'source.sqlite3', 'live')
    account = source.save_account({'name': 'Controlled configuration source', 'cash': '10000', 'holdings': []})
    fixtures = Path(__file__).with_name('fixtures')
    prior = (fixtures / 'fifteenth-manager-prompt.txt').read_text()
    current = (PROMPTS / 'manager.txt').read_text()
    prompt = (fixtures / 'thirteenth-manager-prompt.txt').read_text() if variant == 'prior_thirteenth' else current if variant == 'current' else prior
    if variant == 'edited_builtin':
        prompt += '\nKeep this user-specific scope.'
    with source.connect() as db:
        role = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role.update(system_prompt=prompt, custom=variant == 'custom_exact')
        role['default_binding']['reasoning_effort'] = 'xhigh'
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(role),))
        source_roles = [tuple(row) for row in db.execute('SELECT role_id,version,body FROM agent_definitions ORDER BY role_id')]
        source_meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('fourteenth_role_migration','fifteenth_synthesis_prompt_migration')"))
    original_hash = hashlib.sha256(source.path.read_bytes()).hexdigest()
    isolated, _ = configure_isolated(source.path, tmp_path / 'target.sqlite3', account['id'])
    with isolated.connect() as db:
        actual = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        assert db.execute("SELECT 1 FROM meta WHERE key='fifteenth_synthesis_prompt_migration'").fetchone()
    assert actual['default_binding'] == role['default_binding']
    assert actual['tools'] == role['tools']
    with sqlite3.connect(source.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        assert list(db.execute('SELECT role_id,version,body FROM agent_definitions ORDER BY role_id')) == source_roles
        assert dict(db.execute("SELECT key,value FROM meta WHERE key IN ('fourteenth_role_migration','fifteenth_synthesis_prompt_migration')")) == source_meta
    assert hashlib.sha256(source.path.read_bytes()).hexdigest() == original_hash
    if variant in ('edited_builtin', 'custom_exact'):
        assert actual['system_prompt'] == role['system_prompt']
        assert actual['prompt_version'] == role['prompt_version']
    else:
        assert actual['system_prompt'] == current
        assert actual['prompt_version'] == role['prompt_version'] + (variant != 'current')
