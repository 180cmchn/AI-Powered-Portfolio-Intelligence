"""Explicit local HTTP/start/stop/restart/restore smoke test using a temporary demo DB.

Does not read or alter user accounts, PID files, .env, or running services.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="guanlan-lifecycle-") as folder:
        work = Path(folder)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        db = work / "demo.sqlite3"
        env = {**os.environ, "APP_DB_PATH": str(db), "APP_MODE": "demo", "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
               "MODEL_NAME": "", "SEARCH_PROVIDER": "disabled", "QUOTE_PROVIDER": "manual"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=data, headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=2) as response:
                return json.load(response)

        def launch():
            with (work / "service.log").open("a") as log:
                process = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve.py")], cwd=ROOT, env=env, stdout=log, stderr=log)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("临时服务启动失败")
                try:
                    if request("/api/status")["worker"]["running"]:
                        return process
                except OSError:
                    pass
                time.sleep(0.05)
            process.terminate()
            process.wait(timeout=10)
            raise RuntimeError("临时服务启动超时")

        def stop(process):
            process.terminate()
            process.wait(timeout=10)
            # Uvicorn re-raises the received SIGTERM after graceful lifespan shutdown.
            assert process.returncode in (0, -signal.SIGTERM)
            assert f"Finished server process [{process.pid}]" in (work / "service.log").read_text()

        process = launch()
        try:
            account = request("/api/accounts", {"name": "生命周期验证", "cash": "100", "holdings": []})
            for _ in range(100):
                current = request("/api/accounts/" + account["id"])
                if current["analysis_state"] == "current":
                    break
                time.sleep(0.05)
            assert current["analysis_state"] == "current"
        finally:
            stop(process)
        process = launch()
        try:
            recovered = request("/api/accounts/" + account["id"])
            assert recovered["version"] == 1 and recovered["cash"] == "100"
        finally:
            stop(process)
        backup = work / "backup.sqlite3"
        with sqlite3.connect(db) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        restored = work / "restored.sqlite3"
        subprocess.run([sys.executable, str(ROOT / "scripts/restore.py"), str(backup), "--mode", "demo"],
                       cwd=ROOT, env={**env, "APP_DB_PATH": str(restored)}, check=True, stdout=subprocess.DEVNULL)
        with sqlite3.connect(restored) as result:
            assert result.execute("SELECT cash FROM accounts WHERE id=?", (account["id"],)).fetchone()[0] == "100"
            assert result.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print("PASS: temporary demo HTTP service start, background analysis, graceful stop, restart, schema-v3 restore; no model or external data called")


if __name__ == "__main__":
    main()
