# -*- coding: utf-8 -*-
"""
字段注册表（方案 B 3.1）—— 抽取、校验、表单、导出的**唯一**字段定义来源

设计要点：
- 三类文档的稳定字段键集中在此，前后端不再分别硬编码中文字段含义。
- 抽取保持本地规则，不引入 LLM；不确定/多候选冲突时留空，不编造值填满表格。
- 规范化规则：日期 → ISO；金额 → 十进制字符串（Decimal，允许零与负数，
  最多两位小数；人工输入超精度**报错**而不是静默舍入）；空值是 None 不是 0。
- 币种：只有明确上下文才推断（人民币/CNY/￥、USD/美元 等），
  仅 `$` 或孤立 `¥` 不足以推断；无依据保存 None。

如何新增字段（三步）：
  1. 在对应类型的 FIELD_REGISTRY 列表里加一个 FieldSpec（key/label/kind/patterns）
  2. 若是新 kind，在 normalize_value() 里补规范化分支
  3. 跑 tests/ 与 scripts/verify_delivery.py 相应阶段
  前端表单、详情卡片、Excel 导出、统计全部自动跟随，不需要改其他代码。
"""
import re
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Optional

from app.config import DOC_NUMBER_PATTERNS, DATE_PATTERNS, AMOUNT_PATTERNS

logger = logging.getLogger(__name__)

DOC_TYPES = ("invoice", "contract", "other")


@dataclass
class FieldSpec:
    key: str                 # 稳定字段键（API/库/导出共用）
    label: str               # 中文展示名
    kind: str                # text / date / amount / currency
    patterns: List[str] = field(default_factory=list)  # 抽取正则（单捕获组），可为空=仅人工填写


# ==================== 注册表 ====================
# ⚠️ 金额类正则的「同行匹配」与「汇总性标签优先」两条硬约束来自实测（详见
#    app/config.py AMOUNT_PATTERNS 注释），新增模式时同样适用。

FIELD_REGISTRY: Dict[str, List[FieldSpec]] = {
    "invoice": [
        FieldSpec("invoice_number", "发票号码", "text", [
            r'发票号码[：:\s]*([0-9]{8,})',
            r'(?:票据号码|发票号)[：:\s]*([A-Za-z0-9\-_]+)',
            r'(?:单据编号|编号|单号)[：:\s]*([A-Za-z0-9\-_]+)',
        ]),
        FieldSpec("invoice_date", "开票日期", "date", [
            r'开票日期[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
        ]),
        FieldSpec("buyer_name", "购买方", "text", [
            r'购买方[^。\n]{0,12}?名称[：:\s]*([^\s：:，,；;]{2,40})',
            r'购买方[：:\s]*([^\s：:，,；;]{2,40})',
        ]),
        FieldSpec("seller_name", "销售方", "text", [
            r'销售方[^。\n]{0,12}?名称[：:\s]*([^\s：:，,；;]{2,40})',
            r'销售方[：:\s]*([^\s：:，,；;]{2,40})',
        ]),
        FieldSpec("buyer_tax_id", "购买方税号", "text", [
            r'购买方[^。\n]{0,30}?(?:统一社会信用代码|纳税人识别号)[：:\s]*([0-9A-Z]{15,20})',
        ]),
        FieldSpec("seller_tax_id", "销售方税号", "text", [
            r'销售方[^。\n]{0,30}?(?:统一社会信用代码|纳税人识别号)[：:\s]*([0-9A-Z]{15,20})',
        ]),
        FieldSpec("net_amount", "不含税金额", "amount", [
            r'(?:不含税金额|金额|合计金额)[：:]?[ \t]*[￥¥]?[ \t]*(-?[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        ]),
        FieldSpec("tax_amount", "税额", "amount", [
            r'税额[：:]?[ \t]*[￥¥]?[ \t]*(-?[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        ]),
        FieldSpec("total_amount", "价税合计", "amount", AMOUNT_PATTERNS),
        FieldSpec("currency", "币种", "currency", []),
    ],
    "contract": [
        FieldSpec("contract_title", "合同名称", "text", []),  # 标题版式多样，只人工/首行启发
        FieldSpec("contract_number", "合同编号", "text", [
            r'合同编号[：:\s]*([A-Za-z0-9\-_]+)',
            r'(?:编号|协议编号)[：:\s]*([A-Za-z0-9\-_]+)',
        ]),
        FieldSpec("party_a", "甲方", "text", [
            r'甲方[（(][^）)]{0,10}[）)]?[：:\s]*([^\s：:，,；;]{2,40})',
        ]),
        FieldSpec("party_b", "乙方", "text", [
            r'乙方[（(][^）)]{0,10}[）)]?[：:\s]*([^\s：:，,；;]{2,40})',
        ]),
        FieldSpec("signed_date", "签订日期", "date", [
            r'签订日?期?[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
            r'签约日期[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
        ]),
        FieldSpec("effective_date", "生效日期", "date", [
            r'生效日?期?[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
        ]),
        FieldSpec("expiry_date", "到期日期", "date", [
            r'(?:到期日?|有效期至|截止日期)[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
        ]),
        FieldSpec("contract_amount", "合同金额", "amount", [
            r'合同(?:总?金额|总额|价款)[：:]?[ \t]*[￥¥]?[ \t]*(-?[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
            r'(?:总金额|总额)[：:]?[ \t]*[￥¥]?[ \t]*(-?[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        ]),
        FieldSpec("currency", "币种", "currency", []),
    ],
    "other": [
        FieldSpec("document_number", "编号", "text", DOC_NUMBER_PATTERNS),
        FieldSpec("document_date", "日期", "date", DATE_PATTERNS),
        FieldSpec("notes", "备注", "text", []),
    ],
}


def specs_for(doc_type: str) -> List[FieldSpec]:
    return FIELD_REGISTRY.get(doc_type, FIELD_REGISTRY["other"])


def spec_map(doc_type: str) -> Dict[str, FieldSpec]:
    return {s.key: s for s in specs_for(doc_type)}


# ==================== 规范化 ====================

class NormalizeError(ValueError):
    """人工输入校验失败（如金额超两位小数）——接口层转 400"""


_DATE_RE_LIST = [
    re.compile(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})日?$'),
    re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日?$'),
]


def normalize_date(raw: str) -> Optional[str]:
    """日期规范为 ISO（YYYY-MM-DD）；解析不了返回 None（留空，不编造）"""
    if not raw:
        return None
    s = raw.strip()
    for rx in _DATE_RE_LIST:
        m = rx.match(s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
            return None
    return None


def normalize_amount(raw: str, *, strict: bool = False) -> Optional[str]:
    """
    金额规范为十进制字符串（去千分位/货币符号，允许负数，最多两位小数）。
    strict=True（人工输入）：超过两位小数抛 NormalizeError，不静默舍入；
    strict=False（OCR 抽取）：按 ROUND_HALF_UP 收到两位（原始值另存 field_value_raw）。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace(",", "").replace("￥", "").replace("¥", "").replace("$", "")
    s = s.replace("元", "").strip()
    try:
        dec = Decimal(s)
    except InvalidOperation:
        if strict:
            raise NormalizeError('请输入有效金额')
        return None
    if not dec.is_finite() or abs(dec) >= Decimal('1e18'):
        if strict:
            raise NormalizeError('金额必须是绝对值小于 10^18 的有限数字')
        return None
    q = Decimal("0.01")
    if strict and dec != dec.quantize(q, rounding=ROUND_HALF_UP):
        raise NormalizeError(f"金额最多两位小数: {raw}")
    return str(dec.quantize(q, rounding=ROUND_HALF_UP))


_CURRENCY_RULES = [
    # (正则, ISO 代码) —— 仅明确上下文；顺序即优先级
    (re.compile(r'人民币|RMB|CNY', re.IGNORECASE), "CNY"),
    (re.compile(r'美元|USD|US\$', re.IGNORECASE), "USD"),
    (re.compile(r'欧元|EUR', re.IGNORECASE), "EUR"),
    (re.compile(r'日元|JPY', re.IGNORECASE), "JPY"),
    (re.compile(r'港币|港元|HKD', re.IGNORECASE), "HKD"),
]


def infer_currency(text: str) -> Optional[str]:
    """
    从全文推断币种。仅 `$` 或孤立 `¥` 不足以推断（任务书 3.1）；
    无明确依据返回 None。
    """
    if not text:
        return None
    for rx, code in _CURRENCY_RULES:
        if rx.search(text):
            return code
    return None


def normalize_value(spec: FieldSpec, raw: Optional[str], *, strict: bool = False) -> Optional[str]:
    """按字段 kind 规范化；非法输入返回 None（抽取路径）或抛 NormalizeError（人工 strict）"""
    if raw is None:
        return None
    if spec.kind == "date":
        v = normalize_date(raw)
        if v is None and strict:
            raise NormalizeError(f"日期格式无法识别: {raw}")
        return v
    if spec.kind == "amount":
        return normalize_amount(raw, strict=strict)
    if spec.kind == "currency":
        s = str(raw).strip().upper()
        if re.fullmatch(r'[A-Z]{3}', s):
            return s
        v = infer_currency(raw)
        if v is None and strict and s:
            raise NormalizeError(f"币种需为 ISO 三字母代码（如 CNY/USD）: {raw}")
        return v
    # text：保留可读原文（去首尾空白、合并内部多余空白）
    return re.sub(r'\s+', ' ', str(raw)).strip() or None


# ==================== 类型辅助分类 ====================

_INVOICE_STRONG = re.compile(r'发票号码|发票代码|增值税.{0,6}发票|电子发票')
_CONTRACT_TITLE = re.compile(r'[\u4e00-\u9fa5]{0,12}(合同|协议)')
_PARTY_LABELS = re.compile(r'甲方|乙方')


def classify_doc_type(text: str) -> str:
    """
    本地规则辅助分类（任务书 3.1）：
    - 有明确发票标题/发票号码标签且无合同主标题 → invoice
    - 有明确合同主标题及当事方标签 → contract
    - 证据不足或冲突 → other
    关键约束：不因正文偶然出现"发票"二字把合同判为发票 —— 合同主标题只认
    文本开头 120 字符内的标题位置，发票强特征要求"号码/代码/增值税"级别的标签。
    """
    if not text:
        return "other"
    head = text[:120]
    has_contract_title = bool(_CONTRACT_TITLE.search(head))
    has_parties = bool(_PARTY_LABELS.search(text[:1500]))
    has_invoice_strong = bool(_INVOICE_STRONG.search(text))

    if has_invoice_strong and not has_contract_title:
        return "invoice"
    if has_contract_title and has_parties:
        return "contract"
    return "other"
