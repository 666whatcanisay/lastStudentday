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

    results = []

    # 抽取单据编号
    doc_number = _extract_by_patterns(raw_text, DOC_NUMBER_PATTERNS, "单据编号")
    results.append({"field_name": FIELD_LABELS["doc_number"], "field_value": doc_number})

    # 抽取日期
    date = _extract_by_patterns(raw_text, DATE_PATTERNS, "日期")
    results.append({"field_name": FIELD_LABELS["date"], "field_value": date})

    # 抽取金额
    amount = _extract_by_patterns(raw_text, AMOUNT_PATTERNS, "金额")
    results.append({"field_name": FIELD_LABELS["amount"], "field_value": amount})

    return results


def extract_all_numbers(raw_text: str) -> List[str]:
    """辅助函数：提取文本中所有数字串（用于调试）"""
    return re.findall(r'\d+\.?\d*', raw_text)
