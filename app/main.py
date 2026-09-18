"""
轻量化智能文档OCR+文本信息抽取系统
FastAPI 后端服务入口

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
访问: http://127.0.0.1:8000
API文档: http://127.0.0.1:8000/docs
"""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import APP_NAME, APP_VERSION, STATIC_DIR
from app.database import init_database
from app.routers import file, ocr, document, rag

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ==================== FastAPI 应用 ====================
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="B/S架构文档智能处理平台：图片/PDF上传 → 图像预处理 → 轻量化OCR识别 → 结构化字段抽取 → 检索导出",
)

# CORS 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 全局异常捕获 ====================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"全局异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "msg": f"系统内部错误: {str(exc)}", "data": None},
    )


# ==================== 启动事件 ====================
@app.on_event("startup")
async def startup_event():
    """应用启动：初始化数据库"""
    logger.info(f"=== {APP_NAME} v{APP_VERSION} 启动中 ===")
    init_database()
    logger.info("数据库初始化完成")

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

    logger.info("=== 系统启动完成 ===")


# ==================== 注册路由 ====================
app.include_router(file.router)
app.include_router(ocr.router)
app.include_router(document.router)
app.include_router(rag.router)


# ==================== 健康检查 ====================
@app.get("/api/health")
def health_check():
    """系统健康检查接口"""
    from app.services.ocr_engine import get_engine_info
    return {"code": 200, "msg": "success", "data": {"status": "running", "version": APP_VERSION, "ocr_engine": get_engine_info()}}


# ==================== 静态文件服务（前端页面） ====================
# CDN 版前端（原始版）— 访问 /static/* 时使用
STATIC_VITE_DIR = Path(__file__).resolve().parent / "static_vite"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static_cdn")

# Vite 构建版前端 — 挂载到 /vite/ 子路径（含 assets 子目录）
# 注意：mount 必须放在所有 @app.get 路由之后，否则会覆盖 API 路由
# Vite 的 index.html 通过 / 路由返回，assets 通过 /vite/assets/ 访问


@app.get("/")
def index():
    """返回前端首页 — 优先使用 Vite 构建版，回退到 CDN 版"""
    vite_index = STATIC_VITE_DIR / "index.html"
    if vite_index.exists():
        return FileResponse(str(vite_index))
    cdn_index = STATIC_DIR / "index.html"
    if cdn_index.exists():
        return FileResponse(str(cdn_index))
    return JSONResponse({"code": 200, "msg": f"{APP_NAME} v{APP_VERSION} 后端服务运行中，请配置前端页面", "data": None})


@app.get("/cdn")
def cdn_index():
    """强制返回 CDN 版前端页面"""
    cdn_file = STATIC_DIR / "index.html"
    if cdn_file.exists():
        return FileResponse(str(cdn_file))
    return JSONResponse({"code": 404, "msg": "CDN版前端不存在", "data": None})


# Vite 静态资源挂载（放在所有路由之后，mount 作为子应用）
# 访问 /vite/assets/* 即可获取 Vite 构建的 JS/CSS 文件
if STATIC_VITE_DIR.exists():
    app.mount("/vite", StaticFiles(directory=str(STATIC_VITE_DIR), html=True), name="static_vite")
