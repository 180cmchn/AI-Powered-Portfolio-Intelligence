"""Controlled regressions for extraction provenance and recommendation delivery."""
from copy import deepcopy

import pytest

from app import references
from app.delivery import DeliveryContractError, DeliveryStateConflict, validate_structured_delivery
from app.domain import AccountInput
from app.service import ResearchTools
from app.store import Store
from tests.delivery_helpers import final_result


A, B = 'SZ:159599', 'SZ:002354'
URL = 'https://fixture.invalid/eighteenth'


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'eighteenth.sqlite3', mode='live')
    value.set_meta('settings', {**value.settings(), 'debounce_seconds': 0, 'quote_provider': 'manual'})
    return value


def test_direct_summary_excludes_legacy_origins_but_counts_nested_direct_citations(store):
    # Seed an immutable extraction from before the ResearchTools boundary fix.
    legacy = store.save_material({'scope': 'public', 'representation': 'agent_extraction',
        'text': 'Legacy extraction', 'material_ids': ['web:15']})
    payload = {'report': '受控报告', 'findings': [{'material_ids': [legacy['id']], 'url': URL}],
        'direct_reference_summary': {'unresolved': 99}}
    resolved = store.resolve_result_references(payload)
    citation = resolved['findings'][0]['references'][0]
    assert citation['status'] == 'resolved'
    assert citation['origin_references'] == [{'material_id': 'web:15', 'status': 'unresolved',
        'reason': 'unknown_or_inaccessible'}]
    assert resolved['reference_summary'] == references.reference_counts(resolved) == {
        'resolved': 1, 'source_link': 1, 'unresolved': 1}
    assert resolved['direct_reference_summary'] == references.direct_reference_counts(resolved) == {
        'resolved': 1, 'source_link': 1, 'unresolved': 0}

    with_unknown = store.resolve_result_references({**payload, 'material_ids': ['missing-direct']})
    assert with_unknown['reference_summary']['unresolved'] == 2
    assert with_unknown['direct_reference_summary']['unresolved'] == 1
    assert store.material(legacy['id'])['material_ids'] == ['web:15']


@pytest.mark.asyncio
async def test_save_extraction_keeps_real_ids_and_retains_search_labels_as_metadata(store):
    tools = ResearchTools(store, {'id': 'controlled-public-job', 'account_id': None}, {})
    parent = store.save_material({'scope': 'public', 'text': 'Genuine source'})['id']
    parents = sorted([parent, 'unknown-explicit', 'web:7-suffix'])
    plain = {'text': 'Same extraction text', 'url': URL, 'material_ids': parents}
    original = await tools.save_material(plain)
    material = {**plain, 'material_ids': [parent, ' WEB:1 ', 'search:2', 'source:3', 'unknown-explicit'],
        'source_material_ids': [' SEARCH:2 ', 'ref:4', 'citation:5', 'doc:6', 'web:7-suffix'],
        'unlinked_source_labels': ['handwritten', 'web:1']}
    before = deepcopy(material)
    saved = await tools.save_material(material)
    body = store.material(saved['id'])
    assert material == before
    assert body['url'] == URL
    assert body['material_ids'] == body['source_material_ids'] == parents
    assert body['unlinked_source_labels'] == ['handwritten', 'web:1', 'search:2', 'source:3', 'ref:4', 'citation:5', 'doc:6']
    assert saved['unlinked_source_labels'] == body['unlinked_source_labels']
    assert body['document_version_id'] == original['document_version_id']
    assert store.material(original['id']).get('unlinked_source_labels', []) == []
    assert (await tools.save_material(material))['id'] == saved['id']
    origin = (await tools.resolve_references({'material_ids': [saved['id']]}))['references'][0]['origin_references']
    assert {ref['material_id']: ref['status'] for ref in origin} == {
        parent: 'resolved', 'unknown-explicit': 'unresolved', 'web:7-suffix': 'unresolved'}


def suggestion(sid=A, **fields):
    return {'security_id': sid, 'action': 'watch', 'basis': '受控测试依据', **fields}


def submission(*suggestions, **fields):
    return final_result({'report': '受控报告', 'covered_demand_ids': [], 'suggestions': list(suggestions), **fields})


@pytest.fixture
def expired_delivery(store, frozen_clock):
    frozen_clock.set('2026-11-02T10:00:00+08:00')
    account = store.save_account(AccountInput(name='受控交付账户', cash='100',
        holdings=[{'symbol': sid, 'quantity': '1', 'cost_price': '1'} for sid in (A, B)]))
    first = store.claim_jobs(account_id=account['id'])[0]
    assert store.publish(first['id'], submission(suggestion(valid_until='2026-11-02T11:00:00+08:00'), suggestion(B)), None)
    ids = {item['security_id']: item['id'] for item in store.recommendations(account['id'])}
    frozen_clock.set('2026-11-02T12:00:00+08:00')
    assert store.review_recommendations()[0]['state'] == 'expired'
    job = store.claim_jobs(account_id=account['id'])[0]
    return job, ids[A], ids[B]


def test_expired_id_preflights_and_publishes_new_revision_with_history(store, expired_delivery):
    job, expired, active = expired_delivery
    prior = store.recommendation(job['account_id'], expired)
    assert prior['review_followup']['state'] == 'assigned' and prior['review_followup']['job_id'] == job['id']
    events = store.recommendation_events(expired)
    payload = submission(suggestion(id=expired), suggestion(B, id=active))
    checked = store.preflight_delivery(job, payload)
    assert payload['suggestions'][0]['id'] == expired
    assert 'id' not in checked['suggestions'][0]
    assert checked['suggestions'][0]['supersedes_expired_id'] == expired
    assert checked['suggestions'][1]['id'] == active
    assert store.preflight_delivery(job, checked) == checked

    assert store.publish(job['id'], checked, None)
    receipt = store.job(job['id'])['result']['delivery_application']['suggestions']
    assert [(item['index'], item['security_id'], item['status']) for item in receipt] == [(0, A, 'applied'), (1, B, 'applied')]
    replacement = store.recommendation(job['account_id'], receipt[0]['recommendation_id'])
    assert replacement['id'] != expired and replacement['state'] == 'watching' and replacement['revision'] == 1
    assert replacement['replaces_ids'] == [expired]
    assert receipt[0]['superseded_expired_id'] == expired
    assert receipt[1]['recommendation_id'] == active and 'superseded_expired_id' not in receipt[1]
    assert store.recommendation(job['account_id'], active)['revision'] == 2
    history = store.recommendation(job['account_id'], expired)
    assert history['state'] == history['review_status'] == 'expired' and history['replaced_by_id'] == replacement['id']
    assert history['job_id'] == prior['job_id'] and history['basis'] == prior['basis']
    assert history['review_followup']['state'] == 'completed'
    assert store.recommendation_events(expired) == events
    assert store.publish(job['id'], checked, None)
    assert store.recommendations_page(job['account_id'], view='all')['total'] == 3


@pytest.mark.parametrize(('late_same_recommendation', 'expired_replacement'), [(False, False), (True, False), (False, True)])
def test_replacement_closes_only_pending_expiry_events(store, frozen_clock, late_same_recommendation, expired_replacement):
    frozen_clock.set('2026-11-02T10:00:00+08:00')
    account = store.save_account(AccountInput(name='受控交付账户', cash='100',
        holdings=[{'symbol': sid, 'quantity': '1', 'cost_price': '1'} for sid in (A, B)]))
    account_id = account['id']
    first, = store.claim_jobs(account_id=account_id)
    assert store.publish(first['id'], submission(suggestion(valid_until='2026-11-02T11:00:00+08:00'), suggestion(B)), None)
    ids = {item['security_id']: item['id'] for item in store.recommendations(account_id)}
    store.submit_task(account_id)
    running, = store.claim_jobs(account_id=account_id)
    frozen_clock.set('2026-11-02T12:00:00+08:00')
    store.review_recommendations(material_ids=['late-material'])

    expiry_key = f'recommendation:{ids[A]}:expired'
    late_key = f'recommendation:{ids[B]}:material:late-material'
    same_key = f'recommendation:{ids[A]}:material:later-on-same-suggestion'
    prior = store.recommendation(account_id, ids[A])
    assert prior['state'] == 'expired' and prior['review_pending_since']
    assert prior['review_followup']['event_key'] == expiry_key
    assert prior['review_followup']['state'] == 'pending'
    with store.connect() as db:
        db.execute('''INSERT INTO research_demands
            (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
            SELECT ?,account_id,input_version,kind,reason,state,created_at,updated_at
            FROM research_demands WHERE event_key=?''', (expiry_key + ':recheck', expiry_key))
        if late_same_recommendation:
            db.execute('''INSERT INTO research_demands
                (event_key,account_id,input_version,kind,reason,state,created_at,updated_at)
                SELECT ?,account_id,input_version,kind,reason,state,created_at,updated_at
                FROM research_demands WHERE event_key=?''', (same_key, late_key))
        assert db.execute('SELECT state FROM research_demands WHERE event_key=?', (late_key,)).fetchone()[0] == 'pending'

    replacement_expiry = {'valid_until': '2026-11-02T11:30:00+08:00'} if expired_replacement else {}
    assert store.publish(running['id'], submission(suggestion(id=ids[A], **replacement_expiry), suggestion(B, id=ids[B])), None)
    history = store.recommendation(account_id, ids[A])
    assert history['state'] == history['review_status'] == 'expired'
    replacement = store.recommendation(account_id, history['replaced_by_id'])
    assert replacement['id'] != ids[A] and replacement['replaces_ids'] == [ids[A]]
    assert replacement['state'] == ('expired' if expired_replacement else 'watching')
    with store.connect() as db:
        demands = {r['event_key']: dict(r) for r in db.execute('SELECT event_key,state,detail FROM research_demands')}
    for key in (expiry_key, expiry_key + ':recheck'):
        assert demands[key]['state'] == ('pending' if expired_replacement else 'completed')
        if not expired_replacement:
            assert demands[key]['detail']
    if expired_replacement:
        assert demands[expiry_key]['detail'] == prior['review_followup']['detail']
    assert ('review_pending_since' in history) is (expired_replacement or late_same_recommendation)
    assert demands[late_key]['state'] == 'pending'
    if late_same_recommendation:
        assert demands[same_key]['state'] == 'pending'


def test_expired_id_cannot_act_on_history_or_bypass_new_suggestion_conflicts(store, expired_delivery):
    job, expired, active = expired_delivery
    for field in ('maintained_suggestion_ids', 'review_suggestion_ids', 'revoked_suggestion_ids'):
        with pytest.raises(DeliveryContractError, match=rf'{field}\[0\]'):
            store.preflight_delivery(job, submission(**{field: [expired]}))
    for invalid in (suggestion(B, id=expired), suggestion(id='unknown'), suggestion(id=active)):
        with pytest.raises(DeliveryContractError, match=r'suggestions\[0\]\.id: 必须属于'):
            store.preflight_delivery(job, submission(invalid))

    other = store.save_account(AccountInput(name='受控隔离账户', cash='100',
        holdings=[{'symbol': A, 'quantity': '1', 'cost_price': '1'}]))
    foreign_job = store.claim_jobs(account_id=other['id'])[0]
    for field in ('id', 'supersedes_expired_id'):
        with pytest.raises(DeliveryContractError, match=rf'suggestions\[0\]\.{field}: 必须属于'):
            store.preflight_delivery(foreign_job, submission(suggestion(**{field: expired})))

    rejected = submission(suggestion(id=expired), suggestion(B, id='unknown'))
    before = deepcopy(rejected)
    with pytest.raises(DeliveryContractError, match=r'suggestions\[1\]\.id'):
        store.preflight_delivery(job, rejected)
    assert rejected == before

    with store.connect() as db:
        context = store.delivery_contract_context(job, db)
    context['recommendations']['same-security-active'] = {'security_id': A, 'state': 'watching'}
    for field in ('maintained_suggestion_ids', 'review_suggestion_ids'):
        with pytest.raises(DeliveryContractError, match=rf'suggestions\[0\]\.security_id: .*{field}'):
            validate_structured_delivery(submission(suggestion(id=expired), **{field: ['same-security-active']}),
                                         contract_context=context)
    allowed = submission(suggestion(id=expired), revoked_suggestion_ids=['same-security-active'])
    validate_structured_delivery(allowed, contract_context=context)
    assert allowed['suggestions'][0]['supersedes_expired_id'] == expired


def test_publication_rechecks_normalized_expired_id_and_keeps_failed_intent(store, expired_delivery):
    job, expired, _ = expired_delivery
    checked = store.preflight_delivery(job, submission(suggestion(id=expired)))
    with store.connect() as db:
        db.execute("UPDATE recommendations SET state='revoked' WHERE id=?", (expired,))
    with pytest.raises(DeliveryStateConflict, match=r'suggestions\[0\]\.supersedes_expired_id'):
        store.publish(job['id'], checked, None)
    store.retain_delivery_candidate(job['id'], checked, '受控状态冲突')
    candidate = store.job(job['id'])['result']
    assert candidate['suggestions'][0]['supersedes_expired_id'] == expired
    receipt = candidate['delivery_application']['suggestions'][0]
    assert (receipt['index'], receipt['security_id'], receipt['status']) == (0, A, 'not_applied')
    assert store.recommendations_page(job['account_id'], view='all')['total'] == 2
