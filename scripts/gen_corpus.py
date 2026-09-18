#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试语料生成器 —— 生成一批中文单据 PDF，供 OCR + RAG 检索评估使用。

────────────────────────────────────────────────────────────────
为什么需要它
────────────────────────────────────────────────────────────────
扩充前语料只有 5 篇（去重后 4 篇不同内容），top-5 接近全集，
Recall@5 完全没有区分度 → 任何参数调整都是在拟合噪声。
本脚本把语料扩到 20+ 篇，让检索指标重新具备统计意义。

────────────────────────────────────────────────────────────────
两条硬约束（改动前务必先读，否则会把评估集搞坏）
────────────────────────────────────────────────────────────────
1. **必须避开 4 个负样本的主题**（scripts/eval_set.json 里 expected_doc_ids=[]）：
     社保缴纳基数 / 绩效考核 / 量子芯片良率 / 天气
   一旦新语料里出现这些内容，负样本就变成假负样本，评估直接失效。

2. **必须避开与既有 22 条查询冲突的 token**，否则原本唯一的答案变得不唯一：
     · 金额：888 / 25.99 / 11300（一万一千三）/ 55000（五万五千）
     · 税率：**13%**（查询 17「税率 13% 的那张发票」期望唯一命中 doc1）
       → 所以本语料只做**服务类**发票，税率用 6% / 9% / 3%，
         采购订单与合同**不写税率字段**（现实中订单本来也不体现税率）
     · 日期：2024年01月15日 / 2025年12月19日 / 2026年07月24日
     · 专名：服务器 / 报错排查 / 打车 / 曹操出行 / 广州优行 / 钟寒冰

────────────────────────────────────────────────────────────────
字体说明
────────────────────────────────────────────────────────────────
MuPDF 内建的 `china-s` 字体把 ASCII 数字渲染成**全角**，
OCR 会读成「FP－２０２６6－０００１」这种串，编号就没法做关键词匹配了。
实测（本机，200 DPI + RapidOCR）：
    simhei.ttf  → 单据编号/日期/金额/税率 全部识别正确   ✅ 用它
    msyh.ttc    → 同样正确
    Deng.ttf    → 出错（开票百期 / 鑫额）                ❌
所以优先用系统中文 TTF；找不到则回退内建字体并给出警告。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    python scripts/gen_corpus.py                  # 生成到 test_corpus/
    python scripts/gen_corpus.py --out D:/tmp/corpus
    python scripts/gen_corpus.py --font C:/Windows/Fonts/msyh.ttc
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import fitz

# ---------------------------------------------------------------
# 字体
# ---------------------------------------------------------------
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

# ---------------------------------------------------------------
# 金额生成：排除会与既有评估集冲突的数值
# ---------------------------------------------------------------
FORBIDDEN_SUBSTR = ("888",)
FORBIDDEN_VALUES = {2599, 11300, 55000}


def _money(rng: random.Random, lo: int, hi: int) -> str:
    """生成一个「XX.XX」形式的金额字符串，避开与评估集冲突的数值。"""
    while True:
        cents = rng.randint(lo * 100, hi * 100)
        if cents in FORBIDDEN_VALUES or str(cents).find("888") >= 0:
            continue
        return f"{cents // 100}.{cents % 100:02d}"


def _num(rng: random.Random, lo: int, hi: int) -> int:
    while True:
        v = rng.randint(lo, hi)
        if "888" not in str(v):
            return v


# ---------------------------------------------------------------
# 语料规格
# 每篇：(文件名, 标题, 单据编号, 日期, 往来单位或'—', 税率或None,
#        明细行[(项目, 数量, 单价)], 备注)
# ---------------------------------------------------------------
BUYER = "杭州明远智能装备有限公司"

SPECS: list[dict] = [
    dict(f="01_信息技术服务费发票", t="信息技术服务费发票", no="XX-2026-0101",
         d="2026年01月08日", party="云智软件技术有限公司", rate="6%",
         items=[("系统运维与技术支持服务", 12, (3000, 4000)),
                ("故障响应与远程排障", 6, (1800, 2400)),
                ("数据备份与例行巡检", 4, (1200, 1800))],
         remark="服务期间为 2026 年 1 月至 6 月，按月结算。"),

    dict(f="02_货物运输服务结算单", t="货物运输服务结算单", no="YS-2026-0102",
         d="2026年01月19日", party="顺捷物流有限公司", rate="9%",
         items=[("整车运输 杭州至成都", 8, (4500, 6500)),
                ("零担运输 杭州至西安", 12, (800, 1600)),
                ("装卸搬运服务", 20, (150, 400))],
         remark="起运地杭州，目的地成都与西安，含保价费用。"),

    dict(f="03_仓储保管服务费发票", t="仓储保管服务费发票", no="CC-2026-0203",
         d="2026年02月05日", party="恒仓仓储服务有限公司", rate="6%",
         items=[("常温仓储保管费 1200 平方米", 6, (9000, 13000)),
                ("托盘租赁", 60, (20, 60)),
                ("出入库操作费", 45, (120, 300))],
         remark="仓库位于杭州萧山物流园区，按月计费。"),

    dict(f="04_物业管理服务费发票", t="物业管理服务费发票", no="WY-2026-0204",
         d="2026年02月17日", party="嘉和物业管理有限公司", rate="6%",
         items=[("写字楼物业管理费", 3, (22000, 30000)),
                ("公共区域能耗分摊", 3, (4000, 7000)),
                ("停车场管理费", 3, (1200, 2500))],
         remark="办公楼层为 A 座 12 层，按季度收取。"),

    dict(f="05_管理咨询服务费结算单", t="管理咨询服务费结算单", no="ZX-2026-0305",
         d="2026年03月03日", party="明道管理咨询有限公司", rate="6%",
         items=[("组织架构优化咨询", 1, (60000, 90000)),
                ("岗位职责梳理与定编", 1, (30000, 50000)),
                ("驻场辅导", 15, (2000, 3500))],
         remark="咨询周期三个月，分两期付款。"),

    dict(f="06_广告发布服务费发票", t="广告发布服务费发票", no="GG-2026-0306",
         d="2026年03月21日", party="星耀文化传媒有限公司", rate="6%",
         items=[("户外大屏广告投放", 30, (2500, 4500)),
                ("广告画面设计制作", 4, (1500, 3000)),
                ("活动物料制作", 1, (8000, 15000))],
         remark="投放位置为城站商圈与武林广场，投放期一个月。"),

    dict(f="07_办公用品采购订单", t="办公用品采购订单", no="CG-2026-0407",
         d="2026年04月02日", party="文具之家商贸有限公司", rate=None,
         items=[("A4 复印纸 70g", 200, (18, 30)),
                ("中性签字笔 0.5mm", 500, (2, 5)),
                ("文件夹与档案盒", 300, (4, 12))],
         remark="送货至总部行政部仓库，验收后 30 日内付款。"),

    dict(f="08_便携式计算机采购合同", t="便携式计算机采购合同", no="CG-2026-0408",
         d="2026年04月14日", party="联启数码科技有限公司", rate=None,
         items=[("14 英寸商用笔记本 16G/512G", 20, (4800, 6200)),
                ("扩展坞与电源适配器", 20, (200, 400)),
                ("三年上门保修服务", 20, (300, 600))],
         remark="质保三年，到货验收合格后支付 90% 款项。"),

    dict(f="09_打印机耗材采购订单", t="打印机耗材采购订单", no="CG-2026-0509",
         d="2026年05月06日", party="恒信办公设备有限公司", rate=None,
         items=[("激光打印机硒鼓", 40, (280, 520)),
                ("墨粉盒 黑色", 60, (150, 300)),
                ("感光鼓组件", 15, (400, 800))],
         remark="需为原厂正品，提供一年内质量问题包换。"),

    dict(f="10_办公家具采购订单", t="办公家具采购订单", no="CG-2026-0510",
         d="2026年05月18日", party="木言家具制造有限公司", rate=None,
         items=[("职员办公桌 1.4 米", 30, (700, 1100)),
                ("人体工学办公椅", 30, (500, 900)),
                ("三层文件柜", 20, (400, 700))],
         remark="含上门安装与旧家具清运，验收后付款。"),

    dict(f="11_网络交换设备采购合同", t="网络交换设备采购合同", no="CG-2026-0611",
         d="2026年06月09日", party="华讯网络设备有限公司", rate=None,
         items=[("千兆接入交换机 24 口", 12, (1200, 2200)),
                ("万兆光模块", 24, (600, 1200)),
                ("六类非屏蔽网线 305 米", 10, (500, 900))],
         remark="含三年原厂质保与首次上架调试服务。"),

    dict(f="12_差旅费报销单", t="差旅费报销单", no="BX-2026-0612",
         d="2026年06月23日", party="—", rate=None,
         items=[("高铁票 杭州东至南京南", 2, (110, 160)),
                ("住宿费 标准间", 2, (350, 550)),
                ("市内交通费", 6, (20, 60))],
         remark="出差事由：客户现场技术交流。报销人：王建国。"),

    dict(f="13_会议费报销单", t="会议费报销单", no="BX-2026-0713",
         d="2026年07月08日", party="—", rate=None,
         items=[("会议场地租赁", 1, (2000, 4000)),
                ("参会人员工作餐", 25, (40, 90)),
                ("会议资料印制", 30, (8, 25))],
         remark="会议主题：上半年经营分析会。报销人：李晓梅。"),

    dict(f="14_业务招待费报销单", t="业务招待费报销单", no="BX-2026-0814",
         d="2026年08月11日", party="—", rate=None,
         items=[("客户接待用餐", 3, (400, 900)),
                ("商务茶歇", 2, (120, 300)),
                ("来访伴手礼", 4, (80, 200))],
         remark="接待对象：北方区重要客户考察团。报销人：赵鹏。"),

    dict(f="15_车辆燃油费报销单", t="车辆燃油费报销单", no="BX-2026-0815",
         d="2026年08月26日", party="—", rate=None,
         items=[("公务车 92 号汽油", 60, (7, 9)),
                ("车辆常规保养", 1, (300, 700)),
                ("高速通行费", 8, (30, 90))],
         remark="车牌 浙 A·D5K21，公务用车里程 1,240 公里。报销人：孙志强。"),

    dict(f="16_办公用房租赁合同", t="办公用房租赁合同", no="HT-2026-0916",
         d="2026年09月02日", party="城建置业有限公司", rate=None,
         items=[("办公用房租金 860 平方米", 12, (38000, 52000)),
                ("物业配套服务费", 12, (3000, 6000)),
                ("租赁保证金", 1, (70000, 110000))],
         remark="租赁期限三年，押二付三，租金每年递增 5%。"),

    dict(f="17_设备维保服务合同", t="设备维保服务合同", no="HT-2026-0917",
         d="2026年09月15日", party="精工机电维修有限公司", rate=None,
         items=[("中央空调年度维保", 1, (28000, 42000)),
                ("电梯季度检查", 4, (3000, 6000)),
                ("应急抢修服务", 10, (500, 1200))],
         remark="服务响应时间不超过 4 小时，含易损件更换。"),

    dict(f="18_劳务外包服务协议", t="劳务外包服务协议", no="HT-2026-1018",
         d="2026年10月09日", party="众力人力资源服务有限公司", rate=None,
         items=[("保洁服务 5 人", 12, (4000, 6000)),
                ("前台接待 2 人", 12, (5500, 8000)),
                ("外包管理服务费", 12, (800, 1500))],
         remark="外包人员由乙方管理，甲方按月度考核结果结算。"),

    dict(f="19_固定资产盘点表", t="固定资产盘点表", no="PD-2026-1019",
         d="2026年10月21日", party="—", rate=None,
         items=[("台式计算机 资产编号 ZC-001 至 ZC-060", 60, (3500, 5200)),
                ("激光打印机 资产编号 ZC-101 至 ZC-118", 18, (1800, 3200)),
                ("立式空调 资产编号 ZC-201 至 ZC-212", 12, (4000, 6500))],
         remark="盘点结果：账实相符 88 项，盘亏 2 项，待核实。盘点人：陈雅琴。"),

    dict(f="20_库存物资台账", t="库存物资台账", no="KZ-2026-1120",
         d="2026年11月06日", party="—", rate=None,
         items=[("期初结存 办公耗材", 1200, (5, 30)),
                ("本期入库 劳保用品", 800, (10, 60)),
                ("本期出库 清洁用品", 650, (8, 45))],
         remark="期末结存 1,350 件，账实相符，无超期物资。制表人：陈雅琴。"),

    dict(f="21_项目进度周报", t="项目进度周报", no="ZB-2026-1121",
         d="2026年11月18日", party="—", rate=None,
         items=[("里程碑 M1 需求确认", 1, (0, 0)),
                ("里程碑 M2 原型评审", 1, (0, 0)),
                ("本周工时投入", 86, (0, 0))],
         remark="整体完成率 62%，存在接口联调延期风险，需协调第三方厂商。编写人：赵鹏。"),

    dict(f="22_安全生产检查记录", t="安全生产检查记录", no="AQ-2026-1222",
         d="2026年12月03日", party="—", rate=None,
         items=[("消防通道畅通情况", 1, (0, 0)),
                ("配电房用电安全检查", 1, (0, 0)),
                ("危化品存储合规检查", 1, (0, 0))],
         remark="发现隐患 3 项（应急灯失效 2 处、消防栓遮挡 1 处），已限期整改。检查人：孙志强。"),
]

LINE_H = 24
MARGIN = 50
PAGE_W, PAGE_H = 595, 842


def render(spec: dict, out_dir: Path, fontfile: str | None) -> Path:
    rng = random.Random(hash(spec["no"]) & 0xFFFF)
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    kwargs = {"fontname": "zh", "fontsize": 11}
    if fontfile:
        kwargs["fontfile"] = fontfile

    y = 70
    page.insert_text((MARGIN, y), spec["t"], fontsize=17, **{k: v for k, v in kwargs.items() if k != "fontsize"})
    y += 12
    page.draw_line((MARGIN, y), (PAGE_W - MARGIN, y), color=(0.6, 0.6, 0.6), width=0.8)
    y += LINE_H + 6

    page.insert_text((MARGIN, y), f"单据编号：{spec['no']}", **kwargs)
    page.insert_text((320, y), f"日期：{spec['d']}", **kwargs)
    y += LINE_H
    if spec["party"] != "—":
        page.insert_text((MARGIN, y), f"往来单位：{spec['party']}", **kwargs)
        y += LINE_H
    page.insert_text((MARGIN, y), f"本方单位：{BUYER}", **kwargs)
    y += LINE_H + 6

    page.draw_line((MARGIN, y), (PAGE_W - MARGIN, y), color=(0.6, 0.6, 0.6), width=0.8)
    y += LINE_H
    page.insert_text((MARGIN, y), "项目名称", **kwargs)
    page.insert_text((330, y), "数量", **kwargs)
    page.insert_text((400, y), "单价", **kwargs)
    page.insert_text((480, y), "金额", **kwargs)
    y += LINE_H

    total = 0.0
    for name, qty, (lo, hi) in spec["items"]:
        unit = _money(rng, lo, hi) if hi > 0 else "0.00"
        if hi > 0:
            amount = float(unit) * qty
            total += amount
            amt_s = f"{amount:,.2f}"
        else:
            amt_s = "—"
        page.insert_text((MARGIN, y), name, **kwargs)
        page.insert_text((335, y), str(qty), **kwargs)
        page.insert_text((400, y), unit, **kwargs)
        page.insert_text((480, y), amt_s, **kwargs)
        y += LINE_H

    y += 6
    page.draw_line((MARGIN, y), (PAGE_W - MARGIN, y), color=(0.6, 0.6, 0.6), width=0.8)
    y += LINE_H

    if spec["rate"]:
        rate_pct = float(spec["rate"].rstrip("%"))
        tax = total * rate_pct / 100
        page.insert_text((MARGIN, y), f"合计金额：{total:,.2f} 元", **kwargs)
        y += LINE_H
        page.insert_text((MARGIN, y), f"税率：{spec['rate']}", **kwargs)
        page.insert_text((250, y), f"税额：{tax:,.2f} 元", **kwargs)
        y += LINE_H
        page.insert_text((MARGIN, y), f"价税合计：{total + tax:,.2f} 元", **kwargs)
    else:
        page.insert_text((MARGIN, y), f"金额合计：{total:,.2f} 元", **kwargs)
    y += LINE_H + 6

    page.insert_text((MARGIN, y), f"备注：{spec['remark']}", **kwargs)

    out = out_dir / f"{spec['f']}.pdf"
    # 字体子集化：不做的话每个 PDF 都会完整嵌入 SimHei（约 9.7MB/篇，
    # 22 篇就是 205MB）。子集化后只保留实际用到的字形，单篇降到几十 KB。
    try:
        doc.subset_fonts()
    except Exception as e:
        print(f"  ⚠️ 字体子集化失败（{spec['f']}）: {e}", file=sys.stderr)
    doc.save(str(out), garbage=4, deflate=True)
    doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成测试语料 PDF")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "test_corpus")
    ap.add_argument("--font", default=None, help="中文字体文件路径（默认自动探测）")
    args = ap.parse_args()

    fontfile = args.font
    if not fontfile:
        for c in FONT_CANDIDATES:
            if Path(c).exists():
                fontfile = c
                break
    if not fontfile:
        print("⚠️  未找到系统中文字体，回退到 MuPDF 内建字体。\n"
              "    注意：内建字体会把数字渲染成全角，OCR 出来的编号可能不可用。\n"
              "    建议用 --font 指定一个中文 TTF。", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {args.out}")
    print(f"字体    : {fontfile or '（内建）'}")
    print(f"篇数    : {len(SPECS)}")
    print("-" * 56)
    for spec in SPECS:
        p = render(spec, args.out, fontfile)
        print(f"  {p.name}")
    print("-" * 56)
    print(f"完成：{len(SPECS)} 篇已生成。")
    print("下一步：python scripts/ingest_corpus.py --dir " + str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
