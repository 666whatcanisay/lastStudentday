# -*- coding: utf-8 -*-
"""
数据维护路由（方案 B）
  POST /api/maintenance/backup         手动一致性备份（ZIP 下载；忙返回 409）
  GET  /api/maintenance/restore-info   恢复说明与脚本入口（只给相对路径，不回显绝对路径）
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import APP_VERSION, BASE_DIR, DATA_ROOT, SCHEMA_VERSION
from app.database import get_db
from app.schemas import ApiResponse
from app.services import backup as backup_svc
from app.services.backup_gate import BackupTimeout

router = APIRouter(prefix="/api/maintenance", tags=["数据维护"])
logger = logging.getLogger(__name__)


@router.post("/backup", response_model=None)
def create_backup(db: Session = Depends(get_db)):
    """
    手动备份：写入门禁（60s 等待）→ 一致性 ZIP → 直接下载。
    ⚠️ 备份未加密且含全部文档内容，请下载后另存到其他磁盘。
    """
    try:
        zip_path = backup_svc.create_backup_zip(db)
    except BackupTimeout as e:
        return ApiResponse.error(msg=str(e), code=409)
    except backup_svc.BackupError as e:
        return ApiResponse.error(msg=str(e), code=500)
    except Exception as e:
        logger.error(f"备份失败: {e}", exc_info=True)
        return ApiResponse.error(msg="备份失败，详情见服务端日志", code=500)

    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=zip_path.name,
    )


def _rel(p) -> str:
    """只回显**项目内相对路径**：绝对路径会暴露本机目录结构（任务书 §4.1）"""
    try:
        return str(p.relative_to(BASE_DIR)).replace("\\", "/")
    except Exception:
        return p.name


@router.get("/restore-info", response_model=ApiResponse)
def restore_info():
    """
    恢复说明（供界面展示）：脚本入口、数据目录、版本信息与步骤。

    刻意**不提供**「运行中切库」HTTP 接口 —— 恢复必须是受控停服务流程，
    由本地维护脚本执行（任务书 3.6）。
    """
    data_is_default = str(DATA_ROOT) == str(BASE_DIR)
    return ApiResponse.success(data={
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "script": "scripts/restore_backup.py",
        "backup_dir": _rel(backup_svc.BACKUP_DIR),
        "data_root": "项目根目录（默认）" if data_is_default else f"自定义数据目录：{DATA_ROOT.name}",
        "data_root_note": "可用环境变量 DOC_OCR_DATA_DIR 指向其他目录；备份/恢复都在该目录下进行",
        "command": "scripts\\restore_backup.bat",
        "steps": [
            "1. 先手动备份并下载 ZIP（本页上方按钮），另存到其他磁盘。",
            "2. 关闭正在运行的服务（启动脚本启动的服务可由恢复脚本按记录安全停止）。",
            "3. 双击运行 scripts\\restore_backup.bat，选择要恢复的 ZIP 并确认。",
            "4. 脚本会预检 ZIP 安全性与清单 hash，解包到暂存目录并校验数据库完整性。",
            "5. 校验通过才切换数据；切换前会自动生成恢复前安全快照，失败自动回滚。",
            "6. 恢复完成后手动运行 scripts\\start_all.bat，等待健康检查通过并核对原件。",
        ],
        "warnings": [
            "备份未加密且包含全部文档内容与问答记录，请勿放在共享目录或云盘。",
            "恢复会覆盖当前数据目录内容；脚本保留恢复前快照，但不会做数据合并。",
            "不提供运行中的在线切库接口；服务运行中恢复会导致数据不一致。",
        ],
    })
