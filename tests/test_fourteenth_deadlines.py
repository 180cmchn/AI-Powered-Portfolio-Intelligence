"""Formal cutoffs reach real Service/Agent/ModelClient; HTTP and clocks are controlled."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from app import clock
from app.calendar import SHANGHAI
from app.service import _earliest_formal_deadline
from tests.test_fourteenth_chain import setup_app, execute, final, call, response


CUTOFF = datetime(2026, 11, 2, 18, tzinfo=SHANGHAI)


def bind_batch(store, account, *, kind='formal', at=None):
    batch = store.create_forecast_batch(account['id'], kind=kind,
        at=at or CUTOFF - timedelta(minutes=1), symbols=['SZ:159278'])
    store.bind_forecast_job(batch['id'], account['latest_job_id'])
    return batch


class ControlledDeadlineClock:
    """One explicitly advanced timeline for wall, production and asyncio clocks.

    No time passes during DB/client preparation. asyncio still owns, schedules
    and fires the real timeout callbacks: only its time source is controlled.
    Never move just datetime while leaving monotonic/event-loop timers behind.
    """
    def __init__(self, monkeypatch, before_seconds):
        self.start = time.monotonic()
        self.elapsed = 0.0
        self.wall_start = CUTOFF - timedelta(seconds=before_seconds)
        monkeypatch.setattr(time, 'monotonic', self.monotonic)
        monkeypatch.setattr(asyncio.get_running_loop(), 'time', self.monotonic)
        monkeypatch.setattr(clock, '_utc_now', self.wall_time)

    def monotonic(self):
        return self.start + self.elapsed

    def wall_time(self):
        return (self.wall_start + timedelta(seconds=self.elapsed)).astimezone(timezone.utc)

    def advance(self, seconds):
        self.elapsed += seconds
        assert asyncio.get_running_loop().time() == time.monotonic() == self.monotonic()


def deadline_limits(earliest):
    # Virtual seconds, not extra production allowances or machine-speed margins.
    return {'analysis_timeout_seconds': 20 if earliest == 'analysis' else 40,
            'model_request_hard_timeout_seconds': 20 if earliest == 'request' else 30,
            'synthesis_request_hard_timeout_seconds': 20 if earliest == 'request' else 30}


async def wait_for_stage(task, entered):
    """A completed/failed Service task cannot be mistaken for entry into HTTP."""
    stage = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({task, stage}, return_when=asyncio.FIRST_COMPLETED)
        assert stage in done and entered.is_set(), 'Service finished before reaching the asserted stage'
    finally:
        if not stage.done():
            stage.cancel()
            await asyncio.gather(stage, return_exceptions=True)


def test_earliest_formal_limit_ignores_exploration_and_uses_controlled_wall_clock():
    at = CUTOFF - timedelta(seconds=30)
    batches = [
        {'kind': 'formal', 'deadline': (CUTOFF + timedelta(seconds=10)).isoformat()},
        {'kind': 'exploratory', 'deadline': (CUTOFF - timedelta(days=1)).isoformat()},
        {'kind': 'formal', 'deadline': CUTOFF.isoformat()},
    ]
    assert _earliest_formal_deadline(batches, at=at, monotonic_at=100) == (CUTOFF, 130)
    assert _earliest_formal_deadline(batches[1:2], at=at, monotonic_at=100) is None


@pytest.mark.asyncio
async def test_already_expired_formal_task_sends_no_http_and_cannot_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, '_utc_now', lambda: (CUTOFF + timedelta(seconds=1)).astimezone(timezone.utc))
    sent = []
    def handler(request):
        sent.append(request)
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    batch = bind_batch(store, account)
    saved, reports = await execute(app, store, account)
    assert sent == [] and reports == []
    assert saved['state'] == 'failed' and saved['error_code'] == 'forecast_deadline_exceeded'
    context = store.forecast_job_context(saved['id'])
    assert context['state'] == 'closed'
    assert {sample['state'] for sample in context['items']} == {'missed'}
    with store.connect() as db:
        assert store.publish_forecasts(db, saved, {'forecasts': [{
            'security_id': 'SZ:159278', 'horizon': 1, 'direction': 'abstain',
            'reason': 'too late'}]}, 'not-a-published-report') == []
        assert db.execute('SELECT count(*) FROM forecasts WHERE batch_id=?', (batch['id'],)).fetchone()[0] == 0
    assert store.claim_jobs(account_id=account['id']) == []


@pytest.mark.asyncio
async def test_multiple_assigned_formal_batches_use_the_earliest_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, '_utc_now', lambda: (CUTOFF + timedelta(seconds=1)).astimezone(timezone.utc))
    sent = []
    def handler(request):
        sent.append(request)
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    earlier = bind_batch(store, account)
    later = store.create_forecast_batch(account['id'], kind='formal',
        at=CUTOFF + timedelta(days=1, minutes=-1), symbols=['SZ:159278'])
    with store.connect() as db:
        store.assign_forecast_scope(db, account['latest_job_id'], later['id'], horizons=[1])
    saved, reports = await execute(app, store, account)
    assert saved['error_code'] == 'forecast_deadline_exceeded'
    assert sent == reports == []
    batches = {batch['id']: batch for batch in store.forecast_batches(account['id'])}
    assert batches[earlier['id']]['state'] == 'closed'
    assert batches[later['id']]['state'] != 'closed'


@pytest.mark.asyncio
@pytest.mark.parametrize('earliest', ['formal', 'analysis', 'request'])
@pytest.mark.parametrize('stage', ['before_http', 'inflight', 'complete'])
async def test_request_deadline_boundaries_keep_saved_material(tmp_path, monkeypatch, earliest, stage):
    timeline = ControlledDeadlineClock(monkeypatch, 20 if earliest == 'formal' else 60)
    entered, release = asyncio.Event(), asyncio.Event()
    sent, cancelled = [], []

    async def handler(request):
        sent.append(json.loads(request.content))
        entered.set()  # The real ModelClient/HTTP transport has entered its handler.
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return response(call('finish_analysis', final()))

    if stage == 'before_http':
        from app.model import ModelClient
        receive_stream = ModelClient._receive_stream

        async def before_transport(self, *args, **kwargs):
            # Hold the actual request path before HTTP is sent, with its real
            # timer already armed. This is explicitly NOT an in-flight test.
            entered.set()
            await release.wait()
            return await receive_stream(self, *args, **kwargs)

        monkeypatch.setattr(ModelClient, '_receive_stream', before_transport)

    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        max_model_calls=2, max_agent_turns=2, **deadline_limits(earliest))
    batch = bind_batch(store, account)
    material = store.save_material({'scope': 'public', 'access': 'full_text',
        'url': 'https://fixture.invalid/saved', 'content': 'Previously completed visible material'})
    execution = asyncio.create_task(execute(app, store, account))
    try:
        await wait_for_stage(execution, entered)
        assert len(sent) == (0 if stage == 'before_http' else 1)
        if stage == 'complete':
            timeline.advance(1)  # All three bounded deadlines are still open.
            release.set()
        else:
            # Only the selected 20-second boundary expires. The barrier proves
            # exactly where it happens, regardless of host/SQLite/client speed.
            timeline.advance(21)
        saved, reports = await execution
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    expected = {'formal': 'forecast_deadline_exceeded', 'analysis': 'analysis_budget_exhausted',
                'request': 'request_hard_timeout'}[earliest]
    events = store.job(saved['id'], events=True)['events']
    observations = [request for event in events for request in event.get('payload', {}).get('requests', [])]
    assert observations and observations[0]['effective_limit'] == expected
    assert observations[0]['deadline_remaining_ms'][expected] == 20000
    assert store.material(material['id'])['content'] == 'Previously completed visible material'
    assert store.claim_jobs(account_id=account['id']) == []
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM forecasts WHERE batch_id=?', (batch['id'],)).fetchone()[0] == 0
    if stage == 'complete':
        assert saved['state'] == 'completed', saved.get('error')
        assert len(sent) == len(reports) == 1 and cancelled == []
        assert sum(event['type'] == 'published' for event in events) == 1
        assert store.forecast_job_context(saved['id'])['state'] != 'closed'
        return

    assert saved['state'] == 'failed' and not reports
    assert saved['error_code'] == expected, saved.get('error')
    assert len(sent) == (0 if stage == 'before_http' else 1)
    assert cancelled == ([] if stage == 'before_http' else [True])
    assert not any(event['type'] == 'published' for event in events)
    if earliest == 'formal':
        context = store.forecast_job_context(saved['id'])
        assert context['state'] == 'closed'
        assert {sample['state'] for sample in context['items']} == {'missed'}
        assert not store.forecasts_page(kind='formal')['items'][0].get('forecast_id')
        with store.connect() as db:
            assert store.publish_forecasts(db, saved, {'forecasts': [{
                'security_id': 'SZ:159278', 'horizon': 1, 'direction': 'abstain',
                'reason': 'late result must not backfill'}]}, 'not-a-published-report') == []
    else:
        assert store.forecast_job_context(saved['id'])['state'] != 'closed'


@pytest.mark.asyncio
@pytest.mark.parametrize('batch_kind', [None, 'exploratory'])
async def test_regular_account_report_is_not_capped_by_an_old_exploration_window(tmp_path, monkeypatch, batch_kind):
    monkeypatch.setattr(clock, '_utc_now', lambda: (CUTOFF + timedelta(seconds=1)).astimezone(timezone.utc))
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    if batch_kind:
        bind_batch(store, account, kind=batch_kind)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(sent) == len(reports) == 1
    assert '_forecast_deadline' not in json.dumps(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize('association', ['retry_owner', 'assigned_sample'])
async def test_followup_execution_honors_original_formal_deadline(tmp_path, monkeypatch, association):
    stamp = [CUTOFF - timedelta(minutes=1)]
    monkeypatch.setattr(clock, '_utc_now', lambda: stamp[0].astimezone(timezone.utc))
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    batch = bind_batch(store, account)
    original, original_reports = await execute(app, store, account)
    assert original['state'] == 'completed' and len(sent) == 1
    if association == 'retry_owner':
        following = store.enqueue(account['id'], 'retry')
        assert store.forecast_job_context(following['id'])['id'] == batch['id']
    else:
        # A server-assigned subset has execution provenance even when the
        # batch's current owner still points at its earlier completed task.
        following = store.enqueue(account['id'], 'candidate_scan')
        with store.connect() as db:
            store.assign_forecast_scope(db, following['id'], batch['id'], horizons=[1])
            assert db.execute('SELECT job_id FROM forecast_batches WHERE id=?', (batch['id'],)).fetchone()[0] == original['id']
    stamp[0] = CUTOFF + timedelta(seconds=1)
    saved, reports = await execute(app, store, account)
    assert saved['id'] == following['id']
    assert saved['error_code'] == 'forecast_deadline_exceeded' and saved['state'] == 'failed'
    assert len(sent) == 1  # No additional HTTP for the expired supplement/retry.
    assert [(report['id'], report['status']) for report in reports] == [(original_reports[0]['id'], 'current')]
    batches = store.forecast_batches(account['id'])
    assert len(batches) == 1 and batches[0]['state'] == 'closed'
    assert all(item['state'] == 'missed' for item in store.forecasts_page(kind='formal')['items'])
