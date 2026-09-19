"""
文本结构化信息抽取模块
基于正则表达式匹配通用关键字段：
  - 文档编号（单据编号、合同编号、订单号等）
  - 单据日期
  - 金额数值

将非结构化全文文本拆解为结构化字段数据
"""
import re
import logging
from typing import List, Dict

from app.config import DOC_NUMBER_PATTERNS, DATE_PATTERNS, AMOUNT_PATTERNS

logger = logging.getLogger(__name__)

# 字段中文名映射
FIELD_LABELS = {
    "doc_number": "单据编号",
    "date": "日期",
    "amount": "金额",
}


# OCR 常见「数字书写噪声」修复。
#
# 为什么要做这一步：实测发现 OCR 与抽取的错误会**叠加** ——
# OCR 把 `36,577.56` 读成 `36, 577.56`（千分位后插空格）、
# 把 `54,464.66` 读成全角逗号 `54，464.66`，
# 而金额正则的字符类是 `[0-9,]`（只认半角逗号），
# `\.[0-9]{2}` 又要求逗号后紧跟 3 位数字 —— 于是**连兜底模式也失配**。
#
# ⚠️ 刻意只做这两处定点修复，**不做通用的全角转半角**：
# 单据编号里可能出现全角字符，整体转换会引入新的失配风险。
_FIX_NUMERIC_NOISE = (
    (re.compile(r"(?<=\d)[，](?=\d)"), ","),        # 数字之间的全角逗号 → 半角
    (re.compile(r"(?<=\d),[ \t]+(?=\d{3}\b)"), ","),  # 千分位逗号后的空格 → 移除
)


def _normalize_for_extract(text: str) -> str:
    """抽取前的定点归一化（只修数字书写噪声，不动其他字符）"""
    for pattern, repl in _FIX_NUMERIC_NOISE:
        text = pattern.sub(repl, text)
    return text


def _extract_by_patterns(text: str, patterns: list, field_name: str) -> str:
    """
    使用多个正则模式依次匹配，返回第一个匹配到的值
    """
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            value = matches[0].strip()
            logger.info(f"  [抽取] {field_name}: {value}")
            return value
    logger.info(f"  [抽取] {field_name}: 未匹配")
    return None


def extract_fields(raw_text: str) -> List[Dict[str, str]]:
    """
    从 OCR 全文识别文本中抽取结构化字段
    参数: raw_text — OCR 完整原始识别文本
    返回: [{"field_name": "单据编号", "field_value": "INV-20240101"}, ...]
    """
    if not raw_text:
        return [
            {"field_name": FIELD_LABELS["doc_number"], "field_value": None},
            {"field_name": FIELD_LABELS["date"], "field_value": None},
            {"field_name": FIELD_LABELS["amount"], "field_value": None},
        ]

    # 先做定点归一化，修复 OCR 的数字书写噪声（详见 _normalize_for_extract 注释）
    text = _normalize_for_extract(raw_text)

    results = []

    # 抽取单据编号
    doc_number = _extract_by_patterns(text, DOC_NUMBER_PATTERNS, "单据编号")
    results.append({"field_name": FIELD_LABELS["doc_number"], "field_value": doc_number})

    # 抽取日期
    date = _extract_by_patterns(text, DATE_PATTERNS, "日期")
    results.append({"field_name": FIELD_LABELS["date"], "field_value": date})

    # 抽取金额
    amount = _extract_by_patterns(text, AMOUNT_PATTERNS, "金额")
    results.append({"field_name": FIELD_LABELS["amount"], "field_value": amount})

    return results


def extract_all_numbers(raw_text: str) -> List[str]:
    """辅助函数：提取文本中所有数字串（用于调试）"""
    return re.findall(r'\d+\.?\d*', raw_text)
