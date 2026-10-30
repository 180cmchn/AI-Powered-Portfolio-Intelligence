"""Detached local service control with process, database and API identity checks."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import db_path, _SHELL_ENV_NAMES
from app.runtime_version import API_CONTRACT, REQUIRED_ROUTES, env_file_stamp, source_digest, environment_launch_settings


def process_identity(pid):
    """Never return/log argv: it may contain secrets belonging to other programs."""
    try:
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True, stderr=subprocess.DEVNULL).strip()
        started = subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True, stderr=subprocess.DEVNULL).strip()
        state = subprocess.check_output(['ps', '-p', str(pid), '-o', 'stat='], text=True, stderr=subprocess.DEVNULL).strip()
        entry = str(ROOT / 'scripts' / 'serve.py')
        # Exact absolute entry, with a command token boundary at the end.
        owns = command.endswith(entry) and (command == entry or command[:-len(entry)].endswith(' '))
        return {'pid': pid, 'started': started, 'owned_entry': owns, 'alive': bool(started) and not state.startswith('Z')}
    except (OSError, subprocess.SubprocessError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except (PermissionError, OSError):
            pass
        return {'pid': pid, 'alive': True, 'owned_entry': False, 'identity': 'unavailable'}


def read_record(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {'pid': int(value), 'legacy': True}
    except (OSError, ValueError, TypeError):
        return None


def owned(record):
    if not record:
        return False
    current = process_identity(record.get('pid', 0))
    return bool(current and current['alive'] and current['owned_entry'] and
                (not record.get('started') or current['started'] == record['started']))


def write_record(path, record):
    temporary = path.with_suffix('.pid.tmp')
    temporary.write_text(json.dumps(record))
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(port, path, timeout=1):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}{path}', timeout=timeout) as response:
        if response.status != 200 or 'application/json' not in response.headers.get('Content-Type', ''):
            raise ValueError(f'GET {path}: 响应格式不兼容')
        return json.load(response)


def verify_runtime(port, mode, database, *, pid=None, instance=None, check_disk=True):
    """No inference/capability probe: just bounded local reads of required APIs."""
    health = read_json(port, '/api/health')
    version = health.get('runtime_version') or {}
    expected = {'api_contract': API_CONTRACT, 'project_path': str(ROOT), 'database_path': str(database.resolve()), 'port': port}
    if pid is not None:
        expected['pid'] = pid
    if instance is not None:
        expected['instance_id'] = instance
    differences = [name for name, value in expected.items() if version.get(name) != value]
    if health.get('mode') != mode:
        differences.append('mode')
    if check_disk and version.get('source_sha256_at_start') != source_digest():
        differences.append('source_version')
    if check_disk and version.get('env_file_at_start') != env_file_stamp(ROOT):
        differences.append('environment_file_changed')
    if check_disk and version.get('environment_settings_at_start') != environment_launch_settings():
        differences.append('environment_settings_changed')
    if differences:
        raise ValueError('实例身份或加载配置不一致：' + ', '.join(differences))
    if not health.get('ready') or not health.get('worker_running') or health.get('missing_routes'):
        raise ValueError('基本 API / 后台 worker 尚未就绪')
    schema = read_json(port, '/openapi.json').get('paths', {})
    missing = [f'{method.upper()} {path}' for path, method in REQUIRED_ROUTES.items() if method not in schema.get(path, {})]
    if missing:
        raise ValueError('接口缺失：' + ', '.join(missing))
    for path in REQUIRED_ROUTES:
        if path == '/api/health' or '{' in path:
            continue
        body = read_json(port, path)
        if path in ('/api/providers', '/api/agent-roles', '/api/accounts', '/api/research', '/api/forecast-batches') and not isinstance(body, list):
            raise ValueError(f'GET {path}: 响应格式不兼容')
        if path == '/api/forecasts' and (not isinstance(body, dict) or 'page' not in body):
            raise ValueError(f'GET {path}: 响应格式不兼容')
    return version


def stop_owned(record, timeout=10):
    if not owned(record):
        return False
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not owned(record):
            return True
        time.sleep(.1)
    return False


def verify_stop_target(record, mode, port, database):
    """A legacy PID alone cannot establish mode or the database being stopped."""
    if not record.get('legacy'):
        if record.get('mode') != mode or record.get('database_path') != str(database):
            raise ValueError('pidfile 模式/数据库与本次目标不同，请先诊断；未停止任何进程')
        actual_port = record.get('port')
        if port != actual_port:
            raise ValueError('pidfile 端口与本次目标不同，请显式指定正确 --port；未停止任何进程')
        try:
            health = read_json(actual_port, '/api/health')
        except (OSError, ValueError):
            # A hung API must still be stoppable when the recorded OS creation
            # time proves this is the exact child we launched. No PID-only fallback.
            if record.get('started') and owned(record):
                return
            raise ValueError('API不可达且缺少进程启动时间证据，未停止任何进程') from None
        version = health.get('runtime_version') or {}
        if health.get('mode') != mode or any(version.get(k) != record.get(k) for k in ('pid', 'instance_id', 'database_path', 'project_path')):
            raise ValueError('停止目标无法由运行实例确认；保留 pidfile，未停止其他进程')
        return
    status = read_json(port, '/api/status')
    if status.get('mode') != mode:
        raise ValueError('旧 pidfile 对应模式不匹配，未停止其他实例')
    pids = subprocess.check_output(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'], text=True, stderr=subprocess.DEVNULL).split()
    files = subprocess.check_output(['lsof', '-p', str(record['pid']), '-Fn'], text=True, stderr=subprocess.DEVNULL).splitlines()
    if str(record['pid']) not in pids or 'n' + str(database) + '.lock' not in files:
        raise ValueError('旧 pidfile 缺少目标端口/数据库锁的所有权证据，请运行只读诊断；未停止其他实例')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['start', 'stop', 'status'])
    p.add_argument('--mode', choices=['live', 'demo'], default=os.getenv('APP_MODE', 'live'))
    p.add_argument('--port', type=int, default=None)
    args = p.parse_args()
    mode = args.mode
    port = args.port if args.port is not None else (8766 if mode == 'demo' else int(os.getenv('APP_PORT', '8765')))
    if not 1 <= port <= 65535:
        raise SystemExit('端口必须在 1–65535')
    database = db_path(mode).resolve()
    if os.getenv('APP_HOST', '127.0.0.1') not in ('127.0.0.1', 'localhost'):
        raise SystemExit('管理入口使用 IPv4 回环 127.0.0.1；请将 APP_HOST 设为 127.0.0.1 或 localhost')
    data = Path(os.getenv('APP_RUNTIME_DIR', str(ROOT / 'data'))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    pidfile, logfile = data / f'{mode}.pid', data / f'{mode}.log'
    with open(data / f'{mode}.manage.lock', 'a+') as control:
        try:
            fcntl.flock(control, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit('另一个启停操作正在进行，请稍后重试') from None
        record = read_record(pidfile)
        existing = owned(record)
        if args.action == 'stop':
            if existing:
                try:
                    verify_stop_target(record, mode, port, database)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    raise SystemExit(f'停止目标未核实：{reason}。保留 pidfile，请检查诊断。') from None
                if not stop_owned(record):
                    raise SystemExit(f'停止超时：PID {record["pid"]} 仍存活；保留 pidfile，未强杀。请检查状态与日志。')
                pidfile.unlink(missing_ok=True)
                print(f'{mode} 已停止（PID {record["pid"]}）；持仓及任务保存在数据库。')
            elif record and process_identity(record.get('pid', 0)):
                raise SystemExit('pidfile 指向身份不符或复用的 PID；未停止其他程序，未清理记录。请运行只读诊断。')
            else:
                pidfile.unlink(missing_ok=True)
                print(f'{mode} 服务未运行。')
            return
        if existing:
            try:
                version = verify_runtime(port, mode, database, pid=record['pid'], instance=record.get('instance_id'))
            except (OSError, ValueError, KeyError) as exc:
                # Never print response bodies or a foreign service's headers.
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                raise SystemExit(f'旧实例仍运行或无法验证，不能宣称当前版本就绪（PID {record["pid"]}）：{reason}。\n需要安全重启：先运行 scripts/inspect_runtime.py，使用 scripts/backup.py 一致性备份，再手动停止并启动。') from None
            print(f'{mode} 当前版本已就绪（PID {record["pid"]}，schema {version["schema_version"]}），http://127.0.0.1:{port}')
            return
        if args.action == 'status':
            print(f'{mode}: 未找到可确认的受管实例；目标 http://127.0.0.1:{port}，数据库 {database}。运行 scripts/inspect_runtime.py 核对端口。')
            return
        if record and process_identity(record.get('pid', 0)):
            raise SystemExit('pidfile 指向身份不符或复用的 PID；未启动、未迁移数据库、未停止其他程序。请先只读核对。')
        # The child reserves the port again before constructing Store. This
        # preflight gives a useful error without opening the target database.
        try:
            with socket.socket() as reservation:
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                reservation.bind(('127.0.0.1', port))
                reservation.listen(1)
        except OSError:
            raise SystemExit(f'端口 {port} 已占用或不可绑定；未启动、未迁移数据库。请用 inspect_runtime.py 核对，不能将其他进程视为本实例。') from None
        instance = uuid.uuid4().hex
        env = {**os.environ, 'APP_MODE': mode, 'APP_PORT': str(port), 'APP_DB_PATH': str(database), 'APP_INSTANCE_ID': instance,
               'APP_DOTENV_ORIGIN_NAMES': json.dumps(sorted(set(os.environ) - _SHELL_ENV_NAMES))}
        with open(logfile, 'a') as out:
            logfile.chmod(0o600)
            child = subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'serve.py')], cwd=ROOT,
                                     env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        identity = process_identity(child.pid) or {}
        record = {'pid': child.pid, 'started': identity.get('started'), 'instance_id': instance,
                  'port': port, 'mode': mode, 'database_path': str(database), 'project_path': str(ROOT)}
        write_record(pidfile, record)
        deadline = time.monotonic() + 15
        last_reason = 'API 未响应'
        while time.monotonic() < deadline:
            if child.poll() is not None:
                pidfile.unlink(missing_ok=True)
                raise SystemExit(f'新进程启动失败（退出码 {child.returncode}），查看 {logfile}；不会将端口上的其他服务当作成功。')
            try:
                version = verify_runtime(port, mode, database, pid=child.pid, instance=instance)
                print(f'观澜已在后台启动： http://127.0.0.1:{port} （{mode}，PID {child.pid}，schema {version["schema_version"]}）\n基本 API 与实例身份已核对；模型绑定/原生搜索另行配置。日志：{logfile}')
                return
            except (OSError, ValueError, KeyError) as exc:
                last_reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            time.sleep(.2)
        # Only terminate our exact child. Never kill a port owner or delete the
        # pidfile before observing its actual exit.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise SystemExit(f'启动检查超时且子进程尚未退出；保留 PID {child.pid}，查看 {logfile}') from None
        pidfile.unlink(missing_ok=True)
        raise SystemExit(f'启动检查超时（{last_reason}）；本次子进程已退出。查看 {logfile}')


if __name__ == '__main__':
    main()
