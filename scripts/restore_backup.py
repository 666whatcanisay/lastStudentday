"""Offline restore: validate first, lock data directory, snapshot, switch, recover.

With no argument opens a file picker. --assume-stopped never bypasses the lock.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from app.services.runtime_guard import DataDirectoryLock, atomic_json, process_identity

LOG_NAME = 'restore_log.json'
MAX_UNPACKED = 20 * 1024**3
MAX_METADATA = 8 * 1024**2


class RestoreError(RuntimeError):
    pass


def _log(data_dir, **fields):
    # Never inherit a previous restore's snapshot pointer.
    atomic_json(data_dir / 'data' / LOG_NAME,
                dict(fields, updated_at=datetime.now().isoformat(timespec='seconds')))


def _sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise RestoreError('备份成员路径不合法')
    parts = name.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts):
        raise RestoreError('备份成员路径不合法')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1, 10)],
                *[f'LPT{i}' for i in range(1, 10)]}
    if any(p.split('.')[0].upper() in reserved for p in parts):
        raise RestoreError('备份成员包含保留名称')
    return name


def _bounded(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise RestoreError('维护路径越出目标目录，已停止')
    return path


def _remove_tree(root, path):
    path = _bounded(root, path)
    if path.exists():
        for entry in [path, *path.rglob('*')]:
            if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                raise RestoreError('维护目录含链接，已停止删除')
        shutil.rmtree(path)


def precheck(zip_path):
    if not zip_path.is_file() or not zipfile.is_zipfile(zip_path):
        raise RestoreError('不是有效备份 ZIP')
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        names = [_safe_name(i.filename) for i in entries]
        if len({n.casefold() for n in names}) != len(names):
            raise RestoreError('ZIP 有重复或大小写冲突成员')
        if any(stat.S_ISLNK(i.external_attr >> 16) or i.is_dir() for i in entries):
            raise RestoreError('ZIP 含链接或不支持的目录条目')
        for required in ('backup_meta.json', 'manifest.json', 'data/doc_ocr_v2.db'):
            if required not in names:
                raise RestoreError(f'备份缺少 {required}')
        for name in ('backup_meta.json', 'manifest.json'):
            if zf.getinfo(name).file_size > MAX_METADATA:
                raise RestoreError('备份清单过大')
        meta = json.loads(zf.read('backup_meta.json'))
        if meta.get('format_version') != 1 or meta.get('schema_version') not in (1, 2):
            raise RestoreError('不支持的备份/schema 版本，请先升级应用')
        manifest = json.loads(zf.read('manifest.json'))
        seen, total = set(), 0
        for item in manifest['files']:
            name = _safe_name(item['path'])
            if name != 'data/doc_ocr_v2.db' and not name.startswith('uploads/'):
                raise RestoreError('清单含非数据库或原件成员')
            if name.casefold() in seen or name not in names:
                raise RestoreError('清单重复或与 ZIP 不对应')
            seen.add(name.casefold())
            size = item['size']
            if not isinstance(size, int) or size < 0 or zf.getinfo(name).file_size != size:
                raise RestoreError('成员大小与清单不符')
            digest = item['sha256']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise RestoreError('非法 SHA-256 清单')
            total += size
        if seen != {n.casefold() for n in names if n not in ('manifest.json', 'backup_meta.json')}:
            raise RestoreError('清单未完整覆盖 ZIP')
        if total > MAX_UNPACKED:
            raise RestoreError('备份解包超过 20 GiB 本机维护限制')
    return {'meta': meta, 'manifest': manifest, 'total': total}


def stop_service(data_dir, assume_stopped):
    if assume_stopped:
        return
    pid_file = data_dir / 'data/runtime/server.pid'
    if not pid_file.exists():
        return
    info = json.loads(pid_file.read_text(encoding='utf-8-sig'))
    actual = process_identity(int(info['pid']))
    if not actual:
        return
    if (not info.get('identity') or actual != info['identity'] or
            Path(info.get('data_root', '')).resolve() != data_dir.resolve() or
            Path(info.get('workdir', '')).resolve() != PROJECT_ROOT.resolve() or
            'uvicorn' not in actual.get('command', '').lower()):
        raise RestoreError('服务进程身份无法复核，请手动停止后再恢复')
    print(f"停止已复核服务 PID={actual['pid']}")
    if os.name == 'nt':
        # Console Python has no window to receive taskkill's default close
        # message. Only this identity-verified PID is terminated, never /T.
        result = subprocess.run(['taskkill', '/F', '/PID', str(actual['pid'])], capture_output=True)
        if result.returncode:
            raise RestoreError('停止服务失败，请手动停止')
    else:
        import signal
        os.kill(actual['pid'], signal.SIGTERM)
    for _ in range(20):
        current = process_identity(actual['pid'])
        if not current or current != actual:
            return
        time.sleep(.5)
    raise RestoreError('服务未停止，尚未修改数据')


def _upload_relative(stored):
    normalized = str(stored).replace('\\', '/')
    if 'uploads/' not in normalized:
        raise RestoreError('数据库原件路径无法映射到 uploads')
    return _safe_name(normalized.split('uploads/', 1)[1])


def stage_and_verify(zip_path, staging, manifest):
    if staging.exists():
        raise RestoreError('暂存目录已存在，拒绝覆盖')
    staging.mkdir(parents=True)
    (staging / 'uploads').mkdir()
    expected = {f['path']: f for f in manifest['files']}
    with zipfile.ZipFile(zip_path) as zf:
        for name, item in expected.items():
            out = _bounded(staging, staging / _safe_name(name))
            out.parent.mkdir(parents=True, exist_ok=True)
            count, digest = 0, hashlib.sha256()
            with zf.open(name) as src, out.open('xb') as dst:
                while chunk := src.read(1024 * 1024):
                    count += len(chunk)
                    if count > item['size']:
                        raise RestoreError('解包实际字节数超过清单')
                    digest.update(chunk)
                    dst.write(chunk)
            if count != item['size'] or digest.hexdigest() != item['sha256']:
                raise RestoreError(f'成员大小/hash 校验失败: {name}')
    conn = sqlite3.connect(str(staging / 'data/doc_ocr_v2.db'))
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RestoreError('数据库完整性检查失败')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RestoreError('数据库外键检查失败')
        for doc_id, stored in conn.execute('SELECT id, file_path FROM document_info').fetchall():
            if f'uploads/{_upload_relative(stored)}' not in expected:
                raise RestoreError(f'文档 #{doc_id} 缺少原件')
    finally:
        conn.close()


def make_snapshot(data_dir, ts):
    snap = _bounded(data_dir / 'data', data_dir / 'data' / f'pre_restore_snapshot_{ts}')
    snap.mkdir(parents=True, exist_ok=False)
    for item in ('doc_ocr_v2.db', 'doc_ocr_v2.db-wal', 'doc_ocr_v2.db-shm'):
        src = _bounded(data_dir, data_dir / 'data' / item)
        if src.exists():
            shutil.copy2(src, snap / item)
    up = _bounded(data_dir, data_dir / 'uploads')
    if up.exists():
        shutil.copytree(up, snap / 'uploads')
    return snap


def switch_data(data_dir, staging):
    for item in ('doc_ocr_v2.db', 'doc_ocr_v2.db-wal', 'doc_ocr_v2.db-shm'):
        _bounded(data_dir, data_dir / 'data' / item).unlink(missing_ok=True)
    shutil.copy2(staging / 'data/doc_ocr_v2.db', data_dir / 'data/doc_ocr_v2.db')
    _remove_tree(data_dir, data_dir / 'uploads')
    shutil.copytree(staging / 'uploads', data_dir / 'uploads')


def relocate_paths(data_dir):
    with sqlite3.connect(str(data_dir / 'data/doc_ocr_v2.db')) as conn:
        for doc_id, stored in conn.execute('SELECT id, file_path FROM document_info').fetchall():
            target = _bounded(data_dir / 'uploads', data_dir / 'uploads' / _upload_relative(stored))
            conn.execute('UPDATE document_info SET file_path=? WHERE id=?', (str(target), doc_id))


def rollback(data_dir, snap):
    snap = _bounded(data_dir / 'data', snap)
    if not snap.is_dir() or not snap.name.startswith('pre_restore_snapshot_'):
        raise RestoreError('恢复前快照不存在或位置不合法')
    for item in ('doc_ocr_v2.db', 'doc_ocr_v2.db-wal', 'doc_ocr_v2.db-shm'):
        target = _bounded(data_dir, data_dir / 'data' / item)
        target.unlink(missing_ok=True)
        if (snap / item).exists():
            shutil.copy2(snap / item, target)
    _remove_tree(data_dir, data_dir / 'uploads')
    if (snap / 'uploads').exists():
        shutil.copytree(snap / 'uploads', data_dir / 'uploads')


def recover_pending(data_dir):
    """Caller holds directory lock. Run before application opens its database."""
    path = data_dir / 'data' / LOG_NAME
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('status') in ('done', 'failed', 'rolled_back'):
        return
    if state.get('phase') in ('switching', 'migrating', 'rollback_failed'):
        snapshot = Path(state.get('snapshot', ''))
        rollback(data_dir, snapshot)
        _log(data_dir, status='rolled_back', phase='rolled_back', snapshot=str(snapshot))
    elif state.get('phase') in ('precheck', 'staged', 'snapshot'):
        _log(data_dir, status='failed', phase='early', note='切换前中断，原数据未动')
    else:
        raise RestoreError('存在无法自动处理的恢复日志，请检查安全快照后再启动')


def migrate_restored(data_dir):
    env = dict(os.environ, DOC_OCR_DATA_DIR=str(data_dir), LLM_API_KEY='')
    result = subprocess.run([sys.executable, '-c',
        "from app.database import init_database; init_database(); print('MIGRATE_OK')"],
        cwd=str(PROJECT_ROOT), env=dict(env, PYTHONUTF8='1'), capture_output=True,
        text=True, encoding='utf-8', timeout=120)
    if result.returncode or 'MIGRATE_OK' not in result.stdout:
        raise RestoreError('恢复后迁移失败，正在回退，请保留维护日志供排查')


def main():
    parser = argparse.ArgumentParser(description='校验备份并受控停机恢复')
    parser.add_argument('zip_path', type=Path, nargs='?')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--assume-stopped', action='store_true')
    args = parser.parse_args()
    if args.zip_path is None:
        import tkinter as tk
        from tkinter.filedialog import askopenfilename
        from tkinter.messagebox import askyesno
        window = tk.Tk()
        window.withdraw()
        chosen = askopenfilename(title='选择文档系统备份', filetypes=[('备份 ZIP', '*.zip')])
        confirmed = chosen and askyesno('确认恢复', '恢复会整体替换当前数据并保留安全快照。是否继续？')
        window.destroy()
        if not confirmed:
            return 1
        args.zip_path = Path(chosen)
    data_dir = (args.data_dir or Path(os.getenv('DOC_OCR_DATA_DIR', str(PROJECT_ROOT)))).resolve()
    if data_dir in (Path.home(), Path(data_dir.anchor)):
        raise RestoreError('不能把磁盘根目录或用户主目录作为恢复目标')
    data_dir.mkdir(parents=True, exist_ok=True)
    token = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    staging = data_dir / 'data' / f'restore_staging_{token}'
    snap, guard, switched = None, DataDirectoryLock(data_dir), False
    try:
        info = precheck(args.zip_path.resolve())
        current_size = sum(p.stat().st_size for p in (data_dir / 'uploads').rglob('*') if p.is_file())
        if shutil.disk_usage(data_dir).free < info['total'] * 2 + current_size + 64 * 1024**2:
            raise RestoreError('磁盘空间不足，尚未修改当前数据')
        stage_and_verify(args.zip_path.resolve(), staging, info['manifest'])
        stop_service(data_dir, args.assume_stopped)
        guard.acquire()
        recover_pending(data_dir)
        _log(data_dir, status='in_progress', phase='staged')
        snap = make_snapshot(data_dir, token)
        _log(data_dir, status='in_progress', phase='switching', snapshot=str(snap))
        switched = True
        switch_data(data_dir, staging)
        relocate_paths(data_dir)
        _log(data_dir, status='in_progress', phase='migrating', snapshot=str(snap))
        migrate_restored(data_dir)
        _log(data_dir, status='done', phase='done', snapshot=str(snap))
        print(f'恢复完成；安全快照：{snap}。请双击 scripts/start_all.bat 启动并检查数据。')
        return 0
    except Exception as exc:
        print(f'恢复失败：{exc}', file=sys.stderr)
        if switched:
            try:
                rollback(data_dir, snap)
                _log(data_dir, status='rolled_back', phase='rolled_back', snapshot=str(snap))
            except Exception:
                _log(data_dir, status='rollback_failed', phase='rollback_failed', snapshot=str(snap))
                print('自动回退失败，启动已被阻止，请保留安全快照。', file=sys.stderr)
        return 1
    finally:
        guard.release()
        if staging.exists():
            _remove_tree(data_dir, staging)


if __name__ == '__main__':
    raise SystemExit(main())
