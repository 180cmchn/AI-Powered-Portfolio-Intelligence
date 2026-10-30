"""Round three: deterministic production-store tests, never real model replies."""
from ledger_helpers import save_snapshot_correction
import asyncio

import pytest

from app.domain import AccountInput
from app.service import Service, ResearchTools
from app.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'consistency.sqlite3')
    value.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    return value


@pytest.mark.asyncio
async def test_old_global_completion_cannot_replace_current_scope(store):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def runner(context, config, tools, emit):
        seen.append(context)
        if context['universe_version'] == 2:
            entered.set()
            await release.wait()
        return {'report': str(context['candidates']), 'research_records': [{
            'kind': 'market', 'title': 'same identity', 'reason': 'controlled asynchronous fixture',
            'findings': [{'classification': 'agent_analysis', 'summary': str(context['candidates'])}]}]}

    store.save_universe({'watchlist': [], 'candidates': ['SH:600519']})
    old = store.claim_global()
    task = asyncio.create_task(Service(store, runner).execute_global(old))
    await asyncio.wait_for(entered.wait(), 2)
    store.save_universe({'watchlist': [], 'candidates': ['SZ:000001']})
    latest = store.claim_global()
    assert latest and latest['universe_version'] == 3
    await Service(store, runner).execute_global(latest)
    release.set()
    await task
    current = store.research_records()
    assert len(current) == 1 and current[0]['universe_version'] == 3
    assert 'SZ:000001' in str(current[0]['findings'])
    assert store.global_run(old['id'])['applicability'] == 'stale'
    history = store.research_records(current_only=False)
    assert {r['universe_version'] for r in history} == {2, 3}
    old_report = store.global_run(old['id'])['result']['report_material_id']
    assert store.material(old_report)['classification'] == 'agent_analysis'
    assert seen[0]['candidates'] == ['SH:600519']


def test_scope_change_atomic_account_inputs_and_idempotence(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    universe = {'watchlist': [], 'candidates': ['SH:600519']}
    store.save_universe(universe)
    store.save_universe(universe)
    assert store.account(a['id'])['version'] == 2
    assert store.universe_snapshot()['universe_version'] == 2
    assert len([d for d in store.demands() if d['kind'] == 'universe_changed']) == 1


def test_citations_resolve_ranges_and_do_not_disclose_private_material(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    b = store.save_account(AccountInput(name='B', holdings=[]))
    private = store.save_material({'content': 'B secret', 'scope': 'account'}, b['latest_job_id'], b['id'])
    public = store.save_material({'text': 'selected physical page', 'scope': 'public',
        'url': 'https://example.test/annual.pdf', 'access': 'excerpt', 'document_id': 'fixture-full-bytes',
        'pages': [{'page': 28, 'text': 'selected physical page'}], 'truncated': True,
        'returned_range': {'start_page': 28, 'end_page': 28, 'offset': 0, 'chars': 22}})
    summary = store.save_material({'content': 'summary only', 'scope': 'public',
                                  'url': 'https://example.test/summary', 'access': 'search_excerpt'})
    result = store.resolve_result_references({'report': 'Link https://example.test/unread',
        'material_ids': [public['id'], summary['id'], 'does-not-exist', private['id']],
        'suggestions': [{'material_ids': [private['id'], public['id']]}],
        'research_records': [{'findings': [{'material_id': summary['id']}]}]}, a['id'])
    refs = result['references']
    by_id = {r['material_id']: r for r in refs if r.get('material_id')}
    assert by_id[public['id']]['status'] == by_id[summary['id']]['status'] == 'resolved'
    assert by_id[private['id']]['status'] == by_id['does-not-exist']['status'] == 'unresolved'
    assert by_id[public['id']]['range']['pages'][0]['page'] == 28
    assert by_id[public['id']]['range']['truncated'] is True
    assert by_id[summary['id']]['access'] == 'search_excerpt'
    assert refs[-1]['status'] == 'source_link'
    assert 'B secret' not in str(result) and 'title' not in by_id[private['id']]
    assert next(r for r in result['suggestions'][0]['references'] if r['material_id'] == private['id'])['status'] == 'unresolved'
    assert result['research_records'][0]['findings'][0]['references'][0]['status'] == 'resolved'
    assert store.resolve_result_references({'material_ids': [private['id']]})['references'][0]['status'] == 'unresolved'


def test_publication_preserves_valid_parts_and_unknown_citation_gap(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    job = store.claim_jobs()[0]
    assert store.publish(job['id'], {'report': 'deliver with gap', 'material_ids': ['unknown'],
        'suggestions': [{'security_id': 'SH:600519', 'action': 'watch', 'basis': 'uncertain', 'material_ids': ['unknown']}],
        'research_records': [{'kind': 'market', 'title': 'gap', 'reason': 'unknown ref', 'material_ids': ['unknown']}]}, None)
    assert store.analyses(a['id'])[0]['result']['references'][0]['status'] == 'unresolved'
    assert store.recommendations(a['id'])[0]['references'][0]['status'] == 'unresolved'
    assert store.research_records(a['id'])[0]['references'][0]['status'] == 'unresolved'


def test_failed_fetch_is_a_source_link_not_resolved_evidence(store):
    failed = store.save_material({'scope': 'public', 'url': 'https://example.test/unavailable', 'access': 'failed', 'error': 'controlled failure'})
    ref = store.resolve_result_references({'material_ids': [failed['id']]})['references'][0]
    assert ref['status'] == 'source_link' and ref['reason'] == 'saved_retrieval_failed'
    assert 'locator' not in ref


def test_model_extraction_is_not_raw_evidence_and_parent_references_are_scoped(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    b = store.save_account(AccountInput(name='B', holdings=[]))
    raw = store.save_material({'scope': 'public', 'text': 'raw fixture', 'document_id': 'fixture-byte-hash',
                              'url': 'https://example.test/raw', 'returned_range': {'start_page': 5, 'end_page': 5}})
    private = store.save_material({'content': 'B private', 'scope': 'account'}, account_id=b['id'])
    claim = store.save_material({'scope': 'public', 'representation': 'agent_extraction', 'content': 'model claim',
        'material_ids': [raw['id'], private['id']], 'returned_range': {'start_page': 999, 'end_page': 999}}, account_id=a['id'])
    resolved = store.resolve_result_references({'material_ids': [claim['id']]}, a['id'])
    ref = resolved['references'][0]
    assert ref['representation_kind'] == 'agent_extraction'
    assert 'returned_range' not in ref['range']
    assert ref['claimed_source_range']['returned_range']['start_page'] == 999
    parents = {r['material_id']: r for r in ref['origin_references']}
    assert parents[raw['id']]['range']['returned_range']['start_page'] == 5
    assert parents[private['id']]['status'] == 'unresolved'
    assert resolved['reference_summary']['unresolved'] == 1
    unlinked = store.save_material({'scope': 'public', 'representation': 'agent_extraction', 'content': 'no source ids'})
    assert store.resolve_result_references({'material_ids': [unlinked['id']]})['references'][0]['origin_status'] == 'unlinked'


def test_current_research_and_history_do_not_share_current_label(store):
    for n in range(3):
        store.request_analysis(None, 'fixture', f'fixture:{n}')
        run = store.claim_global()
        store.finish_global(run['id'], 'completed', {'report': str(n)})
    assert len(store.research_records()) == 1
    assert [r['applicability'] for r in store.research_records(current_only=False)].count('current') == 1


def test_account_fit_is_invalidated_without_invalidating_public_facts(store):
    a = store.save_account(AccountInput(name='account version fixture', holdings=[]))
    record = {'kind': 'candidate', 'security_id': 'SH:600519', 'title': 'scope fixture', 'reason': 'controlled'}
    store.save_research_records([record], 'public-fixture')
    job = store.claim_jobs()[0]
    store.publish(job['id'], {'report': 'account fit fixture', 'research_records': [{**record, 'account_fit': 'old input'}]}, None)
    save_snapshot_correction(store, AccountInput(name='updated preferences', holdings=[], preferences={'risk': 'conservative'}), a['id'])
    current = store.research_records(a['id'])
    assert current == []  # Public model conclusions are not account research inputs.
    assert store.research_records()[0]['scope'] == 'public'
    private = next(r for r in store.research_records(a['id'], current_only=False) if r['scope'] == 'account')
    assert private['input_version'] == 1 and private['applicability'] == 'stale'


def test_legacy_public_scope_stays_unknown_and_backup_preserves_ids(store):
    import sqlite3
    a = store.save_account(AccountInput(name='legacy account', holdings=[]))
    m = store.save_material({'content': 'legacy summary', 'scope': 'public'})
    store.save_research_records([{'kind': 'market', 'title': 'legacy', 'reason': 'old'}], 'old')
    with store.connect() as db:
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        db.execute("UPDATE research_records SET universe_version=NULL,applicability='legacy_unknown',logical_identity=NULL")
    upgraded = Store(store.path)
    assert upgraded.meta('schema_version') == 11
    assert upgraded.account(a['id'])['version'] == 1
    assert upgraded.material(m['id'])['content'] == 'legacy summary'
    assert not upgraded.research_records()
    assert upgraded.research_records(current_only=False)[0]['universe_version'] is None
    backup = store.path.with_name(store.path.name + '.before-v3.bak')
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'
    before = backup.read_bytes()
    Store(store.path)
    assert backup.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('max_calls', [1, 3])
async def test_reference_feedback_is_bounded_and_gap_does_not_block_report(store, monkeypatch, max_calls):
    import httpx
    import json
    from app import agents
    from app.model import ModelClient
    a = store.save_account(AccountInput(name='reference runtime fixture', holdings=[]))
    job = store.claim_jobs()[0]
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'fixture-response', 'status': 'completed', 'output': [{
            'type': 'function_call', 'call_id': f'finish-{len(calls)}', 'name': 'finish_analysis',
            'arguments': json.dumps({'report': 'deterministic reference fixture', 'material_ids': ['unknown']})}]})
    transport = httpx.MockTransport(handler)
    monkeypatch.setenv('REFERENCE_TEST_KEY', 'test-only')
    monkeypatch.setattr(agents, 'ModelClient', lambda config, before_request=None: ModelClient(config, before_request, transport))
    config = {**store.settings(), 'model_name': 'protocol-fixture', 'model_key_env': 'REFERENCE_TEST_KEY',
              'model_protocol': 'responses', 'max_model_calls': max_calls, 'max_agent_turns': 4}
    async def emit(event):
        pass
    result = await agents.run_research({'account': a, 'job_id': job['id'], 'input_version': 1}, config, ResearchTools(store, job, config), emit)
    assert result['report'] == 'deterministic reference fixture'
    assert result['references'][0]['status'] == 'unresolved'
    assert len(calls) == (1 if max_calls == 1 else 2)
