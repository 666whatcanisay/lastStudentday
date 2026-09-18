"""
系统配置模块
轻量化智能文档OCR+文本信息抽取系统
"""
import os
from pathlib import Path

# ==================== 路径配置 ====================
# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 上传文件存储目录
UPLOAD_DIR = BASE_DIR / "uploads"
# SQLite 数据库文件路径
DATABASE_PATH = BASE_DIR / "data" / "doc_ocr_v2.db"
# 前端静态文件目录
STATIC_DIR = BASE_DIR / "app" / "static"
# 模型文件目录
MODEL_DIR = BASE_DIR / "models"

# 确保目录存在
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ==================== 文件上传配置 ====================
# 允许的文件扩展名
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".pdf"}
# 单文件最大大小 (50MB)
MAX_FILE_SIZE = 50 * 1024 * 1024

# ==================== OCR 配置 ====================
# OCR 引擎类型: rapidocr / tesseract
OCR_ENGINE = os.getenv("OCR_ENGINE", "rapidocr")
# 是否启用图像预处理（轻量化优化：预处理减少推理计算量）
ENABLE_PREPROCESS = True
# 是否启用轻量化优化模式
LIGHTWEIGHT_MODE = True

# ==================== 数据库配置 ====================
# SQLite 数据库 URL
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# ==================== 应用配置 ====================
APP_NAME = "轻量化智能文档OCR+文本信息抽取系统"
APP_VERSION = "1.0.0"
API_PREFIX = "/api"

# ==================== 正则抽取配置 ====================
# 文档编号正则模式（匹配常见单据编号格式）
DOC_NUMBER_PATTERNS = [
    r'(?:单据编号|编号|单号|凭证编号|订单号|合同编号)[：:\s]*([A-Za-z0-9\-_]+)',
    r'(?:No\.?|NO\.?|Number)[：:\s]*([A-Za-z0-9\-_]+)',
    r'\b([A-Z]{1,6}[-]?\d{4,})\b',  # 如 INV-20240101, HT20240101
]

# 日期正则模式
DATE_PATTERNS = [
    r'(?:日期|单据日期|开票日期|签订日期|时间)[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
    r'\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b',
    r'\b(\d{4}年\d{1,2}月\d{1,2}日)\b',
]

# 金额正则模式
AMOUNT_PATTERNS = [
    r'(?:金额|合计|总计|总额|价税合计|小计|实付|应付|总金额)[：:\s￥¥$]*([0-9,]+\.?\d*)\s*(?:元)?',
    r'[￥¥$]\s*([0-9,]+\.?\d*)',
    r'\b([0-9]{1,3}(?:,[0-9]{3})*\.\d{2})\s*元?\b',  # 1,234.56元
]
