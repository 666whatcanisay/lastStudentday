"""
PDF 文档处理模块
使用 PyMuPDF (fitz) 将 PDF 页面渲染为图像，供 OCR 识别

方案 B：iter_page_images() 逐页生成器 —— 不再一次性把所有页面图像堆进内存
（10 页 200dpi 的 A4 约 8MB/页，全列表意味着近百 MB 峰值），
识别完一页释放一页；try/finally 保证异常路径也关闭文档句柄。
加密/损坏文件抛出带可操作中文信息的 PdfError。
"""
import fitz  # PyMuPDF
import numpy as np
import cv2
import logging
from typing import Dict, Iterator, List, Tuple

logger = logging.getLogger(__name__)


class PdfError(RuntimeError):
    """PDF 不可读（加密/损坏/无页面）——路由层转 400 可操作错误"""


def _open_pdf(pdf_path: str) -> "fitz.Document":
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise PdfError(f"PDF 文件无法打开（可能已损坏）: {e}") from e
    if doc.needs_pass:
        doc.close()
        raise PdfError("PDF 已加密且无法读取，请解密后重新上传")
    if len(doc) == 0:
        doc.close()
        raise PdfError("PDF 没有任何页面")
    return doc


def iter_page_images(pdf_path: str, dpi: int = 200) -> Iterator[Tuple[int, np.ndarray]]:
    """
    逐页渲染 PDF 为图像 (numpy ndarray, BGR 格式)，产出 (页码1起, 图像)。

    用法：
        for page_no, img in iter_page_images(path):
            ...识别 img...      # 上一页的图像在下次迭代时可被 GC 释放
    """
    doc = _open_pdf(pdf_path)
    try:
        zoom = dpi / 72  # PDF 默认 72 DPI
        mat = fitz.Matrix(zoom, zoom)
        for page_num in range(len(doc)):
            page = doc[page_num]
            try:
                pix = page.get_pixmap(matrix=mat)
            except Exception as e:
                raise PdfError(f"PDF 第 {page_num + 1} 页渲染失败: {e}") from e

            # pix.samples 是 RGB 数据，转换为 numpy 数组
            img_array = np.frombuffer(pix.samples, dtype=np.uint8)
            img_array = img_array.reshape(pix.height, pix.width, pix.n)

            # 转换为 BGR (OpenCV 格式)
            if pix.n == 4:  # RGBA
                img_array = cv2.cvtColor(img_array, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:  # RGB
                img_array = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
            elif pix.n == 1:  # Gray
                img_array = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)

            logger.info(f"  [PDF] 第 {page_num + 1} 页渲染完成 ({pix.width}x{pix.height})")
            yield page_num + 1, img_array
    finally:
        doc.close()


def pdf_to_images(pdf_path: str, dpi: int = 200) -> List[np.ndarray]:
    """兼容旧入口：一次性返回全部页面（新代码请用 iter_page_images 逐页处理）"""
    return [img for _, img in iter_page_images(pdf_path, dpi)]


def extract_text_layout(pdf_path: str) -> Tuple[List[Dict], str]:
    """
    抽取 PDF 的**文本层**：带 bbox 的 span 列表 + 阅读顺序全文。

    用途：发票双栏字段（购买方/销售方 名称与税号）需要坐标才能稳定配对；
    text 层 PDF 的文本也比 OCR 更干净（实测 OCR 把 `3,267.88` 读成 `3.267.88`，
    文本层仍是正确的）。

    ⚠️ 这是**抽取用的辅助证据**，不覆盖 OCR 原文：ocr_raw_text / effective_text
    仍按既有链路生成。扫描件（无文本层）返回 ([], "")，由调用方改用 OCR 文本框。

    返回 (spans, full_text)；spans 元素为
    {"text", "x0", "y0", "x1", "y1", "page"}，坐标为 PDF point（72 dpi）。
    """
    spans: List[Dict] = []
    texts: List[str] = []
    doc = _open_pdf(pdf_path)
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            try:
                page_text = page.get_text()
            except Exception as e:
                raise PdfError(f"PDF 第 {page_num + 1} 页文本层读取失败: {e}") from e
            if page_text and page_text.strip():
                texts.append(page_text)
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                blocks = []
            for block in blocks:
                if block.get("type") != 0:      # 只取文本块，跳过图片
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = (span.get("text") or "").strip()
                        if not txt:
                            continue
                        x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                        spans.append({
                            "text": txt, "x0": float(x0), "y0": float(y0),
                            "x1": float(x1), "y1": float(y1), "page": page_num + 1,
                        })
    finally:
        doc.close()
    return spans, "\n".join(texts)
