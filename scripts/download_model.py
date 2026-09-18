#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
下载 RAG 向量模型（Xenova/bge-small-zh-v1.5 的 ONNX INT8 版）。

────────────────────────────────────────────────────────────────
为什么需要这个脚本
────────────────────────────────────────────────────────────────
`models/` 被 `.gitignore` 排除，仓库里**不含**模型文件；而
`app/services/embedding.py` 在找不到模型时**不会报错**，只打一条 warning：

    logger.warning("向量模型加载失败，将降级为 BM25 单路检索: ...")
    return False

→ 结果是向量检索**静默失效**，接口照常返回 200，只有翻日志才知道。
   所以克隆仓库后必须跑一次本脚本。

────────────────────────────────────────────────────────────────
设计要点
────────────────────────────────────────────────────────────────
· **零第三方依赖**：只用标准库 urllib，不引入 huggingface_hub
  （这是一个引导脚本，必须在 `pip install -r requirements.txt` **之前**就能跑）
· 走 `HF_ENDPOINT` 镜像，默认 hf-mirror.com（huggingface.co 在国内实测不可达）
· 先下到 `*.part` 再原子改名 —— 中断不会留下「半个文件」骗过存在性检查
· 下完做体积校验 + JSON 可解析校验，不合格直接失败而不是留下坏文件
· 已存在且校验通过则跳过（`--force` 强制重下）

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    python scripts/download_model.py
    python scripts/download_model.py --force
    HF_ENDPOINT=https://huggingface.co python scripts/download_model.py
    python scripts/download_model.py --dest D:/some/where/bge-small-zh-v1.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------
REPO = "Xenova/bge-small-zh-v1.5"
DEFAULT_ENDPOINT = "https://hf-mirror.com"

# 目标目录默认值。**必须与 app/config.py 的 EMBED_MODEL_DIR 保持一致**
# （config.py:  EMBED_MODEL_DIR = MODEL_DIR / "bge-small-zh-v1.5"，
#             MODEL_DIR      = BASE_DIR / "models"）
_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = _ROOT / "models" / "bge-small-zh-v1.5"

# (仓库内路径, 实测字节数, 体积下限)
# 实测字节数来自 2026-09 的一次真实下载，仅用于打印对比；校验只用下限，
# 避免上游换了 revision 就误报失败。
FILES: list[tuple[str, int, int]] = [
    ("config.json", 716, 100),
    ("tokenizer.json", 439_125, 100_000),
    ("tokenizer_config.json", 367, 100),
    ("onnx/model_int8.onnx", 23_903_394, 10_000_000),
]

# 这几个文件是 JSON，下载后要能解析；其余（.onnx）只校验体积
JSON_FILES = {"config.json", "tokenizer_config.json", "tokenizer.json"}


# ---------------------------------------------------------------
# 工具
# ---------------------------------------------------------------
def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _validate(path: Path, min_size: int) -> tuple[bool, str]:
    """校验单个文件。返回 (是否通过, 说明)。"""
    if not path.exists():
        return False, "文件不存在"
    size = path.stat().st_size
    if size < min_size:
        return False, f"体积过小（{_human(size)} < 下限 {_human(min_size)}），可能下载不完整"
    if path.name in JSON_FILES:
        try:
            with path.open("r", encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            return False, f"JSON 解析失败: {e}"
    return True, _human(size)


def _download(url: str, dest: Path, label: str) -> None:
    """下载 url 到 dest。先写 *.part，成功后原子改名。"""
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": "doc-ocr-system/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            with part.open("wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct != last_pct:
                            last_pct = pct
                            sys.stdout.write(
                                f"\r    {label}: {pct:3d}%  "
                                f"{_human(done)} / {_human(total)}   "
                            )
                            sys.stdout.flush()
            sys.stdout.write(
                f"\r    {label}: 100%  {_human(done)}" + " " * 20 + "\n"
            )
            sys.stdout.flush()
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        raise SystemExit(
            f"\n下载失败：HTTP {e.code} {e.reason}\n"
            f"  URL: {url}\n"
            f"  若为 404，说明该 revision 下没有这个文件；若为 403，尝试换镜像：\n"
            f"    HF_ENDPOINT=https://huggingface.co python {Path(sys.argv[0]).name}"
        )
    except Exception as e:
        part.unlink(missing_ok=True)
        raise SystemExit(
            f"\n下载失败：{type(e).__name__}: {e}\n"
            f"  URL: {url}\n"
            f"  排查：① 网络/代理是否可达 {url.split('/resolve/')[0]}\n"
            f"        ② 换镜像 HF_ENDPOINT=https://huggingface.co 再试"
        )

    os.replace(part, dest)


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载 RAG 向量模型（bge-small-zh-v1.5 ONNX INT8）"
    )
    parser.add_argument(
        "--dest", type=Path, default=DEFAULT_DEST,
        help=f"目标目录（默认 {DEFAULT_DEST}）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="已存在且校验通过时也强制重新下载",
    )
    args = parser.parse_args()

    endpoint = (os.getenv("HF_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    dest: Path = args.dest
    base_url = f"{endpoint}/{REPO}/resolve/main"

    print("=" * 62)
    print("下载 RAG 向量模型")
    print(f"  仓库    : {REPO}")
    print(f"  镜像    : {endpoint}")
    print(f"  目标目录: {dest}")
    print("=" * 62)

    # 第一步：先看哪些已经有了
    todo: list[tuple[str, int, int]] = []
    for rel, actual, min_size in FILES:
        ok, note = _validate(dest / rel, min_size)
        mark = "已存在" if ok else "需下载"
        extra = f"（实测 {_human(actual)}）" if ok else f"（{note}）"
        print(f"  [{mark}] {rel:26s} {extra}")
        if not ok or args.force:
            todo.append((rel, actual, min_size))

    if not todo:
        print("\n全部文件已就绪，无需下载。用 --force 可强制重下。")
        return 0

    print(f"\n开始下载 {len(todo)} 个文件……")
    for rel, actual, min_size in todo:
        url = f"{base_url}/{rel}"
        print(f"\n  → {rel}")
        print(f"    {url}")
        _download(url, dest / rel, rel)

    # 第二步：下载完统一复核一遍
    print("\n" + "-" * 62)
    print("校验：")
    failed: list[str] = []
    for rel, actual, min_size in FILES:
        ok, note = _validate(dest / rel, min_size)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {rel:26s} {note}")
        if not ok:
            failed.append(rel)

    if failed:
        print(f"\n以下文件未通过校验，请重试或加 --force：{failed}", file=sys.stderr)
        return 1

    print("\n完成。向量模型已就绪，RAG 检索将启用「向量 + BM25」双路。")
    print("提示：可用 python scripts/eval_rag.py 验证检索链路。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
