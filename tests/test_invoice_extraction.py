# -*- coding: utf-8 -*-
"""发票字段抽取专项：双栏配对、合计行交叉校验、留空纪律、币种纪律。

这些用例对应真实生产缺陷（见 reports/ 下的私有对照表）：
  · 购买方/销售方 名称与税号在版面上是双栏，纯文本顺序匹配会把「信息」当公司名
  · 不含税金额 + 税额 必须等于价税合计，否则留空（OCR 把 3,267.88 读成 3.267.88）
  · 往来单位/本方单位 不映射为购销方；通用「日期」不当开票日期
  · 出现外币符号时不得套用 CNY 默认值

注意：本文件所有公司名、税号、发票号、单据编号、金额均为**合成测试数据**，
版式坐标取自真实票据但不含任何真实业务内容。
"""
from app.services.extractor import extract_for_type


def values(raw_text, doc_type="invoice", layout=None):
    return {f["field_key"]: f["field_value"]
            for f in extract_for_type(raw_text, doc_type, layout)}


# ==================== 双栏（真实票据版式，文本值已脱敏） ====================

_OCR_EINVOICE = (
    "=== 第 1 页 ===\n"
    "电子发票区普要发票）\n发票号码：\n25990000001234567890\n旅客运输服务\n"
    "开票日期：\n国家税奔总局\n2026年03月05日\n广东省税务局\n"
    "购买方信息\n销售方信息\n"
    "名称：测试购买方科技有限公司\n名称：测试销售方服务有限公司\n"
    "统一社会信用代码/纳税人识别号：91110000ABCDEFGH1X\n"
    "统一社会信用代码/纳税人识别号：91110000ZYXWVUTS9Y\n"
    "单价\n数量\n金额\n项目名称\n税率/征收率\n税额\n"
    "*运输服务*客运服务费\n50.000000\n50. 00\n3%\n1. 50\n"
    "*运输服务*客运服务费\n-10. 00\n3%\n-0. 30\n"
    "¥88. 88\n¥2. 66\n"
    "价税合计（大写）\n玖拾壹圆伍角肆分\n（小写）¥91.54\n开票人：示例开票人\n"
)

# 与上面同页的文本层坐标（PDF point），购买方在左栏、销售方在右栏
_EINVOICE_LAYOUT = {
    "text": "电子发票（普通发票）\n发票号码：\n开票日期：\n25990000001234567890\n2026年03月05日\n",
    "spans": [
        {"text": "名称：", "x0": 33.0, "y0": 104.3, "x1": 59.9, "y1": 113.3, "page": 1},
        {"text": "名称：", "x0": 317.4, "y0": 104.3, "x1": 344.4, "y1": 113.3, "page": 1},
        {"text": "统一社会信用代码/纳税人识别号：", "x0": 32.9, "y0": 121.0, "x1": 155.3, "y1": 129.4, "page": 1},
        {"text": "统一社会信用代码/纳税人识别号：", "x0": 317.3, "y0": 121.0, "x1": 439.7, "y1": 129.4, "page": 1},
        {"text": "测试购买方科技有限公司", "x0": 57.5, "y0": 104.3, "x1": 147.2, "y1": 113.3, "page": 1},
        {"text": "测试销售方服务有限公司", "x0": 341.9, "y0": 104.3, "x1": 431.6, "y1": 113.3, "page": 1},
        {"text": "91110000ABCDEFGH1X", "x0": 151.3, "y0": 116.8, "x1": 280.6, "y1": 131.7, "page": 1},
        {"text": "91110000ZYXWVUTS9Y", "x0": 435.8, "y0": 116.8, "x1": 565.0, "y1": 131.7, "page": 1},
        # 明细表列头（带空格，用于定列）
        {"text": "金  额", "x0": 383.5, "y0": 150.8, "x1": 410.5, "y1": 159.8, "page": 1},
        {"text": "税  额", "x0": 553.9, "y0": 150.8, "x1": 580.8, "y1": 159.8, "page": 1},
        # 合计行
        {"text": "合", "x0": 58.1, "y0": 198.6, "x1": 67.1, "y1": 207.6, "page": 1},
        {"text": "计", "x0": 103.4, "y0": 198.6, "x1": 112.4, "y1": 207.6, "page": 1},
        {"text": "¥88.88", "x0": 381.8, "y0": 198.6, "x1": 408.7, "y1": 207.6, "page": 1},
        {"text": "¥2.66", "x0": 559.2, "y0": 198.6, "x1": 581.6, "y1": 207.6, "page": 1},
        # 价税合计小写
        {"text": "（小写）", "x0": 406.2, "y0": 279.6, "x1": 442.1, "y1": 288.5, "page": 1},
        {"text": "¥91.54", "x0": 440.1, "y0": 278.2, "x1": 467.0, "y1": 287.2, "page": 1},
    ],
}


def test_dual_column_einvoice_parties_and_total_row():
    v = values(_OCR_EINVOICE, layout=_EINVOICE_LAYOUT)
    assert v["invoice_number"] == "25990000001234567890"
    assert v["invoice_date"] == "2026-03-05"
    # 双栏配对：左=购买方、右=销售方
    assert v["buyer_name"] == "测试购买方科技有限公司"
    assert v["seller_name"] == "测试销售方服务有限公司"
    assert v["buyer_tax_id"] == "91110000ABCDEFGH1X"
    assert v["seller_tax_id"] == "91110000ZYXWVUTS9Y"
    # 合计行按列取值，且 88.88 + 2.66 == 91.54 精确成立
    assert v["net_amount"] == "88.88"
    assert v["tax_amount"] == "2.66"
    assert v["total_amount"] == "91.54"
    assert v["currency"] == "CNY"


def test_label_word_never_becomes_company_name():
    """纯文本（无坐标）时也不得把版面提示词「信息」当公司名。"""
    text = ("增值税电子普通发票\n购买方信息\n销售方信息\n"
            "名称：测试购买方科技有限公司\n名称：测试销售方服务有限公司\n")
    v = values(text)
    assert v["buyer_name"] == "测试购买方科技有限公司"
    assert v["seller_name"] == "测试销售方服务有限公司"
    assert "信息" not in (v["buyer_name"] or "", v["seller_name"] or "")


# ==================== 金额交叉校验 ====================

def test_amount_conflict_leaves_net_and_tax_blank():
    """OCR 噪声让税额读成 2.34（真值 234.56）→ 与价税合计冲突 → 留空。"""
    text = ("信息技术服务费发票\n合计金额：12,345.67 元\n"
            "税额：2.34 元\n价税合计：12,580.23 元")
    v = values(text)
    assert v["total_amount"] == "12580.23"
    assert v["net_amount"] is None
    assert v["tax_amount"] is None


def test_clean_amounts_pass_cross_check():
    text = ("信息技术服务费发票\n合计金额：12,345.67 元\n"
            "税额：234.56 元\n价税合计：12,580.23 元")
    v = values(text)
    assert v["net_amount"] == "12345.67"
    assert v["tax_amount"] == "234.56"
    assert v["total_amount"] == "12580.23"


def test_fullwidth_thousands_comma_is_handled():
    """语料里确实存在全角千分位「12，345.67」；不能在这里断成 12。"""
    text = ("物业管理服务费发票\n合计金额：12，345.67元\n"
            "税额：234.56元\n价税合计：12，580.23元")
    v = values(text)
    assert v["total_amount"] == "12580.23"
    assert v["net_amount"] == "12345.67"
    assert v["tax_amount"] == "234.56"


def test_ocr_digit_space_noise_is_repaired():
    """OCR 把 `¥88. 88` 读成带空格；归一化后应能参与交叉校验。"""
    text = ("增值税电子普通发票\n*运输服务*客运服务费\n50. 00\n"
            "¥88. 88\n¥2. 66\n价税合计（大写）\n（小写）¥91.54")
    v = values(text)
    assert v["total_amount"] == "91.54"
    assert v["net_amount"] == "88.88"
    assert v["tax_amount"] == "2.66"


def test_detail_rows_are_not_taken_as_total():
    """明细行的金额不能被当成价税合计；无明确合计标签时金额留空。"""
    text = ("信息技术服务费发票\n项目名称\n数量\n单价\n金额\n"
            "系统运维与技术支持服务\n12\n250.00\n3,000.00\n")
    v = values(text)
    assert v["total_amount"] is None
    assert v["net_amount"] is None
    assert v["tax_amount"] is None


# ==================== 留空纪律 ====================

def test_legacy_party_labels_are_not_mapped_to_buyer_seller():
    text = ("仓储保管服务费发票\n单据编号：SYN-2026-0203\n日期：2026年02月05日\n"
            "往来单位：示例往来单位有限公司\n本方单位：示例本方单位有限公司\n"
            "合计金额：10,000.00 元\n税额：600.00 元\n价税合计：10,600.00 元")
    v = values(text)
    assert v["buyer_name"] is None
    assert v["seller_name"] is None
    # 通用「日期」不是开票日期
    assert v["invoice_date"] is None
    assert v["net_amount"] == "10000.00"
    assert v["total_amount"] == "10600.00"


def test_generic_date_label_is_not_invoice_date():
    assert values("信息技术服务费发票\n日期：2026年01月08日\n价税合计：100.00")["invoice_date"] is None
    assert values("信息技术服务费发票\n开票日期：2026年01月08日\n价税合计：100.00")["invoice_date"] == "2026-01-08"


# ==================== 币种纪律 ====================

def test_foreign_currency_symbol_is_not_overwritten_by_cny_default():
    # 明确代码 → 取该代码
    assert values("价税合计：100.00 USD")["currency"] == "USD"
    assert values("价税合计：100.00 美元")["currency"] == "USD"
    # 只有外币符号、没有明确代码 → 不套 CNY 默认值
    assert values("价税合计：$100.00")["currency"] is None
    # 多币种冲突 → 留空
    assert values("价税合计：100.00 USD；另列 80.00 EUR")["currency"] is None
    # 无任何币种证据 → 业务约定默认人民币
    assert values("价税合计：100.00")["currency"] == "CNY"
