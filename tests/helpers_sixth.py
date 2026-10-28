"""Explicit HTTP/key-vault substitutes for older production protocol fixtures.

They seed a model via the actual provider discovery and binding APIs. They never
contact upstream providers or write macOS Keychain, and never represent real
model capability evidence.
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.credentials import CredentialError, fingerprint


class FixtureCredentials:
    def __init__(self):
        self.values = {}
        self.sequence = 0

    def put(self, value):
        self.sequence += 1
        identifier = f'fixture-only-{self.sequence}'
        self.values[identifier] = value
        return {'kind': 'keychain', 'id': identifier, 'fingerprint': fingerprint(value)}

    def get(self, reference):
        value = self.values.get(reference.get('id'))
        if not value or fingerprint(value) != reference.get('fingerprint'):
            raise CredentialError('fixture-only credential missing')
        return value

    def delete(self, reference):
        self.values.pop(reference.get('id'), None)


def configure_test_team(store, cfg):
    """Bind all global roles to one explicit fixture-discovered provider/model.

    Synchronous for legacy fixtures, including callers inside an async test.
    No production migration or model-name validation is bypassed.
    """
    if not hasattr(store, '_credential_store'):
        store._credential_store = FixtureCredentials()
    secret = os.environ.get(cfg.get('model_key_env') or 'SIXTH_MISSING_TEST_KEY', '') or 'sixth-protocol-fixture-secret'
    model_id = cfg.get('model_name') or 'fixture-model'
    payload = {'base_url': cfg.get('model_base_url', 'https://fixture.invalid/v1'),
               'protocol': cfg.get('model_protocol', 'responses')}
    if getattr(store, '_fixture_effective_key', None) != secret:
        payload['api_key'] = secret
    provider_id = getattr(store, '_fixture_provider_id', None)
    async def prepare():
        provider = await store.save_provider(payload, provider_id,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                'data': [{'id': model_id, 'owned_by': 'deterministic-test-fixture'}]})))
        # Changing only the model doesn't change the connection revision; ask
        # for the current controlled directory explicitly for that fixture.
        if model_id not in {m['id'] for m in provider['catalog']['models']}:
            await store.discover_models(provider['id'], force=True,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'id': model_id}]})))
        return provider
    with ThreadPoolExecutor(max_workers=1) as executor:
        provider = executor.submit(lambda: asyncio.run(prepare())).result()
    store._fixture_provider_id = provider['id']
    store._fixture_effective_key = secret
    for role in store.list_agent_roles():
        store.update_agent_definition(role['role_id'], {'default_binding': {
            'provider_id': provider['id'], 'model_id': model_id,
            'reasoning_effort': cfg.get('reasoning_effort', 'medium')}})
    return provider
