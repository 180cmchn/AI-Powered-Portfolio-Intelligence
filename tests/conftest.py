"""Collection must never initialize or migrate the user's configured database."""
import os
from tempfile import TemporaryDirectory

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
