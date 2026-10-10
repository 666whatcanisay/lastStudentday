# -*- coding: utf-8 -*-
"""
发票字段抽取（位置感知版）—— extract_for_type 在 doc_type=invoice 时的实现。

为什么单独一个模块：
  发票的「购买方/销售方 名称与税号」在版面上是**双栏**的 ——
  标签（`名称：`、`统一社会信用代码/纳税人识别号：`）与值不在同一行，
  纯文本顺序匹配要么抓空，要么把「信息」这种版面提示词当成公司名（实测 bug）。
  只有拿到**文本块坐标**才能把值稳定地配到左栏（购买方）与右栏（销售方）。

坐标从哪来（两条链路，互不替代）：
  · text 层 PDF —— PyMuPDF 取 span 文本 + bbox（pdf_handler.extract_text_layout）
  · 扫描件 / 图片 —— RapidOCR 返回的文本框（ocr_engine 的 boxes_info）
  两条链路统一成 `layout = {"spans":[{text,x0,y0,x1,y1,page}], "text": 阅读顺序文本}`。

硬约束（都来自实测，改前先读）：
  1. **标签与数值必须同行** —— 分隔符只用 `[ \\t]`，不用 `\\s`（含换行）。
     否则独占一行的列头「金额」「税额」会被当成标签，向后跳到第一个数字，
     实测抓成数量列的值。
  2. **金额必须交叉校验**：不含税金额 + 税额 = 价税合计（Decimal 精确比较）。
     三者不能同时成立时**留空**，不编造、不用近似值凑。
  3. **`往来单位` / `本方单位` 不映射为购销方** —— 旧式服务费票据用这两个词，
     但它们不标明哪一方是购买方，强行映射就是编造。留空由人工确认。
  4. **通用 `日期` 不当开票日期** —— 只有出现 `开票日期` 标签才取。
  5. **外币不得被默认值覆盖成 CNY** —— 未标注币种时才按业务约定默认人民币；
     出现明确外币代码或外币符号时不套默认值。
"""
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from app.services import field_registry as registry
from app.services.text_normalize import normalize as normalize_text

logger = logging.getLogger(__name__)


# ⚠️ text_normalize.normalize 只转全角**数字/字母**，**不转全角逗号** ——
#    而语料里确实存在「12，345.67」这种全角千分位。不补这一刀，
#    金额正则的 `[0-9,]` 字符类会在逗号处断掉，把 12345.67 抽成 12。
#    刻意只做数字之间的全角逗号，避免动到正文里的全角标点。
_RE_FULLWIDTH_COMMA = re.compile(r'(?<=\d)[，](?=\d)')
_RE_COMMA_SPACE = re.compile(r'(?<=\d),[ \t]+(?=\d{3}\b)')


def normalize_numeric_noise(text: str) -> str:
    """复用系统级归一化（修 `¥25. 23`→`¥25.23`、全角数字、OCR 错字），
    再补全角千分位逗号这一刀。

    为什么不自造一套：文本归一化必须与索引链路**同一套规则**，
    否则「字段里是 25.23、正文里是 25. 23」，检索与展示会对不上。
    该函数保持行数不变，不影响「标签与值同行」的假设。
    """
    s = normalize_text(text or "")
    s = _RE_FULLWIDTH_COMMA.sub(",", s)
    s = _RE_COMMA_SPACE.sub(",", s)
    # 不换行空格（部分字体/文本层会插入 \xa0）不是正则里的 `[ \t]`，
    # 会把「（小写）\xa0¥91.54」这类切断 → 统一成普通空格
    return s.replace("\u00a0", " ")


# ==================== 标签模式（全部同行匹配） ====================
_NAME_LABEL = re.compile(r'名称[：:][ \t]*([^\s：:，,；;]{2,40})')
_TAX_LABEL = re.compile(
    r'(?:统一社会信用代码|纳税人识别号)[^：:\n]{0,24}[：:][ \t]*([0-9A-Za-z]{15,20})')
# 只有「购买方」紧跟冒号才算显式标签 —— 「购买方信息」没有冒号，故不会误抓「信息」
_BUYER_DIRECT = re.compile(r'购买方(?:名称)?[：:][ \t]*([^\s：:，,；;]{2,40})')
_SELLER_DIRECT = re.compile(r'销售方(?:名称)?[：:][ \t]*([^\s：:，,；;]{2,40})')
_INV_NO = re.compile(r'发票号码[：:][ \t]*([0-9]{8,})')
_INV_NO_WRAP = re.compile(r'([0-9]{8,})')          # 「发票号码」标签后换行时的值
_INV_NO_ALT = re.compile(r'(?:票据号码|发票号)[：:][ \t]*([A-Za-z0-9\-_]{4,})')
_DOC_NO = re.compile(r'(?:单据编号|编号|单号)[：:][ \t]*([A-Za-z0-9\-_]{4,})')
_INV_DATE = re.compile(r'开票日期[：:][ \t]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)')
_DATE_ANY = re.compile(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)')
_TOTAL_SMALL = re.compile(r'[（(]小写[）)][：:]?[ \t]*[￥¥]?[ \t]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)')
_TOTAL = re.compile(r'价税合计[^0-9\n]{0,12}?([0-9][0-9,]*(?:\.[0-9]{1,2})?)')
_NET = re.compile(r'(?:不含税金额|合计金额|金额合计)[：:]?[ \t]*[￥¥]?[ \t]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)')
_TAX = re.compile(r'税额[：:]?[ \t]*[￥¥]?[ \t]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)')
# 汇总性标签（③级）—— 只认明确的汇总词，**不含**裸「合计」与裸数字，
# 否则明细行的金额会被当成价税合计（实测风险：36,577.56 这种行内金额）
_TOTAL_SUMMARY = re.compile(
    r'(?:合计金额|金额合计|总金额|价税金额|总计|总额|小计|实付|应付)[：:]?[ \t]*[￥¥]?[ \t]*'
    r'([0-9][0-9,]*(?:\.[0-9]{1,2})?)')

# 版面提示词 —— 绝不能被当成公司名
_LABEL_WORDS = {"信息", "名称", "单位", "税号", "代码", "纳税人", "识别号", "地址", "电话", "开户行", "账号"}

# 价税合计锚点（含「（小写）」）—— 用于「无坐标时取合计行」的文本兜底
_TOTAL_ANCHOR = re.compile(r'(?:价税合计|[（(]小写[）)])')

_NUMERIC_SPAN = re.compile(r'^[￥¥$]?[ \t]*-?[0-9][0-9,]*(?:\.[0-9]{1,2})?$')
_NAME_LABEL_SPAN = re.compile(r'^名称[：:]?$')
# 标签可能是合并写法「统一社会信用代码/纳税人识别号：」，也可能是单独一种
_TAX_LABEL_SPAN = re.compile(
    r'^(?:统一社会信用代码|纳税人识别号)[\s/／]*(?:纳税人识别号)?[\s/／]*[：:]?$')

# 外币符号：出现即抑制 CNY 默认值（$ 有 USD/HKD 歧义，一律不猜）
_FOREIGN_SYMBOL = re.compile(r'US\$|＄|\$|€|£|₩|₽')


def _m1(text: str, pattern: re.Pattern) -> Optional[str]:
    m = pattern.search(text or "")
    return m.group(1).strip() if m else None


def _label_wrapped_value(text: str, label: str, value_re: re.Pattern,
                         max_lines: int = 4) -> Optional[str]:
    """标签与值被 OCR 拆到不同行时，在标签所在行及随后若干行内找值。

    只用于「发票号码」这类**值形态强**（连续 8+ 位数字）的字段；
    窗口设上限，避免跨行抓错。
    """
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if label in ln:
            for nxt in lines[i:i + max_lines + 1]:
                m = value_re.search(nxt)
                if m:
                    return m.group(1)
    return None


def _dec(s: Optional[str]) -> Optional[Decimal]:
    if s is None:
        return None
    try:
        return Decimal(str(s).replace(",", "").replace("￥", "").replace("¥", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _clean_name(v: Optional[str]) -> Optional[str]:
    """公司名清洗：剔除纯版面提示词（实测 bug：把「信息」当成购买方）"""
    if not v:
        return None
    s = re.sub(r'\s+', '', v)
    if not s or s in _LABEL_WORDS:
        return None
    # 全为提示词字符的短串也丢弃（如「信息」「名称信息」）
    if len(s) <= 4 and all(ch in "信息名称单位税号代码" for ch in s):
        return None
    return s


# ==================== 坐标工具 ====================

def _span_center_y(s: Dict) -> float:
    return (s["y0"] + s["y1"]) / 2.0


def _span_center_x(s: Dict) -> float:
    return (s["x0"] + s["x1"]) / 2.0


def _v_overlap_ratio(a: Dict, b: Dict) -> float:
    top = max(a["y0"], b["y0"])
    bot = min(a["y1"], b["y1"])
    if bot <= top:
        return 0.0
    span = min(a["y1"] - a["y0"], b["y1"] - b["y0"]) or 1.0
    return (bot - top) / span


def _value_right_of_label(label: Dict, spans: List[Dict]) -> Optional[Dict]:
    """同一视觉行内，标签右侧最近的文本块（纵向重叠 > 50%，起点在标签起点之后）"""
    best = None
    best_dx = None
    for s in spans:
        if s is label or s.get("page") != label.get("page"):
            continue
        if _v_overlap_ratio(label, s) < 0.5:
            continue
        if s["x0"] < label["x0"] + 1:
            continue
        dx = s["x0"] - label["x0"]
        if best is None or dx < best_dx:
            best, best_dx = s, dx
    return best


# ==================== 双栏配对 ====================

def _order_two(items: List[Dict]) -> List[Dict]:
    """
    把两个候选块排成 [购买方, 销售方]。
    发票版面：购买方在左（或上），销售方在右（或下）。
    左右排布看 x，上下排布看 y —— 取位移更大的那个维度判定。
    """
    if len(items) != 2:
        return items
    a, b = items
    dx = abs(a["x0"] - b["x0"])
    dy = abs(a["y0"] - b["y0"])
    key = (lambda s: s["x0"]) if dx >= dy else (lambda s: s["y0"])
    return sorted(items, key=key)


def _pair_by_layout(spans: List[Dict], label_re: re.Pattern) -> Tuple[Optional[str], Optional[str]]:
    """用坐标把「名称：」「纳税人识别号：」的值配到购买方/销售方"""
    labels = [s for s in spans if label_re.match(s["text"].strip())]
    pairs: List[Dict] = []
    for lb in labels:
        val = _value_right_of_label(lb, spans)
        if val is not None:
            pairs.append(val)
    if len(pairs) < 2:
        return None, None
    ordered = _order_two(pairs[:2])
    return ordered[0]["text"].strip(), ordered[1]["text"].strip()


# ==================== 各字段候选值 ====================

def _parties(layout: Optional[Dict], texts: List[str]) -> Dict[str, Optional[str]]:
    """
    购买方/销售方 名称与税号。
    优先级：坐标配对（可靠） > 显式标签 > 出现顺序（购买方在前）。
    """
    spans = (layout or {}).get("spans") or []

    buyer_name = seller_name = buyer_tax = seller_tax = None
    if spans:
        buyer_name, seller_name = _pair_by_layout(spans, _NAME_LABEL_SPAN)
        buyer_tax, seller_tax = _pair_by_layout(spans, _TAX_LABEL_SPAN)

    # 文本层兜底：显式「购买方名称：」「销售方名称：」
    if buyer_name is None and seller_name is None:
        for t in texts:
            be = _m1(t, _BUYER_DIRECT)
            se = _m1(t, _SELLER_DIRECT)
            if be or se:
                buyer_name, seller_name = _clean_name(be), _clean_name(se)
                break
    # 仍无：按出现顺序（购买方标签在版面更靠前）
    if buyer_name is None and seller_name is None:
        for t in texts:
            names = [_clean_name(x) for x in _NAME_LABEL.findall(t or "")]
            names = [n for n in names if n]
            if len(names) >= 2:
                buyer_name, seller_name = names[0], names[1]
                break

    if buyer_tax is None and seller_tax is None:
        for t in texts:
            taxes = _TAX_LABEL.findall(t or "")
            if len(taxes) >= 2:
                buyer_tax, seller_tax = taxes[0], taxes[1]
                break

    return {
        "buyer_name": buyer_name,
        "seller_name": seller_name,
        "buyer_tax_id": buyer_tax,
        "seller_tax_id": seller_tax,
    }


def _total_row_amounts(layout: Optional[Dict]) -> Tuple[Optional[str], Optional[str]]:
    """
    从「合计」行按列取 不含税金额 / 税额（无标签版式，如增值税电子发票）。

    只在有坐标时可用：先用带空格的列头「金 额」「税 额」定列，再取合计行上落在
    对应列的数值；找不到列头时退回「合计行上从左到右第 1、2 个数值」。
    """
    spans = (layout or {}).get("spans") or []
    if not spans:
        return None, None

    def _norm(t: str) -> str:
        return re.sub(r'\s+', '', t or "")

    # 列头：原始文本带空格、归一化后恰为「金额」「税额」
    headers: Dict[str, Dict] = {}
    for s in spans:
        raw = (s["text"] or "").strip()
        n = _norm(raw)
        if n in ("金额", "税额") and raw != n:
            headers.setdefault(n, s)
    if len(headers) < 2:
        return None, None

    # 合计行：同一页上同时出现独立「合」与「计」的视觉行
    pages = sorted({s.get("page") for s in spans})
    for pg in pages:
        page_spans = [s for s in spans if s.get("page") == pg]
        he = [s for s in page_spans if (s["text"] or "").strip() == "合"]
        ji = [s for s in page_spans if (s["text"] or "").strip() == "计"]
        if not he or not ji:
            continue
        row_y = (_span_center_y(he[0]) + _span_center_y(ji[0])) / 2.0
        row_nums = [s for s in page_spans
                    if _NUMERIC_SPAN.match((s["text"] or "").strip())
                    and abs(_span_center_y(s) - row_y) < 8.0]
        if not row_nums:
            continue

        def _pick(header_key: str) -> Optional[str]:
            h = headers[header_key]
            in_col = [s for s in row_nums
                      if h["x0"] - 6 <= _span_center_x(s) <= h["x1"] + 6]
            if in_col:
                return in_col[0]["text"].strip()
            return None

        net = _pick("金额")
        tax = _pick("税额")
        if net is None and tax is None:
            row_nums.sort(key=_span_center_x)
            net = row_nums[0]["text"].strip() if len(row_nums) >= 1 else None
            tax = row_nums[1]["text"].strip() if len(row_nums) >= 2 else None
        return net, tax
    return None, None


def _amounts_before_total(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    无坐标时的合计行兜底：价税合计锚点**之前**最后两个带货币符号的数值，
    即明细表合计行的「金额列合计」与「税额列合计」。

    为什么必须带货币符号：明细行的单价/数量没有 `¥`，只有合计行才有，
    所以「锚点前最后两个 ¥ 数值」在真实票据上稳定指向合计行。
    """
    m = _TOTAL_ANCHOR.search(text or "")
    if not m:
        return None, None
    nums = [n for n in re.findall(
        r'[￥¥][ \t]*(-?[0-9][0-9,]*(?:\.[0-9]{1,2})?)', text[:m.start()]) if n]
    if len(nums) >= 2:
        return nums[-2], nums[-1]
    return None, None


def _cross_check(total: Optional[str], net_cands: List[str], tax_cands: List[str]) -> Tuple[Optional[str], Optional[str], str]:
    """
    用 Decimal 精确校验 不含税金额 + 税额 = 价税合计。
    只有能配出一组**精确成立**的组合才保留；否则留空（冲突或证据不足）。
    返回 (net, tax, 校验结论)
    """
    td = _dec(total)
    if td is None:
        return None, None, "no_total"
    if not net_cands or not tax_cands:
        return None, None, "insufficient"
    for n in net_cands:
        for t in tax_cands:
            nd, tt = _dec(n), _dec(t)
            if nd is None or tt is None:
                continue
            if nd + tt == td:
                return n, t, "ok"
    return None, None, "mismatch"


# ==================== 主入口 ====================

def extract_invoice_fields(raw_text: Optional[str], layout: Optional[Dict] = None) -> List[Dict]:
    """
    发票字段抽取。返回顺序与注册表 invoice 字段一致。
    layout 为 None 时退化为纯文本规则（仍修「信息」误抓、仍做金额交叉校验）。
    """
    ocr_text = normalize_numeric_noise(raw_text or "")
    lay_text = normalize_numeric_noise((layout or {}).get("text") or "")
    # 证据源：OCR 原文优先（它是归档原文），版本文本层作为补充
    texts = [ocr_text] + ([lay_text] if lay_text and lay_text != ocr_text else [])

    parties = _parties(layout, texts)

    # 发票号码：同行 → 标签换行 → 票据号码/发票号 → 单据编号兜底（与旧注册表一致）
    invoice_number = None
    for t in texts:
        invoice_number = (
            _m1(t, _INV_NO)
            or _label_wrapped_value(t, "发票号码", _INV_NO_WRAP)
            or _m1(t, _INV_NO_ALT)
            or _m1(t, _DOC_NO)
        )
        if invoice_number:
            break

    # 开票日期：只认「开票日期」标签；值可能在下一行（OCR 常把标签与值拆行）
    invoice_date = None
    for t in texts:
        invoice_date = _m1(t, _INV_DATE)
        if invoice_date:
            break
        lines = (t or "").splitlines()
        for i, ln in enumerate(lines):
            if "开票日期" in ln:
                for nxt in lines[i:i + 4]:
                    d = _DATE_ANY.search(nxt)
                    if d:
                        invoice_date = d.group(1)
                        break
                break
        if invoice_date:
            break

    # 价税合计：小写锚点优先 → 「价税合计」标签 → 其他明确汇总标签。
    # ⚠️ 刻意**不用**裸数字兜底：那会把明细行的金额当成总额（必须区分合计行与明细行）。
    total = None
    for t in texts:
        total = _m1(t, _TOTAL_SMALL) or _m1(t, _TOTAL) or _m1(t, _TOTAL_SUMMARY)
        if total:
            break

    # 不含税金额 / 税额：标签候选 + 合计行候选，最后交叉校验
    net_cands: List[str] = []
    tax_cands: List[str] = []
    for t in texts:
        v = _m1(t, _NET)
        if v and v not in net_cands:
            net_cands.append(v)
        v = _m1(t, _TAX)
        if v and v not in tax_cands:
            tax_cands.append(v)
    pnet, ptax = _total_row_amounts(layout)
    if pnet and pnet not in net_cands:
        net_cands.append(pnet)
    if ptax and ptax not in tax_cands:
        tax_cands.append(ptax)
    for t in texts:                      # 无坐标兜底：锚点前最后两个 ¥ 数值
        tnet, ttax = _amounts_before_total(t)
        if tnet and tnet not in net_cands:
            net_cands.append(tnet)
        if ttax and ttax not in tax_cands:
            tax_cands.append(ttax)

    net, tax, check = _cross_check(total, net_cands, tax_cands)

    # 币种：明确代码 > （无外币符号时）业务默认 CNY；外币符号存在时不套默认
    codes = registry.explicit_currency_codes(raw_text or "")
    if not codes:
        currency = None if _FOREIGN_SYMBOL.search(raw_text or "") else "CNY"
    elif len(codes) == 1:
        currency = next(iter(codes))
    else:
        currency = None

    raw_values = {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "buyer_name": parties["buyer_name"],
        "seller_name": parties["seller_name"],
        "buyer_tax_id": parties["buyer_tax_id"],
        "seller_tax_id": parties["seller_tax_id"],
        "net_amount": net,
        "tax_amount": tax,
        "total_amount": total,
        "currency": currency,
    }

    logger.info(
        f"  [发票抽取] 号码={invoice_number} 日期={invoice_date} "
        f"购={parties['buyer_name']}/{parties['buyer_tax_id']} "
        f"销={parties['seller_name']}/{parties['seller_tax_id']} "
        f"不含税={net} 税额={tax} 价税合计={total} 校验={check} 币种={currency}")

    results = []
    for spec in registry.specs_for("invoice"):
        raw = raw_values.get(spec.key)
        value = registry.normalize_value(spec, raw) if raw is not None else None
        results.append({
            "field_key": spec.key,
            "field_name": spec.label,
            "field_value": value,
            "field_value_raw": raw if spec.kind != "currency" else None,
        })
    return results
