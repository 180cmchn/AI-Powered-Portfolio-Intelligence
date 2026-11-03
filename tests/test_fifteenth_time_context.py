"""Actual final requests distinguish execution, valuation and historical clocks."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from app import agents, clock
from app.model import ModelClient, ModelError
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, setup, output, seed


EXECUTION = '2026-11-02T20:30:00+08:00'
VALUATION = '2026-10-30T15:00:00+08:00'
CHECKPOINT = '2026-11-01T23:14:23.530972+08:00'


@pytest.mark.asyncio
@pytest.mark.parametrize('historical,checkpoint,expected', [
    (False, None, EXECUTION),
    (True, CHECKPOINT, CHECKPOINT),
    (True, '2026-11-01T15:14:23.530972Z', '2026-11-01T15:14:23.530972+00:00'),
    (True, None, None),
    (True, '2026-11-01', None),
    (True, '2026-11-01T23:14:23', None),
    (True, '2026-02-30T23:14:23+08:00', None),
    (True, {'untrusted': CHECKPOINT}, None),
])
async def test_actual_final_request_exposes_only_supported_decision_clock(tmp_path,monkeypatch,historical,checkpoint,expected):
    monkeypatch.setattr(clock,'_utc_now',lambda:datetime.fromisoformat(EXECUTION).astimezone(timezone.utc))
    cfg=config(monkeypatch,max_model_calls=1,max_agent_turns=1)
    _,tools,context=setup(tmp_path,cfg)
    context.update(data_as_of=VALUATION,generated_at='2026-10-01T00:00:00+08:00',historical_checkpoint_at='1999-01-01T00:00:00+08:00')
    if historical:
        context['historical_evidence']={} if checkpoint is None else {'historical_checkpoint_at':checkpoint}
    requests=[]
    def handler(request):
        payload=json.loads(request.content);requests.append(payload)
        visible=seed(payload);time_context=visible['runtime_status']['time_context']
        assert time_context['execution_time']==EXECUTION
        assert time_context['data_as_of']==visible['data_as_of']==VALUATION
        assert time_context['historical_replay'] is historical
        assert time_context['decision_as_of']==expected
        assert time_context['decision_time_basis']==('historical_checkpoint_at' if historical and expected else
            'historical_checkpoint_unavailable' if historical else 'execution_time')
        assert '相对日期必须说明所依据的时点' in time_context['instruction']
        assert ('历史判断时点未知' in time_context['instruction'])==(historical and expected is None)
        assert '正文关键来源就近使用已有 URL、材料ID或子报告身份' in visible['runtime_status']['instruction']
        assert payload['tool_choice']=={'type':'function','name':'finish_analysis'}
        properties=payload['tools'][0]['parameters']['properties']
        assert {'suggestions','forecasts','research_records','information_impacts'} <= properties.keys()
        return output('responses',[('finish_analysis',delivery_arguments(report='Controlled dated report.',
            findings=[{'claim':'A legitimate optional result remains allowed.'}]))])
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    async def emit(event): pass
    result=await agents.run_research(context,cfg,tools,emit)
    assert len(requests)==1 and result['data_as_of']==VALUATION
    assert result['findings']==[{'claim':'A legitimate optional result remains allowed.'}]
    assert context.get('historical_checkpoint_at')=='1999-01-01T00:00:00+08:00'


@pytest.mark.asyncio
@pytest.mark.parametrize('earliest',['formal','analysis','request'])
@pytest.mark.parametrize('checkpoint',[CHECKPOINT,'not-a-date'])
async def test_historical_decision_time_never_changes_inflight_hard_deadlines(tmp_path,monkeypatch,earliest,checkpoint):
    from tests.test_fourteenth_deadlines import ControlledDeadlineClock,wait_for_stage
    timeline=ControlledDeadlineClock(monkeypatch,60)
    cfg=config(monkeypatch,max_model_calls=1,max_agent_turns=1,analysis_timeout_seconds=40,
        model_request_hard_timeout_seconds=20 if earliest=='request' else 30,
        synthesis_request_hard_timeout_seconds=20 if earliest=='request' else 30)
    store,tools,context=setup(tmp_path,cfg)
    context.update(data_as_of=VALUATION,historical_evidence={'historical_checkpoint_at':checkpoint},
        _analysis_deadline=timeline.monotonic()+(20 if earliest=='analysis' else 40))
    if earliest=='formal':
        context['_forecast_deadline']=timeline.monotonic()+20
    entered,release=asyncio.Event(),asyncio.Event()
    sent=[];cancelled=[]
    async def handler(request):
        payload=json.loads(request.content);sent.append(payload)
        time_context=seed(payload)['runtime_status']['time_context']
        assert time_context['decision_as_of']==(CHECKPOINT if checkpoint==CHECKPOINT else None)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return output('responses',[('finish_analysis',delivery_arguments(report='Must never finish after deadline.'))])
    monkeypatch.setattr(agents,'ModelClient',lambda cfg,before_request=None:ModelClient(cfg,before_request,httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime=agents.ResearchRuntime(context,cfg,tools,emit)
    execution=asyncio.create_task(runtime.run())
    expected={'formal':'forecast_deadline_exceeded','analysis':'analysis_budget_exhausted','request':'request_hard_timeout'}[earliest]
    try:
        await wait_for_stage(execution,entered)
        assert len(sent)==1
        timeline.advance(21)
        with pytest.raises(ModelError) as caught:
            await execution
        assert caught.value.code==expected
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution,return_exceptions=True)
        await runtime.close()
    assert cancelled==[True]
    observation=runtime.client.requests[0]
    assert observation['effective_limit']==expected
    assert observation['deadline_remaining_ms'][expected]==20000
    assert store.analyses(context['account']['id'])==[]
