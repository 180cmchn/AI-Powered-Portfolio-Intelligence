"""Explicit coverage declarations through real Runtime/Store and fixture HTTP.

No supplier requests or fabricated usage; the scripted completions omit usage.
"""
import copy
import json

import pytest

from app import agents
from app.delivery import DeliveryContractError, validate_final_result
from app.domain import AccountInput
from app.store import Store, dumps, loads
from tests.delivery_helpers import delivery_arguments, final_result
from tests.test_fourteenth_chain import setup_app, execute
from tests.test_ninth_calculation_context import assert_hot_closeout, output


def terminal(report='本轮已核现金，不强迫完成其他需求。', **extra):
    return delivery_arguments(report, **extra)


def tool_defs(payload):
    return {item.get('name') or item.get('function', {}).get('name'): item.get('function', item)
            for item in payload['tools'] if item.get('type') == 'function'}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('forced', [False, True])
async def test_missing_declaration_is_one_terminal_correction_even_when_forced(tmp_path, monkeypatch, protocol, forced):
    payloads=[]
    def handler(request):
        payload=json.loads(request.content);payloads.append(payload);defs=tool_defs(payload)
        assert 'covered_demand_ids' in defs['finish_analysis']['parameters']['required']
        if len(payloads)==1:
            assert (set(defs)=={'finish_analysis'}) is forced
            return output(protocol,[('finish_analysis',terminal())])
        assert len(payloads)==2
        assert_hot_closeout(payload, payloads[0], 'finish_analysis')
        assert (set(defs)=={'finish_analysis'}) is forced
        assert ({'type': 'web_search'} in payload['tools']) is (protocol == 'responses' and not forced)
        wire=json.dumps(payload,ensure_ascii=False)
        assert 'covered_demand_ids' in wire and '未覆盖可填 []' in wire
        return output(protocol,[('finish_analysis',terminal(covered_demand_ids=[]))])
    app,store,account=setup_app(tmp_path,monkeypatch,handler,model_protocol=protocol,
        model_native_search=True,max_model_calls=None,max_agent_turns=2 if forced else None,
        synthesis_max_calls=None,analysis_timeout_seconds=30)
    demand=store.request_analysis(account['id'],'ledger_update','declaration:trade',reason='模拟卖出已入账，研究账本变化')
    saved,reports=await execute(app,store,account)
    assert saved['state']=='completed',saved.get('error')
    result=reports[0]['result'];assert result['covered_demand_ids']==[]
    assert result['submission_corrections']==1 and result['submission_outcome']=='recovered'
    assert result['demand_coverage']['uncovered']>=1
    assert next(x for x in store.demands(account['id']) if x['id']==demand['id'])['state']=='uncovered'
    events=store.job(saved['id'],events=True)['events']
    assert sum(e['type']=='terminal_submission_rejected' for e in events)==1
    observed=[q for e in events if e['type']=='model_usage' for q in e['payload']['requests']]
    assert len(observed)==2
    assert observed[-1]['native_search_requested'] is observed[0]['native_search_requested']
    assert observed[-1]['native_search_requested'] is (protocol == 'responses' and not forced)
    assert all(q['usage'] is None for q in observed)
    # A task-local required field never mutates the global schema or other roles.
    original=next(t for t in agents.MANAGER_TOOLS if t['name']=='finish_analysis')
    assert 'covered_demand_ids' not in original['parameters']['required']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_explicit_empty_is_accepted_first_time_without_forced_coverage(tmp_path,monkeypatch,protocol):
    calls=[]
    def handler(request):
        calls.append(request)
        return output(protocol,[('finish_analysis',terminal(covered_demand_ids=[]))])
    app,store,account=setup_app(tmp_path,monkeypatch,handler,model_protocol=protocol)
    store.request_analysis(account['id'],'ledger_update','empty:trade')
    saved,reports=await execute(app,store,account)
    assert saved['state']=='completed' and len(calls)==1
    assert reports[0]['result']['submission_corrections']==0
    assert reports[0]['result']['demand_coverage']['completed']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_no_frozen_demands_keeps_missing_field_compatible(tmp_path,monkeypatch,protocol):
    calls=[]
    def handler(request):
        payload=json.loads(request.content);calls.append(payload)
        assert 'covered_demand_ids' not in tool_defs(payload)['finish_analysis']['parameters']['required']
        return output(protocol,[('finish_analysis',terminal())])
    app,store,account=setup_app(tmp_path,monkeypatch,handler,model_protocol=protocol)
    saved,reports=await execute(app,store,account)
    assert saved['state']=='completed' and len(calls)==1
    assert 'covered_demand_ids' not in reports[0]['result']


def scoped_store(tmp_path, count=2):
    store=Store(tmp_path/'scope.sqlite3');store.set_meta('settings',{**store.settings(),'debounce_seconds':0})
    account=store.save_account(AccountInput(name='coverage account',cash='1000',
        holdings=[{'symbol':'SH:518880','quantity':'1','cost_price':'100'}]))
    for n in range(count):store.request_analysis(account['id'],'ledger_update',f'ledger:{n}')
    job=store.claim_jobs(account_id=account['id'])[0]
    return store,account,job


def test_store_transaction_rechecks_presence_without_marking_any_demand_read(tmp_path):
    store,account,job=scoped_store(tmp_path)
    page=store.task_demands(job['id'],account['id'],limit=1)
    with store.connect() as db:before=[tuple(x) for x in db.execute('select * from research_demand_reads')]
    candidate=final_result(terminal())
    with pytest.raises(DeliveryContractError,match='covered_demand_ids'):
        store.preflight_delivery(job,candidate)
    checked=store.preflight_delivery(job,final_result(terminal(covered_demand_ids=[])))
    checked.pop('covered_demand_ids')
    with pytest.raises(DeliveryContractError,match='covered_demand_ids'):
        store.publish(job['id'],checked,None)
    assert store.analyses(account['id'])==[]
    with store.connect() as db:assert [tuple(x) for x in db.execute('select * from research_demand_reads')]==before
    assert page['has_more']


@pytest.mark.parametrize('scope', [('account',),('SH:518880',)])
def test_explicit_claims_still_obey_read_account_and_report_scope(tmp_path,scope):
    store,account,job=scoped_store(tmp_path)
    first=store.task_demands(job['id'],account['id'],limit=1)['items'][0]['id']
    own=[x['id'] for x in store.demands(account['id'],job_id=job['id'])]
    other=store.save_account(AccountInput(name='other',cash='10'))
    foreign=store.request_analysis(other['id'],'ledger_update','foreign')['id']
    # Use the authoritative validator to test shape independently of a target
    # universe change; publication scope is separately preserved by old tests.
    candidate=final_result(terminal(scope=scope,covered_demand_ids=[*own,foreign]))
    checked=store.preflight_delivery(job,candidate)
    with store.connect() as db:
        coverage=store.complete_task_demands(db,job['id'],checked)
    assert coverage['completed']==int(scope==('account',))
    assert coverage['uncovered']==len(own)-int(scope==('account',))
    assert foreign in coverage['unaccepted_claims']
    if scope==('account',):assert first not in coverage['unaccepted_claims']
    else:assert first in coverage['unaccepted_claims']
    assert store.demands(other['id'])[0]['state']=='assigned'


@pytest.mark.parametrize('bad',[None,True,{},[True],['1'],[0],[-1]])
def test_presence_is_not_enough_for_non_integer_array(bad):
    with pytest.raises(DeliveryContractError,match='covered_demand_ids'):
        validate_final_result(final_result(terminal(covered_demand_ids=bad)),require_demand_coverage=True)


def test_global_contract_requires_declaration_before_account_early_return(tmp_path):
    store=Store(tmp_path/'global.sqlite3');store.save_universe({'watchlist':['SH:518880'],'candidates':[]})
    run=store.claim_global();assert run
    page=store.task_demands(run['id'],None);assert page['total']
    candidate=final_result(terminal(scope=('public',)))
    with pytest.raises(DeliveryContractError,match='covered_demand_ids'):store.preflight_delivery(run,candidate)
    checked=store.preflight_delivery(run,final_result(terminal(scope=('public',),covered_demand_ids=[])))
    assert checked['covered_demand_ids']==[]


def test_tool_revision_migrates_once_without_prompts_bindings_or_old_snapshot_changes(tmp_path):
    store=Store(tmp_path/'migration.sqlite3')
    store.update_agent_definition('manager',{'system_prompt':'User custom prompt must stay exact.'})
    with store.connect() as db:
        before={row['role_id']:loads(row['body']) for row in db.execute('select role_id,body from agent_definitions')}
        db.execute("delete from meta where key='seventeenth_demand_declaration_contract_migration'")
    account=store.save_account(AccountInput(name='frozen snapshot',cash='100'))
    with store.connect() as db:
        frozen=store.task_config_snapshot(db,account['id'])
        db.execute('update jobs set config_snapshot=? where account_id=?',(dumps(frozen),account['id']))
        snapshots=[tuple(x) for x in db.execute('select id,config_snapshot from jobs')]
    reopened=Store(store.path)
    with reopened.connect() as db:
        after={row['role_id']:loads(row['body']) for row in db.execute('select role_id,body from agent_definitions')}
        assert [tuple(x) for x in db.execute('select id,config_snapshot from jobs')]==snapshots
    for role_id,old in before.items():
        bump=int('finish_analysis' in old['tools'])
        assert after[role_id]['tool_version']==old['tool_version']+bump
        assert after[role_id]['version']==old['version']+bump
        for key in set(old)-{'version','tool_version','updated_at'}:assert after[role_id][key]==old[key]
    again=Store(store.path)
    assert again.meta('seventeenth_demand_declaration_contract_migration')['updated_tool_roles']==['manager']
    with again.connect() as db:assert {x['role_id']:loads(x['body']) for x in db.execute('select role_id,body from agent_definitions')}==after


@pytest.mark.parametrize('source_current', [False, True])
def test_isolated_copy_inherits_source_contract_marker_not_new_target_marker(tmp_path, source_current):
    from scripts.verify_seventeenth_live import configure_isolated
    from tests.helpers_sixth import configure_test_team
    source = Store(tmp_path / 'synthetic-source.sqlite3')
    configure_test_team(source, {**source.settings(), 'model_name': 'controlled-model', 'reasoning_effort': 'xhigh'})
    account = source.save_account(AccountInput(name='isolated contract source', cash='100'))
    source.update_agent_definition('manager', {'system_prompt': 'Exact user instruction remains unchanged.'})
    marker = 'seventeenth_demand_declaration_contract_migration'
    with source.connect() as db:
        if not source_current:
            db.execute('DELETE FROM meta WHERE key=?', (marker,))
        before = list(db.iterdump())
        roles = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
    isolated, _ = configure_isolated(source.path, tmp_path / 'isolated.sqlite3', account['id'])
    for _ in range(2):
        with isolated.connect() as db:
            actual = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
        for name, old in roles.items():
            bump = int(not source_current and 'finish_analysis' in old['tools'])
            assert actual[name]['version'] == old['version'] + bump
            assert actual[name]['tool_version'] == old['tool_version'] + bump
            for key in set(old) - {'version', 'tool_version', 'updated_at'}:
                assert actual[name][key] == old[key]
        isolated = Store(isolated.path)
    with source.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize('source_current', [False, True])
def test_isolated_copy_market_marker_tracks_exact_source_role_revision(tmp_path, source_current):
    from pathlib import Path
    from scripts.verify_seventeenth_live import configure_isolated
    from tests.helpers_sixth import configure_test_team
    source = Store(tmp_path / 'synthetic-market-source.sqlite3')
    configure_test_team(source, {**source.settings(), 'model_name': 'controlled-model', 'reasoning_effort': 'xhigh'})
    account = source.save_account(AccountInput(name='market migration source', cash='100'))
    marker = 'seventeenth_market_semantics_prompt_migration'
    originals = json.loads(Path('tests/fixtures/seventeenth-pre-semantics-prompts.json').read_text())
    with source.connect() as db:
        if not source_current:
            db.execute('DELETE FROM meta WHERE key=?', (marker,))
            for row in db.execute('SELECT * FROM agent_definitions').fetchall():
                body = loads(row['body'])
                body.update(system_prompt=originals[row['role_id']], custom=False, tool_version=1)
                db.execute('UPDATE agent_definitions SET body=? WHERE role_id=?', (dumps(body), row['role_id']))
        before = list(db.iterdump())
        roles = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
    isolated, _ = configure_isolated(source.path, tmp_path / 'isolated-market.sqlite3', account['id'])
    for _ in range(2):
        with isolated.connect() as db:
            actual = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
        for name, old in roles.items():
            expected = Path('app/prompts/' + name + '.txt').read_text()
            if name != 'manager':
                expected += '\n\n' + Path('app/prompts/researcher.txt').read_text()
            assert actual[name]['system_prompt'] == expected
            assert actual[name]['tool_version'] == old['tool_version'] + int(not source_current)
            assert actual[name]['default_binding'] == old['default_binding']
            assert actual[name]['tools'] == old['tools']
        isolated = Store(isolated.path)
    with source.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_small_final_context_retains_exact_calculation_through_missing_declaration_correction(tmp_path, monkeypatch, protocol):
    import httpx
    from app.model import ModelClient
    from tests.test_ninth_calculation_context import config, setup, seed
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=10000,
                 max_model_calls=None, max_agent_turns=3, synthesis_max_calls=None)
    store, tools, context = setup(tmp_path, cfg)
    account = context['account']
    store.request_analysis(account['id'], 'ledger_update', 'small-budget-demand')
    page = store.task_demands(context['job_id'], account['id'])
    assert page['total'] > 0
    context.update(business_demands=page['items'], business_demand_coverage={'total': page['total']})
    payloads, events = [], []
    values = ['1477619199.45', '1093814390.25']
    async def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        assert 'covered_demand_ids' in tool_defs(payload)['finish_analysis']['parameters']['required']
        if len(payloads) == 1:
            return output(protocol, [('calculate', {'operation': 'subtract', 'values': values})])
        assert_hot_closeout(payload, payloads[-2], 'finish_analysis')
        assert all(t.get('type') != 'web_search' for t in payload['tools'])
        calc = seed(payload)['calculation_summaries'][0]
        assert calc['inputs'] == values and calc['result'] == '383804809.20'
        # The configured context bound applies to session history. Chat's
        # separately supplied system prompt is not part of that history.
        history = [item for item in payload.get('input', payload.get('messages'))
                   if item.get('role') != 'system']
        assert len(json.dumps(history, ensure_ascii=False)) < cfg['max_context_chars']
        if len(payloads) == 2:
            return output(protocol, [('finish_analysis', terminal())])
        assert len(payloads) == 3
        return output(protocol, [('finish_analysis', terminal(covered_demand_ids=[]))])
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None:
        ModelClient(config, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    result = await agents.run_research(context, cfg, tools, emit)
    assert result['submission_corrections'] == 1 and result['covered_demand_ids'] == []
    assert len(payloads) == 3
    assert sum(e['type'] == 'terminal_submission_rejected' for e in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_missing_declaration_does_not_create_extra_budget_or_publish(tmp_path, monkeypatch, protocol):
    calls=[]
    def handler(request):
        calls.append(request)
        return output(protocol, [('finish_analysis', terminal())])
    app, store, account = setup_app(tmp_path, monkeypatch, handler, model_protocol=protocol,
                                   max_model_calls=1, max_agent_turns=1)
    store.request_analysis(account['id'], 'ledger_update', 'single-attempt-demand')
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'failed' and reports == [] and len(calls) == 1
    events = store.job(saved['id'], events=True)['events']
    rejected = [e for e in events if e['type'] == 'terminal_submission_rejected']
    assert len(rejected) == 1 and 'covered_demand_ids' in json.dumps(rejected)
    assert not any(e['type'] == 'published' for e in events)


@pytest.mark.parametrize('variant', ['exact_previous', 'custom_flag', 'edited_text', 'already_current'])
def test_scope_guidance_migrates_only_exact_default_once_with_tool_revision(tmp_path, variant):
    import hashlib
    from pathlib import Path
    from app.agent_config import SEVENTEENTH_DEMAND_PREVIOUS_MANAGER_PROMPT
    from tests.helpers_sixth import configure_test_team
    store = Store(tmp_path / 'scope-migration.sqlite3')
    configure_test_team(store, {**store.settings(), 'model_name': 'controlled-model', 'reasoning_effort': 'xhigh'})
    account = store.save_account(AccountInput(name='scope migration', cash='100'))
    old_prompt = Path('tests/fixtures/seventeenth-pre-demand-manager.txt').read_text()
    new_prompt = Path('app/prompts/manager.txt').read_text()
    assert hashlib.sha256(old_prompt.encode()).hexdigest() == SEVENTEENTH_DEMAND_PREVIOUS_MANAGER_PROMPT
    marker = 'seventeenth_demand_declaration_contract_migration'
    with store.connect() as db:
        role = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        role['system_prompt'] = new_prompt if variant == 'already_current' else old_prompt
        role['custom'] = variant == 'custom_flag'
        if variant == 'edited_text':
            role['system_prompt'] += '\n用户自定义精确内容。'
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(role),))
        db.execute('DELETE FROM meta WHERE key=?', (marker,))
        frozen = store.task_config_snapshot(db, account['id'])
        db.execute('UPDATE jobs SET config_snapshot=? WHERE account_id=?', (dumps(frozen), account['id']))
        old_jobs = [tuple(r) for r in db.execute('SELECT id,config_snapshot FROM jobs')]
        old_roles = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
    updated = Store(store.path)
    with updated.connect() as db:
        roles = {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')}
        assert [tuple(r) for r in db.execute('SELECT id,config_snapshot FROM jobs')] == old_jobs
    after = roles['manager']
    assert after['system_prompt'] == (new_prompt if variant == 'exact_previous' else role['system_prompt'])
    assert after['prompt_version'] == role['prompt_version'] + int(variant == 'exact_previous')
    assert after['version'] == role['version'] + 1
    assert after['tool_version'] == role['tool_version'] + 1
    for key in set(role) - {'version', 'tool_version', 'prompt_version', 'system_prompt', 'updated_at'}:
        assert after[key] == role[key]
    assert {k: v for k, v in roles.items() if k != 'manager'} == {k: v for k, v in old_roles.items() if k != 'manager'}
    assert updated.meta(marker)['updated_prompt_roles'] == (['manager'] if variant == 'exact_previous' else [])
    again = Store(store.path)
    with again.connect() as db:
        assert {r['role_id']: loads(r['body']) for r in db.execute('SELECT * FROM agent_definitions')} == roles


@pytest.mark.parametrize('scope,declared,completed', [(('SZ:159278',), True, 0), (('public',), True, 1), (('public',), False, 0)])
def test_single_candidate_does_not_expand_local_scope_to_whole_public_demand(tmp_path, scope, declared, completed):
    store = Store(tmp_path / 'one-candidate.sqlite3')
    store.save_universe({'watchlist': [], 'candidates': ['SZ:159278']})
    task = store.claim_global()
    demands = store.task_demands(task['id'], None)['items']
    assert len(demands) == 1 and demands[0]['kind'] == 'universe_changed'
    ids = [demands[0]['id']]
    body = final_result(terminal(scope=scope, covered_demand_ids=ids if declared else []))
    assert store.finish_global(task['id'], 'completed', body)
    result = store.global_run(task['id'])['result']
    coverage = result['demand_coverage']
    assert coverage['completed'] == completed and coverage['uncovered'] == 1 - completed
    assert coverage['unaccepted_claims'] == (ids if declared and not completed else [])
    definition = next(t for t in agents.MANAGER_TOOLS if t['name'] == 'finish_analysis')
    assert 'target_security' in definition['parameters']['properties']['scope']['description']
    assert '局部证券scope不能声明完成' in definition['parameters']['properties']['covered_demand_ids']['description']
