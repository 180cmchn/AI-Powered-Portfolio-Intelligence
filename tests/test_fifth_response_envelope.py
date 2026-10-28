"""Single-layer forwarder-envelope protocol fixtures, not live endpoint support."""
import json

import httpx
import pytest

from app.model import ModelClient, ModelProtocolError


HIDDEN = 'controlled-opaque-reasoning-must-not-be-logged'


def config(protocol='chat_completions'):
    return {'model_name':'controlled-model', 'model_base_url':'http://localhost:19001/v1',
        'model_allow_no_key':True, 'model_key_env':'ENVELOPE_TEST_UNUSED_KEY',
        'model_protocol':protocol, 'reasoning_effort':'max', 'model_native_search':True,
        'model_retries':0, 'max_output_tokens':4000}


def chat(content='visible final', **extra):
    return {'id':'chat-inner-id', 'model':'controlled-model',
        'choices':[{'message':{'role':'assistant','content':content},'finish_reason':'stop'}], **extra}


def responses(text='visible final', **extra):
    return {'id':'response-inner-id', 'status':'completed',
        'output':[{'type':'message','content':[{'type':'output_text','text':text}]}], **extra}


def wrapped(body):
    return {'success':True, 'data':body, 'id':'forwarder-id',
        'reasoning_effort':'low', 'usage':{'prompt_tokens':15,'completion_tokens':0,
            'unsafe_string':HIDDEN, 'detail':{'numeric':2,'opaque':HIDDEN}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions','responses'])
async def test_one_layer_envelope_uses_inner_usage_effort_id_without_changing_request(protocol):
    inner_usage = {'prompt_tokens':17,'completion_tokens':29,'total_tokens':46,
        'completion_tokens_details':{'reasoning_tokens':23,'opaque':HIDDEN}}
    body = (chat if protocol=='chat_completions' else responses)(usage=inner_usage, reasoning_effort='max')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json=wrapped(body))
    client = ModelClient(config(protocol),transport=httpx.MockTransport(handler))
    try:
        session = client.session('controlled instruction', {'job_id':'controlled-task'})
        turn = await client.complete(session,[])
        assert turn.text == 'visible final'
        diag = client.requests[0]
        assert diag['response_envelope'] == 'data'
        assert diag['response_id'] == body['id']
        assert diag['reported_reasoning_effort'] == 'max'
        assert diag['usage']['completion_tokens'] == 29
        assert diag['usage']['completion_tokens_details'] == {'reasoning_tokens':23}
        assert diag['forwarder_usage'] == {'prompt_tokens':15,'completion_tokens':0,'detail':{'numeric':2}}
        assert client.usage_summary(client.requests)['usage']['prompt_tokens'] == 17
        assert client.usage_summary(client.requests)['usage']['completion_tokens'] == 29
        assert diag['request_accepted'] is True
        assert HIDDEN not in json.dumps(client.requests)
        assert sent[0]['model'] == 'controlled-model'
        if protocol=='responses':
            assert sent[0]['reasoning']['effort'] == 'max'
            assert {'type':'web_search'} in sent[0]['tools']
        else:
            assert sent[0]['reasoning_effort'] == 'max'
        assert client.config['model_native_search'] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_chat_envelope_tools_replay_opaque_fields_only_in_memory():
    payloads = []
    initial = chat(None)
    initial['choices'][0] = {'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
        'reasoning_content':HIDDEN, 'tool_calls':[{'id':'call-1','type':'function',
            'function':{'name':'calculate','arguments':'{"values":[2,3]}'}}]}}
    def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        return httpx.Response(200,json=wrapped(initial if len(payloads)==1 else chat('5')))
    client = ModelClient(config(),transport=httpx.MockTransport(handler))
    try:
        session = client.session('controlled instruction',{})
        first = await client.complete(session,[])
        assert first.text == '' and first.calls[0].arguments == {'values':[2,3]}
        client.add_results(session,[(first.calls[0],{'value':5})])
        assert (await client.complete(session,[])).text == '5'
        assistant = next(m for m in payloads[1]['messages'] if m['role']=='assistant')
        assert assistant['reasoning_content'] == HIDDEN
        assert any(m.get('tool_call_id')=='call-1' for m in payloads[1]['messages'])
        assert HIDDEN not in json.dumps(client.requests)
        assert not session.pending_calls
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_responses_envelope_native_events_and_opaque_continuation():
    search = {'type':'web_search_call','id':'search-1','status':'completed','action':{
        'type':'search','query':'controlled query','sources':[{'url':'https://controlled.example/source','description':'Visible source fragment'}]}}
    opaque = {'type':'reasoning','id':'reasoning-1','encrypted_content':HIDDEN,'summary':[]}
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,json=wrapped(responses(output=[opaque,search]) if len(payloads)==1 else responses()))
    client = ModelClient(config('responses'),transport=httpx.MockTransport(handler))
    try:
        session = client.session('controlled instruction',{})
        first = await client.complete(session,[])
        assert first.continuation_required and not first.text
        assert first.native_events[0]['id'] == 'search-1'
        assert first.sources[0]['visible_excerpt'] == 'Visible source fragment'
        assert first.native_status['status'] == 'observed'
        assert (await client.complete(session,[])).text == 'visible final'
        assert opaque in payloads[1]['input']
        assert HIDDEN not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions','responses'])
async def test_standard_root_takes_precedence_over_data_and_keeps_usage(protocol):
    build = chat if protocol=='chat_completions' else responses
    body = {**build('standard result',usage={'completion_tokens':7}),
        'success':True,'data':build('must not replace root',usage={'completion_tokens':99})}
    client = ModelClient(config(protocol),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
    try:
        assert (await client.complete(client.session('controlled instruction',{}),[])).text == 'standard result'
        assert client.requests[0]['usage']['completion_tokens'] == 7
        assert 'response_envelope' not in client.requests[0]
        assert 'forwarder_usage' not in client.requests[0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_absent_inner_usage_remains_unknown_even_when_forwarder_reports_zero():
    client = ModelClient(config(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=wrapped(chat()))))
    try:
        await client.complete(client.session('controlled instruction',{}),[])
        assert client.requests[0]['usage'] is None
        assert client.requests[0]['reported_reasoning_effort'] is None  # Outer low is not the model's report.
        assert client.requests[0]['forwarder_usage']['completion_tokens'] == 0
        assert client.usage_summary(client.requests)['usage'] is None
        assert client.usage_summary(client.requests)['usage_reported_requests'] == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_incomplete_wrapped_responses_retains_completed_search_but_no_partial_final():
    search = {'type':'web_search_call','id':'completed-search','status':'completed'}
    body = responses('PARTIAL_RESULT', status='incomplete')
    body['output'].insert(0,search)
    client = ModelClient(config('responses'),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=wrapped(body))))
    try:
        session = client.session('controlled instruction',{})
        initial = list(session.history)
        with pytest.raises(ModelProtocolError,match='未完成') as caught:
            await client.complete(session,[])
        assert caught.value.native_turn.native_events[0]['id'] == 'completed-search'
        assert session.history == initial and not session.pending_calls
        assert 'PARTIAL_RESULT' not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions','responses'])
@pytest.mark.parametrize('kind', ['false','string_true','nested','missing_shape','wrong_protocol','empty',
    'truncated','error','inner_error','inner_false','malformed_standard_root'])
async def test_invalid_envelopes_never_produce_a_successful_turn(protocol,kind):
    build = chat if protocol=='chat_completions' else responses
    body = wrapped(build())
    root = 'choices' if protocol=='chat_completions' else 'output'
    if kind=='false': body['success']=False
    elif kind=='string_true': body['success']='true'
    elif kind=='nested': body['data']=wrapped(build())
    elif kind=='missing_shape': body['data']={'message':'unstructured prose'}
    elif kind=='wrong_protocol': body['data']=(responses if protocol=='chat_completions' else chat)()
    elif kind=='empty': body['data'][root]=[]
    elif kind=='truncated':
        if protocol=='chat_completions': body['data']['choices'][0]['finish_reason']='length'
        else: body['data']['status']='incomplete'
    elif kind=='error': body['error']={'message':HIDDEN}
    elif kind=='inner_error': body['data']['error']={'message':HIDDEN}
    elif kind=='inner_false': body['data']['success']=False
    else: body[root]=None  # Presence of a standard root must not be hidden by a valid nested response.
    client = ModelClient(config(protocol),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
    try:
        with pytest.raises(ModelProtocolError) as caught:
            await client.complete(client.session('controlled instruction',{}),[])
        assert client.requests[0]['request_accepted'] is True
        assert len(client.requests)==1
        assert HIDDEN not in str(caught.value) and HIDDEN not in json.dumps(client.requests)
    finally:
        await client.aclose()
