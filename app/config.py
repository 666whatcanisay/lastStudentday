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

# ---------- 向量路过滤：绝对下限 + 相对带宽 ----------
#
# ⚠️ 实测发现（重要，不要改回单一绝对阈值）：
#    bge-small-zh 的相似度是**长度相关**的 —— 短查询的分数系统性偏低：
#      「打车」          top1 = 0.3441
#      「从哪到哪」       top1 = 0.3787
#      「打车花了多少钱」   top1 = 0.4331
#      「谁开的发票」      top1 = 0.5433
#      「INV-...-001…」  top1 = 0.6398
#    曾经用单一阈值 0.45，结果把「打车」「从哪到哪」这类短口语查询的
#    向量路**整条掐断**（0.3441 远低于 0.45），而长查询却全通过。
#    绝对阈值无法同时适配长短查询，这是结构性缺陷，不是参数没调好。
#
#    另一个观察：无论查询长短，**top1 与 top2 的落差都很小**
#    （「谁开的发票」0.5433 vs 0.5320，仅差 0.011），真正明显的落差出现在
#    top2 之后。所以正确的做法是「跟着 top1 走」，而不是「卡一个死线」。
#
# ⚠️⚠️ 更关键的实测结论：**向量路无法区分相关与无关**
#    在同一批语料上，无关查询的向量分甚至比相关查询更高：
#      打车（相关）        0.3441
#      从哪到哪（相关）     0.3787
#      股票行情分析（无关）  0.4005   ← 比"打车"还高
#      今天天气怎么样（无关） 0.3707
#      量子纠缠退相干（无关） 0.3710
#    根因：语料是同质的（5 篇全是票据/订单），任何查询与任何文档都有
#    中等相似度，分数分布高度重叠。这是稠密检索在小而同质语料上的固有局限，
#    不是参数没调好 —— 换任何阈值都分不开。
#
#    所以本项目的架构是：
#      · 关键词路（BM25）承担**相关性判定** —— 它对无关查询真的返回 0 分，
#        是有真实判别力的那一路
#      · 向量路只承担**召回扩展** —— 负责把"语义相近但用词不同"的捞回来，
#        不负责说"没有"
#      · 生成层用防幻觉 Prompt 兜底（"资料不足必须回答无法回答"）
#
# 三级参数：
#   RETRIEVE_MIN_SCORE     —— 向量噪声下限。只挡掉 0.2x 一档的明显垃圾，
#                             不承担区分相关/无关的职责。
#   RETRIEVE_SCORE_MARGIN  —— 向量相对带宽。保留 score >= top1 - margin，
#                             让过滤强度自适应查询长度。
#   RETRIEVE_NO_HIT_SCORE  —— 无结果判定的向量置信度兜底。见下方说明。
RETRIEVE_MIN_SCORE = 0.30
RETRIEVE_SCORE_MARGIN = 0.10

# 判定「检索不到相关内容」时，向量路需要的置信度下限。
#
# 规则：**关键词路零命中 且 向量路 top1 低于本值** → 判定为无结果。
#       （仅 vec 模式时退化为只看向量）
#
# 为什么用"且"：单看向量会把"股票行情分析"(0.4005) 判成有结果；
#              单看关键词会把纯语义查询（无字面重叠）判成无结果。
#              两者都弱才敢说"没有"。
#
# ⚠️ 本值是**语料相关**的经验值：它成立的前提是本语料上"口语化查询都能
#    通过同义扩展被 BM25 覆盖"。语料换成非票据类（用词与口语差异大）后
#    必须重测，阶段五用评估集确认。
RETRIEVE_NO_HIT_SCORE = 0.45

# ---------- 分数融合 ----------
FUSION_MODE = "rrf"          # rrf（默认，鲁棒） / weighted（加权归一化，便于对比调优）
RRF_K = 60                   # RRF 平滑常数

# 两路权重（rrf 与 weighted 模式共用）
#
# ⚠️ 实测依据（scripts/measure_rewrite.py，5 篇真实语料 / 8 条查询）：
#   bm25_weight=1.0 → 融合 Top-1 命中 6/8
#   bm25_weight=1.5 → 融合 Top-1 命中 8/8
#   根因：短口语查询下向量路会把「语义像但事实错」的文档排到第一
#        （如「谁开的发票」→ doc1 与 doc5 的向量分仅差 0.011，无法区分），
#        等权融合时向量路的错误排序会把关键词路的正确排序稀释掉。
#   本领域（票据/合同）的判别特征高度依赖**字面量**（编号、日期、金额、专有名词），
#   这正是 BM25 的强项，因此给关键词路更高权重是有领域依据的，不只是拟合数据。
#
# ⚠️ 但这组权重只在 5 篇语料上调过，样本过小、有过拟合风险。
#    阶段五用 20+ 查询的评估集重跑 Recall@K / MRR 后需要重新确认。
FUSION_WEIGHT_VEC = 1.0      # 向量路权重
FUSION_WEIGHT_BM25 = 1.5     # 关键词路权重（实测最优）

# ---------- BM25 参数 ----------
BM25_K1 = 1.5
BM25_B = 0.75

# ---------- 查询改写 ----------
QUERY_REWRITE_ENABLE = True
QUERY_REWRITE_MODE = "auto"  # auto: 有 LLM Key 用 LLM 改写，否则用规则表兜底

# 口语词 → 语料术语 的同义扩展表（仅作用于 BM25 关键词路）
#
# 收录原则（重要）：**只收录语料里不存在的口语词**。
#   已经出现在语料中的词（如「发票」「金额」）不加扩展 ——
#   BM25 本来就能命中，额外扩展只会引入噪声。
#   这张表的作用是「补语料与用户用词之间的缺口」，不是同义词词典。
# 表中每个词条均依据真实语料 document_chunk 的实际用词归纳，非凭空编写。
QUERY_SYNONYM_MAP = {
    # 出行类：语料中的实际用词是「*运输服务*客运服务费」「旅客运输服务」
    "打车": ["客运服务费", "运输服务", "旅客运输"],
    "打的": ["客运服务费", "运输服务"],
    "出租车": ["客运服务费", "运输服务"],
    "网约车": ["客运服务费", "运输服务"],
    "地铁": ["运输服务", "旅客运输"],
    "高铁": ["运输服务", "旅客运输"],
    "机票": ["运输服务", "旅客运输"],
    "行程": ["出行日期", "出发地", "到达地"],
    "行程单": ["出行日期", "出发地", "到达地"],
    "起点": ["出发地"],
    "终点": ["到达地"],
    # 实测补录：查询「从哪到哪」两路均零命中，是真实召回缺口
    "从哪到哪": ["出发地", "到达地"],
    "从哪到哪儿": ["出发地", "到达地"],
    "哪儿到哪儿": ["出发地", "到达地"],
    "哪里到哪里": ["出发地", "到达地"],
    "去哪": ["到达地"],
    # 金额类：语料用「金额」「价税合计」「单价」
    "多少钱": ["金额", "价税合计"],
    "花了多少": ["金额", "价税合计"],
    "总价": ["价税合计", "金额"],
    "合计": ["价税合计"],
    "一共": ["价税合计", "金额"],
    "价格": ["单价", "金额"],
    "费用": ["金额", "价税合计"],
    # 票据标识类：语料用「单据编号」「发票号码」「订单号」
    "单号": ["单据编号", "发票号码", "订单号"],
    "编号": ["单据编号", "发票号码", "订单号"],
    "票号": ["发票号码"],
    # 主体类：语料用「购买方」「销售方」「名称」「开票人」
    "抬头": ["购买方", "名称"],
    "买家": ["购买方"],
    "客户": ["购买方", "客户名称"],
    "卖家": ["销售方"],
    "供应商": ["销售方"],
    "收款方": ["销售方"],
    "谁开的": ["开票人"],
    "税号": ["统一社会信用代码", "纳税人识别号"],
    # 时间类：语料用「开票日期」「日期」「出行日期」
    "什么时候": ["开票日期", "日期"],
    "几号": ["开票日期", "日期"],
    # 商品类：语料用「商品名称」「项目名称」
    "货物": ["商品名称", "项目名称"],
    "品名": ["商品名称", "项目名称"],
}

# ---------- Prompt 上下文预算 ----------
MAX_CONTEXT_CHARS = 3000     # 送入 Prompt 的召回内容总字符上限
LLM_MAX_TOKENS = 1024        # 单次生成的最大 token 数

# ---------- 云端 LLM（OpenAI 兼容协议，换厂商只改 base_url 与 model） ----------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = 60
LLM_TEMPERATURE = 0.1        # 抽取类任务需要稳定输出，温度压低

# 是否让 httpx 读取系统环境变量里的 HTTP_PROXY / HTTPS_PROXY。
# 本机实测：带代理与不带代理都能连通 api.deepseek.com（均返回 401=可达），
# 但本机代理端口会随会话变化（历史出现过 52392 / 55212），
# 一旦代理进程未启动就会导致 LLM 调用整体不可用。
# 因此默认 False：直连，行为确定、不依赖外部进程。
LLM_USE_ENV_PROXY = False


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


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量，容忍 true/1/yes/on 等写法"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


LLM_USE_ENV_PROXY = _env_bool("LLM_USE_ENV_PROXY", LLM_USE_ENV_PROXY)
