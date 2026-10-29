"""Manual scoring is not an enable/pause configuration change."""
import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.calendar import SHANGHAI
from app.main import create_app


@pytest.mark.parametrize('manual_hour', [10, 16])
def test_manual_evaluation_does_not_pause_or_create_batches_and_missed_window_survives(tmp_path, monkeypatch, manual_hour):
    stamp = [datetime(2026, 10, 28, 10, tzinfo=SHANGHAI)]
    import app.forecasts as forecasts
    original_local = forecasts._local
    monkeypatch.setattr(forecasts, '_local', lambda at=None: stamp[0] if at is None else original_local(at))
    for module in ('app.store', 'app.forecasts', 'app.forecast_windows'):
        monkeypatch.setattr(module + '.now', lambda: stamp[0].isoformat())
    app = create_app(tmp_path / 'manual-scoring.sqlite3', worker=False)
    store = app.state.store
    store.set_meta('settings', {'scheduled_research_enabled': True, 'forecast_evaluation_enabled': True})
    with TestClient(app) as client:
        response = client.post('/api/accounts', json={'name': 'manual-scoring fixture',
            'holdings': [{'symbol': 'SZ:159278', 'quantity': '10000'}]})
        assert response.status_code == 201
        account_id = response.json()['id']
        assert client.put('/api/accounts/' + account_id + '/forecast-settings', json={'enabled': True}).status_code == 200
        jobs_before = len(store.jobs(account_id))
        stamp[0] = stamp[0].replace(hour=manual_hour)
        response = client.post('/api/forecasts/evaluate')
        assert response.status_code == 200
        assert response.json()['batches'] == []
        assert store.forecast_batches(account_id) == []
        assert len(store.jobs(account_id)) == jobs_before
        assert store.settings()['scheduled_research_enabled'] is True
        # The service does not run again until after the complete generation window.
        stamp[0] = stamp[0].replace(hour=19)
        asyncio.run(store.forecast_tick(store.settings(), at=stamp[0]))
        result = client.get('/api/forecast-window-runs', params={'account_id': account_id}).json()
        assert result['page']['total'] == 1
        row = result['page']['items'][0]
        assert row['status'] == 'missed'
        assert 'scheduled_research_paused' not in row['detail']['disabled_reasons']
        assert row['expected_count'] == 3
        assert store.forecasts_page()['total'] == 0
