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

# ==================== RAG 检索增强生成配置 ====================
# 开关：关闭后 RAG 相关接口直接返回不可用，不影响原有 OCR 链路
RAG_ENABLED = True

# ---------- 向量模型 ----------
# 本地 ONNX 向量模型（与项目 RapidOCR 的 ONNX 技术栈一致）
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBED_MODEL_DIR = MODEL_DIR / "bge-small-zh-v1.5"
EMBED_DIM = 512                     # bge-small-zh-v1.5 输出维度
# 模型下载镜像：本机实测 huggingface.co 不可达（返回 000），必须走 hf-mirror.com
HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")

# ---------- OCR 文本归一化 ----------
NORMALIZE_ENABLE = True
# OCR 常见错字纠正表（可持续补充，这是领域知识沉淀点）
OCR_TYPO_MAP = {
    "税奔": "税务",       # 实测出现：国家税奔总局
    "圆": "元",           # 发票大写金额单位规范：贰拾伍圆 → 贰拾伍元
}
# 是否移除数字内部误插入的空格（实测出现 31. 30 / ¥25. 23）
NORMALIZE_DIGIT_SPACE = True

# ---------- 文本分块 ----------
CHUNK_SIZE = 400        # 目标块长度（字符）
CHUNK_OVERLAP = 50      # 相邻块重叠字符数，避免答案被切断在块边界
CHUNK_MIN_LEN = 20      # 低于此长度不单独成块
CHUNK_PAIR_GAP = 3      # 标签-值配对时向下搜索的最大行数

# ---------- 双路检索 ----------
RETRIEVE_TOP_N_VEC = 20      # 向量路召回条数
RETRIEVE_TOP_N_BM25 = 20     # 关键词路召回条数
RETRIEVE_TOP_K = 5           # 融合后最终送入 Prompt 的条数

# 向量相似度阈值，低于此值在融合前丢弃。
# ⚠️ 实测发现：bge-small-zh 的相似度分布被压缩在较窄区间
#    （相关文档约 0.46~0.60，无关文档约 0.40~0.45），
#    因此常用默认值 0.30 在此模型上几乎不起过滤作用。
#    0.45 是按当前 5 篇真实语料的分布取的经验值，待阶段五用评估集做数据驱动调优。
RETRIEVE_MIN_SCORE = 0.45

# ---------- 分数融合 ----------
FUSION_MODE = "rrf"          # rrf（默认，鲁棒） / weighted（加权归一化，便于对比调优）
RRF_K = 60                   # RRF 平滑常数
FUSION_WEIGHT_VEC = 0.5      # weighted 模式下的向量权重
FUSION_WEIGHT_BM25 = 0.5     # weighted 模式下的关键词权重

# ---------- BM25 参数 ----------
BM25_K1 = 1.5
BM25_B = 0.75

# ---------- 查询改写 ----------
QUERY_REWRITE_ENABLE = True
QUERY_REWRITE_MODE = "auto"  # auto: 有 LLM Key 用 LLM 改写，否则用规则表兜底

# ---------- Prompt 上下文预算 ----------
MAX_CONTEXT_CHARS = 3000     # 送入 Prompt 的召回内容总字符上限

# ---------- 云端 LLM（OpenAI 兼容协议，换厂商只改 base_url 与 model） ----------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = 60


def load_dotenv_if_exists():
    """
    加载项目根目录下的 .env 文件（若存在）。

    用于把 LLM_API_KEY 等敏感配置从环境变量注入，避免写入代码或提交到仓库。
    不引入 python-dotenv 依赖，仅支持 KEY=VALUE 的简单格式。
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                # 已存在的环境变量优先，不被 .env 覆盖
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


load_dotenv_if_exists()

# .env 加载后再读取一次，使 .env 中的配置生效
LLM_BASE_URL = os.getenv("LLM_BASE_URL", LLM_BASE_URL)
LLM_API_KEY = os.getenv("LLM_API_KEY", LLM_API_KEY)
LLM_MODEL = os.getenv("LLM_MODEL", LLM_MODEL)
