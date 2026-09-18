"""
BM25 关键词检索模块（纯 Python 实现，零第三方依赖）

存在意义（对应方案 P0-1）：
  票据类文档的查询大量是**精确 token** —— 单据编号、金额、日期。
  512 维的向量模型对 "PO-20260724-888" 这类随机字符串几乎无区分度
  （大量编号的向量会聚集在同一区域），纯向量检索大概率召不回目标文档；
  而 BM25 一次精确匹配即可命中。

  因此 BM25 不是"降级兜底"，而是与向量检索**并列的第一公民**。

分词策略（关键设计）：
  - 中文串 → **字符 bigram**： "客运服务费" → 客运 / 运服 / 服务 / 务费
    选 bigram 而非 jieba 的理由：零依赖、无需词典、对 OCR 错字更鲁棒
    （错一个字只影响 2 个 bigram，词级分词则会整词失配）
  - 字母数字串 → **保持整词**： "PO-20260724-888" 是一个完整 token
    这是精确编号检索能生效的前提，若被 bigram 切碎则 P0-1 无法解决
  - 整词之外额外产出**分段子词**（长度 ≥3）： "PO-20260724-888" 额外产出 20260724、888
    用于支持部分匹配（用户只记得编号后几位时仍可召回）
"""
import re
import math
import logging
from collections import Counter
from typing import List, Dict, Optional

from app.config import BM25_K1, BM25_B

logger = logging.getLogger(__name__)

# 字母数字串（含常见连接符）或中文串
_RE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\./_]*|[\u4e00-\u9fff]+")
# 判断是否为中文串
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
# 子词切分：按连接符拆
_RE_SUBSPLIT = re.compile(r"[\-\./_]+")

# 子词最小长度（过滤 "00"、"01" 这类噪声片段）
_MIN_SUB_TOKEN_LEN = 3


def tokenize(text: str) -> List[str]:
    """
    对文本分词，返回 token 列表。

    - 中文串按字符 bigram 切分（单字串则保留单字）
    - 字母数字串保持整词（小写化），并额外产出长度 ≥3 的分段子词
    """
    if not text:
        return []

    tokens: List[str] = []

    for m in _RE_TOKEN.finditer(text):
        s = m.group(0)

        if _RE_CJK.match(s[0]):
            # 中文串 → 字符 bigram
            if len(s) == 1:
                tokens.append(s)
            else:
                for i in range(len(s) - 1):
                    tokens.append(s[i:i + 2])
        else:
            # 字母数字串 → 整词（小写化）
            whole = s.lower()
            tokens.append(whole)

            # 额外产出分段子词，支持"只记得编号片段"的部分匹配
            parts = [p for p in _RE_SUBSPLIT.split(whole) if len(p) >= _MIN_SUB_TOKEN_LEN]
            if len(parts) > 1:
                tokens.extend(parts)

    return tokens


class BM25Index:
    """
    BM25 倒排索引（内存驻留）

    评分公式（标准 BM25，IDF 加 1 避免负值）：
        IDF(t)  = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        score   = Σ IDF(t) · tf·(k1+1) / (tf + k1·(1 - b + b·dl/avgdl))
    """

    def __init__(self, k1: float = BM25_K1, b: float = BM25_B):
        self.k1 = k1
        self.b = b

        self._chunk_ids: List[int] = []                  # 下标 → chunk_id
        self._meta: List[Dict] = []                      # 下标 → 分块元信息
        self._tf: List[Counter] = []                     # 下标 → 词频
        self._doc_len: List[int] = []                    # 下标 → 文档长度
        self._postings: Dict[str, List[int]] = {}        # token → 命中的下标列表
        self._df: Counter = Counter()                    # token → 文档频次
        self._avgdl: float = 0.0
        self._built: bool = False

    # ---------- 构建 ----------

    def build(self, chunks: List[Dict]) -> None:
        """
        从分块列表重建索引。

        参数 chunks: [{"chunk_id":1, "doc_id":1, "chunk_text":"...", "page_no":1,
                       "file_name":"...", "chunk_index":0, ...}, ...]
        """
        self._chunk_ids = []
        self._meta = []
        self._tf = []
        self._doc_len = []
        self._postings = {}
        self._df = Counter()

        for idx, ch in enumerate(chunks):
            toks = tokenize(ch.get("chunk_text") or "")
            if not toks:
                continue
            tf = Counter(toks)
            self._chunk_ids.append(ch["chunk_id"])
            self._meta.append(ch)
            self._tf.append(tf)
            self._doc_len.append(len(toks))
            for t in tf:
                self._postings.setdefault(t, []).append(idx)
                self._df[t] += 1

        n = len(self._doc_len)
        self._avgdl = (sum(self._doc_len) / n) if n else 0.0
        self._built = True
        logger.info(f"BM25 索引构建完成：{n} 个分块，{len(self._postings)} 个不同 token，"
                    f"平均长度 {self._avgdl:.1f}")

    def invalidate(self) -> None:
        """标记索引失效，下次检索前需重建"""
        self._built = False

    @property
    def is_ready(self) -> bool:
        return self._built

    @property
    def doc_count(self) -> int:
        return len(self._doc_len)

    # ---------- 检索 ----------

    def _idf(self, token: str) -> float:
        n = len(self._doc_len)
        df = self._df.get(token, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_n: int = 20) -> List[Dict]:
        """
        关键词检索。

        参数: query — 查询文本（内部会分词）
              top_n — 返回条数
        返回: [{"chunk_id":.., "doc_id":.., "score":.., "bm25_rank":.., ...}, ...]
              score 为原始 BM25 分值（无上界，融合时由 fusion 模块处理）
        """
        if not self._built or not self._doc_len:
            return []

        q_tokens = tokenize(query)
        if not q_tokens:
            return []

        # 只保留出现在索引中的查询词，并对重复词去重（避免重复累加）
        uniq = [t for t in dict.fromkeys(q_tokens) if t in self._postings]
        if not uniq:
            return []

        scores: Dict[int, float] = {}
        avgdl = self._avgdl or 1.0

        for token in uniq:
            idf = self._idf(token)
            if idf <= 0:
                continue
            for idx in self._postings[token]:
                tf = self._tf[idx].get(token, 0)
                if not tf:
                    continue
                dl = self._doc_len[idx]
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                scores[idx] = scores.get(idx, 0.0) + idf * tf * (self.k1 + 1.0) / denom

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

        results = []
        for rank, (idx, score) in enumerate(ranked, start=1):
            item = dict(self._meta[idx])
            item["score"] = round(score, 4)
            item["bm25_rank"] = rank
            results.append(item)
        return results


# ==================== 模块级单例 ====================

_index: Optional[BM25Index] = None


def get_bm25_index() -> BM25Index:
    """获取全局 BM25 索引实例（懒加载）"""
    global _index
    if _index is None:
        _index = BM25Index()
    return _index


def rebuild_bm25_index(chunks: List[Dict]) -> BM25Index:
    """用给定分块重建全局 BM25 索引"""
    idx = get_bm25_index()
    idx.build(chunks)
    return idx


if __name__ == "__main__":
    # 自测：验证分词策略与检索效果
    # 运行: .venv/Scripts/python.exe -m app.services.bm25_index
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 72)
    print("分词策略验证")
    print("=" * 72)
    for s in ["PO-20260724-888", "INV-20240115-001", "客运服务费", "价税合计", "（小写）¥25.99", "55,000.00元"]:
        print(f"  {s!r:26s} → {tokenize(s)}")

    print()
    print("=" * 72)
    print("检索验证（用真实分块）")
    print("=" * 72)

    demo_chunks = [
        {"chunk_id": 1, "doc_id": 1, "chunk_index": 0, "page_no": None, "file_name": "test_invoice.png",
         "chunk_text": "电子发票\n单据编号：INV-20240115-001\n日期：2024-01-15\n客户名称：测试科技有限公司\n"
                       "商品名称：服务器设备一批\n数量：5台\n单价：2,000.00元\n金额：10,000.00元\n"
                       "税率：13%\n价税合计：11,300.00"},
        {"chunk_id": 2, "doc_id": 2, "chunk_index": 0, "page_no": None, "file_name": "test_new.png",
         "chunk_text": "采购订单\n订单号：PO-20260724-888\n日期：2026-07-24\n金额：55,000.00元"},
        {"chunk_id": 3, "doc_id": 5, "chunk_index": 0, "page_no": 1,
         "file_name": "曹操出行-25.99元.pdf",
         "chunk_text": "电子发票\n发票号码：25447000001712590935\n开票日期：2025年12月19日\n"
                       "*运输服务*客运服务费\n31.30\n（小写）¥25.99"},
    ]

    idx = rebuild_bm25_index(demo_chunks)

    queries = [
        "PO-20260724-888",      # 精确编号 —— P0-1 的核心场景
        "INV-20240115-001",     # 精确编号
        "888",                  # 编号片段（部分匹配）
        "客运服务费",            # 中文关键词
        "采购订单",              # 中文关键词
        "25.99",                # 金额
        "不存在的词xyz",         # 无结果
    ]

    for q in queries:
        hits = idx.search(q, top_n=3)
        print(f"\n查询: {q!r}")
        if not hits:
            print("   (无结果)")
        for h in hits:
            print(f"   #{h['bm25_rank']}  score={h['score']:8.4f}  doc{h['doc_id']}  {h['file_name']}")
