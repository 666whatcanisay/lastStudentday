"""Targeted, source-verified legacy layouts; no filename-based classification."""

from app.services.extractor import extract_for_type
from app.services.field_registry import classify_doc_type


def values(text: str, doc_type: str) -> dict:
    return {field["field_key"]: field["field_value"]
            for field in extract_for_type(text, doc_type)}


def test_legacy_invoice_requires_title_and_tax_evidence():
    text = ("信息技术服务费发票\n单据编号：XX-2026-0101 日期：2026年01月08日\n"
            "金额\n项目 1 36,577.56\n合计金额：54,464.66 元\n"
            "税率：6% 税额：3,267.88 元\n价税合计：57,732.54 元")
    assert classify_doc_type(text) == "invoice"
    extracted = values(text, "invoice")
    assert extracted["invoice_date"] is None  # generic 日期 is not necessarily 开票日期
    assert extracted["net_amount"] == "54464.66"
    assert extracted["tax_amount"] == "3267.88"
    assert extracted["total_amount"] == "57732.54"
    assert extracted["currency"] is None  # 「元」不足以锁定币种
    assert classify_doc_type("报销单\n备注：请附发票\n金额：99.00") == "other"


def test_legacy_contract_does_not_invent_party_roles():
    text = ("便携式计算机采购合同\n单据编号：CG-2026-0408 日期：2026年04月14日\n"
            "往来单位：联启数码科技有限公司\n本方单位：杭州明远智能装备有限公司\n"
            "金额合计：130,229.00 元")
    assert classify_doc_type(text) == "contract"
    extracted = values(text, "contract")
    assert extracted["contract_title"] == "便携式计算机采购合同"
    assert extracted["contract_number"] == "CG-2026-0408"
    assert extracted["signed_date"] is None  # generic 日期 is not necessarily 签订日期
    assert extracted["contract_amount"] == "130229.00"
    assert extracted["party_a"] is None
    assert extracted["party_b"] is None
    assert classify_doc_type("采购合同\n请附发票\n本方单位：甲公司") == "other"


def test_other_document_keeps_generic_date_without_amount_inference():
    text = "货物运输服务结算单\n单据编号：YS-2026-0102 日期：2026年01月19日\n金额合计：88.00 元"
    assert classify_doc_type(text) == "other"
    assert values(text, "other")["document_date"] == "2026-01-19"


def test_split_route_registration_keeps_fixed_paths_before_document_id():
    from app.main import app

    paths = [route.path for route in app.routes]
    for path in ("/api/document/stats", "/api/document/analytics",
                 "/api/document/analytics/details", "/api/document/export",
                 "/api/document/export/report", "/api/document/export/reports"):
        assert paths.count(path) == 1
        assert paths.index(path) < paths.index("/api/document/{doc_id}")
