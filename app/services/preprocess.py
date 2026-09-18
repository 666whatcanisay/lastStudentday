"""
图像预处理服务模块
基于 OpenCV 实现：
  1. 灰度化处理（降低识别计算量）
  2. 自适应图像降噪（去除光斑、噪点）
  3. 文档自动倾斜矫正（修正倾斜扫描件）
  4. 自适应亮度对比度调整（提升暗光文档识别准确率）

轻量化优化核心：通过预处理减少 OCR 推理的计算量，降低 CPU 耗时
"""
import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def to_gray(img: np.ndarray) -> np.ndarray:
    """灰度化处理，降低识别计算量"""
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def adaptive_denoise(img: np.ndarray) -> np.ndarray:
    """自适应图像降噪，去除拍照文档光斑、噪点"""
    if len(img.shape) == 3:
        img = to_gray(img)
    # 使用非局部均值降噪，有效去除扫描噪点同时保留文字边缘
    return cv2.fastNlMeansDenoising(img, None, h=10, searchWindow=21, templateWindow=7)


def deskew(img: np.ndarray) -> np.ndarray:
    """
    文档自动倾斜矫正
    通过最小外接矩形检测文本方向角度，进行仿射变换修正
    """
    if len(img.shape) == 3:
        gray = to_gray(img)
    else:
        gray = img

    # 二值化（自适应阈值）
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

    # 膨胀文字区域形成连通块
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 5))
    dilate = cv2.dilate(thresh, kernel, iterations=1)

    # 查找轮廓
    contours = cv2.findContours(dilate, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = contours[0] if len(contours) == 2 else contours[1]

    if not contours:
        return img

    # 取最大的文本区域轮廓计算倾斜角度
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    if area < 100:
        return img

    rect = cv2.minAreaRect(largest)
    angle = rect[-1]

    # minAreaRect 返回的角度范围是 [-90, 0)，需要修正
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    # 倾斜角度过小则不矫正
    if abs(angle) < 0.5 or abs(angle) > 45:
        return img

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated


def adjust_brightness_contrast(img: np.ndarray, brightness: int = 0, contrast: int = 0) -> np.ndarray:
    """
    自适应亮度对比度调整，提升暗光文档识别准确率
    使用 CLAHE（限制对比度自适应直方图均衡化）自动增强
    """
    if len(img.shape) == 3:
        img = to_gray(img)

    # CLAHE 自适应直方图均衡化，增强局部对比度
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(img)


def preprocess_image(img: np.ndarray) -> np.ndarray:
    """
    完整图像预处理流水线（轻量化优化核心）
    按顺序执行：灰度化 → 降噪 → 倾斜矫正 → 亮度对比度增强
    返回预处理后的图像供 OCR 推理使用
    """
    try:
        # Step 1: 灰度化
        img = to_gray(img)
        logger.info("  [预处理] 灰度化完成")

        # Step 2: 自适应降噪
        img = adaptive_denoise(img)
        logger.info("  [预处理] 自适应降噪完成")

        # Step 3: 倾斜矫正
        img = deskew(img)
        logger.info("  [预处理] 倾斜矫正完成")

        # Step 4: 亮度对比度增强
        img = adjust_brightness_contrast(img)
        logger.info("  [预处理] 亮度对比度增强完成")

        return img
    except Exception as e:
        logger.warning(f"  [预处理] 预处理异常，使用原图: {e}")
        return img


def load_image_from_bytes(file_bytes: bytes) -> np.ndarray:
    """从字节流加载图像（支持 jpg/png/bmp）"""
    nparr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码图像文件，可能文件已损坏")
    return img


def load_image_from_path(file_path: str) -> np.ndarray:
    """从文件路径加载图像"""
    img = cv2.imread(file_path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图像文件: {file_path}")
    return img
