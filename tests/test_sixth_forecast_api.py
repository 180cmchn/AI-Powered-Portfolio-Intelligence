"""API fixtures; formal labels here test segregation, never real forward accuracy."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.calendar import SHANGHAI
from app.main import create_app


AT = datetime(2026, 10, 28, 16, tzinfo=SHANGHAI)


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "forecast-api-fixtures.sqlite3", worker=False)
    with TestClient(app) as client:
        yield client, app.state.store


def create_predictions(client, store, name, *, kind="formal", symbols=None, direction="up", threshold="0.005"):
    symbols = symbols or ["SZ:159278"]
    response = client.post("/api/accounts", json={"name": name, "holdings": [{"symbol": symbol, "quantity": "100"} for symbol in symbols]})
    assert response.status_code == 201, response.text
    account = response.json()
    if threshold != "0.005":
        assert client.put(f"/api/accounts/{account['id']}/forecast-settings", json={"threshold": threshold}).status_code == 200
    batch = store.create_forecast_batch(account["id"], at=AT, kind=kind)
    store.bind_forecast_job(batch["id"], account["latest_job_id"])
    job = store.job(account["latest_job_id"])
    result = {"report": "Synthetic API fixture", "forecasts": [{"security_id": symbol, "horizon": horizon,
        "direction": direction, "reason": "controlled API fixture"} for symbol in symbols for horizon in (1, 5, 20)]}
    with store.connect() as db:
        ids = store.publish_forecasts(db, job, result, "fixture-report-" + name, AT)
    return account, batch, ids


def score_one(store, forecast_id):
    forecast = store.forecast_detail(forecast_id)
    store.record_forecast_evaluation(forecast_id, {"symbol": forecast["symbol"], "base_date": forecast["base_date"],
        "target_date": forecast["target_date"], "status": "ready", "source": "API controlled fixture",
        "base_price": "100", "target_price": "101", "price_policy": "same_source_pair_v1"},
        at=datetime(2026, 10, 29, 16, tzinfo=SHANGHAI))


def test_default_and_blank_filters_exclude_test_records(api):
    client, store = api
    formal, _, formal_ids = create_predictions(client, store, "formal fixture")
    test, _, _ = create_predictions(client, store, "test fixture", kind="test")
    response = client.get("/api/forecasts", params={"account_id": "", "kind": "", "start": " ", "model_id": "", "security_id": " "})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"]["total"] == 3 and body["statistics"]["filters"] == {"kind": "formal"}
    assert {r["account_id"] for r in body["page"]["items"]} == {formal["id"]}
    response = client.get("/api/forecasts", params={"kind": "test"})
    assert {r["account_id"] for r in response.json()["page"]["items"]} == {test["id"]}
    assert len(client.get("/api/forecast-batches", params={"account_id": ""}).json()) == 2
    assert client.get("/api/forecasts", params={"kind": "all"}).status_code == 422


def test_rule_edit_cannot_relabel_existing_forecasts_and_filter_is_exact(api):
    client, store = api
    first, batch, ids = create_predictions(client, store, "old rules")
    second, newer, _ = create_predictions(client, store, "different rules", threshold="0.02")
    assert batch["rule_version"] != newer["rule_version"]
    response = client.put(f"/api/accounts/{first['id']}/forecast-settings", json={"horizons": [1, 5, 30], "threshold": "0.03"})
    assert response.status_code == 200
    detail = client.get(f"/api/forecasts/{ids[-1]}").json()
    assert detail["horizon"] == 20 and detail["rule"]["threshold"] == "0.005"
    filtered = client.get("/api/forecasts", params={"rule_version": batch["rule_version"]}).json()
    assert filtered["page"]["total"] == 3
    assert {g["rule_version"] for g in filtered["statistics"]["groups"]} == {batch["rule_version"]}
    assert {r["account_id"] for r in filtered["page"]["items"]} == {first["id"]}


def test_comparison_counts_only_shared_matching_mature_samples(api):
    client, store = api
    a, batch_a, ids_a = create_predictions(client, store, "account A", direction="up")
    b, batch_b, ids_b = create_predictions(client, store, "account B", symbols=["SZ:159278", "SH:510300"], direction="down")
    score_one(store, ids_a[0])
    # Return order follows the explicit submission order, not sorted batch rows.
    score_one(store, ids_b[0])
    response = client.get("/api/forecasts/compare", params=[("account_ids", a["id"]), ("account_ids", b["id"]), ("kind", ""), ("start", "")])
    assert response.status_code == 200, response.text
    comparison = response.json()
    assert comparison["matching_dimensions"] == ["base_date", "symbol", "horizon", "rule_version"]
    assert comparison["overlap_expected"] == 3 and comparison["overlap_evaluated"] == 1
    assert comparison["unmatched_counts"] == {a["id"]: 0, b["id"]: 3}
    group = next(g for g in comparison["groups"] if g["horizon"] == 1)
    accounts = {entry["account_id"]: entry for entry in group["accounts"]}
    assert accounts[a["id"]]["accuracy"] == 1 and accounts[b["id"]]["accuracy"] == 0
    assert all(g["accounts"][0]["accuracy"] is None for g in comparison["groups"] if g["horizon"] != 1)
    assert "严格对照实验" in comparison["note"]


def test_comparison_different_rules_and_test_kind_cannot_fake_overlap(api):
    client, store = api
    a, _, _ = create_predictions(client, store, "standard")
    b, _, _ = create_predictions(client, store, "different", threshold="0.01")
    c, _, _ = create_predictions(client, store, "test record", kind="test")
    response = client.get("/api/forecasts/compare", params=[("account_ids", a["id"]), ("account_ids", b["id"])])
    assert response.json()["overlap_expected"] == 0 and response.json()["groups"] == []
    response = client.get("/api/forecasts/compare", params=[("account_ids", a["id"]), ("account_ids", c["id"])])
    assert response.json()["overlap_expected"] == 0
    assert response.json()["unmatched_counts"][c["id"]] == 0  # test records excluded from formal
    assert client.get("/api/forecasts/compare", params=[("account_ids", a["id"]), ("account_ids", a["id"])]).status_code == 422
