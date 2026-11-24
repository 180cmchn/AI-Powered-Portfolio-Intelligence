"""Native-source wrappers survive final projection and necessary compression."""
import json

import httpx
import pytest

from app import agents
from app.context import archive
from app.model import ModelClient
from tests.delivery_helpers import delivery_arguments
from tests.test_ninth_calculation_context import config, output, setup


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('large_correction', [False, True])
async def test_final_native_source_messages_remain_visible_inside_original_budget(
        tmp_path, monkeypatch, protocol, large_correction):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=20000,
                 max_agent_turns=6, max_model_calls=10, model_native_search=True)
    store, tools, context = setup(tmp_path, cfg)
    sources = [{'native_source_materials': [{'id': f'source-{i}',
                'url': f'https://issuer.invalid/report-{i}',
                'summary': f'NATIVE-VISIBLE-{i}', 'access': 'search_excerpt'}]}
               for i in range(4)]
    payloads, events = [], []

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        history = payload.get('input', payload.get('messages'))
        users = [json.loads(item['content']) for item in history if item.get('role') == 'user']
        direct = [value for value in users if 'native_source_materials' in value]
        retained = [value for user in users for value in user.get('retained_visible_source_contexts', [])]
        # Ordinary final calls keep their established independent user messages;
        # a compression epoch pins the exact same selected package in the seed.
        visible = direct if direct else retained
        assert sorted(visible, key=lambda x: x['native_source_materials'][0]['id']) == sources
        assert len(direct) + len(retained) == 4
        wire = json.dumps(history, ensure_ascii=False)
        assert all(json.dumps(visible).count(f'NATIVE-VISIBLE-{i}') == 1 for i in range(4))
        assert 'UNREAD-NATIVE-ARCHIVE' not in wire
        assert len(wire) < cfg['max_context_chars'] * .78
        names = [item.get('name') or item.get('function', {}).get('name') for item in payload['tools']]
        assert names == ['finish_analysis']
        assert 'web_search_call.action.sources' not in payload.get('include', [])
        if len(payloads) == 1:
            assert len(direct) == 4
            draft = 'candidate ' * (1200 if large_correction else 1)
            return output(protocol, [('finish_analysis', delivery_arguments(report=draft)),
                                     ('finish_analysis', delivery_arguments(report=draft))])
        old_history = payloads[0].get('input', payloads[0].get('messages'))
        if large_correction:
            assert runtime.manager_session.compactions == 1
            assert runtime.manager_session.cache_epoch > initial_epoch
            assert runtime.manager_session.cache_epoch_reason == 'compression'
            assert len(retained) == 4
        else:
            assert history[:len(old_history)] == old_history
            assert len(direct) == 4 and not retained
        return output(protocol, [('finish_analysis', delivery_arguments(report='Visible native summaries; originals remain unverified.'))])

    monkeypatch.setattr(agents, 'ModelClient', lambda settings, before_request=None:
        ModelClient(settings, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        for value in sources:
            runtime.client.add_user(runtime.manager_session, value)
        archive(runtime.manager_session, {'text': 'UNREAD-NATIVE-ARCHIVE'}, 'tool_result')
        runtime.budget_closing = True
        initial_epoch = runtime.manager_session.cache_epoch
        result = await runtime._loop('manager', runtime.manager_session, manager=True)
        assert result['report'] == 'Visible native summaries; originals remain unverified.'
        assert len(payloads) == 2
        assert runtime.manager_session.native_search is False
        assert sum(event['type'] == 'synthesis_context_projected' for event in events) == 1
    finally:
        await runtime.close()
