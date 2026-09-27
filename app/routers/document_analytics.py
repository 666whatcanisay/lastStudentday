"""Document analytics routes. URLs and response shapes remain compatible."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DocumentInfo
from app.schemas import ApiResponse
from app.services import analytics
from app.services import field_registry as registry

router = APIRouter(prefix="/api/document", tags=["文档统计"])

@router.get("/stats", response_model=ApiResponse)
def get_document_stats(db: Session = Depends(get_db)):
    """简单统计（保留兼容）：总数/成功数/平均耗时；排除回收站。"""
    base = db.query(DocumentInfo).filter(DocumentInfo.deleted_at.is_(None))
    total = base.count()
    success_count = base.filter(DocumentInfo.status == 1).count()
    unreviewed_count = base.filter(
        DocumentInfo.current_revision.isnot(None),
        or_(DocumentInfo.reviewed_revision.is_(None),
            DocumentInfo.reviewed_revision != DocumentInfo.current_revision),
    ).count()
    avg_cost = db.query(func.avg(DocumentInfo.infer_cost)).filter(
        DocumentInfo.deleted_at.is_(None),
        DocumentInfo.status == 1, DocumentInfo.infer_cost != None
    ).scalar()
    avg_cost = round(avg_cost, 1) if avg_cost else 0.0

    return ApiResponse.success(data={
        "total": total,
        "success": success_count,
        "unreviewed": unreviewed_count,
        "avg_cost": avg_cost,
    })


@router.get("/analytics", response_model=ApiResponse)
def get_document_analytics(
    doc_type: Optional[str] = Query(None, description="类型：invoice/contract/other"),
    date_from: Optional[str] = Query(None, description="业务日期起（YYYY-MM-DD，含当天）"),
    date_to: Optional[str] = Query(None, description="业务日期止（YYYY-MM-DD，含当天）"),
    date_unknown: Optional[bool] = Query(None, description="true=只看日期未知；false=排除日期未知"),
    currency: Optional[str] = Query(None, description="币种 ISO 代码（CNY/USD…），或 UNKNOWN=只看币种缺失"),
    reviewed: Optional[bool] = Query(None, description="校对状态过滤"),
    party_role: Optional[str] = Query(None,
                                      description="排行角色（一次一个）：buyer/seller/party_a/party_b"),
    db: Session = Depends(get_db),
):
    """
    分类统计（任务书 3.4 唯一口径）。

    与明细穿透 `/analytics/details` 共用 `analytics.apply_filters`，
    保证「图与明细同口径」。金额一律为十进制字符串，前端只展示不重算。
    """
    import re
    if doc_type and doc_type not in registry.DOC_TYPES:
        return ApiResponse.error(
            msg=f"不支持的文档类型: {doc_type}（可选 {', '.join(registry.DOC_TYPES)}）", code=400)
    for name, val in (("date_from", date_from), ("date_to", date_to)):
        if val and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val.strip()):
            return ApiResponse.error(msg=f"{name} 需为 YYYY-MM-DD 格式: {val}", code=400)

    filters = {
        "doc_type": doc_type,
        "date_from": date_from,
        "date_to": date_to,
        "date_unknown": date_unknown,
        "currency": (currency or "").strip().upper() or None,
        "reviewed": reviewed,
    }
    docs = analytics.apply_filters(analytics.load_valid_docs(db), **filters)
    data = analytics.build_analytics(docs, filters=filters, party_role=party_role)
    return ApiResponse.success(data=data)


@router.get("/analytics/details", response_model=ApiResponse)
def get_document_analytics_details(
    doc_type: Optional[str] = Query(None, description="类型：invoice/contract/other"),
    date_from: Optional[str] = Query(None, description="业务日期起（含当天）"),
    date_to: Optional[str] = Query(None, description="业务日期止（含当天）"),
    date_unknown: Optional[bool] = Query(None),
    currency: Optional[str] = Query(None),
    reviewed: Optional[bool] = Query(None),
    party_role: Optional[str] = Query(None, description="排行角色，用于点某个交易对象"),
    party: Optional[str] = Query(None, description="交易对象名称（配合 party_role）"),
    month: Optional[str] = Query(None, description="月度分组键 2026-01，或 unknown"),
    require_amount: bool = Query(False, description="true=只要金额与币种都有效的文档（金额图穿透）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    统计明细穿透：与图表**同一套过滤条件**，可再叠加 month/party/require_amount。

    amount_totals 按币种分组给出明细合计 —— 点击金额图时应与图上数字一致。
    """
    import re
    if doc_type and doc_type not in registry.DOC_TYPES:
        return ApiResponse.error(
            msg=f"不支持的文档类型: {doc_type}（可选 {', '.join(registry.DOC_TYPES)}）", code=400)
    for name, val in (("date_from", date_from), ("date_to", date_to)):
        if val and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val.strip()):
            return ApiResponse.error(msg=f"{name} 需为 YYYY-MM-DD 格式: {val}", code=400)

    docs = analytics.apply_filters(
        analytics.load_valid_docs(db),
        doc_type=doc_type, date_from=date_from, date_to=date_to,
        date_unknown=date_unknown,
        currency=(currency or "").strip().upper() or None,
        reviewed=reviewed, month=month, require_amount=require_amount,
        party_role=party_role, party=party,
    )
    data = analytics.build_details(docs, page=page, page_size=page_size,
                                   party_role=party_role)
    return ApiResponse.success(data=data)
