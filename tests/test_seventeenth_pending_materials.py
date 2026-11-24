"""Unsent completed tool outputs are guarded evidence, never old read receipts."""
import copy
import json

import pytest

from app.domain import AccountInput
from app.recovery import checkpoint_material_evidence_issues, restored_manager_material_evidence
from app.store import Store


def pending_case(tmp_path):
    store = Store(tmp_path / 'pending.sqlite3')
    store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0,
        'max_model_calls': None, 'max_agent_turns': None, 'synthesis_max_calls': None})
    account = store.save_account(AccountInput(name='Independent pending fixture', holdings=[], research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    material = store.save_material({'scope': 'account', 'url': 'https://issuer.example/pending',
        'text': 'Actual completed visible content, before the next HTTP is accepted.'}, job['id'], account['id'])
    result = store.material_range(material['id'], start=0, length=24)
    pending = store.prepare_pending_tool_output(job['id'], account['id'], 'manager', 'get_material', result)
    context = {'account': account, 'universe_version': store.universe_snapshot()['universe_version'],
        'metrics': {'positions': []}}
    body = {'phase': 'synthesis', 'task_id': job['id'], 'account_id': account['id'],
        'input_version': job['input_version'], 'pending_tool_outputs': [pending], 'reports': []}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job['id'], 'failed', 'next HTTP never accepted input')
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0])
    return store, account, job, material, result, context, saved


def test_pending_source_is_saved_guarded_and_restored_without_old_delivery(tmp_path):
    store, account, job, material, result, context, saved = pending_case(tmp_path)
    assert store.checkpoint_deliveries(job['id']) == []
    assert saved['recovery_state'] == 'needs_research'
    assert saved['material_ids'] == [material['id']]
    assert not checkpoint_material_evidence_issues(saved)
    assert store.checkpoint_status(job['id'])['completed_not_delivered_outputs'] == 1
    evidence = restored_manager_material_evidence(saved)
    assert len(evidence) == 1 and evidence[0]['result'] == result
    assert evidence[0]['visibility'] == 'completed_not_delivered'
    assert evidence[0]['source_delivery_id'] is None and evidence[0]['previously_delivered'] is False
    store.submit_task(account['id'], parent_job_id=job['id'], research_scope='cash_only')
    retry = store.claim_jobs(account_id=account['id'])[0]
    cp = store.reusable_synthesis_checkpoint(retry, context, continue_research=True)
    assert cp['recovery_mode'] == 'continue_research'
    assert cp['checkpoint_job_id'] == job['id']
    # The new endpoint has now accepted precisely the bounded restored object.
    receipts = store.record_material_delivery(retry['id'], account['id'], 'manager', 'get_material', result)
    assert len(receipts) == 1
    store.save_synthesis_checkpoint(retry, {**cp, 'task_id': retry['id']}, context)
    with store.connect() as db:
        current = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (retry['id'],)).fetchone()[0])
    assert current['pending_tool_outputs'] == []
    assert current['material_deliveries'][0]['job_id'] == retry['id']
    assert store.checkpoint_deliveries(job['id']) == []
    assert restored_manager_material_evidence(current)[0]['previously_delivered'] is True


@pytest.mark.parametrize('change', ['hash', 'guard', 'status', 'correction', 'scope'])
def test_pending_output_cannot_bypass_source_hash_version_or_scope(tmp_path, change):
    store, account, job, material, result, context, saved = pending_case(tmp_path)
    if change in ('hash', 'guard', 'status'):
        changed = copy.deepcopy(saved)
        output = changed['pending_tool_outputs'][0]
        if change == 'hash': output['result']['text'] = 'fabricated larger content'
        if change == 'guard': output['source_guard'] = {}
        if change == 'status': output['previously_delivered'] = True
        assert checkpoint_material_evidence_issues(changed)
        assert restored_manager_material_evidence(changed) == []
        return
    if change == 'correction':
        store.save_material({'scope': 'account', 'url': material['url'], 'text': 'New corrected content'}, job['id'], account['id'])
        store.submit_task(account['id'], parent_job_id=job['id'], research_scope='cash_only')
        retry = store.claim_jobs(account_id=account['id'])[0]
    else:
        other = store.save_account(AccountInput(name='Separate account', holdings=[], research_scope='cash_only'))
        retry = store.claim_jobs(account_id=other['id'])[0]
        context = {**context, 'account': other}
        assert store.prepare_pending_tool_output(retry['id'], other['id'], 'manager', 'get_material', result) is None
    assert store.reusable_synthesis_checkpoint(retry, context, continue_research=True) is None


def test_half_result_and_undelivered_child_do_not_become_pending_manager_work(tmp_path):
    store, account, job, material, result, context, saved = pending_case(tmp_path)
    for bad in ({**result, 'failed': True}, {**result, 'truncated_for_context': True}):
        assert store.prepare_pending_tool_output(job['id'], account['id'], 'manager', 'get_material', bad) is None
    assert store.prepare_pending_tool_output(job['id'], account['id'], 'child', 'get_material', result) is None
    assert store.checkpoint_deliveries(job['id']) == []


def advancing_head_case(tmp_path):
    """Production pending/save path with 8/12/16-period views of one stream.

    Mirrors the stopped live checkpoint: two pending reads of the same immutable
    8-period material captured different available heads before checkpoint save.
    All values are controlled local fixtures; no live database is modified.
    """
    store = Store(tmp_path / 'head-advancement.sqlite3')
    store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0,
        'max_model_calls': None, 'max_agent_turns': None, 'synthesis_max_calls': None})
    account = store.save_account(AccountInput(name='Head advancement fixture', holdings=[], research_scope='cash_only'))
    job = store.claim_jobs(account_id=account['id'])[0]
    rows = [{'report_period': f'controlled-period-{i:02d}', 'revenue': str(100-i)} for i in range(16)]
    def save_view(count):
        return store.save_material({'scope': 'account', 'access': 'structured_data',
            'source_identifier': 'controlled:fundamentals:one-source', 'source_title': 'fundamentals',
            'content': json.dumps({'status': 'ok', 'rows': rows[:count]})}, job['id'], account['id'])
    old = save_view(8)
    result = {'material_id': old['id'], 'status': 'ok', 'rows': rows[:8]}
    first = store.prepare_pending_tool_output(job['id'], account['id'], 'manager', 'fundamentals', result)
    middle = save_view(12)
    excerpt = store.material_range(old['id'], start=0, length=64)
    second = store.prepare_pending_tool_output(job['id'], account['id'], 'manager', 'read_material_range', excerpt)
    latest = save_view(16)
    context = {'account': account, 'universe_version': store.universe_snapshot()['universe_version'],
        'metrics': {'positions': []}}
    body = {'phase': 'synthesis', 'task_id': job['id'], 'account_id': account['id'],
        'input_version': job['input_version'], 'pending_tool_outputs': [first, second], 'reports': []}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job['id'], 'failed', 'controlled shutdown before accepted input')
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0])
    return store, account, job, old, middle, latest, result, excerpt, context, saved


def test_pending_historical_read_survives_head_advance_but_only_as_continue_research(tmp_path):
    store, account, job, old, middle, latest, result, excerpt, context, saved = advancing_head_case(tmp_path)
    original = copy.deepcopy(saved)
    mid = old['id']
    pending = saved['pending_tool_outputs']
    assert pending[0]['source_guard'][mid]['latest_available_document_version_id'] == old['document_version_id']
    assert pending[1]['source_guard'][mid]['latest_available_document_version_id'] == middle['document_version_id']
    verified = saved['verified_material_versions'][mid]
    assert verified['latest_available_document_version_id'] == latest['document_version_id']
    assert all(item['source_guard'][mid]['read_document_version_id'] == old['document_version_id'] for item in pending)
    assert verified['read_document_version_id'] == old['document_version_id']
    assert not checkpoint_material_evidence_issues(saved)
    assert saved['recovery_state'] == 'needs_research'
    coverage = saved['source_coverage'][verified['source_stream']]
    assert coverage['status'] == 'needs_research'
    assert coverage['latest_material_ids'] == [] and mid in coverage['historical_material_ids']
    evidence = restored_manager_material_evidence(saved)
    assert [item['result'] for item in evidence] == [result, excerpt]
    assert all(item['previously_delivered'] is False and item['source_delivery_id'] is None for item in evidence)
    assert store.checkpoint_deliveries(job['id']) == []
    store.submit_task(account['id'], parent_job_id=job['id'], research_scope='cash_only')
    retry = store.claim_jobs(account_id=account['id'])[0]
    # Accepting immutable old evidence never authorizes synthesis-only recovery.
    assert store.reusable_synthesis_checkpoint(retry, context) is None
    resumed = store.reusable_synthesis_checkpoint(retry, context, continue_research=True)
    assert resumed['recovery_mode'] == 'continue_research'
    assert resumed['verified_material_versions'] == saved['verified_material_versions']
    assert resumed['pending_tool_outputs'] == pending
    assert resumed['source_coverage'][verified['source_stream']]['status'] == 'needs_research'
    assert saved == original
    with store.connect() as db:
        stored = json.loads(db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0])
    assert stored == original


@pytest.mark.parametrize('field', ['source_stream', 'source_key', 'read_document_version_id',
    'read_document_version', 'representation_hash', 'read_range', 'representation_kind', 'parent_material_ids'])
def test_pending_guard_still_rejects_every_immutable_field_change(tmp_path, field):
    *_, saved = advancing_head_case(tmp_path)
    changed = copy.deepcopy(saved)
    item = changed['pending_tool_outputs'][0]
    item['source_guard'][item['material_id']][field] = 'changed-controlled-fixture'
    assert 'pending_tool_output_guard_incomplete_or_changed' in checkpoint_material_evidence_issues(changed)
    assert restored_manager_material_evidence(changed) == []


def test_pending_guard_does_not_accept_missing_head_field_or_post_save_head_change(tmp_path):
    store, account, job, old, middle, latest, result, excerpt, context, saved = advancing_head_case(tmp_path)
    changed = copy.deepcopy(saved)
    item = changed['pending_tool_outputs'][0]
    del item['source_guard'][item['material_id']]['latest_available_document_version_id']
    assert 'pending_tool_output_guard_incomplete_or_changed' in checkpoint_material_evidence_issues(changed)
    # A correction after checkpoint save must still invalidate its saved current
    # guard. This differs from capturing a known head advance before save.
    store.save_material({'scope': 'account', 'access': 'structured_data',
        'source_identifier': 'controlled:fundamentals:one-source', 'source_title': 'fundamentals',
        'content': json.dumps({'status': 'ok', 'rows': [{'report_period': 'controlled-period-00', 'revenue': '999'}]})},
        job['id'], account['id'])
    store.submit_task(account['id'], parent_job_id=job['id'], research_scope='cash_only')
    retry = store.claim_jobs(account_id=account['id'])[0]
    assert store.checkpoint_material_guard(list(saved['verified_material_versions']), account['id']) != saved['verified_material_versions']
    assert store.reusable_synthesis_checkpoint(retry, context, continue_research=True) is None
