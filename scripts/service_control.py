"""Stop only the recorded and identity-verified local server."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.restore_backup import PROJECT_ROOT, stop_service
from app.services.runtime_guard import DataDirectoryLock

if __name__ == '__main__':
    root = Path(os.getenv('DOC_OCR_DATA_DIR', str(PROJECT_ROOT))).resolve()
    try:
        stop_service(root, False)
        with DataDirectoryLock(root):
            print('本项目数据目录已停止使用。未停止其他应用。')
    except Exception as exc:
        print(f'停止失败：{exc}', file=sys.stderr)
        raise SystemExit(1)
