"""
查询改写模块（阶段三）

为什么需要这一层
----------------
真实用户提问是**口语化 + 含相对时间**的，而语料是票据/订单/合同这类
结构化文书，用词是**术语化 + 绝对时间**的。两侧存在三重落差：

  1. 时间落差：用户说「上个月」，语料里写的是「2026-07-24」
  2. 用词落差：用户说「打车」，语料里写的是「*运输服务*客运服务费」
  3. 噪声落差：用户输入可能带全角字符、多余空格（复用索引侧同一套归一化）

不做这一层，两条检索路都会系统性漏召回。

最重要的设计取舍：改写结果**分路投喂**，而不是把扩写后的长串同时喂给两条路
--------------------------------------------------------------------------------
  · 向量路（vec_query）→ 归一化原句（必要时补上解析出的绝对时间）
      理由：bge 语义模型本身就能跨过「打车 ↔ 客运服务费」的用词差异
      （本机实测相似度 0.5507），再拼一长串同义词只会稀释语义中心，
      把原本精准的向量拉向"平均化"。
  · 关键词路（bm25_query）→ 归一化原句 + 绝对时间 + 同义词扩展
      理由：BM25 是**字面匹配**，用户说「打车」时语料里的「客运服务费」
      一个字都对不上，必须靠扩展词把这道缺口桥接起来。

一句话：**语义路靠模型自己跨，关键词路靠改写来跨。**
实测依据见 RAG_WORKFLOW.md 的「查询改写分路实证」小节。
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from app.config import QUERY_REWRITE_ENABLE, QUERY_SYNONYM_MAP
from app.services.text_normalize import normalize_line

logger = logging.getLogger(__name__)


# ==================== 时间区间计算工具 ====================

def _month_range(year: int, month: int):
    """返回某年某月的 (首日, 末日)"""
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _quarter_range(year: int, quarter: int):
    """返回某年某季度的 (首日, 末日)"""
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def _shift_month(year: int, month: int, delta: int):
    """把 (年,月) 整体平移 delta 个月，delta 可为负"""
    total = year * 12 + (month - 1) + delta
    y, m = divmod(total, 12)
    return y, m + 1


def _cn_date(d: date, with_day: bool = True) -> str:
    """ISO 日期 → 语料里的中文日期形态（语料写的是「2025年12月19日」，不是 2025-12-19）"""
    return f"{d.year}年{d.month}月{d.day}日" if with_day else f"{d.year}年{d.month}月"


def _scope(label: str, start: date, end: date, kind: str, raw: str,
           bm25_text: Optional[str] = None, vec_hint: Optional[str] = None) -> Dict:
    """
    构造时间范围描述。

    三个字段各有明确用途，不能混用：
      label     — 人类可读，进 LLM Prompt（模型看得懂 ISO，但中文日期更贴合语料语境）
      bm25_text — 中文日期形态，进 BM25 关键词路。
                  ⚠️ 这里必须是中文形态：语料里写的是「2025年12月19日」，
                  若喂 ISO 的「2025-12-19」，bigram 分词后与语料完全对不上，
                  等于没加。这是实测踩过的坑。
      vec_hint  — 进向量路的短提示。**跨度为 None 表示不追加**：
                  「最近7天的订单」本身已是自然语句，再拼一段日期区间
                  只会稀释语义中心（见本模块开头「分路投喂」说明）。
    """
    return {
        "label": label,
        "bm25_text": bm25_text or label,
        "vec_hint": vec_hint,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "kind": kind,
        "raw": raw,
    }


# ==================== 相对时间 → 绝对区间 ====================

def _parse_time_scope(query: str, now: datetime) -> Optional[Dict]:
    """
    从查询中解析时间范围，返回绝对区间。

    解析优先级：先看"最近N天/N个月"这类带数量的，再看绝对日期（2025年12月），
    最后才匹配"上个月"这类纯相对词 —— 因为带数量的表达更具体，不易被误判。
    """
    today = now.date()

    # ---------- 最近N天 / 近N天 / 过去N天 / 前N天 ----------
    m = re.search(r"(?:最近|近|过去|前)\s*(\d{1,3})\s*[天日]", query)
    if m:
        n = int(m.group(1))
        if 0 < n <= 3650:
            start = today - timedelta(days=n - 1)
            return _scope(
                f"最近{n}天", start, today, "day_span", m.group(0),
                bm25_text=f"{_cn_date(start)}至{_cn_date(today)}",
                vec_hint=None,   # 跨度类不追加，避免稀释语义
            )

    # ---------- 最近N个月 / 近N个月 ----------
    m = re.search(r"(?:最近|近|过去)\s*(\d{1,2})\s*个月", query)
    if m:
        n = int(m.group(1))
        if 0 < n <= 120:
            sy, sm = _shift_month(today.year, today.month, -(n - 1))
            start, _ = _month_range(sy, sm)
            _, end = _month_range(today.year, today.month)
            return _scope(
                f"最近{n}个月", start, end, "month_span", m.group(0),
                bm25_text=f"{sy}年{sm}月至{today.year}年{today.month}月",
                vec_hint=None,
            )

    # ---------- 相对年份 + 月日：去年12月19日 / 今年3月 ----------
    # ⚠️ 必须排在「纯相对词」（去年/今年）之前，否则"去年12月19日"会被
    #    粗粒度的"去年"抢先匹配成整年，把一天的范围放大成 365 天。
    #    这是自测中实际暴露的缺陷。
    relative_years = {"前年": -2, "去年": -1, "上一年": -1, "今年": 0, "本年": 0, "明年": 1}
    m = re.search(r"(前年|去年|上一年|今年|本年|明年)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", query)
    if m:
        y = today.year + relative_years[m.group(1)]
        try:
            d = date(y, int(m.group(2)), int(m.group(3)))
            return _scope(_cn_date(d), d, d, "day", m.group(0),
                          bm25_text=_cn_date(d), vec_hint=_cn_date(d))
        except ValueError:
            pass

    m = re.search(r"(前年|去年|上一年|今年|本年|明年)\s*(\d{1,2})\s*月", query)
    if m:
        y, mo = today.year + relative_years[m.group(1)], int(m.group(2))
        if 1 <= mo <= 12:
            start, end = _month_range(y, mo)
            return _scope(f"{y}年{mo}月", start, end, "month", m.group(0),
                          bm25_text=f"{y}年{mo}月", vec_hint=f"{y}年{mo}月")

    # ---------- 绝对日期：2025年12月19日 / 2025-12-19 / 2025/12/19 ----------
    m = re.search(r"(20\d{2})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*[日号]?", query)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return _scope(_cn_date(d), d, d, "day", m.group(0),
                          bm25_text=_cn_date(d), vec_hint=_cn_date(d))
        except ValueError:
            pass

    # ---------- 绝对月份：2025年12月 / 2025-12 ----------
    m = re.search(r"(20\d{2})\s*[年\-/]\s*(\d{1,2})\s*月?", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            start, end = _month_range(y, mo)
            return _scope(f"{y}年{mo}月", start, end, "month", m.group(0),
                          bm25_text=f"{y}年{mo}月", vec_hint=f"{y}年{mo}月")

    # ---------- 绝对年份：2025年 ----------
    m = re.search(r"(20\d{2})\s*年", query)
    if m:
        y = int(m.group(1))
        return _scope(f"{y}年", date(y, 1, 1), date(y, 12, 31), "year", m.group(0),
                      bm25_text=f"{y}年", vec_hint=f"{y}年")

    # ---------- 纯相对词（长词优先，避免"上个月"被"个月"抢先匹配） ----------
    # 顺序敏感：多字词必须排在它的子串前面
    relative_rules = [
        ("上个月", lambda t: _shift_month(t.year, t.month, -1), "month"),
        ("上月", lambda t: _shift_month(t.year, t.month, -1), "month"),
        ("上一个月", lambda t: _shift_month(t.year, t.month, -1), "month"),
        ("本月", lambda t: (t.year, t.month), "month"),
        ("这个月", lambda t: (t.year, t.month), "month"),
        ("当月", lambda t: (t.year, t.month), "month"),
        ("上个季度", None, "quarter_prev"),
        ("上季度", None, "quarter_prev"),
        ("本季度", None, "quarter_cur"),
        ("这个季度", None, "quarter_cur"),
        ("去年", None, "year_prev"),
        ("上一年", None, "year_prev"),
        ("今年", None, "year_cur"),
        ("本年", None, "year_cur"),
        ("本年度", None, "year_cur"),
        ("上周", None, "week_prev"),
        ("上星期", None, "week_prev"),
        ("上个星期", None, "week_prev"),
        ("本周", None, "week_cur"),
        ("这周", None, "week_cur"),
        ("这个星期", None, "week_cur"),
        ("今天", None, "today"),
        ("今日", None, "today"),
        ("本日", None, "today"),
        ("昨天", None, "yesterday"),
        ("昨日", None, "yesterday"),
        ("前天", None, "day_before_yesterday"),
    ]

    for word, month_fn, kind in relative_rules:
        if word not in query:
            continue

        if kind == "month":
            y, mo = month_fn(today)
            start, end = _month_range(y, mo)
            text = f"{y}年{mo}月"
            return _scope(text, start, end, "month", word, bm25_text=text, vec_hint=text)

        if kind in ("quarter_cur", "quarter_prev"):
            q = (today.month - 1) // 3 + 1
            if kind == "quarter_prev":
                if q == 1:
                    y, q = today.year - 1, 4
                else:
                    y = today.year
                    q -= 1
            else:
                y = today.year
            start, end = _quarter_range(y, q)
            text = f"{y}年第{q}季度"
            return _scope(text, start, end, "quarter", word, bm25_text=text, vec_hint=text)

        if kind == "year_cur":
            y = today.year
            return _scope(f"{y}年", date(y, 1, 1), date(y, 12, 31), "year", word,
                          bm25_text=f"{y}年", vec_hint=f"{y}年")

        if kind == "year_prev":
            y = today.year - 1
            return _scope(f"{y}年", date(y, 1, 1), date(y, 12, 31), "year", word,
                          bm25_text=f"{y}年", vec_hint=f"{y}年")

        if kind in ("week_cur", "week_prev"):
            monday = today - timedelta(days=today.weekday())
            if kind == "week_prev":
                monday -= timedelta(days=7)
            sunday = monday + timedelta(days=6)
            return _scope(
                f"{_cn_date(monday)} 至 {_cn_date(sunday)}", monday, sunday, "week", word,
                bm25_text=f"{_cn_date(monday)}至{_cn_date(sunday)}",
                vec_hint=None,   # 跨度类不追加
            )

        if kind == "today":
            return _scope(_cn_date(today), today, today, "day", word,
                          bm25_text=_cn_date(today), vec_hint=_cn_date(today))

        if kind == "yesterday":
            d = today - timedelta(days=1)
            return _scope(_cn_date(d), d, d, "day", word,
                          bm25_text=_cn_date(d), vec_hint=_cn_date(d))

        if kind == "day_before_yesterday":
            d = today - timedelta(days=2)
            return _scope(_cn_date(d), d, d, "day", word,
                          bm25_text=_cn_date(d), vec_hint=_cn_date(d))

    return None


# ==================== 口语词 → 语料术语扩展 ====================

def _expand_terms(query: str) -> List[str]:
    """
    按 QUERY_SYNONYM_MAP 扩展口语词。

    只做「命中即追加」，不删除原词 —— BM25 会同时匹配原词与扩展词，
    命中任一即得分，因此扩展是单向增益，不会削弱原查询。
    """
    terms: List[str] = []
    for colloquial, synonyms in QUERY_SYNONYM_MAP.items():
        if colloquial in query:
            for s in synonyms:
                if s not in terms:
                    terms.append(s)
    return terms


# ==================== 对外主函数 ====================

def rewrite(query: str, now: Optional[datetime] = None,
            enable: Optional[bool] = None,
            use_llm: bool = False,
            llm_client=None) -> Dict:
    """
    查询改写主入口。

    参数:
      query     — 用户原始提问
      now       — 参照时间（默认取当前时间；显式传入便于单元测试）
      enable    — 是否启用改写，默认取配置 QUERY_REWRITE_ENABLE
      use_llm   — 是否额外调用 LLM 做改写（失败会自动回落到规则表）
      llm_client— 已构造的 LLMClient（复用连接）

    返回:
      {
        "original":   "上个月打车花了多少钱",
        "normalized": "上个月打车花了多少钱",
        "time_scope": {...} | None,
        "expansions": ["客运服务费", "运输服务", "旅客运输"],
        "vec_query":  "上个月打车花了多少钱（2026年8月）",
        "bm25_query": "上个月打车花了多少钱 2026年8月 客运服务费 运输服务 旅客运输",
        "method":     "rule" | "none" | "llm",
        "llm_rewrite": "..." | None,
      }
    """
    now = now or datetime.now()
    enable = QUERY_REWRITE_ENABLE if enable is None else enable

    result = {
        "original": query,
        "normalized": query,
        "time_scope": None,
        "expansions": [],
        "vec_query": query,
        "bm25_query": query,
        "method": "none",
        "llm_rewrite": None,
    }

    if not query or not query.strip():
        return result

    if not enable:
        result["normalized"] = normalize_line(query)
        result["vec_query"] = result["normalized"]
        result["bm25_query"] = result["normalized"]
        return result

    # ---------- 1. 归一化（与索引侧同一套规则，保证两侧用词一致） ----------
    # 注意：用 normalize_line 而非 normalize —— 查询是单行文本，
    # 而 normalize 的错字纠正表里有"圆→元"这类按行处理的规则，两者结果一致，
    # 但 normalize_line 更贴合"单行查询"的语义。
    normalized = normalize_line(query)

    # ---------- 2. 时间解析 ----------
    time_scope = _parse_time_scope(normalized, now)

    # ---------- 3. 同义扩展 ----------
    expansions = _expand_terms(normalized)

    # ---------- 4. 分路组装 ----------
    # 向量路：原句 + 绝对时间短提示（以自然短语形式追加，不拼关键词串）
    # 两个跳过条件缺一不可：
    #   · vec_hint 为 None（跨度类查询）→ 不追加，避免稀释语义中心
    #   · 原句已含该提示（"2025年12月的发票…"）→ 不重复追加，避免同一信息被强调两次
    vec_query = normalized
    if time_scope and time_scope.get("vec_hint"):
        hint = time_scope["vec_hint"]
        if hint not in normalized:
            vec_query = f"{normalized}（{hint}）"

    # 关键词路：原句 + 中文日期 + 同义词（BM25 靠字面匹配，扩得越全越好）
    bm25_parts = [normalized]
    if time_scope and time_scope.get("bm25_text"):
        text = time_scope["bm25_text"]
        if text not in normalized:
            bm25_parts.append(text)
    bm25_parts.extend(expansions)
    bm25_query = " ".join(p for p in bm25_parts if p)

    result.update({
        "normalized": normalized,
        "time_scope": time_scope,
        "expansions": expansions,
        "vec_query": vec_query,
        "bm25_query": bm25_query,
        "method": "rule",
    })

    # ---------- 5. 可选的 LLM 改写（仅作补充，失败不影响主流程） ----------
    if use_llm and llm_client is not None and llm_client.is_configured:
        try:
            llm_q = llm_client.rewrite_query(
                normalized,
                now_text=now.strftime("%Y-%m-%d"),
                time_label=time_scope["label"] if time_scope else "",
            )
            if llm_q:
                result["llm_rewrite"] = llm_q
                # LLM 改写结果并入关键词路（作为额外召回面）
                result["bm25_query"] = f"{bm25_query} {llm_q}"
                result["method"] = "llm"
        except Exception as e:  # 兜底：LLM 改写永不阻断检索
            logger.warning(f"LLM 查询改写失败，已回落规则表: {e}")

    return result
