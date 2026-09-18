"""
语义聚合式分块模块

背景（来自真实 OCR 结果实测）：
  OCR 输出的文本是**逐行**的，没有段落结构；票据类文档更是「标签一行、值一行」，
  且表格布局会导致**阅读顺序错乱**。真实样本：

      发票号码：
      25447000001712590935          ← 标签与值分离（本可接受）
      开票日期：
      国家税奔总局                   ← 中间插入了无关内容
      2025年12月19日                ← 真正的值被隔开
      价税合计（大写）
      贰拾伍圆玖角玖分               ← 标签与值分离

  若按换行简单切分，会产生大量 5~15 字的碎片，且「标签」与「值」被切进不同分块，
  导致**检索到"金额"标签却拿不到金额数字**。

本模块的解法：
  1. 标签-值**类型感知配对**：识别标签行后，在其后若干行内寻找"形态匹配"的值行
     （日期类标签找日期形态、编号类标签找字母数字串、金额类标签找数字或中文大写金额）
  2. 配对后**重排**：把值行紧跟在标签行之后，修复阅读顺序错乱带来的相邻性破坏
  3. 逻辑单元**累积成块** + 滑窗重叠，避免答案被切断在块边界
  4. 保留**原文**（chunk_text_raw）用于溯源展示，与检索用的归一化文本分离

设计约束：
  - 归一化是行级的且不改变行数，因此原文行与归一化行可按行号对齐
  - 所有偏移量（char_start/char_end）基于**原文**计算，用于前端高亮定位
"""
import re
import logging
from typing import List, Dict, Optional

from app.config import CHUNK_SIZE, CHUNK_OVERLAP, CHUNK_MIN_LEN, CHUNK_PAIR_GAP
from app.services.text_normalize import normalize_line

logger = logging.getLogger(__name__)


# ==================== 正则与规则 ====================

# 页码标记：OCR 对 PDF 逐页识别后写入 "=== 第 N 页 ==="
_RE_PAGE_MARK = re.compile(r"^=+\s*第\s*(\d+)\s*页\s*=+$")

# 冒号标签行：整行形如 "标签："（全角或半角冒号）
_RE_COLON_LABEL = re.compile(r"^(.{1,20})[：:]\s*$")

# 无冒号标签行的关键词（如 "价税合计（大写）"、"单价"）
_LABEL_KEYWORDS = (
    "日期", "时间", "号码", "编号", "单号", "识别号", "证件号", "代码",
    "金额", "合计", "总计", "价税", "税额", "单价", "小计", "名称",
    "数量", "税率", "开票人", "出行人", "出发地", "到达地", "备注",
)

# 标签类型 → 期望的值形态。用于"类型感知配对"：
# 日期类标签向下找日期，编号类向下找字母数字串，金额类向下找数字或中文大写金额。
_VALUE_RULES = [
    (re.compile(r"日期|时间|开票日|出行日"),
     re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}")),
    (re.compile(r"号码|编号|单号|识别号|证件号|代码"),
     re.compile(r"[A-Za-z0-9\-]{6,}")),
    (re.compile(r"金额|合计|总计|价税|税额|单价|小计|实付|应付"),
     re.compile(r"[¥￥$]?\s*\d[\d,]*\.?\d*|[壹贰叁肆伍陆柒捌玖拾佰仟万亿零]{2,}")),
]


def _detect_page_no(line: str) -> Optional[int]:
    """识别页码标记行，返回页码；非页码行返回 None"""
    m = _RE_PAGE_MARK.match(line.strip())
    return int(m.group(1)) if m else None


def _matches_any_value_pattern(line: str) -> bool:
    """判断该行是否本身就像一个"值"（用于避免把值行误判为标签）"""
    return any(vp.search(line) for _, vp in _VALUE_RULES)


def _detect_label(line: str) -> Optional[str]:
    """
    判断该行是否为「需要向下配对的值缺失标签行」，是则返回标签文本（不含冒号）。

    三种情况：
      1. 以冒号结尾且冒号后无内容   → 需要配对     如 "发票号码："、"开票日期："
      2. 冒号后已有内容             → 不需要配对   如 "名称：广东工商职业技术大学"
         （这类行本身已是完整的"标签：值"，若再向下配对会造成错误重排）
      3. 无冒号的短行且含标签关键词 → 需要配对     如 "价税合计（大写）"
         —— 需排除本身即像"值"的行，避免误判
    """
    if not line:
        return None

    # 情况 1：冒号结尾的空标签
    m = _RE_COLON_LABEL.match(line)
    if m:
        return m.group(1).strip()

    # 情况 2：冒号后已有内容 → 已是完整的"标签：值"，作为独立单元
    if "：" in line or ":" in line:
        return None

    # 情况 3：无冒号短行 + 标签关键词
    s = line.strip()
    if 0 < len(s) <= 16 and not _matches_any_value_pattern(s):
        if any(kw in s for kw in _LABEL_KEYWORDS):
            return s

    return None


def _expected_value_pattern(label: str):
    """根据标签文本推断期望的值形态；无对应规则返回 None"""
    for label_pat, value_pat in _VALUE_RULES:
        if label_pat.search(label):
            return value_pat
    return None


def _find_pair_index(norm_lines: List[str], label_idx: int, label: str) -> Optional[int]:
    """
    在标签行之后寻找配对的值行，返回其行号；找不到返回 None。

    配对策略：
      - 表头块保护：若紧随其后的行也是标签，说明进入连续表头区域
        （如 "单价 / 数量 / 金额 / 项目名称"），强行配对会造成错误重排，直接不配对
      - 标签有类型规则 → 只接受**形态匹配**的行（跳过干扰行）
        例："开票日期：" 会跳过 "国家税务总局"，命中 "2025年12月19日"
      - 标签无类型规则 → 接受紧随其后的第一个候选行（通用兜底）
    """
    expected = _expected_value_pattern(label)

    # 表头块保护：看紧随其后的第一个非空行是否也是标签
    for k in range(label_idx + 1, len(norm_lines)):
        if norm_lines[k]:
            if _detect_label(norm_lines[k]) is not None:
                return None
            break

    limit = min(label_idx + 1 + CHUNK_PAIR_GAP, len(norm_lines))

    for j in range(label_idx + 1, limit):
        cand = norm_lines[j]
        if not cand:
            continue
        if _detect_page_no(cand) is not None:
            continue
        if _detect_label(cand) is not None:
            continue

        if expected is not None:
            if expected.search(cand):
                return j
            # 形态不匹配 → 继续向下找，不急于配对
        else:
            return j

    return None


def _build_units(raw_lines: List[str], norm_lines: List[str], line_pages: List[Optional[int]]) -> List[Dict]:
    """
    把逐行文本聚合成「逻辑单元」。

    逻辑单元 = 单个独立行，或「标签行 + 配对的値行 + 中间被跳过的行」。
    对配对单元，归一化文本会把值行**重排到标签行之后**，修复阅读顺序错乱。
    """
    units: List[Dict] = []
    i = 0
    n = len(raw_lines)

    while i < n:
        norm = norm_lines[i]

        # 跳过空行与页码标记行（页码标记是噪声，不参与检索）
        if not norm or _detect_page_no(norm) is not None:
            i += 1
            continue

        label = _detect_label(norm)
        # 无冒号的裸表头（如 "单价"、"税额"、"数量"）不参与配对：
        # 这类 2~3 字的表头在票据里常成片出现，强行与下方数字配对会造成错误重排。
        # 只有「冒号结尾」或「≥4 字的复合标签」（如 "价税合计（大写）"）才值得配对。
        pairable = norm.endswith(("：", ":")) or len(label) >= 4 if label else False

        if label is not None and pairable:
            pair_idx = _find_pair_index(norm_lines, i, label)
            if pair_idx is not None:
                # 归一化文本：标签 + 值 重排到最前，其余行依次跟在后面
                head = norm
                if norm.endswith(("：", ":")):
                    head = norm + norm_lines[pair_idx]          # "发票号码：" + "2544..."
                else:
                    head = norm + "\n" + norm_lines[pair_idx]    # "价税合计（大写）" 换行接值

                others = [
                    norm_lines[k]
                    for k in range(i + 1, pair_idx + 1)
                    if k != pair_idx and norm_lines[k]
                ]
                unit_norm = "\n".join([head] + others)

                units.append({
                    "line_start": i,
                    "line_end": pair_idx,
                    "norm": unit_norm,
                    "is_pair": True,
                })
                i = pair_idx + 1
                continue

        # 普通单行单元
        units.append({
            "line_start": i,
            "line_end": i,
            "norm": norm,
            "is_pair": False,
        })
        i += 1

    return units


def _make_chunk(units: List[Dict], chunk_index: int, raw_lines: List[str],
                line_offsets: List[int], line_pages: List[Optional[int]]) -> Dict:
    """把一组逻辑单元组装成一条分块记录"""
    line_start = units[0]["line_start"]
    line_end = units[-1]["line_end"]

    # 原文：保持 OCR 原始行序，用于溯源展示（忠实于原图）
    raw_text = "\n".join(raw_lines[line_start:line_end + 1])
    # 归一化文本：逻辑单元顺序拼接，用于向量化与 BM25 索引
    norm_text = "\n".join(u["norm"] for u in units if u["norm"])

    page_no = next(
        (line_pages[k] for k in range(line_start, line_end + 1) if line_pages[k] is not None),
        None,
    )

    return {
        "chunk_index": chunk_index,
        "chunk_text": norm_text,
        "chunk_text_raw": raw_text,
        "char_start": line_offsets[line_start],
        "char_end": line_offsets[line_end] + len(raw_lines[line_end]),
        "page_no": page_no,
        "line_start": line_start,
        "line_end": line_end,
    }


def _accumulate(units: List[Dict], raw_lines: List[str],
                line_offsets: List[int], line_pages: List[Optional[int]]) -> List[Dict]:
    """把逻辑单元累积成块，并在相邻块之间保留滑窗重叠"""
    if not units:
        return []

    chunks: List[Dict] = []
    idx = 0
    n = len(units)
    chunk_index = 0

    while idx < n:
        cur: List[Dict] = []
        cur_len = 0
        j = idx

        # 累积逻辑单元直到接近 CHUNK_SIZE（至少包含一个单元，避免死循环）
        while j < n and (cur_len + len(units[j]["norm"]) <= CHUNK_SIZE or not cur):
            cur.append(units[j])
            cur_len += len(units[j]["norm"])
            j += 1

        chunks.append(_make_chunk(cur, chunk_index, raw_lines, line_offsets, line_pages))
        chunk_index += 1

        if j >= n:
            break

        # 滑窗重叠：回退末尾若干逻辑单元作为下一块的开头
        # 以「逻辑单元」为最小重叠粒度，保证标签-值不被重新拆开
        back, back_len, k = 0, 0, j - 1
        while k >= idx and back_len + len(units[k]["norm"]) <= CHUNK_OVERLAP:
            back_len += len(units[k]["norm"])
            back += 1
            k -= 1

        next_idx = j - back
        idx = next_idx if next_idx > idx else j   # 保证游标严格前进

    # 过短的尾块并入前一块，避免产生无意义的碎片
    if len(chunks) > 1 and len(chunks[-1]["chunk_text"]) < CHUNK_MIN_LEN:
        tail = chunks.pop()
        prev = chunks[-1]
        prev["chunk_text"] = prev["chunk_text"] + "\n" + tail["chunk_text"]
        prev["chunk_text_raw"] = prev["chunk_text_raw"] + "\n" + tail["chunk_text_raw"]
        prev["char_end"] = tail["char_end"]
        prev["line_end"] = tail["line_end"]

    return chunks


def chunk_text(raw_text: str) -> List[Dict]:
    """
    对 OCR 原始文本执行语义聚合式分块。

    参数: raw_text — OCR 完整原始识别文本
    返回: 分块列表，每项包含
          chunk_index / chunk_text(归一化) / chunk_text_raw(原文)
          char_start / char_end / page_no

    行数对齐说明：归一化不改变行数，因此 raw_lines[k] 与 norm_lines[k] 是同一行。
    """
    if not raw_text or not raw_text.strip():
        return []

    raw_lines = raw_text.split("\n")
    norm_lines = [normalize_line(ln) for ln in raw_lines]

    # 每行在原文中的起始偏移（用于前端高亮定位）
    line_offsets: List[int] = []
    offset = 0
    for ln in raw_lines:
        line_offsets.append(offset)
        offset += len(ln) + 1   # +1 表示换行符

    # 建立「行号 → 页码」映射（页码标记之后的行归属该页）
    line_pages: List[Optional[int]] = []
    current_page: Optional[int] = None
    for ln in norm_lines:
        p = _detect_page_no(ln)
        if p is not None:
            current_page = p
        line_pages.append(current_page)

    units = _build_units(raw_lines, norm_lines, line_pages)
    chunks = _accumulate(units, raw_lines, line_offsets, line_pages)

    logger.info(
        f"分块完成：原文 {len(raw_lines)} 行 → {len(units)} 个逻辑单元 → {len(chunks)} 个分块"
    )
    return chunks


if __name__ == "__main__":
    # 自测：用真实 OCR 文本验证分块质量
    # 运行: .venv/Scripts/python.exe -m app.services.chunker
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sample = """=== 第 1 页 ===
电子发票区普要发票）
发票号码：
25447000001712590935
旅客运输服务
开票日期：
国家税奔总局
2025年12月19日
购买方信息
销售方信息
名称：广东工商职业技术大学
名称：广州优行科技有限公司
统一社会信用代码/纳税人识别号：524400007701704956
统一社会信用代码/纳税人识别号：91440101MA59LP3T32
单价
数量
金额
项目名称
税率/征收率
税额
*运输服务*客运服务费
31.300971
31. 30
3%
0. 94
*运输服务*客运服务费
-6. 07
3%
-0. 18
¥25. 23
¥0. 76
有效身份证件号
出行日期
出行人
出发地
到达地
等级
交通工具类型
价税合计（大写）
贰拾伍圆玖角玖分
（小写）¥25.99
开票人：钟寒冰"""

    print("=" * 70)
    print(f"原始文本 {len(sample)} 字符 / {len(sample.splitlines())} 行")
    print("=" * 70)
    print(sample)
    print()

    result = chunk_text(sample)
    print("=" * 70)
    print(f"分块结果：{len(result)} 块")
    print("=" * 70)

    for c in result:
        print()
        print(f"--- 块 #{c['chunk_index']}  页码={c['page_no']}  "
              f"原文偏移=[{c['char_start']}, {c['char_end']}]  行={c['line_start']}~{c['line_end']} ---")
        print("【归一化文本（检索用）】")
        print(c["chunk_text"])
        print("【原文（溯源展示用）】")
        print(c["chunk_text_raw"])
