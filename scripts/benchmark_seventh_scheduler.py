"""Controlled old/new scheduler benchmark; never calls a real supplier.

Usage: .venv/bin/python scripts/benchmark_seventh_scheduler.py --size 5000
The baseline methods are loaded verbatim from the local review commit, applied
only to generated temporary SQLite databases. Results are measurements, not SLA.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.calendar import SHANGHAI
from app.store import Store
import app.forecasts as forecast_module

AT = datetime(2026, 10, 28, 16, tzinfo=SHANGHAI)
DUE = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
BASELINE = "537e390856c9b2d203b2df49d122f1eac1094416"


def insert(db, table, row):
    db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES({','.join('?' for _ in row)})", list(row.values()))


def fixture(path, size):
    store = Store(path)
    accounts = [store.save_account({"name": f"controlled {i}", "holdings": [{"symbol": "SZ:159278", "quantity": "100", "cost": "1"}], "preferences": {}}) for i in range(10)]
    a = accounts[0]
    store.save_forecast_settings(a["id"], {"horizons": [1], "labels": ["one"]})
    batch = store.create_forecast_batch(a["id"], kind="exploratory", at=AT)
    store.bind_forecast_job(batch["id"], a["latest_job_id"])
    with store.connect() as db:
        first = store.publish_forecasts(db, store.job(a["latest_job_id"]), {"forecasts": [{"symbol": "SZ:159278", "horizon": 1, "direction": "up", "reason": "controlled benchmark"}]}, "controlled-report", AT)[0]
        b = dict(db.execute("SELECT * FROM forecast_batches WHERE id=?", (batch["id"],)).fetchone())
        s = dict(db.execute("SELECT * FROM forecast_samples WHERE batch_id=?", (batch["id"],)).fetchone())
        f = dict(db.execute("SELECT * FROM forecasts WHERE id=?", (first,)).fetchone())
        t = dict(db.execute("SELECT * FROM forecast_evaluation_tasks WHERE forecast_id=?", (first,)).fetchone())
        ids = [first]
        for i in range(1, size):
            aid = accounts[i % len(accounts)]["id"]
            bid, sid, fid = f"b{i}", f"s{i}", f"f{i}"
            insert(db, "forecast_batches", {**b, "id": bid, "account_id": aid, "job_id": None})
            insert(db, "forecast_samples", {**s, "id": sid, "batch_id": bid, "account_id": aid})
            insert(db, "forecasts", {**f, "id": fid, "sample_id": sid, "batch_id": bid, "account_id": aid})
            insert(db, "forecast_evaluation_tasks", {**t, "forecast_id": fid})
            ids.append(fid)
        historical = int(size * .9)
        for i, fid in enumerate(ids[:historical]):
            db.execute("INSERT INTO forecast_evaluations VALUES(?,?,1,?,'benchmark-version','evaluated',NULL,'0.01','up',1,'{}',NULL)", (f"evaluation{i}", fid, DUE.isoformat()))
            db.execute("UPDATE forecast_evaluation_tasks SET state='evaluated',next_attempt='2026-10-01T00:00:00+08:00' WHERE forecast_id=?", (fid,))
    store.save_forecast_settings(accounts[-1]["id"], {"enabled": True})
    return set(ids[:historical]), len(ids) - historical


def baseline_methods():
    source = subprocess.check_output(["git", "show", BASELINE + ":app/forecasts.py"], text=True)
    tree = ast.parse(source)
    methods = [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "ForecastStore" for n in c.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name in ("forecast_tick", "evaluate_due_forecasts")]
    scope = dict(vars(forecast_module))
    exec(compile(ast.Module(body=methods, type_ignores=[]), "review-baseline-forecasts.py", "exec"), scope)
    return {m.name: scope[m.name] for m in methods}


class Supplier:
    def __init__(self, started):
        self.started, self.calls, self.first_baseline = started, 0, None

    async def history(self, symbol, **opts):
        self.calls += 1
        if opts["start"] == opts["end"] and self.first_baseline is None:
            self.first_baseline = time.perf_counter() - self.started
        await asyncio.sleep(.002)  # deterministic local stand-in, no network
        rows = [{"time": opts["start"], "close": "100", "volume": "100"}]
        if opts["start"] != opts["end"]:
            rows.append({"time": opts["end"], "close": "101", "volume": "100"})
        return {"symbol": symbol, "status": "ok", "rows": rows, "source": "controlled benchmark", "adjustment": opts["adjust"]}


async def measure(path, historical, old=False):
    store = Store(path)
    if old:
        for name, method in baseline_methods().items():
            setattr(store, name, MethodType(method, store))
    tracemalloc.start()
    start = time.perf_counter()
    supplier = Supplier(start)
    result = await store.forecast_tick({}, at=DUE, provider=supplier)
    elapsed = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {"elapsed_seconds": round(elapsed, 6), "python_peak_bytes": peak,
            "supplier_history_requests": supplier.calls,
            "first_baseline_request_seconds": round(supplier.first_baseline, 6) if supplier.first_baseline is not None else None,
            "first_scores_completed": len(set(result["evaluated"]) - historical),
            "historical_reviews_completed": len(set(result["evaluated"]) & historical),
            "new_ready_batches": len(result["batches"])}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="seventh-scheduler-benchmark-") as directory:
        root = Path(directory)
        historical, first = fixture(root / "base.sqlite3", args.size)
        for name in ("before", "after"):
            shutil.copy2(root / "base.sqlite3", root / (name + ".sqlite3"))
        result = {"fixture": {"forecast_count": args.size, "historical_reviews": len(historical), "first_due": first,
                              "accounts": 10, "same_security_and_dates": True, "supplier": "local async fixture; 2 ms per history call"},
                  "environment": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
                  "baseline_commit": BASELINE, "repetitions": 1,
                  "before": await measure(root / "before.sqlite3", historical, old=True),
                  "after": await measure(root / "after.sqlite3", historical),
                  "limitations": "Single controlled run; Python tracemalloc excludes SQLite/native memory. Setup/migration excluded. Highly reusable same-symbol data deliberately tests deduplication, not worst-case throughput or production SLA."}
    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    asyncio.run(main())
