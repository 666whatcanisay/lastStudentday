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
from typing import Iterator, List, Tuple

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
