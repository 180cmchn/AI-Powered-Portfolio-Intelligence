"""Collection must never initialize or migrate the user's configured database."""
import os
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

import pytest

_collection_database = TemporaryDirectory(prefix='guanlan-pytest-')
os.environ['APP_DB_PATH'] = _collection_database.name + '/collection.sqlite3'
os.environ['APP_MODE'] = 'live'
# Deterministic tests must not inherit newly configured real model credentials.
# Individual protocol tests explicitly install their own mock key/transport.
os.environ['MODEL_NAME'] = ''
os.environ['MODEL_KEY_ENV'] = 'GUANLAN_TEST_UNCONFIGURED_KEY'
os.environ['MODEL_BASE_URL'] = 'https://api.openai.com/v1'
os.environ['MODEL_PROTOCOL'] = 'responses'
os.environ['SEARCH_PROVIDER'] = 'disabled'
os.environ['SEARCH_KEY_ENV'] = 'GUANLAN_TEST_UNCONFIGURED_SEARCH'


@pytest.fixture
def frozen_clock(monkeypatch):
    """Explicit, per-test wall clock shared by every production time reader.

    Also applies to FastAPI's worker threads and concurrent ledger writers. This
    does not replace time.monotonic/perf_counter used for timeout enforcement.
    Call set/advance to make the test's timeline visible at each operation.
    """
    from app import clock

    class Clock:
        def set(self, value):
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if parsed.tzinfo is None:
                raise ValueError('Test clocks require an explicit timezone')
            self.instant = parsed.astimezone(timezone.utc)
            return self.instant

        def advance(self, **delta):
            self.instant += timedelta(**delta)
            return self.instant

    result = Clock()
    result.set('2026-10-30T12:00:00+08:00')
    monkeypatch.setattr(clock, '_utc_now', lambda: result.instant)
    return result
