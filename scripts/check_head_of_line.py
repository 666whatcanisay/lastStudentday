# -*- coding: utf-8 -*-
"""
队头阻塞体检脚本 —— 验证「一个慢请求会不会把整个服务卡住」

用法：
  # 后端已在 127.0.0.1:8000 运行
  ./.venv/Scripts/python.exe scripts/check_head_of_line.py

背景：
  本项目所有路由都声明为 `async def`，但实现全是**同步阻塞**调用
  （httpx.Client / ONNX 推理 / 纯 Python BM25 / 全量读上传文件），
  于是事件循环被占住 —— 一个请求在跑，其他请求全部排队。

  详见 ARCHITECTURE_REVIEW.md 的 P0-1。

思路（比「N 个并发比总耗时」更锋利）：
  1. 先测健康检查接口的基线延迟（本该是毫秒级）
  2. 在后台发一个耗时的 /api/rag/ask（含一次同步 LLM 调用，约 1s）
  3. 几乎同时再打一次 /api/health
  4. 若事件循环被阻塞，这次健康检查会被**拖到和慢请求一起结束**

判据：延迟放大 > 20 倍且绝对值 > 0.3s → 判定队头阻塞成立。

修复后（把 async def 改成 def，或 await run_in_threadpool(...)）
本脚本应显示放大倍数接近 1 —— 可直接当作回归验证用。
"""
import json
import sys
import threading
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, payload=None):
    if payload is None:
        req = urllib.request.Request(BASE + path, method="GET")
    else:
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    t0 = time.perf_counter()
    with OPENER.open(req, timeout=180) as r:
        r.read()
    return time.perf_counter() - t0


def main():
    # 基线
    call("/api/health")
    base = min(call("/api/health") for _ in range(5))
    print("健康检查基线延迟: %.4f s" % base)

    slow_elapsed = {}
    slow_started = threading.Event()

    def slow():
        slow_started.set()
        slow_elapsed["t"] = call("/api/rag/ask", {"question": "发票号码是多少？"})

    th = threading.Thread(target=slow)
    t_start = time.perf_counter()
    th.start()
    slow_started.wait()
    time.sleep(0.05)          # 确保慢请求已经进入服务端

    probe = call("/api/health")
    t_probe_done = time.perf_counter() - t_start

    th.join()
    print("慢请求(/api/rag/ask) 耗时: %.3f s" % slow_elapsed["t"])
    print("与慢请求并发时，健康检查延迟: %.4f s" % probe)
    print("健康检查完成时刻（相对慢请求开始）: %.3f s" % t_probe_done)
    print()

    factor = probe / max(base, 1e-6)
    print("延迟放大倍数: %.0fx" % factor)
    if factor > 20 and probe > 0.3:
        print("→ 判定：**队头阻塞成立**。一个慢请求把整个服务卡住了，")
        print("  期间所有其他请求（哪怕只是健康检查）全部排队。")
    else:
        print("→ 判定：未观察到明显队头阻塞。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
