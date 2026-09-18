"""
PDF 文档处理模块
使用 PyMuPDF (fitz) 将 PDF 页面渲染为图像，供 OCR 识别
"""
import fitz  # PyMuPDF
import numpy as np
import cv2
import logging
from typing import List

logger = logging.getLogger(__name__)


def pdf_to_images(pdf_path: str, dpi: int = 200) -> List[np.ndarray]:
    """
    将 PDF 每一页渲染为图像 (numpy ndarray, BGR 格式)
    参数: pdf_path — PDF 文件路径, dpi — 渲染分辨率
    返回: 图像列表
    """
    images = []
    try:
        doc = fitz.open(pdf_path)
        zoom = dpi / 72  # PDF 默认 72 DPI
        mat = fitz.Matrix(zoom, zoom)

        for page_num in range(len(doc)):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)

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

            images.append(img_array)
            logger.info(f"  [PDF] 第 {page_num + 1} 页渲染完成 ({pix.width}x{pix.height})")

        doc.close()
    except Exception as e:
        logger.error(f"  [PDF] PDF解析失败: {e}")
        raise

    return images
