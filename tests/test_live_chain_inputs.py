"""Deterministic harness/config checks; no model or network is invoked."""
import importlib.util
import json
from pathlib import Path
import sqlite3


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_chain.py"
SPEC = importlib.util.spec_from_file_location("verify_live_chain", SCRIPT)
live_chain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_chain)


def test_live_chain_reads_only_settings_without_migrating_database(tmp_path, monkeypatch):
    path = tmp_path / "controlled-live.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO meta VALUES('mode','\"live\"')")
        db.execute("INSERT INTO meta VALUES('schema_version','1')")
        db.execute("INSERT INTO meta VALUES('settings',?)", (json.dumps({"model_name": "controlled-model", "model_protocol": "responses"}),))
    before = path.read_bytes()
    monkeypatch.setattr(live_chain, "defaults", lambda: {"model_name": "", "quote_provider": "manual"})
    config, origin = live_chain.configuration(path)
    assert config["model_name"] == "controlled-model" and config["quote_provider"] == "manual"
    assert origin == "project_environment_plus_read_only_live_settings"
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 1


def test_live_chain_config_summary_and_nested_output_do_not_expose_keys(monkeypatch):
    secret = 'controlled-secret-with-"-quote'
    monkeypatch.setenv("CONTROLLED_MODEL_KEY", secret)
    config = {"model_key_env": "CONTROLLED_MODEL_KEY", "model_name": "controlled-model",
              "model_base_url": "https://controlled-model.example.test/v1", "model_protocol": "responses"}
    summary = live_chain.public_config(config, "controlled")
    assert summary["configured"] and summary["model_host"] == "controlled-model.example.test"
    assert "key" not in summary and secret not in json.dumps(summary)
    redacted = json.loads(live_chain.scrubbed({"events": [{"message": "value=" + secret}]}, config))
    assert redacted["events"][0]["message"] == "value=[redacted]"


def test_live_chain_unconfigured_is_distinct_from_model_or_network_failure(monkeypatch):
    monkeypatch.delenv("CONTROLLED_ABSENT_KEY", raising=False)
    config = {"model_name": "controlled", "model_key_env": "CONTROLLED_ABSENT_KEY", "model_base_url": "https://example.test/v1"}
    assert not live_chain.public_config(config, "controlled")["configured"]
    assert live_chain.classify_failure("HTTP 401") == "authentication_failed"
    assert live_chain.classify_failure("ConnectTimeout") == "network_or_timeout"
    assert live_chain.classify_failure("budget exceeded") == "research_execution_failed"
