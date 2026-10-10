# -*- coding: utf-8 -*-
"""
识别闭环服务（方案 B 3.2）

核心规则：
- **临时结果 → 原子发布**：全部页面 OCR + 抽取在临时变量中完成，全部成功才
  在一个事务里 publish_revision；任何一步失败只更新 last_attempt_*，
  不覆盖之前有效版本；首次失败则该文档无有效结果。
- **重新识别需显式确认**：已有有效版本的文档必须 force=True（前端弹确认后重发）。
- **同文档识别互斥**：进程内按 doc_id 加锁，超时重试不会产生重复版本。
- **引擎显式化**：rapidocr/tesseract 才算真实识别；mock 引擎只在测试环境
  （DOC_OCR_ALLOW_MOCK=1）允许，否则返回"引擎不可用"，不生成成功占位文档。
- **写入门禁**：长任务不持锁，逐页边界检查 abort_if_closed；
  发布版本的事务走 write_section。
"""
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.models import DocumentInfo
from app.services import revisions as rev_svc
from app.services.backup_gate import GATE, BackupInProgress
from app.services.extractor import extract_for_type
from app.services.field_registry import classify_doc_type
from app.services.ocr_engine import recognize, get_engine_info, get_engine_type
from app.services.pdf_handler import iter_page_images, extract_text_layout, PdfError
from app.services.preprocess import load_image_from_path, preprocess_image

logger = logging.getLogger(__name__)

# 同文档识别互斥锁注册表（进程级；单进程部署下足够，见任务书单进程约束）
_doc_locks: Dict[int, threading.Lock] = {}
_doc_locks_guard = threading.Lock()


def _doc_lock(doc_id: int) -> threading.Lock:
    with _doc_locks_guard:
        return _doc_locks.setdefault(doc_id, threading.Lock())


class RecognitionError(RuntimeError):
    """识别失败（可读原因）；不携带堆栈给前端"""

    def __init__(self, msg: str, code: int = 500):
        super().__init__(msg)
        self.code = code


def _mock_allowed() -> bool:
    return os.getenv("DOC_OCR_ALLOW_MOCK", "").strip().lower() in ("1", "true", "yes")


def _boxes_to_spans(boxes, page_no: int):
    """把 OCR 返回的文本框（4 点多边形）转成统一 span（bbox + 页码）。

    发票的双栏字段（购买方/销售方 名称与税号）靠坐标配对；
    扫描件没有文本层，只能用 OCR 文本框当版面证据。
    """
    spans = []
    for b in boxes or []:
        box = b.get("box") if isinstance(b, dict) else None
        if box is None:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, IndexError, ValueError):
            continue
        spans.append({
            "text": (b.get("text") or "").strip(),
            "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "page": page_no,
        })
    return spans


@GATE.writer
def run_recognition(db: Session, doc: DocumentInfo, *, force: bool = False) -> dict:
    """
    执行识别闭环。成功返回结果 dict；失败抛 RecognitionError（已落 last_attempt_*）。
    """
    # 重新识别确认：已有有效版本必须显式 force（界面确认语义由前端完成）
    if doc.current_revision is not None and not force:
        raise RecognitionError(
            "该文档已有有效识别结果，重新识别需确认后重试（force=true）", code=409)

    # 引擎检查：不能用 optimized 判断真假（tesseract 也是真引擎）
    engine_type = get_engine_type()
    if engine_type == "mock" and not _mock_allowed():
        raise RecognitionError(
            "OCR 引擎不可用（当前为模拟引擎），无法进行正式识别，"
            "请检查 rapidocr-onnxruntime 安装后重启服务", code=503)

    file_path = Path(doc.file_path)
    if not file_path.exists():
        raise RecognitionError("原始文件不存在", code=404)

    lock = _doc_lock(doc.id)
    if not lock.acquire(blocking=False):
        raise RecognitionError("该文档正在识别中，请稍后重试", code=409)
    try:
        # file_path **显式传参**：它是 run_recognition 的局部变量，
        # 靠闭包"意外可见"在重构挪动代码后会变成 NameError —— 这里吃过一次亏。
        return _do_recognition(db, doc, engine_type, file_path)
    finally:
        lock.release()


def _do_recognition(db: Session, doc: DocumentInfo, engine_type: str,
                    file_path: Path) -> dict:
    t0 = time.time()
    GATE.abort_if_closed("识别")
    logger.info(f"=== 开始识别文档 ID={doc.id} 文件={doc.file_name}（{engine_type}）===")

    try:
        # ---- 阶段 1：全部页面临时结果（不落库）----
        page_texts = []
        ocr_spans = []          # OCR 文本框（扫描件的版面证据）
        total_cost = 0.0
        if doc.file_type == "pdf":
            for page_no, img in iter_page_images(str(file_path)):
                GATE.abort_if_closed(f"识别第 {page_no} 页")
                processed = preprocess_image(img)
                text, cost, boxes = recognize(processed)
                page_texts.append(f"=== 第 {page_no} 页 ===\n{text}")
                total_cost += cost
                ocr_spans.extend(_boxes_to_spans(boxes, page_no))
            if not page_texts:
                raise RecognitionError("PDF 解析失败，未获取到页面图像", code=400)
        else:
            img = load_image_from_path(str(file_path))
            processed = preprocess_image(img)
            text, cost, boxes = recognize(processed)
            page_texts.append(text)
            total_cost = cost
            ocr_spans.extend(_boxes_to_spans(boxes, 1))

        full_text = "\n".join(page_texts)

        # 版面坐标（仅供抽取用，不改原文/版本机制）：
        #   text 层 PDF → 文本层 span（更干净且带坐标）
        #   扫描件/图片 → OCR 文本框
        layout = None
        if doc.file_type == "pdf":
            try:
                spans, lay_text = extract_text_layout(str(file_path))
            except Exception as e:      # 文本层异常不能拖垮识别
                logger.warning(f"文档 {doc.id} 文本层读取失败，改用 OCR 文本框: {e}")
                spans, lay_text = [], ""
            if spans:
                layout = {"spans": spans, "text": lay_text}
        if layout is None and ocr_spans:
            layout = {"spans": ocr_spans, "text": full_text}

        # ---- 阶段 2：分类与字段抽取（仍是临时结果）----
        # 类型规则：首次识别用本地规则分类；已有版本的重新识别保留用户设定的类型
        if doc.current_revision is None:
            doc_type = classify_doc_type(full_text)
        else:
            doc_type = doc.doc_type or "other"
        fields = extract_for_type(full_text, doc_type, layout)

        # ---- 阶段 3：单事务原子发布新版本 ----
        with GATE.write_section("发布识别版本"):
            revision = rev_svc.publish_revision(
                db, doc,
                change_reason=rev_svc.REASON_OCR,
                doc_type=doc_type,
                ocr_raw_text=full_text,
                effective_text=full_text,
                fields=fields,
                engine_type=engine_type,
            )
            doc.status = 1
            doc.error_msg = None
            doc.infer_cost = round(total_cost, 2)
            doc.last_attempt_status = 1
            doc.last_attempt_error = None
            db.commit()
        db.refresh(doc)

        logger.info(
            f"识别完成 doc={doc.id} r{revision} type={doc_type} "
            f"文本 {len(full_text)} 字 耗时 {total_cost:.0f}ms "
            f"总耗时 {(time.time() - t0) * 1000:.0f}ms")
        return {
            "doc_id": doc.id,
            "status": doc.status,
            "revision": revision,
            "doc_type": doc_type,
            "ocr_raw_text": full_text,
            "infer_cost": doc.infer_cost,
            "fields": fields,
        }

    except (RecognitionError, PdfError, BackupInProgress):
        raise
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            raise
        logger.exception("识别处理异常 doc=%s", doc.id)
        raise RecognitionError("识别处理失败，请检查文件后重试") from e


def record_attempt_failure(db: Session, doc: DocumentInfo, err: str) -> None:
    """识别失败落库：只记最近尝试，不动旧有效版本；首次失败则无有效结果。"""
    try:
        with GATE.write_section('识别失败状态'):
            db.rollback()
            db.refresh(doc)
            doc.last_attempt_status = 2
            doc.last_attempt_error = err[:500]
            if doc.current_revision is None:
                doc.status = 2
                doc.error_msg = err[:500]
            db.commit()
    except Exception as e:
        logger.error(f"记录识别失败状态出错 doc={doc.id}: {e}")
        db.rollback()
