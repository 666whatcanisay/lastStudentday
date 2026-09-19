#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模型精度量化对比（FP32 vs INT8）—— 报告二「对比项 C」的评测脚本。

────────────────────────────────────────────────────────────────
为什么做成三组而不是两组
────────────────────────────────────────────────────────────────
onnxruntime 的 `quantize_dynamic` **默认只量化 MatMul / Gemm 这类算子，不含 Conv**。
而 PP-OCRv3 的 det / rec 都是**卷积为主**的 CNN —— 也就是说，
默认动态量化很可能"量化了但没加速"。若只测一组，会得出"量化无用"的错误结论。

所以本脚本测三组：
  1. fp32        —— 原始 FP32 模型（基线）
  2. int8_dyn    —— 动态量化，**默认算子集**（不含 Conv）
  3. int8_conv   —— 动态量化，**显式纳入 Conv**

另附「动态量化 vs 静态量化」的说明：CNN 要拿到真正的 INT8 加速，
通常需要静态量化（QDQ + 校准集），本脚本**未做**（需要额外准备校准数据），
因此 int8_conv 的结果是**动态量化的上限**，不是 INT8 的理论上限。

────────────────────────────────────────────────────────────────
额外依赖（**刻意不进 requirements.txt**）
────────────────────────────────────────────────────────────────
本脚本需要两个生产环境用不到的包：
    .venv/Scripts/python.exe -m pip install onnx sympy
  · `onnx`  —— onnxruntime.quantization 的前置依赖
  · `sympy` —— quant_pre_process 的符号形状推断需要
它们只服务于量化实验，装进生产依赖会让部署多背两个包，故不入库。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    .venv/Scripts/python.exe scripts/eval_ocr_quantization.py
    .venv/Scripts/python.exe scripts/eval_ocr_quantization.py --dpi 200
"""
from __future__ import annotations

import argparse
import re
import shutil
import statistics
import sys
import time
import unicodedata
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pdf_handler import pdf_to_images  # noqa: E402
from app.services.preprocess import preprocess_image  # noqa: E402

CORPUS = ROOT / "test_corpus"
OUT_DIR = ROOT / "models" / "quantized"          # 已 gitignore，产物不入库

FP32_MODELS = {
    "det": "ch_PP-OCRv3_det_infer.onnx",
    "rec": "ch_PP-OCRv3_rec_infer.onnx",
    "cls": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
}


def norm(s: str) -> str:
    s = re.sub(r"===\s*第\s*\d+\s*页\s*===", "", s or "")
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(gt: str, hyp: str) -> float:
    return levenshtein(gt, hyp) / max(len(gt), 1)


def find_fp32_models() -> dict[str, Path]:
    """定位 rapidocr 包内自带的 FP32 ONNX 模型"""
    import rapidocr_onnxruntime
    base = Path(rapidocr_onnxruntime.__file__).resolve().parent / "models"
    found = {}
    for key, name in FP32_MODELS.items():
        p = base / name
        if not p.exists():
            raise FileNotFoundError(f"未找到 FP32 模型: {p}")
        found[key] = p
    return found


def quantize(src: Path, dst: Path, op_types: list[str] | None, pre: Path) -> None:
    """动态量化：权重转 INT8，激活在推理时动态量化。

    ⚠️ 必须先跑 `quant_pre_process` —— 这是 ONNX Runtime 官方推荐的量化前置步骤。
    不加这一步时，实测量化后模型体积**反而变大**（13.07MB → 13.28MB，102%），
    且识别准确率从 93.2% 崩到 59.1%：量化其实**没有真正生效**，
    只是往图里塞了一堆量化元数据，还把原图搞坏了。
    ORT 会在日志里持续提示 "Please consider to run pre-processing before quantization"。
    """
    from onnxruntime.quantization import QuantType, quant_pre_process, quantize_dynamic

    # 符号形状推断需要 sympy；缺失时退化为跳过。
    # ⚠️ 注意：**即使装了 sympy 也可能失败** —— PP-OCRv3 的 rec 模型里有含两个 `-1`
    # 的 Reshape，会触发 onnxruntime 符号形状推断的断言
    # `assert new_sympy_shape.count(-1) < 2`。这是 ONNX 侧已知限制，
    # 所以这里捕获**所有**异常并退回 skip_symbolic_shape=True，而不是只捕 ImportError。
    try:
        quant_pre_process(str(src), str(pre), skip_symbolic_shape=False)
    except Exception as e:
        print(f"    ⚠️ 符号形状推断失败（{type(e).__name__}），退回 skip_symbolic_shape=True")
        quant_pre_process(str(src), str(pre), skip_symbolic_shape=True)

    kwargs = dict(
        model_input=str(pre),
        model_output=str(dst),
        weight_type=QuantType.QUInt8,
    )
    if op_types:
        kwargs["op_types_to_quantize"] = op_types
    quantize_dynamic(**kwargs)


def count_quant_ops(path: Path) -> dict:
    """统计模型里真正落地的量化算子。

    **必须检查这一项** —— 光看文件大小会被骗：
    `quantize_dynamic` 默认只覆盖 MatMul / Gemm 等算子，**不含 Conv**，
    而 PP-OCRv3 的 det / rec 是卷积为主的 CNN。
    若这里返回空 dict，说明权重根本没被量化，后面测出来的耗时差异都是噪声。
    """
    import onnx

    m = onnx.load(str(path))
    ops: dict[str, int] = {}
    for n in m.graph.node:
        ops[n.op_type] = ops.get(n.op_type, 0) + 1
    return {o: c for o, c in ops.items() if "Quant" in o or "QLinear" in o}


def build_engine(models: dict[str, Path]):
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(
        det_model_path=str(models["det"]),
        rec_model_path=str(models["rec"]),
        cls_model_path=str(models["cls"]),
    )


def run_config(name: str, engine, images: dict[str, object], truths: dict[str, str]):
    """跑一遍全语料，返回 (准确率, 耗时中位数, 逐篇耗时)"""
    from app.services.ocr_engine import recognize

    # 预热：首次调用含 ONNX session 初始化，不预热会把模型加载时间算进去
    for fn in list(images)[:2]:
        recognize(images[fn])

    cers, costs = [], []
    for fn, img in images.items():
        import app.services.ocr_engine as oe
        oe._ocr_engine = ("rapidocr", engine)        # 注入本次要测的引擎
        text, cost, _ = recognize(img)
        cers.append(cer(truths[fn], norm(text)))
        costs.append(cost)
    return statistics.mean(cers), statistics.median(costs), costs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", type=int, default=200, help="渲染 DPI（生产链路为 200）")
    args = ap.parse_args()

    pdfs = sorted(CORPUS.glob("*.pdf"))
    if not pdfs:
        print(f"未找到语料：{CORPUS}")
        return 1

    print("=" * 78)
    print("模型精度量化对比（FP32 vs INT8）")
    print("=" * 78)
    print(f"语料     : {len(pdfs)} 篇")
    print(f"渲染     : {args.dpi} DPI + 图像预处理（与生产链路一致）")
    print(f"真值     : PDF 文本层（NFKC + 去空白）")
    print()

    # ---------------- 准备量化模型 ----------------
    fp32 = find_fp32_models()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("── 量化模型 ──────────────────────────────")
    variants = {
        "fp32": fp32,
        "int8_dyn": {},
        "int8_conv": {},
    }
    for key in ("det", "rec", "cls"):
        src = fp32[key]
        for tag, op_types in (("int8_dyn", None), ("int8_conv", ["Conv", "MatMul"])):
            dst = OUT_DIR / f"{tag}_{src.name}"
            if not dst.exists():
                try:
                    quantize(src, dst, op_types, OUT_DIR / f"pre_{tag}_{src.name}")
                except Exception as e:
                    print(f"  {tag}/{key} 量化失败：{type(e).__name__}: {e}")
                    continue
            variants[tag][key] = dst

    print(f"\n{'模型':<8}{'FP32':>10}{'INT8(默认)':>12}{'INT8(+Conv)':>13}{'压缩比':>8}"
          f"{'量化算子(dyn)':>15}{'量化算子(conv)':>16}")
    print("-" * 84)
    for key in ("det", "rec", "cls"):
        s0 = fp32[key].stat().st_size
        p1, p2 = variants["int8_dyn"].get(key), variants["int8_conv"].get(key)
        s1 = p1.stat().st_size if p1 else 0
        s2 = p2.stat().st_size if p2 else 0
        q1 = sum(count_quant_ops(p1).values()) if p1 else 0
        q2 = sum(count_quant_ops(p2).values()) if p2 else 0
        ratio = f"{s1 / s0:.0%}" if s1 else "—"
        print(f"{key:<8}{s0/1024/1024:>8.2f}MB{s1/1024/1024:>10.2f}MB{s2/1024/1024:>11.2f}MB"
              f"{ratio:>8}{q1:>15}{q2:>16}")
    tot0 = sum(fp32[k].stat().st_size for k in fp32)
    tot1 = sum(v.stat().st_size for v in variants["int8_dyn"].values())
    tot2 = sum(v.stat().st_size for v in variants["int8_conv"].values())
    print(f"{'合计':<8}{tot0/1024/1024:>8.2f}MB{tot1/1024/1024:>10.2f}MB{tot2/1024/1024:>11.2f}MB"
          f"{tot1/tot0:>8.0%}")
    if tot1 == 0:
        print("\n  ⚠️ 量化算子为 0 → 权重未被真正量化，后续耗时差异属噪声，结论不可用。")

    # ---------------- 预渲染（三组共用同一批图，避免渲染差异干扰）----------------
    print(f"\n── 预渲染 {len(pdfs)} 篇 @ {args.dpi} DPI ──────────")
    images, truths = {}, {}
    for p in pdfs:
        images[p.name] = preprocess_image(pdf_to_images(str(p), dpi=args.dpi)[0])
        truths[p.name] = norm("".join(pg.get_text() for pg in fitz.open(p)))
    print(f"  完成，图像尺寸 {images[list(images)[0]].shape[1]}x{images[list(images)[0]].shape[0]}")

    # ---------------- 逐组评测 ----------------
    results = {}
    for tag, models in variants.items():
        if not models:
            continue
        print(f"\n── 评测 {tag} ────────────────────────────")
        engine = build_engine(models)
        acc, cost, costs = run_config(tag, engine, images, truths)
        results[tag] = (acc, cost, costs)
        print(f"  字符准确率 {1-acc:.1%}   耗时中位数 {cost:.0f}ms   "
              f"区间 {min(costs):.0f}~{max(costs):.0f}ms")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"{'配置':<16}{'模型体积':>12}{'耗时中位数':>14}{'字符准确率':>12}{'相对加速':>12}")
    print("-" * 78)
    base = results.get("fp32")
    for tag, models in variants.items():
        if tag not in results:
            continue
        acc, cost, _ = results[tag]
        size = sum(v.stat().st_size for v in models.values())
        speed = f"{base[1] / cost:.2f}x" if base and cost else "—"
        print(f"{tag:<16}{size/1024/1024:>10.2f}MB{cost:>11.0f}ms{1-acc:>11.1%}{speed:>12}")
    if base:
        print(f"\n  基线（fp32）：{base[1]:.0f}ms / {1-base[0]:.1%}")

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    print(f"\n  已清理临时量化模型：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
