"""
轻量化智能文档OCR+文本信息抽取系统
FastAPI 后端服务入口

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
访问: http://127.0.0.1:8000
API文档: http://127.0.0.1:8000/docs
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import (
    APP_NAME, APP_VERSION, STATIC_DIR, MAX_FILE_SIZE, MAX_UPLOAD_BODY_SIZE,
)
from app.database import init_database
from app.routers import file, ocr, document, rag, maintenance, auth
from app.services.local_security import (
    LOCAL_CORS_ORIGINS, local_security_middleware,
)
from app.services.demo_auth import DEMO_AUTH

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
from app.config import DATA_ROOT, BASE_DIR
from app.services.runtime_guard import DataDirectoryLock, atomic_json, process_identity
_runtime_guard = DataDirectoryLock(DATA_ROOT)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Own the complete process lifecycle and always release the data lock."""
    try:
        await startup_event()
    except Exception:
        _runtime_guard.release()
        raise
    try:
        yield
    finally:
        shutdown_event()

# ==================== FastAPI 应用 ====================
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="B/S架构文档智能处理平台：图片/PDF上传 → 图像预处理 → 轻量化OCR识别 → 结构化字段抽取 → 检索导出",
    lifespan=lifespan,
)

# CORS 跨域配置（方案 B：本机白名单，不再 "*" + credentials 的无效且危险组合）
app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 上传体积前置拦截 ====================
# 为什么需要它：Starlette 解析 multipart 时**整个请求体已经先收完**
# （超过 1MB 的部分会落到临时文件）。等进到路由函数再校验大小，
# 磁盘和带宽早就被消耗掉了 —— 超大请求仍能把服务拖垮。
# 这里在读 body 之前只看 Content-Length，超限直接 413 返回。
#
# ⚠️ 两层缺一不可：Content-Length 缺失（chunked 传输）时这里拦不住，
# 路由内的分块累计校验（app/routers/file.py）是兜底。
#
# 说明：中间件体是纯头部判断 + await call_next，不含同步阻塞调用，
# 不会重新引入 P0-1 的队头阻塞问题。
@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    if request.url.path == "/api/file/upload":
        raw = request.headers.get("content-length")
        if raw and raw.isdigit() and int(raw) > MAX_UPLOAD_BODY_SIZE:
            logger.warning(
                f"上传前置拦截: Content-Length={raw} 超过上限 {MAX_UPLOAD_BODY_SIZE}"
            )
            return JSONResponse(
                status_code=413,
                content={
                    "code": 413,
                    "msg": f"文件大小超过限制({MAX_FILE_SIZE // 1024 // 1024}MB)",
                    "data": None,
                },
            )
    return await call_next(request)


# 演示认证先于请求体读取；Host/Origin 校验在最外层执行。
app.middleware("http")(DEMO_AUTH.middleware)
app.middleware("http")(local_security_middleware)


# ==================== 全局异常捕获 ====================
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code,
                        content={'code': exc.status_code, 'msg': str(exc.detail), 'data': None})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # 方案 B：对外只给追踪 ID，不回显路径/SQL/Key/堆栈；细节进日志（可脱敏检索）
    import uuid
    trace_id = uuid.uuid4().hex[:8]
    logger.error(f"全局异常 [trace={trace_id}] {request.method} {request.url.path}: {exc}",
                 exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500,
                 "msg": f"系统内部错误，请稍后重试（追踪ID: {trace_id}）",
                 "data": None},
    )


# ==================== 启动事件 ====================
async def startup_event():
    """应用启动：初始化数据库"""
    logger.info(f"=== {APP_NAME} v{APP_VERSION} 启动中 ===")
    _runtime_guard.acquire()
    from scripts.restore_backup import recover_pending
    recover_pending(DATA_ROOT)
    init_database()
    atomic_json(DATA_ROOT / 'data/runtime/server.pid', {
        'pid': os.getpid(), 'identity': process_identity(os.getpid()),
        'workdir': str(BASE_DIR), 'data_root': str(DATA_ROOT),
    })
    logger.info("数据库初始化完成")

    # 清理本应用遗留临时产物（仅限 config.TMP_DIR 下、本应用前缀的产物，
    # 绝不触碰系统临时目录或用户文件）
    from app.config import TMP_DIR
    if TMP_DIR.exists():
        cleaned = 0
        for p in TMP_DIR.iterdir():
            if p.name.startswith(("backup_staging_", "export_")):
                try:
                    if p.is_dir():
                        import shutil
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink()
                    cleaned += 1
                except Exception as e:
                    logger.warning(f"清理临时产物失败 {p.name}: {e}")
        if cleaned:
            logger.info(f"已清理遗留临时产物 {cleaned} 项")

    # 预加载 OCR 引擎
    from app.services.ocr_engine import get_ocr_engine, get_engine_info
    get_ocr_engine()
    info = get_engine_info()
    logger.info(f"OCR引擎: {info['name']}")

    # 预热 RAG 向量模型（失败不阻断启动，检索自动降级为 BM25 单路）
    from app.config import RAG_ENABLED
    if RAG_ENABLED:
        from app.services.embedding import get_embedding_model
        emb = get_embedding_model()
        if emb.load():
            logger.info(f"RAG向量模型: {emb.get_info()['name']} ({emb.dim}维)")
        else:
            logger.warning("RAG向量模型加载失败，检索将降级为 BM25 单路（功能仍可用）")

        # 启动补偿：补齐"当前有效、未删除、索引缺失/过期/失败"的文档索引。
        # ⚠️ 只补索引，**不重新 OCR**（任务书 3.2）。守护线程不阻塞启动。
        from app.services.rag_indexer import compensate_on_startup
        compensate_on_startup()

    logger.info("=== 系统启动完成 ===")


def shutdown_event():
    from app.database import engine
    engine.dispose()
    _runtime_guard.release()


# ==================== 注册路由 ====================
app.include_router(file.router)
app.include_router(ocr.router)
app.include_router(document.router)
app.include_router(rag.router)
app.include_router(maintenance.router)
app.include_router(auth.router)


# ==================== 健康检查 ====================
@app.get("/api/health")
def health_check():
    """系统健康检查接口"""
    from app.services.ocr_engine import get_engine_info
    return {"code": 200, "msg": "success", "data": {"status": "running", "version": APP_VERSION, "ocr_engine": get_engine_info()}}


# ==================== 静态文件服务（前端页面） ====================
# 正式前端**只维护 Vite 构建版**（任务书 §4.1）：
#   · 旧 CDN 版源码保留在仓库里（本轮不删除），但运行入口退役
#   · `/static` 不再挂载，`/cdn` 只返回停用说明与正式入口
#   · `/` 缺少 Vite 构建时给明确的构建提示，**不回退旧界面**
#     —— 回退会造成"改动没生效"的假象：用户以为在用新界面，其实是旧 CDN 页
STATIC_VITE_DIR = Path(__file__).resolve().parent / "static_vite"
VITE_BUILD_HINT = (
    "前端尚未构建。请在项目根目录执行：\n"
    "    cd frontend\n"
    "    npm install\n"
    "    npm run build\n"
    "构建产物会输出到 app/static_vite/，然后刷新本页即可。\n"
    "开发调试可用 `npm run dev`，访问 http://localhost:5173/vite/。"
)


@app.get("/")
def index():
    """返回前端首页（仅 Vite 构建版；缺构建时给构建提示）"""
    vite_index = STATIC_VITE_DIR / "index.html"
    if vite_index.exists():
        return FileResponse(str(vite_index))
    return JSONResponse(status_code=503, content={
        "code": 404,
        "msg": f"{APP_NAME} v{APP_VERSION}：{VITE_BUILD_HINT}",
        "data": None,
    })


@app.get("/cdn")
def cdn_index():
    """旧 CDN 版前端入口已停用（保留路由只为给出可操作的说明，不做 302）"""
    return JSONResponse(status_code=410, content={
        "code": 410,
        "msg": "CDN 版前端已停用。正式前端只有 Vite 构建版，请访问 / 使用；"
               "若首页提示未构建，请按提示执行 npm run build。",
        "data": None,
    })


# Vite 静态资源挂载（放在所有路由之后，mount 作为子应用）
# 访问 /vite/assets/* 即可获取 Vite 构建的 JS/CSS 文件
if STATIC_VITE_DIR.exists():
    app.mount("/vite", StaticFiles(directory=str(STATIC_VITE_DIR), html=True), name="static_vite")
