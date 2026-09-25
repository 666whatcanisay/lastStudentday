# -*- coding: utf-8 -*-
"""fusion.weighted_fuse 的定值回归测试（方案 B：修复 _minmax 运算符优先级 bug）

bug 回顾：`(h.get("score") or 0.0 - lo) / rng` 实际解析为
`(score or (0.0 - lo)) / rng` —— score 非零时 lo 未被减去，归一化结果整体错误。
本测试构造已知分数的定值断言，防止回归。不触碰 RRF 默认调参。
"""
from app.services.fusion import weighted_fuse


def _hit(cid, score):
    return {"chunk_id": cid, "doc_id": cid, "chunk_text": "t", "score": score}


def test_minmax_normalization_values():
    # vec 分数 [0.2, 0.6, 1.0] → 归一化应为 [0.0, 0.5, 1.0]
    # bug 版本会得到 [0.25, 0.5, 1.0]（lo 未被减去，(score)/0.8）
    vec = [_hit(1, 0.2), _hit(2, 0.6), _hit(3, 1.0)]
    bm25 = [_hit(1, 10.0), _hit(4, 20.0)]  # bm25 归一化 [0.0, 1.0]

    out = {h["chunk_id"]: h["score"] for h in weighted_fuse(vec, bm25, alpha=1.0, beta=0.0)}
    # alpha=1,beta=0：融合分 == vec 归一化分
    assert abs(out[1] - 0.0) < 1e-6
    assert abs(out[2] - 0.5) < 1e-6
    assert abs(out[3] - 1.0) < 1e-6


def test_minmax_constant_scores():
    # 所有分数相同 → 该路归一化恒为 1.0（既有约定，见 weighted_fuse 注释）
    vec = [_hit(1, 0.5), _hit(2, 0.5)]
    out = {h["chunk_id"]: h["score"] for h in weighted_fuse(vec, [], alpha=1.0, beta=1.0)}
    assert abs(out[1] - 1.0) < 1e-6
    assert abs(out[2] - 1.0) < 1e-6


def test_minmax_negative_lo_subtraction():
    # 含负分场景：lo 为负，必须正确相减
    # scores [-1.0, 1.0] → lo=-1, rng=2 → 归一化 [0.0, 1.0]
    vec = [_hit(1, -1.0), _hit(2, 1.0)]
    out = {h["chunk_id"]: h["score"] for h in weighted_fuse(vec, [], alpha=1.0, beta=0.0)}
    assert abs(out[1] - 0.0) < 1e-6
    assert abs(out[2] - 1.0) < 1e-6


def test_weighted_order_prefers_higher_norm():
    vec = [_hit(1, 0.9), _hit(2, 0.1)]
    bm25 = [_hit(2, 100.0), _hit(1, 1.0)]
    out = weighted_fuse(vec, bm25, alpha=1.0, beta=1.0)
    # 两条各自在一路最强：两路归一化均 1.0 → 融合分相同，但都必须大于 0
    scores = {h["chunk_id"]: h["score"] for h in out}
    assert all(s > 0 for s in scores.values())
