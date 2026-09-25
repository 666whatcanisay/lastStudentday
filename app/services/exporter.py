# -*- coding: utf-8 -*-
"""
导出服务（方案 B §3.5）—— PDF 识别结果报告 / 批量 ZIP / Excel

关键约束（改代码前请先读）：
  1. **版本锁定**：调用方在请求开始时一次性取出有效版本快照，导出全程只用这份快照，
     正文与字段不会在生成途中混用不同版本（路由层负责取快照，本模块只消费）。
  2. 回收站文档不可导出；本模块只校验，拒绝动作在路由层（单份报错 / 批量记进清单）。
  3. 选了「附原件」但原件缺失 → 该份导出**失败**，绝不静默省略附录。
  4. PDF 用 PyMuPDF Story 排版 + 内建 china-s 字体（**离线可用**），不加载任何外部资源；
     所有用户内容经 html.escape 转义后进入 HTML 模板。
  5. 逐份写入受控临时目录（config.TMP_DIR），不在内存中堆积 PDF；
     响应完成/失败后由调用方清理；启动时清理本应用遗留的 export_* 目录。
  6. Excel：按类型分工作表，金额为数值单元格、币种独立列，文本强制按字符串写入。
"""
import html
import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from app.config import APP_VERSION, SCHEMA_VERSION, TMP_DIR, UPLOAD_DIR

logger = logging.getLogger(__name__)

A4 = fitz.paper_rect("a4")
MARGIN_X = 50.0
MARGIN_TOP = 56.0
MARGIN_BOTTOM = 54.0
MAX_BATCH = 20
MANIFEST_FORMAT_VERSION = 1
EXPORT_PREFIX = "export_"        # 启动清理只认这个前缀，绝不碰系统临时目录


class ExportError(RuntimeError):
    """单份导出失败（原件缺失/快照缺失等）——批量时记进清单，不中断其他份"""


# ==================== HTML 模板 ====================

_CSS = """
body { font-family: china-s; font-size: 10.5pt; color: #222222; }
h1 { font-size: 16pt; text-align: center; margin: 0 0 2px; }
.sub { text-align: center; color: #909399; font-size: 8.5pt; margin: 0 0 12px; }
h2 { font-size: 11.5pt; margin: 14px 0 6px; color: #303133; }
table { width: 100%; border-collapse: collapse; }
th { background: #f5f7fa; text-align: left; width: 92pt; font-weight: normal; color: #606266; }
th, td { border: 1px solid #dcdfe6; padding: 4px 6px; font-size: 9.5pt; }
td { word-break: break-all; }
.note { color: #E6A23C; font-size: 9pt; margin: 4px 0 0; }
.muted { color: #909399; }
.missing { color: #C0C4CC; }
.textblock { font-size: 10pt; line-height: 1.55; margin: 2px 0 8px; word-break: break-all; }
.foot { color: #909399; font-size: 8pt; margin-top: 10px; }
"""

_FOOT_NOTE = (
    "本报告由系统根据 OCR 识别结果与人工校对内容生成，仅用于信息整理，"
    "<b>不等同于原件</b>，也不构成对原件内容的确认。"
)


def _e(v: Any) -> str:
    """HTML 转义：所有用户内容进模板前必须过这一道"""
    return html.escape("" if v is None else str(v), quote=True)


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def _paragraphs(text: Optional[str]) -> str:
    """有效全文 → 段落（换行保留；空文本给明确占位，不假装有内容）"""
    if not text or not text.strip():
        return '<p class="missing">（无有效正文）</p>'
    out = []
    for line in str(text).splitlines():
        line = line.rstrip()
        out.append(f'<p class="textblock">{_e(line) if line else "&nbsp;"}</p>')
    return "".join(out)


def _build_html(doc, snap, fields: List[Dict[str, Any]], *,
                attach_original: bool, export_time: str) -> str:
    reviewed = bool(doc.current_revision is not None
                    and doc.reviewed_revision == doc.current_revision)
    type_label = {"invoice": "发票", "contract": "合同", "other": "其他"}.get(
        snap.doc_type or "other", "其他")
    engine_label = {
        "rapidocr": "RapidOCR（本地）", "tesseract": "Tesseract（本地）",
        "mock": "模拟引擎（非真实识别）", "legacy_unknown": "历史数据（引擎未知）",
    }.get(snap.engine_type, snap.engine_type or "未知")

    meta_rows = [
        ("文档 ID", str(doc.id)),
        ("文件名", doc.file_name),
        ("文件类型", "PDF" if doc.file_type == "pdf" else "图片"),
        ("上传时间", _fmt_dt(doc.upload_time)),
        ("识别引擎", engine_label),
        ("单据类型", type_label),
        ("有效版本", f"r{snap.revision}" + ("（当前）" if snap.revision == doc.current_revision else "（历史版本）")),
        ("版本来源", {"ocr": "OCR 识别", "edit": "人工编辑", "retype": "切换类型",
                      "legacy": "旧库迁移"}.get(snap.change_reason, snap.change_reason or "未知")),
        ("校对状态", "已校对" if reviewed else "未校对"),
        ("导出时间", export_time),
    ]
    meta_html = "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in meta_rows)

    field_rows = []
    for f in fields:
        value = f.get("field_value")
        cell = f'<span class="missing">未识别/缺失</span>' if value in (None, "") else _e(value)
        field_rows.append(f"<tr><th>{_e(f.get('field_name') or f.get('field_key'))}</th><td>{cell}</td></tr>")
    fields_html = "".join(field_rows) or '<tr><td class="missing">该类型无字段定义</td></tr>'

    review_note = "" if reviewed else (
        '<p class="note">⚠ 当前版本<b>尚未人工校对</b>，字段值由自动抽取得到，'
        '请与下方正文核对后再使用。</p>')
    appendix_note = '<p class="muted">附录：已附加原件</p>' if attach_original else \
        '<p class="muted">附录：未附加原件（导出时未勾选）</p>'

    return f"""<html><body>
<h1>文档识别结果报告</h1>
<p class="sub">{_e(doc.file_name)} · r{snap.revision} · {_e(type_label)}</p>
<h2>一、文档基本信息</h2>
<table>{meta_html}</table>
{review_note}
<h2>二、有效字段（{_e(type_label)}）</h2>
<table>{fields_html}</table>
<p class="muted">字段与正文互不覆盖：改字段不改写正文，改正文不重抽字段。</p>
<h2>三、有效全文</h2>
{_paragraphs(snap.effective_text)}
{appendix_note}
<p class="foot">{_FOOT_NOTE}</p>
</body></html>"""


# ==================== PDF 生成 ====================

def _append_original(out: "fitz.Document", file_path: str, file_type: str) -> int:
    """
    追加原件，返回新增页数。
      · PDF：按原页顺序整份追加
      · 图片：新建一页，等比缩放适应页面（**不拉伸**）
    原件缺失/不可读 → 抛 ExportError（调用方把它记为失败，不静默省略）
    """
    path = Path(file_path)
    if not path.exists():
        raise ExportError("原件缺失，无法附加（可在文档详情中确认原件是否存在）")
    # 路径越界防护：只附加本应用上传目录内的原件，防止异常数据把任意文件塞进报告
    try:
        root = Path(UPLOAD_DIR).resolve()
        if root not in Path(path).resolve().parents:
            raise ExportError("原件路径不在本应用存储目录内，已拒绝附加")
    except ExportError:
        raise
    except Exception:
        raise ExportError("原件路径无法校验，已拒绝附加")
    try:
        if file_type == "pdf":
            with fitz.open(str(path)) as src:
                if src.needs_pass:
                    raise ExportError("原件 PDF 已加密，无法附加")
                if len(src) == 0:
                    raise ExportError("原件 PDF 无页面")
                before = len(out)
                out.insert_pdf(src, from_page=0, to_page=len(src) - 1)
                return len(out) - before
        pix = fitz.Pixmap(str(path))
        try:
            page = out.new_page(width=A4.width, height=A4.height)
            box = fitz.Rect(MARGIN_X, MARGIN_TOP,
                            A4.width - MARGIN_X, A4.height - MARGIN_BOTTOM)
            scale = min(box.width / pix.width, box.height / pix.height)
            w, h = pix.width * scale, pix.height * scale
            x0 = box.x0 + (box.width - w) / 2
            y0 = box.y0 + (box.height - h) / 2
            page.insert_image(fitz.Rect(x0, y0, x0 + w, y0 + h), pixmap=pix)
            return 1
        finally:
            pix = None
    except ExportError:
        raise
    except Exception as e:
        raise ExportError(f"原件读取失败，无法附加: {e}") from e


def _stamp_footers(out: "fitz.Document", appendix_from: Optional[int]):
    """页码：报告页「第 X 页 / 共 Y 页」，附录页「附录 第 i 页 / 共 n 页」"""
    total = len(out)
    for i, page in enumerate(out):
        if appendix_from is not None and i >= appendix_from:
            idx = i - appendix_from + 1
            n = total - appendix_from
            text = f"附录 第 {idx} 页 / 共 {n} 页"
        else:
            text = f"第 {i + 1} 页 / 共 {total} 页"
        w = fitz.get_text_length(text, fontname="china-s", fontsize=8)
        x = (page.rect.width - w) / 2
        y = page.rect.height - 28
        page.insert_text((x, y), text, fontname="china-s", fontsize=8,
                         color=(0.56, 0.58, 0.60))


def build_report_pdf(*, doc, snap, fields: List[Dict[str, Any]],
                     attach_original: bool, tmp_dir: Path) -> bytes:
    """
    生成单份 A4 报告 PDF（bytes）。

    snap 由调用方在请求开始时取出并锁定，整个生成过程只用它 ——
    不会出现「正文是 r3、字段是 r4」这种混版报告。
    """
    if snap is None:
        raise ExportError("该文档没有有效版本，无法导出报告")

    tmp_dir.mkdir(parents=True, exist_ok=True)
    body_path = tmp_dir / f"body_{uuid.uuid4().hex}.pdf"
    export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_text = _build_html(doc, snap, fields, attach_original=attach_original,
                            export_time=export_time)

    story = fitz.Story(html=html_text, user_css=_CSS)
    # 中文字体：PyMuPDF 内建 china-s（Droid Sans Fallback），**离线可用**，
    # 不引用任何系统字体文件或网络资源。
    story.body.set_font(fitz.Font("china-s"))
    writer = fitz.DocumentWriter(str(body_path))

    def rectfn(rect_num, filled):
        return (A4,
                fitz.Rect(MARGIN_X, MARGIN_TOP, A4.width - MARGIN_X, A4.height - MARGIN_BOTTOM),
                None)

    story.write(writer, rectfn)
    writer.close()

    appendix_from = None
    out = fitz.open(str(body_path))
    try:
        if attach_original:
            appendix_from = len(out)
            _append_original(out, doc.file_path, doc.file_type)
        _stamp_footers(out, appendix_from)
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()
        try:
            body_path.unlink(missing_ok=True)
        except Exception:
            pass


# ==================== 批量 ZIP ====================

def build_reports_zip(*, items: List[Dict[str, Any]], attach_original: bool,
                      work_root: Path) -> Tuple[bytes, Dict[str, Any]]:
    """
    批量导出：一份一个 PDF + export_manifest.json。

    items 由调用方准备好，每项：{doc, snap, fields, error?}
      · 有 error → 该项直接记失败，不生成 PDF
      · 部分成功照样产出 ZIP；全部失败由调用方返回错误（不产出空 ZIP）
    """
    work_root.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix=EXPORT_PREFIX, dir=str(work_root)))
    zip_path = batch_dir / "reports.zip"
    manifest_items: List[Dict[str, Any]] = []
    ok_files: List[Tuple[Path, str]] = []

    try:
        for it in items:
            doc = it["doc"]
            snap = it.get("snap")
            entry: Dict[str, Any] = {
                "doc_id": doc.id,
                "file_name": doc.file_name,
                "revision": (snap.revision if snap else None),
                "status": "failed",
                "file": None,
                "reason": None,
            }
            if it.get("error"):
                entry["reason"] = it["error"]
                manifest_items.append(entry)
                continue
            try:
                data = build_report_pdf(
                    doc=doc, snap=snap, fields=it.get("fields") or [],
                    attach_original=attach_original, tmp_dir=batch_dir)
                name = f"doc_{doc.id}_r{snap.revision}_报告.pdf"
                p = batch_dir / name
                p.write_bytes(data)
                ok_files.append((p, name))
                entry["status"] = "ok"
                entry["file"] = name
            except ExportError as e:
                entry["reason"] = str(e)
            except Exception as e:
                logger.error(f"文档 {doc.id} 报告生成失败: {e}", exc_info=True)
                entry["reason"] = f"报告生成失败：{str(e)[:120]}"
            manifest_items.append(entry)

        success = sum(1 for m in manifest_items if m["status"] == "ok")
        failed = len(manifest_items) - success
        manifest = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "attach_original": attach_original,
            "requested": [it["doc"].id for it in items],
            "success_count": success,
            "failed_count": failed,
            "items": manifest_items,
        }

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("export_manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))
            for p, name in ok_files:
                zf.write(p, arcname=name)
        return zip_path.read_bytes(), manifest
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)


# ==================== Excel ====================

_FORMULA_PREFIX = ("=", "+", "-", "@")


def _write_text(ws, row: int, col: int, value: Optional[str]):
    """
    写入文本单元格：始终按**字符串**写入（openpyxl 写字符串不会被 Excel 当公式执行），
    对以 = + - @ 开头的内容额外把单元格格式设成文本（@），
    防止它被重新编辑或另存 CSV 后被当成公式。
    """
    s = "" if value is None else str(value)
    c = ws.cell(row=row, column=col, value=s)
    c.data_type = "s"
    if s.startswith(_FORMULA_PREFIX):
        c.number_format = "@"
    return c


def _write_amount(ws, row: int, col: int, value: Optional[str]):
    """金额写成**数值**单元格（币种另起一列，不做字符串拼接）"""
    if value in (None, ""):
        ws.cell(row=row, column=col, value=None)
        return
    try:
        ws.cell(row=row, column=col, value=float(Decimal(str(value))))
    except (InvalidOperation, ValueError):
        # 解析不了就留空，绝不写成看似数字的假值
        ws.cell(row=row, column=col, value=None)


def build_excel_workbook(*, rows: List[Dict[str, Any]]) -> bytes:
    """
    按类型分工作表（发票/合同/其他，只生成有数据的），列为：
    文档ID | 文件名 | 文件类型 | 上传时间 | 校对状态 | 版本 | <该类型字段…> | 有效全文
    rows 由调用方按锁定好的版本快照准备好。
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    TYPE_SHEETS = [("invoice", "发票"), ("contract", "合同"), ("other", "其他")]

    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="409EFF", end_color="409EFF", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")

    created = False
    for doc_type, sheet_name in TYPE_SHEETS:
        type_rows = [r for r in rows if r["doc_type"] == doc_type]
        if not type_rows:
            continue  # 三类工作表按需存在，空表不生成
        created = True
        ws = wb.create_sheet(title=sheet_name)
        spec_keys = []
        for r in type_rows:
            for f in r["fields"]:
                if f.get("field_key") and f["field_key"] not in spec_keys:
                    spec_keys.append(f["field_key"])

        headers = ["文档ID", "文件名", "文件类型", "上传时间", "校对状态", "版本"]
        labels = {}
        for r in type_rows:
            for f in r["fields"]:
                labels.setdefault(f.get("field_key"), f.get("field_name"))
        headers += [labels.get(k, k) for k in spec_keys]
        headers += ["有效全文"]

        for col, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=col, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = header_align

        for ri, r in enumerate(type_rows, start=2):
            col = 1
            ws.cell(row=ri, column=col, value=r["doc_id"]); col += 1
            _write_text(ws, ri, col, r["file_name"]); col += 1
            ws.cell(row=ri, column=col, value=("PDF" if r["file_type"] == "pdf" else "图片")); col += 1
            ws.cell(row=ri, column=col, value=_fmt_dt(r["upload_time"])); col += 1
            ws.cell(row=ri, column=col, value=("已校对" if r["reviewed"] else "未校对")); col += 1
            ws.cell(row=ri, column=col, value=(f"r{r['revision']}" if r["revision"] else "")); col += 1
            fmap = {f.get("field_key"): f for f in r["fields"]}
            for k in spec_keys:
                f = fmap.get(k) or {}
                if k.endswith("_amount") or k in ("total_amount", "contract_amount",
                                                  "net_amount", "tax_amount"):
                    _write_amount(ws, ri, col, f.get("field_value"))
                else:
                    _write_text(ws, ri, col, f.get("field_value"))
                col += 1
            _write_text(ws, ri, col, r["effective_text"])

        widths = [8, 28, 10, 20, 10, 8] + [18] * len(spec_keys) + [60]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        ws.freeze_panes = "A2"

    if not created:
        ws = wb.create_sheet(title="无数据")
        ws.cell(row=1, column=1, value="所选文档没有可导出的有效版本")

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ==================== 受控临时目录 ====================

def make_work_root() -> Path:
    """
    导出临时根目录（受控：只在 config.TMP_DIR 下创建）。

    启动时的遗留清理由 app/main.py 的 startup_event 统一处理
    （只清理 `backup_staging_` / `export_` 前缀，绝不碰系统临时目录），
    这里不再重复实现一份，避免两处规则漂移。
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR
