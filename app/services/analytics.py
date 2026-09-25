# -*- coding: utf-8 -*-
"""
统计分析服务（方案 B §3.4「统计的唯一口径」）

这个文件是全部统计数字的**唯一来源**：图表接口、明细穿透、Excel 导出都走这里，
不允许前端或别处自行再做一次金额汇总（前端只展示，不重新计算财务合计）。

口径要点（改代码前请先读文档，别凭直觉改）：
  1. 基础有效集 = 未在回收站 + 有当前有效版本 + 非已知 Mock。
     最近一次重新识别失败但有旧有效版本的文档仍按旧有效版本统计，但会给出警告。
  2. 金额只认发票 `total_amount` 和合同 `contract_amount`；
     按「类型 + 币种」独立汇总，**不提供跨类型/跨币种总金额**，也不叫收入/利润。
  3. 日期：发票取开票日期、合同取签订日期、其他取文档日期；缺失归「日期未知」，
     绝不用上传时间替代。有日期范围时，无日期文档不纳入（并在 warnings 里说明）。
  4. 金额以 Decimal 累加，对外输出十进制字符串（保留两位）；前端不做二次计算。
  5. 币种缺失的文档单列，不输出其金额合计（避免无意义的混加）。
  6. 交易对象排行一次只选一个角色（发票 buyer/seller、合同 party_a/party_b），
     一个角色单独累计金额，绝不把双方金额重复累计成总额。
  7. 名称只去首尾空白 + 合并内部空白，不做模糊合并公司。
"""
import logging
import re
from datetime import date
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import DocumentInfo, DocumentRevision
from app.services import revisions as rev_svc

logger = logging.getLogger(__name__)

MOCK_ENGINE = "mock"          # 已知 Mock：不进基础有效集
DOC_TYPE_LABELS = {"invoice": "发票", "contract": "合同", "other": "其他"}
DOC_TYPES = ("invoice", "contract", "other")

# 业务金额字段：只有发票与合同有"业务金额"，other 没有，不参与金额统计
BUSINESS_AMOUNT_FIELD = {"invoice": "total_amount", "contract": "contract_amount"}
# 业务日期字段：类型不同、语义不同，不能互相替代
BUSINESS_DATE_FIELD = {
    "invoice": "invoice_date",
    "contract": "signed_date",
    "other": "document_date",
}

# 交易对象角色 —— 一次只选一个（role 决定文档类型与字段，天然不会双方重复累计）
PARTY_ROLES: Dict[str, Dict[str, Any]] = {
    "buyer": {"doc_type": "invoice", "field": "buyer_name", "label": "购买方（仅发票）"},
    "seller": {"doc_type": "invoice", "field": "seller_name", "label": "销售方（仅发票）"},
    "party_a": {"doc_type": "contract", "field": "party_a", "label": "甲方（仅合同）"},
    "party_b": {"doc_type": "contract", "field": "party_b", "label": "乙方（仅合同）"},
}
# 角色别名：任务书里写作 buyer/seller/party_a/party_b，这里允许带类型前缀的写法
ROLE_ALIASES = {
    "invoice_buyer": "buyer", "invoice_seller": "seller",
    "contract_party_a": "party_a", "contract_party_b": "party_b",
}
DEFAULT_ROLE_BY_TYPE = {"invoice": "buyer", "contract": "party_a", "other": "buyer"}

UNKNOWN_MONTH = "unknown"          # 日期未知的月度分组键
UNKNOWN_CURRENCY = "UNKNOWN"       # 币种筛选的特殊取值（不是合法 ISO 代码，不会撞真币种）
TOP_N = 10
_Q = Decimal("0.01")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


# ==================== 值解析 ====================

def _dec(v: Optional[Decimal]) -> Optional[str]:
    """Decimal → 两位小数十进制字符串（对外一律字符串，避免浮点误差与前端二次计算）"""
    if v is None:
        return None
    return str(v.quantize(_Q, rounding=ROUND_HALF_UP))


def parse_amount(raw: Optional[str]) -> Optional[Decimal]:
    """金额解析：无效/空 → None（调用方记为缺失，不按 0 累加）"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        value = Decimal(s.replace(",", ""))
        return value if value.is_finite() and abs(value) < Decimal('1e18') else None
    except (InvalidOperation, ValueError):
        return None


def parse_date(raw: Optional[str]) -> Optional[str]:
    """业务日期：只接受已规范化的 ISO（YYYY-MM-DD），其余一律算缺失"""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        return date.fromisoformat(s).isoformat() if _ISO_DATE.fullmatch(s) else None
    except ValueError:
        return None


def parse_currency(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().upper()
    return s if _CURRENCY_CODE.match(s) else None


def normalize_party(raw: Optional[str]) -> Optional[str]:
    """交易对象名称：仅去首尾空白 + 合并内部空白，不做模糊合并公司"""
    if not raw:
        return None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    return s or None


def resolve_role(role: Optional[str], doc_type: Optional[str]) -> str:
    """角色归一化；未指定时按当前类型过滤给一个默认角色"""
    if role:
        r = ROLE_ALIASES.get(role, role)
        if r in PARTY_ROLES:
            return r
        # 未知角色：退回默认，不报错（统计是只读查询，没必要 400）
        logger.warning(f"未知的排行角色: {role}，已退回默认")
    return DEFAULT_ROLE_BY_TYPE.get(doc_type or "other", "buyer")


# ==================== 基础有效集 ====================

def load_valid_docs(db: Session) -> List[Dict[str, Any]]:
    """
    读取基础有效集：未在回收站 + 有当前有效版本 + 非已知 Mock。

    字段一律取自**当前有效版本快照**（不是 document_extract 的现值），
    保证统计与其版本一一对应，不存在"指针已更新、快照还没换"的半套状态。
    """
    rows = (
        db.query(DocumentInfo, DocumentRevision)
        .join(
            DocumentRevision,
            (DocumentRevision.doc_id == DocumentInfo.id)
            & (DocumentRevision.revision == DocumentInfo.current_revision),
        )
        .filter(
            DocumentInfo.deleted_at.is_(None),
            DocumentInfo.current_revision.isnot(None),
        )
        .all()
    )

    out: List[Dict[str, Any]] = []
    for doc, rev in rows:
        if doc.engine_type == MOCK_ENGINE or rev.engine_type == MOCK_ENGINE:
            continue
        doc_type = (rev.doc_type or doc.doc_type or "other")
        if doc_type not in BUSINESS_DATE_FIELD:
            doc_type = "other"
        fields = rev_svc.snapshot_fields(rev)
        fmap = {f.get("field_key"): f.get("field_value") for f in fields if f.get("field_key")}

        date_key = BUSINESS_DATE_FIELD.get(doc_type)
        amount_key = BUSINESS_AMOUNT_FIELD.get(doc_type)
        biz_date = parse_date(fmap.get(date_key)) if date_key else None
        amount = parse_amount(fmap.get(amount_key)) if amount_key else None
        currency = parse_currency(fmap.get("currency"))

        out.append({
            "doc_id": doc.id,
            "file_name": doc.file_name,
            "doc_type": doc_type,
            "business_date": biz_date,
            "month": biz_date[:7] if biz_date else UNKNOWN_MONTH,
            "amount": amount,
            "amount_invalid": amount is None,          # 无金额字段 or 金额解析不出
            "has_amount_field": amount_key is not None,
            "currency": currency,
            "currency_missing": currency is None and amount is not None,
            "reviewed": bool(doc.current_revision is not None
                             and doc.reviewed_revision == doc.current_revision),
            "revision": doc.current_revision,
            "engine_type": rev.engine_type or doc.engine_type,
            # 最近一次重新识别失败，但仍有旧有效版本 → 计入统计但要警告
            "stale_attempt": doc.last_attempt_status == 2,
            "fields": fmap,
        })
    return out


# ==================== 过滤 ====================

def apply_filters(docs: List[Dict[str, Any]], *,
                  doc_type: Optional[str] = None,
                  date_from: Optional[str] = None,
                  date_to: Optional[str] = None,
                  date_unknown: Optional[bool] = None,
                  currency: Optional[str] = None,
                  reviewed: Optional[bool] = None,
                  month: Optional[str] = None,
                  require_amount: bool = False,
                  party_role: Optional[str] = None,
                  party: Optional[str] = None) -> List[Dict[str, Any]]:
    """共用过滤 —— 图表与明细必须走同一套，否则"图与明细同口径"无从谈起"""
    has_range = bool(date_from or date_to)
    cur = (currency or "").strip().upper() or None
    is_unknown_cur = cur == UNKNOWN_CURRENCY

    # 角色只用于排行/明细穿透：一旦给出，文档类型随之锁定（避免跨类型混排）
    role = resolve_role(party_role, doc_type) if (party_role or party) else None
    role_spec = PARTY_ROLES.get(role) if role else None

    out = []
    for d in docs:
        if doc_type and d["doc_type"] != doc_type:
            continue
        if role_spec and d["doc_type"] != role_spec["doc_type"]:
            continue

        if has_range:
            # 有日期范围 → 无日期文档不纳入（任务书 3.4）
            if not d["business_date"]:
                continue
            if date_from and d["business_date"] < date_from:
                continue
            if date_to and d["business_date"] > date_to:
                continue
        if date_unknown is True and d["business_date"] is not None:
            continue
        if date_unknown is False and d["business_date"] is None:
            continue
        if month:
            if month == UNKNOWN_MONTH:
                if d["business_date"] is not None:
                    continue
            elif d["month"] != month:
                continue

        if cur:
            if is_unknown_cur:
                if d["currency"] is not None:
                    continue
            elif d["currency"] != cur:
                continue

        if reviewed is True and not d["reviewed"]:
            continue
        if reviewed is False and d["reviewed"]:
            continue

        if require_amount and (d["amount"] is None or d["currency"] is None):
            continue

        if role_spec and party:
            name = normalize_party(d["fields"].get(role_spec["field"]))
            if name != party:
                continue

        out.append(d)
    return out


# ==================== 汇总 ====================

def _warn(code: str, message: str, count: int) -> Dict[str, Any]:
    return {"code": code, "message": message, "count": count}


def build_analytics(docs: List[Dict[str, Any]], *,
                    filters: Dict[str, Any],
                    party_role: Optional[str]) -> Dict[str, Any]:
    """按 §3.4 口径汇总：summary / monthly / type_counts / amounts / party_ranking"""
    role = resolve_role(party_role, filters.get("doc_type"))
    role_spec = PARTY_ROLES[role]

    # ---------- summary ----------
    doc_count = len(docs)
    unreviewed = sum(1 for d in docs if not d["reviewed"])
    # 金额/币种缺失只在"有业务金额字段"的类型上统计（other 没有金额字段，不该算缺失）
    amount_base = [d for d in docs if d["has_amount_field"]]
    missing_date = sum(1 for d in docs if d["business_date"] is None)
    missing_amount = sum(1 for d in amount_base if d["amount"] is None)
    missing_currency = sum(1 for d in amount_base if d["currency"] is None)
    unknown_currency_amount = sum(1 for d in amount_base if d["currency_missing"])

    summary = {
        "doc_count": doc_count,
        "reviewed_count": doc_count - unreviewed,
        "unreviewed_count": unreviewed,
        "missing_date_count": missing_date,
        "missing_amount_count": missing_amount,
        "missing_currency_count": missing_currency,
        # 分母：缺失计数可重叠，各自的分母必须说清楚（任务书 3.4）
        "missing_date_denominator": doc_count,
        "missing_amount_denominator": len(amount_base),
        "missing_currency_denominator": len(amount_base),
        "unknown_currency_amount_count": unknown_currency_amount,
    }

    # ---------- 月度数量 ----------
    monthly_map: Dict[str, int] = defaultdict(int)
    for d in docs:
        monthly_map[d["month"]] += 1
    monthly = [{"month": m, "doc_count": c}
               for m, c in sorted(monthly_map.items(),
                                  key=lambda kv: (kv[0] == UNKNOWN_MONTH, kv[0]))]

    # ---------- 类型分布（三类恒出，0 也是真实数字，不是假数据） ----------
    type_map: Dict[str, int] = defaultdict(int)
    for d in docs:
        type_map[d["doc_type"]] += 1
    type_counts = [
        {"doc_type": t, "label": DOC_TYPE_LABELS[t], "doc_count": type_map.get(t, 0)}
        for t in DOC_TYPES
    ]

    # ---------- 金额：按 (类型, 币种) 独立汇总 ----------
    # 结构：amounts = [{doc_type, currency, doc_count, amount_total, monthly:[...]}]
    # 币种缺失单独成组且不输出合计。
    groups: Dict[tuple, Dict[str, Any]] = {}
    for d in docs:
        if d["amount"] is None:
            continue
        key = (d["doc_type"], d["currency"] or UNKNOWN_CURRENCY)
        g = groups.setdefault(key, {
            "doc_type": d["doc_type"],
            "currency": d["currency"],          # 缺失为 None
            "doc_count": 0,
            "_total": Decimal("0"),
            "_monthly": defaultdict(lambda: {"total": Decimal("0"), "count": 0}),
        })
        g["doc_count"] += 1
        if d["currency"] is None:
            continue        # 币种缺失：只计数，不累加（"不输出未知币种合计"）
        g["_total"] += d["amount"]
        m = g["_monthly"][d["month"]]
        m["total"] += d["amount"]
        m["count"] += 1

    amounts: List[Dict[str, Any]] = []
    for (dtype, cur_key), g in sorted(
        groups.items(),
        key=lambda kv: (DOC_TYPES.index(kv[0][0]), kv[0][1] == UNKNOWN_CURRENCY, kv[0][1]),
    ):
        monthly_rows = [
            {"month": m, "amount_total": _dec(v["total"]), "doc_count": v["count"]}
            for m, v in sorted(g["_monthly"].items(),
                               key=lambda kv: (kv[0] == UNKNOWN_MONTH, kv[0]))
        ]
        amounts.append({
            "doc_type": dtype,
            "doc_type_label": DOC_TYPE_LABELS[dtype],
            "currency": g["currency"],
            "doc_count": g["doc_count"],
            # 币种缺失 → 明确不给出合计（null，而不是 0 或混加值）
            "amount_total": None if g["currency"] is None else _dec(g["_total"]),
            "monthly": [] if g["currency"] is None else monthly_rows,
            "note": "币种缺失，按口径不提供金额合计" if g["currency"] is None else None,
        })

    # ---------- 交易对象排行（单角色，按币种分组） ----------
    rank_map: Dict[Optional[str], Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for d in docs:
        if d["doc_type"] != role_spec["doc_type"]:
            continue
        name = normalize_party(d["fields"].get(role_spec["field"]))
        if not name:
            continue
        item = rank_map[d["currency"]].setdefault(name, {
            "party": name,
            "currency": d["currency"],
            "doc_count": 0,
            "amount_doc_count": 0,
            "_total": Decimal("0"),
        })
        item["doc_count"] += 1
        if d["amount"] is not None and d["currency"] is not None:
            item["amount_doc_count"] += 1
            item["_total"] += d["amount"]

    rank_items: List[Dict[str, Any]] = []
    for cur, parties in sorted(rank_map.items(),
                               key=lambda kv: (kv[0] is None, kv[0] or "")):
        for item in sorted(parties.values(),
                           key=lambda it: (-it["_total"], it["party"]))[:TOP_N]:
            rank_items.append({
                "party": item["party"],
                "currency": item["currency"],
                "doc_count": item["doc_count"],
                "amount_doc_count": item["amount_doc_count"],
                "amount_total": None if item["currency"] is None else _dec(item["_total"]),
            })
    currencies_seen = sorted({d["currency"] for d in docs if d["currency"]},
                             key=lambda c: c)

    # ---------- 警告 ----------
    warnings: List[Dict[str, Any]] = []
    if filters.get("date_from") or filters.get("date_to"):
        warnings.append(_warn(
            "date_range_excludes_unknown",
            "已按业务日期范围过滤，日期未知的文档未纳入本次统计",
            doc_count))
    if filters.get("date_unknown") is False:
        warnings.append(_warn(
            "date_unknown_excluded", "已排除日期未知的文档", doc_count))
    stale = sum(1 for d in docs if d["stale_attempt"])
    if stale:
        warnings.append(_warn(
            "stale_attempt_kept",
            f"{stale} 份文档最近一次重新识别失败，当前统计沿用其旧有效版本", stale))
    if unknown_currency_amount:
        warnings.append(_warn(
            "unknown_currency_amount",
            f"{unknown_currency_amount} 份文档金额有效但币种缺失，未计入任何币种合计",
            unknown_currency_amount))
    if missing_amount:
        warnings.append(_warn(
            "missing_amount",
            f"{missing_amount} 份文档无有效业务金额，未计入金额统计", missing_amount))

    return {
        "summary": summary,
        "monthly": monthly,
        "type_counts": type_counts,
        "amounts": amounts,
        "party_ranking": {
            "role": role,
            "role_label": role_spec["label"],
            "doc_type": role_spec["doc_type"],
            "top_n": TOP_N,
            "currencies": currencies_seen,
            "items": rank_items,
        },
        "warnings": warnings,
        "filters": {k: v for k, v in filters.items() if v is not None},
    }


def build_details(docs: List[Dict[str, Any]], *, page: int, page_size: int,
                  party_role: Optional[str]) -> Dict[str, Any]:
    """
    明细穿透：与图表共用过滤后的同一批 docs，保证「图与明细同口径」。
    金额图点开时调用方额外带 require_amount=true（金额与币种都必须有效）。
    """
    role = resolve_role(party_role, None)
    role_spec = PARTY_ROLES.get(role)
    field_key = role_spec["field"] if role_spec else None

    rows = [{
        "doc_id": d["doc_id"],
        "file_name": d["file_name"],
        "doc_type": d["doc_type"],
        "doc_type_label": DOC_TYPE_LABELS[d["doc_type"]],
        "business_date": d["business_date"],
        "month": d["month"],
        "amount": _dec(d["amount"]),
        "currency": d["currency"],
        "party": normalize_party(d["fields"].get(field_key)) if field_key else None,
        "reviewed": d["reviewed"],
        "revision": d["revision"],
        "engine_type": d["engine_type"],
        "stale_attempt": d["stale_attempt"],
    } for d in docs]
    # 有日期的按日期升序，日期未知排在最后（不拿它冒充最早）
    rows.sort(key=lambda r: (r["business_date"] is None, r["business_date"] or "", r["doc_id"]))

    # 明细合计：依旧按币种分组，不跨币种混加
    totals: Dict[Optional[str], Dict[str, Any]] = {}
    for r in rows:
        if r["amount"] is None:
            continue
        t = totals.setdefault(r["currency"], {
            "currency": r["currency"], "doc_count": 0, "_total": Decimal("0")})
        t["doc_count"] += 1
        if r["currency"] is not None:
            t["_total"] += Decimal(r["amount"])
    amount_totals = [
        {"currency": cur,
         "doc_count": t["doc_count"],
         "amount_total": None if cur is None else _dec(t["_total"])}
        for cur, t in sorted(totals.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    ]

    start = (page - 1) * page_size
    return {
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "items": rows[start:start + page_size],
        "amount_totals": amount_totals,
    }
