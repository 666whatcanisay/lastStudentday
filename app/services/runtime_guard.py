"""Cross-process data-directory ownership for the server and offline restore.

The lock is held by an OS file handle, not by the existence of a PID file. It is
released on a crash, while a stale/reused PID never grants permission to stop a
different process. This module deliberately does not import application config.
"""
import json
import os
import subprocess
from pathlib import Path


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.new')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def process_identity(pid: int) -> dict:
    """Read PID creation identity and command line; never execute either value."""
    if os.name != 'nt':
        proc = Path('/proc') / str(int(pid))
        if not proc.exists():
            return {}
        return {'pid': int(pid), 'created': (proc / 'stat').read_text().split()[21],
                'command': (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode()}
    command = (
        '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); '
        f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}"; '
        'if ($p) { [ordered]@{pid=$p.ProcessId; '
        'created=$p.CreationDate.ToUniversalTime().Ticks.ToString(); '
        'command=$p.CommandLine} | ConvertTo-Json -Compress }'
    )
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('无法核实服务进程身份，请手动关闭服务')
    return json.loads(result.stdout.strip()) if result.stdout.strip() else {}


class DataDirectoryLock:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.stream = None

    def acquire(self):
        path = self.root / 'data/runtime/instance.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError('此数据目录正在由服务或维护任务使用，请先停止对应服务') from exc
        self.stream = stream
        return self

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
