"""Actual adapter payloads distinguish omitted output limits from explicit values."""
import json

import httpx
import pytest

from app.model import ModelClient, ModelConfigurationError, probe_model, probe_native_search
from tests.test_fourteenth_model import FUNCTION, body, config


OUTPUT_FIELDS = ('max_output_tokens', 'max_completion_tokens', 'max_tokens')


def output_fields(payload):
    return {key: payload[key] for key in OUTPUT_FIELDS if key in payload}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,completion_field,field', [
    ('responses', False, 'max_output_tokens'),
    ('chat_completions', False, 'max_tokens'),
    ('chat_completions', True, 'max_completion_tokens'),
])
@pytest.mark.parametrize('limit', [None, 4000, 'missing'])
async def test_real_adapter_omits_or_sends_exact_numeric_field(protocol, completion_field, field, limit):
    cfg = config(protocol, max_output_tokens=limit, chat_use_max_completion_tokens=completion_field)
    if limit == 'missing':
        cfg.pop('max_output_tokens')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, calls=[{}], usage={'output_tokens': 5000}))
    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    try:
        session = client.session('controlled output policy', {})
        await client.complete(session, [FUNCTION], tool_choice='finish_analysis')
        expected = {field: limit} if isinstance(limit, int) else {}
        assert output_fields(sent[0]) == expected
        observation = client.requests[0]
        assert observation['output_limit_fields_present'] == list(expected)
        assert observation['requested_max_output_tokens'] == (limit if expected else None)
        if expected:
            assert observation['reported_output_exceeds_requested'] is True
        else:
            assert 'reported_output_exceeds_requested' not in observation
        assert observation['usage']['output_tokens'] == 5000
        assert session.pending_calls[0].name == 'finish_analysis'
    finally:
        await client.aclose()


@pytest.mark.parametrize('limit', [0, -1, True, 4000.0, '4000'])
def test_invalid_output_limits_fail_before_http_client_construction(monkeypatch, limit):
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: pytest.fail('invalid limit must not open a client'))
    with pytest.raises(ModelConfigurationError, match='max_output_tokens'):
        ModelClient(config(max_output_tokens=limit))


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [None, 4000])
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_function_probe_retains_explicit_output_policy_in_both_turns(limit, protocol):
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        expected = {} if limit is None else {('max_output_tokens' if protocol == 'responses' else 'max_tokens'): limit}
        assert output_fields(payload) == expected
        if len(sent) == 1:
            return httpx.Response(200, json=body(protocol, calls=[{'name': 'capability_echo', 'arguments': '{}'}]))
        messages = payload['input'] if protocol == 'responses' else payload['messages']
        result = next(item for item in reversed(messages) if item.get('type') == 'function_call_output' or item.get('role') == 'tool')
        token = json.loads(result.get('output', result.get('content')))['token']
        return httpx.Response(200, json=body(protocol, text=token))
    result = await probe_model(config(protocol, max_output_tokens=limit), transport=httpx.MockTransport(handler))
    assert result['ok'] and len(sent) == 2
    assert all(bool(item['output_limit_fields_present']) is (limit is not None) for item in result['requests'])


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,expected', [(None, None), (4000, 1200), (800, 800), ('missing', None)])
async def test_native_probe_never_invents_limit_for_omission_and_keeps_numeric_cap(limit, expected):
    cfg = config(max_output_tokens=limit, model_native_search=True)
    if limit == 'missing':
        cfg.pop('max_output_tokens')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body('responses', text='controlled result'))
    result = await probe_native_search(cfg, transport=httpx.MockTransport(handler))
    assert len(sent) == 1
    assert output_fields(sent[0]) == ({} if expected is None else {'max_output_tokens': expected})
    assert result['requests'][0]['requested_max_output_tokens'] == expected
    assert result['requests'][0]['output_limit_fields_present'] == ([] if expected is None else ['max_output_tokens'])
    assert cfg.get('max_output_tokens') == (None if limit == 'missing' else limit)


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', [{}, {'max_output_tokens': None}, {'max_tokens': 0}])
async def test_diagnostics_record_actual_field_presence_even_for_external_wire_payload(wire):
    client = ModelClient(config(max_output_tokens=None), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body('responses', text='controlled'))))
    try:
        await client._post('responses', {'model': client.model, **wire})
        assert client.requests[0]['output_limit_fields_present'] == list(wire)
    finally:
        await client.aclose()
