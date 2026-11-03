"""Long-task defaults migrate only identified factory profiles for new work."""
import copy
import asyncio
import time

import httpx
import pytest

from app.config import LONG_TASK_TIMEOUT_DEFAULTS, defaults, migrate_timeout_policy, timeout_policy


PREVIOUS = {
    'model_first_event_timeout_seconds':90,'model_stream_idle_timeout_seconds':120,
    'model_request_hard_timeout_seconds':300,'synthesis_request_hard_timeout_seconds':360,
    'agent_timeout_seconds':600,'analysis_timeout_seconds':1200,
}
LEGACY = {'model_request_timeout_seconds':90,'synthesis_request_timeout_seconds':120,
          'agent_timeout_seconds':120,'analysis_timeout_seconds':600}


@pytest.mark.parametrize('saved',[{},LEGACY,PREVIOUS,{**PREVIOUS,'model_request_timeout_seconds':90,'synthesis_request_timeout_seconds':120}])
def test_identified_factory_profiles_get_bounded_long_task_defaults_without_mutation(saved):
    original=copy.deepcopy(saved)
    migrated=migrate_timeout_policy(saved)
    assert {key:migrated[key] for key in LONG_TASK_TIMEOUT_DEFAULTS}==LONG_TASK_TIMEOUT_DEFAULTS
    assert migrated['timeout_defaults_revision']==3
    assert saved==original and migrate_timeout_policy(migrated)==migrated
    assert timeout_policy(migrated)['policy_version']==2
    assert timeout_policy(migrated)['request_hard_seconds']==900
    assert timeout_policy(migrated,'synthesis')['request_hard_seconds']==900
    assert (migrated['model_first_event_timeout_seconds'],migrated['model_stream_idle_timeout_seconds'])==(90,300)


@pytest.mark.parametrize('field',list(PREVIOUS))
def test_custom_field_prevents_whole_profile_upgrade(field):
    saved={**PREVIOUS,field:PREVIOUS[field]+1}
    assert migrate_timeout_policy(saved)==saved


@pytest.mark.parametrize('field',list(PREVIOUS))
def test_partial_old_profile_does_not_infer_an_automatic_upgrade(field):
    saved={key:value for key,value in PREVIOUS.items() if key!=field}
    assert migrate_timeout_policy(saved)==saved
    assert 'timeout_defaults_revision' not in migrate_timeout_policy(saved)


def test_explicit_old_values_and_legacy_custom_absolute_limits_remain_user_choices():
    explicit={**PREVIOUS,'timeout_defaults_revision':3}
    assert migrate_timeout_policy(explicit)==explicit
    custom={**LEGACY,'model_request_timeout_seconds':180}
    result=migrate_timeout_policy(custom)
    assert all(result[key]==value for key,value in custom.items())
    assert result['model_request_hard_timeout_seconds']==180
    assert result['synthesis_request_hard_timeout_seconds']==120
    assert 'timeout_defaults_revision' not in result
    conflicting={**PREVIOUS,'model_request_timeout_seconds':180}
    assert migrate_timeout_policy(conflicting)==conflicting


def test_saved_revision3_idle120_and_implicit_legacy_snapshot_keep_their_limits():
    saved={**LONG_TASK_TIMEOUT_DEFAULTS,'model_stream_idle_timeout_seconds':120,'timeout_defaults_revision':3}
    assert migrate_timeout_policy(saved)==saved
    assert timeout_policy(saved)['stream_idle_seconds']==120
    assert timeout_policy(LEGACY)['stream_idle_seconds']==120


def test_only_new_jobs_get_migrated_profile_and_raw_settings_or_old_snapshots_are_not_rewritten(tmp_path):
    from app.domain import AccountInput
    from app.store import Store
    store=Store(tmp_path/'timeout-profiles.sqlite3')
    store.set_meta('settings',{**PREVIOUS,'timeout_defaults_revision':3,'debounce_seconds':0,'quote_provider':'manual'})
    first=store.save_account(AccountInput(name='already running',cash='100000'))
    running=store.claim_jobs(account_id=first['id'])[0]
    old_snapshot=copy.deepcopy(running['config_snapshot'])
    assert timeout_policy(old_snapshot)['request_hard_seconds']==300
    assert timeout_policy(old_snapshot)['stream_idle_seconds']==120
    old_factory={**PREVIOUS,'debounce_seconds':0,'quote_provider':'manual'}
    store.set_meta('settings',old_factory)
    second=store.save_account(AccountInput(name='new work',cash='100000'))
    claimed=store.claim_jobs(account_id=second['id'])[0]
    assert {key:claimed['config_snapshot'][key] for key in LONG_TASK_TIMEOUT_DEFAULTS}==LONG_TASK_TIMEOUT_DEFAULTS
    assert store.job(running['id'])['config_snapshot']==old_snapshot
    assert store.meta('settings')==old_factory
    assert timeout_policy(LEGACY)['request_hard_seconds']==90
    assert timeout_policy(LEGACY,'synthesis')['request_hard_seconds']==120
    assert defaults()['timeout_defaults_revision']==3


@pytest.mark.parametrize('phase',['research','synthesis'])
async def test_default_idle_allows_controlled_180_second_gap_after_real_progress(monkeypatch,phase):
    from app.model import ModelClient
    from tests.test_eleventh_timeouts import config, event, terminal
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock, wait_for_stage
    timeline=ControlledDeadlineClock(monkeypatch,1000)
    monkeypatch.setattr(time,'perf_counter',timeline.monotonic)
    first_read,second_read=asyncio.Event(),asyncio.Event()
    next_chunk=asyncio.Queue()
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield event('response.output_text.delta',delta='initial progress')
            first_read.set()
            yield await next_chunk.get()
            second_read.set()
            yield await next_chunk.get()
        async def aclose(self): self.closed=True
    stream=Stream()
    sent=[]
    def handler(request):
        sent.append(request)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
    client=ModelClient(config(**LONG_TASK_TIMEOUT_DEFAULTS),transport=httpx.MockTransport(handler))
    task=None
    try:
        session=client.session('controlled fixture',{}); session.phase=phase
        task=asyncio.create_task(client.complete(session,[]))
        await wait_for_stage(task,first_read)
        assert len(sent)==1
        timeline.advance(180)  # Beyond old idle120, before new idle300/hard900.
        next_chunk.put_nowait(event('response.output_text.delta',delta='continued progress'))
        await wait_for_stage(task,second_read)
        assert not task.done()
        next_chunk.put_nowait(terminal())
        assert (await task).text=='完整报告'
        row=client.requests[0]
        assert row['stream_max_idle_ms']==row['duration_ms']==180000
        assert row['stream_terminal']=='response.completed' and row['http_body_complete'] is True
        assert row['error_code'] is None and len(sent)==1 and stream.closed
        assert row['request_timeout_seconds']==900
    finally:
        if task is not None and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize('idle_seconds',[120,300])
async def test_idle_remains_bounded_and_saved120_still_expires_without_retry(monkeypatch,idle_seconds):
    from app.model import ModelClient, ModelTimeoutError
    from tests.test_eleventh_timeouts import config, event
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock, wait_for_stage
    timeline=ControlledDeadlineClock(monkeypatch,1000)
    monkeypatch.setattr(time,'perf_counter',timeline.monotonic)
    entered=asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield event('response.output_text.delta',delta='initial progress')
            entered.set()
            await asyncio.Event().wait()
        async def aclose(self): self.closed=True
    stream=Stream()
    sent=[]
    def handler(request):
        sent.append(request)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
    saved={**LONG_TASK_TIMEOUT_DEFAULTS,'model_stream_idle_timeout_seconds':idle_seconds,'timeout_defaults_revision':3}
    client=ModelClient(config(**migrate_timeout_policy(saved)),transport=httpx.MockTransport(handler))
    task=None
    try:
        task=asyncio.create_task(client.complete(client.session('controlled fixture',{}),[]))
        await wait_for_stage(task,entered)
        assert len(sent)==1
        timeline.advance(idle_seconds+1)
        with pytest.raises(ModelTimeoutError) as caught: await task
        assert caught.value.code=='stream_idle_timeout'
        row=client.requests[0]
        assert row['cancelled_by']==row['error_code']=='stream_idle_timeout'
        assert row['stream_last_progress_ms']==0 and row['stream_max_idle_ms']==(idle_seconds+1)*1000
        assert row['stream_terminal'] is None and len(sent)==1 and stream.closed
    finally:
        if task is not None and not task.done():
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await client.aclose()
