"""Sixth iteration: production stores/worker with explicit controlled input fixtures."""
import json

import pytest

from app.domain import AccountInput
from app.service import ResearchTools, Service
from app.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'sixth-repairs.sqlite3')
    value.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
                                'scheduled_research_enabled': False})
    return value


@pytest.mark.asyncio
async def test_150_demands_production_service_pages_and_explicit_coverage(store):
    account = store.save_account(AccountInput(name='Coverage fixture', holdings=[]))
    for index in range(150):
        store.request_analysis(account['id'], 'scheduled', f'fixture-demand:{index}', reason=f'研究事项 {index}')
    job = store.claim_jobs(account_id=account['id'])[0]
    visited = []

    async def runner(context, config, tools, emit):
        assert context['business_demand_coverage']['total'] == 150
        assert len(context['business_demands']) == 25
        assert context['business_demand_coverage']['has_more']
        visited.extend(context['business_demands'])
        # Concurrent account work cannot displace this task's own demand pages.
        for index in range(120):
            store.request_analysis(account['id'], 'external', f'fixture-late:{index}')
        offset = 25
        while offset is not None:
            page = await tools.list_business_demands(limit=50, offset=offset)
            assert page['total'] == 150
            visited.extend(page['items'])
            offset = page['next_offset']
        return {'report': 'Controlled 100-item coverage, 50 still unresolved',
                'covered_demand_ids': [row['id'] for row in visited[:100]]}

    service = Service(store, runner)
    await service.execute(job)
    assert len(visited) == len({item['id'] for item in visited}) == 150
    rows = store.demands(account['id'], limit=1000, job_id=job['id'])
    assert sum(row['state'] == 'completed' for row in rows) == 100
    assert sum(row['state'] == 'uncovered' for row in rows) == 50
    report = store.analyses(account['id'])[0]['result']
    assert report['demand_coverage']['expected'] == 150
    assert report['demand_coverage']['completed'] == 100
    assert report['demand_coverage']['uncovered'] == 50
    store.publish(job['id'], {'report': 'retry', 'covered_demand_ids': [row['id'] for row in rows]}, None)
    assert sum(row['state'] == 'completed' for row in store.demands(account['id'], 1000, job_id=job['id'])) == 100
    reopened = Store(store.path)
    # Explicit retry includes uncovered work; ordinary dispatcher does not retry it endlessly.
    retry = reopened.enqueue(account['id'], 'manual')
    page = reopened.task_demands(retry['id'], account['id'])
    assert page['total'] == 170


@pytest.mark.asyncio
async def test_unread_claim_and_other_task_ids_cannot_complete_work_or_repeat_forever(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    b = store.save_account(AccountInput(name='B', holdings=[]))
    for index in range(30):
        store.request_analysis(a['id'], 'scheduled', f'a:{index}')
    foreign = store.request_analysis(b['id'], 'scheduled', 'b:foreign')
    job = store.claim_jobs(account_id=a['id'])[0]
    async def runner(context, config, tools, emit):
        rows = store.demands(a['id'], job_id=job['id'])
        return {'report': 'No page 2 read', 'covered_demand_ids': [rows[0]['id'], foreign['id']]}
    await Service(store, runner).execute(job)
    assert all(row['state'] == 'uncovered' for row in store.demands(a['id']))
    assert store.demands(b['id'])[0]['state'] == 'assigned'
    for _ in range(3):
        store.dispatch_demands()
        assert store.claim_jobs(account_id=a['id']) == []


def observed(store, content, *, text=None, observation=None, **extra):
    url = 'https://fixture.example/report'
    observation = observation or store.begin_material_observation(url)
    return store.save_material({'scope': 'public', 'url': url, 'document_id': content,
        'text': text or content, 'access': 'full_text', 'truncated': False, **extra}, observation_id=observation)


def checkpoint(store, source, *, team='old-team', context=None):
    account = store.save_account(AccountInput(name='Recovery fixture', holdings=[]))
    job = store.claim_jobs(account_id=account['id'])[0]
    context = context or {'account': account, 'universe_version': store.universe_snapshot()['universe_version'],
                         'metrics': {'positions': []}, 'team_snapshot': {'id': team}}
    receipts = store.record_material_delivery(job['id'], account['id'], 'child', 'get_material',
        store.material_range(source['id']))
    body = {'phase': 'synthesis', 'reports': [{'summary': 'Observed v1 report', 'team_id': team,
        'agent_id': 'child', 'material_ids': [source['id']], 'material_delivery_ids': receipts}],
            'material_ids': [source['id']], 'team_snapshot': {'id': team}}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job['id'], 'failed', 'controlled transient synthesis failure')
    return job, context


@pytest.mark.parametrize('arrival', ['before_checkpoint', 'after_checkpoint'])
def test_checkpoint_records_read_v1_not_latest_v2(store, arrival):
    first = observed(store, 'v1')
    if arrival == 'before_checkpoint':
        second = observed(store, 'v2')
    job, context = checkpoint(store, first)
    if arrival == 'after_checkpoint':
        second = observed(store, 'v2')
    with store.connect() as db:
        body = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0])
    entry = body['verified_material_versions'][first['id']]
    assert entry['read_document_version_id'] == first['document_version_id']
    assert entry['read_document_version_id'] != second['document_version_id']
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_unchanged_document_new_fragment_and_late_observation_preserve_reuse(store):
    token = store.begin_material_observation('https://fixture.example/report')
    first = observed(store, 'v1', text='page one', observation=token, returned_range={'start_page': 1})
    job, context = checkpoint(store, first)
    page_two = observed(store, 'v1', text='page two', observation=token, returned_range={'start_page': 2})
    assert first['id'] != page_two['id']
    assert first['document_version_id'] == page_two['document_version_id']
    resumed = store.reusable_synthesis_checkpoint(job, {**context, 'team_snapshot': {'id': 'new-team'}})
    assert resumed['reports'][0]['team_id'] == 'old-team'
    assert resumed['configuration_relation']['previous_team_snapshot']['id'] == 'old-team'
    assert resumed['configuration_relation']['current_team_snapshot']['id'] == 'new-team'
    guard = resumed['verified_material_versions'][first['id']]
    assert guard['read_range']['returned_range'] == {'start_page': 1}


def test_late_old_observation_does_not_invalidate_current_but_real_rollback_does(store):
    first = observed(store, 'v1')
    late_token = store.begin_material_observation('https://fixture.example/report')
    current = observed(store, 'v2')
    job, context = checkpoint(store, current)
    late = observed(store, 'v1', observation=late_token)
    assert late['document_version_id'] == first['document_version_id']
    assert store.reusable_synthesis_checkpoint(job, context)
    rollback = observed(store, 'v1')
    assert rollback['document_version'] == 3
    assert store.reusable_synthesis_checkpoint(job, context) is None


@pytest.mark.asyncio
async def test_generated_material_and_parent_chains_never_cross_account_scope(store):
    a = store.save_account(AccountInput(name='A', holdings=[]))
    b = store.save_account(AccountInput(name='B', holdings=[]))
    job_a = store.claim_jobs(account_id=a['id'])[0]
    job_b = store.claim_jobs(account_id=b['id'])[0]
    tools_a = ResearchTools(store, job_a, store.settings())
    tools_b = ResearchTools(store, job_b, store.settings())
    raw = observed(store, 'Shared actual source')
    extracted = await tools_a.save_material({'scope': 'public', 'content': 'A private conclusion',
        'material_ids': [raw['id']], 'representation': 'original_document'})
    public = store.save_material({'scope': 'public', 'content': 'Global investment judgment',
                                 'classification': 'agent_analysis', 'access': 'generated_report'})
    own = await tools_b.save_material({'content': 'B own conclusion', 'material_ids': [raw['id'], extracted['id'], public['id']]})
    # A historical accidental material-use link does not become an access grant.
    with store.connect() as db:
        db.execute('INSERT INTO material_uses VALUES(?,?,?)', (extracted['id'], job_b['id'], b['id']))
    visible = {item['id'] for item in await tools_b.list_materials()}
    assert {raw['id'], own['id']} <= visible
    assert not {extracted['id'], public['id']} & visible
    assert not await tools_b.search_materials('A private conclusion')
    for material_id in [extracted['id'], public['id']]:
        with pytest.raises(KeyError):
            await tools_b.get_material(material_id)
    refs = store.resolve_result_references({'material_ids': [own['id']]}, b['id'])
    origins = {ref['material_id']: ref for ref in refs['references'][0]['origin_references']}
    assert origins[raw['id']]['status'] == 'resolved'
    assert origins[extracted['id']]['reason'] == origins[public['id']]['reason'] == 'unknown_or_inaccessible'
    store.save_research_records([{'kind': 'market', 'title': 'global', 'reason': 'private generated global conclusion'}], 'public-job')
    assert not store.research_records(b['id'])


@pytest.mark.asyncio
async def test_extraction_checkpoint_checks_actual_parent_version(store):
    source = observed(store, 'v1')
    a = store.save_account(AccountInput(name='Parent guard', holdings=[]))
    job = store.claim_jobs(account_id=a['id'])[0]
    tools = ResearchTools(store, job, store.settings())
    extraction = await tools.save_material({'content': 'Derived from v1', 'material_ids': [source['id']]})
    observed(store, 'v2')
    context = {'account': a, 'universe_version': store.universe_snapshot()['universe_version']}
    store.save_synthesis_checkpoint(job, {'material_ids': [extraction['id']]}, context)
    store.set_job_state(job['id'], 'failed', 'controlled')
    with store.connect() as db:
        body = json.loads(db.execute('SELECT body FROM synthesis_checkpoints').fetchone()[0])
    assert body['verified_material_versions'][source['id']]['read_document_version_id'] == source['document_version_id']
    assert store.reusable_synthesis_checkpoint(job, context) is None
