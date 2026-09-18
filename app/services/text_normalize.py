"""
OCR 文本归一化模块

背景（来自真实 OCR 结果实测）：
  RapidOCR 对票据类文档的识别结果存在以下系统性噪声，会同时破坏「向量语义检索」
  与「BM25 关键词检索」两路的效果，因此在建立索引前必须统一清洗：

    1. 数字内部误插入空格      "31. 30" / "¥25. 23"  → 搜 "25.23" 检索失败
    2. 全角数字与字母          "１２３" / "ＡＢＣ"     → 与半角查询词无法匹配
    3. 货币单位用字不规范      "贰拾伍圆玖角玖分"      → 与"元"字查询不匹配
    4. OCR 错字                "国家税奔总局"          → 搜"税务总局"永远搜不到

设计原则：
  - 归一化后的文本**同时**用于向量化与 BM25 索引，保证两路输入一致
  - 归一化是**行级**的且**保持行数不变**，以便与原文按行对齐
    （chunker 依赖这一特性：同一行号即可取到原文与归一化文本）
  - 归一化必须**幂等**：normalize(normalize(x)) == normalize(x)
  - 原文始终保留在 chunk_text_raw 中，引用展示时用原文，避免与用户看到的图片不符
"""
import re
import logging
from typing import Dict, List, Tuple

from app.config import OCR_TYPO_MAP, NORMALIZE_DIGIT_SPACE

logger = logging.getLogger(__name__)


# ==================== 全角 → 半角映射表 ====================
# 只处理全角数字与全角字母：
#   全角数字 ０-９ (U+FF10..FF19) → 0-9
#   全角大写 Ａ-Ｚ (U+FF21..FF3A) → A-Z
#   全角小写 ａ-ｚ (U+FF41..FF5A) → a-z
# 刻意不处理全角标点（：（）等）：中文正文中它们是正确排版，转半角反而降低可读性
_FULLWIDTH_TABLE: Dict[int, int] = {}
for _i in range(10):
    _FULLWIDTH_TABLE[0xFF10 + _i] = 0x30 + _i
for _i in range(26):
    _FULLWIDTH_TABLE[0xFF21 + _i] = 0x41 + _i
    _FULLWIDTH_TABLE[0xFF41 + _i] = 0x61 + _i


# 小数点周围误插入的空格：  "31. 30" → "31.30"；"25 . 23" → "25.23"
_RE_DOT_SPACE = re.compile(r"(?<=\d)\s*\.\s*(?=\d)")
# 货币符号与数字之间的空格："¥ 25.99" → "¥25.99"
_RE_CURRENCY_SPACE = re.compile(r"([¥￥$])\s+(?=\d)")
# 千分位逗号周围误插入的空格："2, 000.00" → "2,000.00"
_RE_COMMA_SPACE = re.compile(r"(?<=\d)\s*,\s*(?=\d{3}(?!\d))")
# 行内连续空白（含全角空格）压缩为单个半角空格
_RE_INLINE_SPACE = re.compile(r"[ \t\u3000]+")


def normalize_line(line: str) -> str:
    """
    对单行文本做归一化。

    注意：本函数不改变行数，也不做跨行处理，保证与原文逐行对齐。
    """
    if not line:
        return ""

    s = line

    # 1. 全角数字 / 字母 → 半角
    s = s.translate(_FULLWIDTH_TABLE)

    # 2. 修复数字内部被误插入的空格（OCR 在字符间插入空格的典型表现）
    if NORMALIZE_DIGIT_SPACE:
        s = _RE_DOT_SPACE.sub(".", s)          # 小数点周围
        s = _RE_COMMA_SPACE.sub(",", s)        # 千分位逗号周围
        s = _RE_CURRENCY_SPACE.sub(r"\1", s)   # 货币符号之后

    # 3. OCR 错字纠正（规则表可配置、可持续补充）
    for wrong, right in OCR_TYPO_MAP.items():
        if wrong in s:
            s = s.replace(wrong, right)

    # 4. 行内空白压缩 + 去除首尾空白
    s = _RE_INLINE_SPACE.sub(" ", s).strip()

    return s


def normalize(text: str) -> str:
    """
    对整段文本做归一化，保持原有换行结构。

    参数: text — OCR 原始识别文本
    返回: 归一化后的文本（行数与原文一致）
    """
    if not text:
        return ""
    return "\n".join(normalize_line(ln) for ln in text.split("\n"))


def diff_lines(text: str) -> List[Tuple[int, str, str]]:
    """
    返回归一化过程中实际发生变化的行，用于验收演示与问题排查。

    返回: [(行号(从1开始), 原始行, 归一化后行), ...]
    """
    changes: List[Tuple[int, str, str]] = []
    for idx, raw_line in enumerate(text.split("\n"), start=1):
        new_line = normalize_line(raw_line)
        if new_line != raw_line:
            changes.append((idx, raw_line, new_line))
    return changes


if __name__ == "__main__":
    # 自测：直接用真实 OCR 片段验证归一化效果
    # 运行: .venv/Scripts/python.exe -m app.services.text_normalize
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sample = (
        "电子发票区普要发票）\n"
        "发票号码：\n"
        "25447000001712590935\n"
        "开票日期：\n"
        "国家税奔总局\n"
        "2025年12月19日\n"
        "*运输服务*客运服务费\n"
        "31. 30\n"
        "（小写）¥25. 23\n"
        "价税合计（大写）\n"
        "贰拾伍圆玖角玖分"
    )

    print("=" * 60)
    print("原始文本")
    print("=" * 60)
    print(sample)
    print()
    print("=" * 60)
    print("归一化结果")
    print("=" * 60)
    print(normalize(sample))
    print()
    print("=" * 60)
    print("发生变化的行")
    print("=" * 60)
    for no, before, after in diff_lines(sample):
        print(f"  第{no}行: {before!r}  →  {after!r}")

    # 幂等性自检
    once = normalize(sample)
    twice = normalize(once)
    print()
    print(f"幂等性检查: {'通过' if once == twice else '失败（归一化不幂等！）'}")
