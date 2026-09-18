"""
图像预处理服务模块
基于 OpenCV 实现：
  0. 降采样（长边压到 MAX_SIDE 以内）—— **唯一真正降低 OCR 推理计算量的一步**
  1. 灰度化处理
  2. 自适应图像降噪（去除光斑、噪点；代价是每页增加约 1s 耗时）
  3. 文档自动倾斜矫正（修正倾斜扫描件）
  4. 自适应亮度对比度调整（提升暗光文档识别准确率）

⚠️ 关于「轻量化」的准确说法：只有第 0 步减少计算量。
第 1/4 步很便宜（毫秒级）但也不减计算量，第 2/3 步是**增加**耗时的，
它们的价值在识别准确率 —— 不要把它们当成加速手段。
"""
import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

# ==================== 轻量化参数 ====================
# 预处理的目标长边（像素）。**这是整条流水线里唯一能真正降低 OCR 推理计算量的一步。**
#
# 实测（1653x2339 的 A4 扫描图，RapidOCR CPU 推理，本机）：
#     图像宽 1653 → 4231ms / 识别 61 字
#     图像宽 1400 → 3046ms / 识别 69 字
#     图像宽 1200 → 2604ms / 识别 69 字
#     图像宽  800 → 2315ms / 识别 52 字
# 降到 1200~1400 可省 30~38% 推理时间，且识别字数不降反升（缩图去掉了噪点像素）；
# 再往下压（800）开始丢字。取 1400 作上限，留出余量。
MAX_SIDE = 1400

# NL-means 降噪参数。
# ⚠️ 参数名坑：OpenCV 的 Python 绑定里叫 templateWindowSize / searchWindowSize。
# 这里早期写成了 searchWindow / templateWindow，导致**每次调用都抛 TypeError**，
# 又被 preprocess_image 的 except 吞掉 → 整条预处理流水线长期静默失效。
# 现在改用位置参数，避免以后 OpenCV 再改参数名时又静默坏掉。
_NLMEANS_H = 10          # 滤波强度
_NLMEANS_TEMPLATE = 7    # 模板窗口（应为奇数）
_NLMEANS_SEARCH = 21     # 搜索窗口（应为奇数，耗时与之近似线性）


def downscale(img: np.ndarray, max_side: int = MAX_SIDE) -> np.ndarray:
    """
    等比缩小到长边不超过 max_side。

    **这才是真正的「轻量化」步骤** —— 直接减少送进 OCR 的像素数，
    推理耗时随之下降。其余步骤（灰度化/降噪/矫正/增强）都不减少计算量，
    降噪反而是**增加**耗时的。

    使用 INTER_AREA：缩小时它等价于带抗锯齿的像素平均，比 INTER_LINEAR 更适合降采样。
    """
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)


def to_gray(img: np.ndarray) -> np.ndarray:
    """灰度化处理，降低识别计算量"""
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def adaptive_denoise(img: np.ndarray) -> np.ndarray:
    """自适应图像降噪，去除拍照文档光斑、噪点"""
    if len(img.shape) == 3:
        img = to_gray(img)
    # 使用非局部均值降噪，有效去除扫描噪点同时保留文字边缘。
    # 注意：这一步是**加耗时**的（实测 1653x2339 图约 1.15s），
    # 它的价值在识别准确率，不在速度 —— 不要把它当成轻量化手段。
    return cv2.fastNlMeansDenoising(
        img, None, _NLMEANS_H, _NLMEANS_TEMPLATE, _NLMEANS_SEARCH
    )


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
    完整图像预处理流水线
    按顺序执行：降采样 → 灰度化 → 降噪 → 倾斜矫正 → 对比度增强

    **关于「轻量化」的准确说法**：整条流水线里真正降低 OCR 计算量的只有第一步
    「降采样」（减少送进模型的像素数，实测省 30~38% 推理时间）。
    其余步骤不减少计算量 —— 灰度化/CLAHE 很便宜（毫秒级），
    降噪反而要 1 秒上下，倾斜矫正也不便宜。它们的价值在**识别准确率**。
    所以不要把它们统称为「轻量化优化」。
    """
    try:
        # Step 0: 降采样 —— 放最前面，让后面每一步都在更小的图上做，整体更省
        img = downscale(img)
        logger.info(f"  [预处理] 降采样完成 → {img.shape[1]}x{img.shape[0]}")

        # Step 1: 灰度化
        img = to_gray(img)

        # Step 2: 自适应降噪
        img = adaptive_denoise(img)

        # Step 3: 倾斜矫正
        img = deskew(img)

        # Step 4: 亮度对比度增强
        img = adjust_brightness_contrast(img)

        logger.info("  [预处理] 灰度化 → 降噪 → 倾斜矫正 → 对比度增强 全部完成")
        return img
    except Exception as e:
        # 用 ERROR 而不是 WARNING：流水线整体失败意味着「图像预处理」这个功能
        # 在本次请求上完全没生效，属于功能级故障。曾经因为只打 warning +
        # 异常被吞，一个 TypeError 让整条流水线坏了很久都没人发现。
        logger.error(
            f"  [预处理] 流水线整体失败，本次使用未处理（或仅部分处理）的图像: "
            f"{type(e).__name__}: {e}"
        )
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
