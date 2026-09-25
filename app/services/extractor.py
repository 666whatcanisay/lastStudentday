# -*- coding: utf-8 -*-
"""
文本结构化信息抽取模块（方案 B：注册表驱动）

extract_for_type() 按字段注册表（app/services/field_registry.py）抽取并规范化，
返回 [{field_key, field_name, field_value, field_value_raw}]，供版本发布直接使用。

旧三字段入口 extract_fields() 保留为兼容包装（other 类型），不新增使用。
"""
import re
import logging
from typing import List, Dict, Optional

from app.config import AMOUNT_PATTERNS
from app.services import field_registry as registry

logger = logging.getLogger(__name__)

# 旧三字段中文名映射（仅旧脚本兼容用；新代码用注册表）
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


def _first_match(text: str, patterns: List[str]) -> Optional[str]:
    """依次尝试模式，返回第一个命中的捕获值（模式顺序即优先级）"""
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            return matches[0].strip()
    return None


def extract_for_type(raw_text: Optional[str], doc_type: str) -> List[Dict[str, Optional[str]]]:
    """
    按注册表抽取某类型文档的全部字段。
    不确定/未匹配 → field_value=None（空值），绝不编造值填满表格；
    原始抽取值保留在 field_value_raw，规范值在 field_value。
    """
    text = _normalize_for_extract(raw_text) if raw_text else ""
    results = []
    for spec in registry.specs_for(doc_type):
        raw = _first_match(text, spec.patterns) if spec.patterns else None
        value = registry.normalize_value(spec, raw) if raw is not None else None
        if raw is not None:
            logger.info(f"  [抽取] {spec.label}: {value}（原文 {raw}）")
        results.append({
            "field_key": spec.key,
            "field_name": spec.label,
            "field_value": value,
            "field_value_raw": raw,
        })

    # 币种是全文推断而非单点抽取（见 registry.infer_currency 的保守规则）
    cur = next((r for r in results if r["field_key"] == "currency"), None)
    if cur is not None:
        cur["field_value"] = registry.infer_currency(raw_text or "")
        cur["field_value_raw"] = None
    return results


# ==================== 兼容包装（旧调用方） ====================

def extract_fields(raw_text: str) -> List[Dict[str, str]]:
    """旧三字段入口（单据编号/日期/金额），保留给 eval_ocr_fields / reextract_fields
    等旧脚本，行为与旧实现一致；新代码请用 extract_for_type()。"""
    text = _normalize_for_extract(raw_text) if raw_text else ""
    doc_number = _first_match(text, registry.spec_map("other")["document_number"].patterns)
    date = _first_match(text, registry.spec_map("other")["document_date"].patterns)
    amount = _first_match(text, AMOUNT_PATTERNS)
    return [
        {"field_name": FIELD_LABELS["doc_number"], "field_value": doc_number},
        {"field_name": FIELD_LABELS["date"], "field_value": date},
        {"field_name": FIELD_LABELS["amount"], "field_value": amount},
    ]


def extract_all_numbers(raw_text: str) -> List[str]:
    """辅助函数：提取文本中所有数字串（用于调试）"""
    return re.findall(r'\d+\.?\d*', raw_text)
