# -*- coding: utf-8 -*-
"""
并发正确性体检 —— 验证加锁之后，并发请求不会拿到错乱结果

用法：
  # 后端已在 127.0.0.1:8000 运行
  ./.venv/Scripts/python.exe scripts/check_concurrency.py

背景：
  路由从 async def 改为 def 后，请求由 FastAPI 线程池**并发**执行，
  模块级共享状态（BM25 索引 / 向量缓存 / 模型单例）不再天然串行。
  为此加了锁（见各 service 模块的并发控制注释）。
  本脚本用来证明这些锁确实起作用，而不只是"看起来加了"。

测三件事：
  1. 并发检索：20 个线程同时打 /api/rag/search，结果必须**逐条一致**
     （同一查询在同一索引状态下应当返回同样的命中列表）
  2. 重建与检索交错：一边持续检索，一边触发 /api/rag/index 重建，
     期间不得出现 5xx，也不得出现"空结果"这种半成品索引的特征
  3. 并发问答：3 个线程同时打 /api/rag/ask，不得降级、不得报错

退出码：0 表示全部通过。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = "[PASS]", "[FAIL]"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {PASS if cond else FAIL} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with OPENER.open(req, timeout=180) as r:
        body = json.loads(r.read().decode("utf-8"))
    return r.status, body, time.perf_counter() - t0


def search_once(query, mode="hybrid"):
    status, body, _ = post("/api/rag/search", {"query": query, "mode": mode})
    hits = (body.get("data") or {}).get("hits") or []
    return status, [h.get("chunk_id") for h in hits]


def main():
    QUERY = "买服务器花了多少钱"

    # 预热：触发懒加载（OCR/向量模型/BM25 首次构建），避免把初始化耗时算进来
    search_once(QUERY)

    print("=" * 72)
    print("1. 并发检索一致性（20 线程同一查询）")
    print("=" * 72)
    with ThreadPoolExecutor(max_workers=20) as ex:
        outs = list(ex.map(lambda _: search_once(QUERY), range(20)))
    statuses = {s for s, _ in outs}
    id_lists = {tuple(ids) for _, ids in outs}
    check("全部返回 200", statuses == {200}, f"实际状态码集合={statuses}")
    check("20 次命中列表完全一致", len(id_lists) == 1,
          f"出现 {len(id_lists)} 种不同结果")
    check("命中非空（不是半成品索引）",
          all(len(ids) > 0 for _, ids in outs),
          f"样本={sorted(id_lists)[0][:5] if id_lists else 'N/A'}")

    print()
    print("=" * 72)
    print("2. 重建与检索交错（一边检索一边 /index 重建）")
    print("=" * 72)
    stop = threading.Event()
    errors = []
    empty = []
    counts = {"n": 0}

    def hammer():
        while not stop.is_set():
            try:
                status, ids = search_once(QUERY)
                counts["n"] += 1
                if status != 200:
                    errors.append(status)
                if not ids:
                    empty.append(1)
            except Exception as e:                      # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()

    time.sleep(0.3)
    try:
        status, body, cost = post("/api/rag/index", {})
        idx_ok = status == 200 and (body.get("code") == 200)
    except Exception as e:                              # noqa: BLE001
        idx_ok = False
        cost = 0.0
        print(f"      重建请求异常: {e}")

    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    check("重建请求成功", idx_ok, f"耗时 {cost:.2f}s")
    check("重建期间检索无 5xx / 无异常", not errors,
          f"异常 {len(errors)} 次：{errors[:3]}")
    check("重建期间无空结果（说明没有读到半成品索引）", not empty,
          f"空结果 {len(empty)} 次，共 {counts['n']} 次检索")

    print()
    print("=" * 72)
    print("3. 并发问答（3 线程同时 /ask）")
    print("=" * 72)
    with ThreadPoolExecutor(max_workers=3) as ex:
        asks = list(ex.map(
            lambda q: post("/api/rag/ask", {"question": q}),
            ["发票号码是多少？", "价税合计是多少？", "订单金额是多少？"],
        ))
    check("全部返回 200", all(s == 200 for s, _, _ in asks),
          f"状态码={[s for s, _, _ in asks]}")
    degraded = [bool((b.get("data") or {}).get("degraded")) for _, b, _ in asks]
    check("均未降级", not any(degraded), f"degraded={degraded}")
    answers = [(b.get("data") or {}).get("answer_text") or "" for _, b, _ in asks]
    check("均给出回答", all(a.strip() for a in answers),
          " | ".join(a[:24] for a in answers))

    print()
    print("=" * 72)
    passed = sum(1 for _, ok in results if ok)
    failed = len(results) - passed
    print(f"并发体检结果：{passed}/{len(results)} 通过"
          + (f"，{failed} 项失败" if failed else ""))
    print("=" * 72)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
