"""macOS-friendly detached server control. No shell sourcing of secret files."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["start", "stop", "status"])
    p.add_argument("--mode", choices=["live", "demo"], default=os.getenv("APP_MODE", "live"))
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args()
    mode, port = args.mode, args.port or (8766 if args.mode == "demo" else int(os.getenv("APP_PORT", "8765")))
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    pidfile, logfile = data / f"{mode}.pid", data / f"{mode}.log"
    existing = None
    if pidfile.exists():
        try:
            candidate = int(pidfile.read_text())
            command = subprocess.check_output(["ps", "-p", str(candidate), "-o", "command="], text=True).strip()
            if str(ROOT / "scripts" / "serve.py") in command:
                existing = candidate
        except (ValueError, subprocess.CalledProcessError):
            pass
    if args.action == "stop":
        if existing:
            os.kill(existing, signal.SIGTERM)
            for _ in range(50):
                try:
                    os.kill(existing, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            print(f"已向 {mode} 服务发送停止信号（PID {existing}）；持仓及任务保存在数据库。")
        else:
            print(f"{mode} 服务未运行。")
        pidfile.unlink(missing_ok=True)
        return
    if args.action == "status":
        print(f"{mode}: " + (f"PID {existing}" if existing else "未运行"))
        return
    if existing:
        print(f"{mode} 已运行（PID {existing}），http://127.0.0.1:{port}")
        return
    env = {**os.environ, "APP_MODE": mode, "APP_PORT": str(port)}
    with open(logfile, "a") as out:
        logfile.chmod(0o600)
        child = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "serve.py")], cwd=ROOT,
                                 env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    pidfile.write_text(str(child.pid))
    for _ in range(50):
        if child.poll() is not None:
            pidfile.unlink(missing_ok=True)
            raise SystemExit(f"启动失败，查看 {logfile}")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                status = json.load(response)
                if status["mode"] == mode:
                    print(f"观澜已在后台启动： http://127.0.0.1:{port} （{mode}，PID {child.pid}）\n日志：{logfile}")
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"启动检查超时。检查 {logfile} 和端口 {port}。")


if __name__ == "__main__":
    main()
