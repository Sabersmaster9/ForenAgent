"""生成专利附图（黑白线图），同时输出 SVG 与 300dpi PNG。

主案 7 张、分案 9 张，共 16 张。图面只用线条、矩形、菱形与文字，
不含灰度填充与彩色，符合专利附图惯例。

用法：
    D:/CondaData/envs/forenagent/python.exe tools/make_patent_figures.py [输出目录]
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle  # noqa: E402

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "SimSun"]
matplotlib.rcParams["axes.unicode_minus"] = False

LW = 1.1
FS = 9.0
FSS = 8.0


# ---------------------------------------------------------------- 绘图基元

class Canvas:
    """以「单位」为坐标的绘图区，12 单位 = 1 英寸。"""

    def __init__(self, width: float = 100.0, height: float = 70.0):
        self.w, self.h = width, height
        self.fig, self.ax = plt.subplots(figsize=(width / 12.0, height / 12.0))
        self.ax.set_xlim(0, width)
        self.ax.set_ylim(0, height)
        self.ax.set_aspect("equal")
        self.ax.axis("off")
        self.fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def box(self, cx, cy, w, h, text="", fs=FS, ls="-", rounded=False, lw=LW):
        style = "round,pad=0,rounding_size=2" if rounded else "square,pad=0"
        patch = Rectangle((cx - w / 2, cy - h / 2), w, h, fill=False,
                          edgecolor="black", linewidth=lw, linestyle=ls)
        if rounded:
            from matplotlib.patches import FancyBboxPatch
            patch = FancyBboxPatch((cx - w / 2 + 2, cy - h / 2 + 1.5),
                                   w - 4, h - 3, boxstyle="round,pad=1.5,rounding_size=3",
                                   fill=False, edgecolor="black", linewidth=lw, linestyle=ls)
        self.ax.add_patch(patch)
        if text:
            self.text(cx, cy, text, fs=fs)
        return (cx, cy, w, h)

    def diamond(self, cx, cy, w, h, text="", fs=FSS):
        pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
        self.ax.add_patch(Polygon(pts, closed=True, fill=False, edgecolor="black", linewidth=LW))
        if text:
            self.text(cx, cy, text, fs=fs)

    def text(self, x, y, s, fs=FS, ha="center", va="center", weight="normal", rot=0, color="black"):
        self.ax.text(x, y, s, fontsize=fs, ha=ha, va=va, weight=weight,
                     rotation=rot, color=color, linespacing=1.45)

    def arrow(self, p1, p2, ls="-", lw=LW, rad=0.0, head=7.0, label=None, lfs=FSS, loff=(0, 0)):
        style = "-|>" if ls != "--" else "-|>"
        p = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=head,
                            linewidth=lw, linestyle=ls, color="black",
                            connectionstyle=f"arc3,rad={rad}", shrinkA=0, shrinkB=0)
        self.ax.add_patch(p)
        if label:
            mx, my = (p1[0] + p2[0]) / 2 + loff[0], (p1[1] + p2[1]) / 2 + loff[1]
            self.text(mx, my, label, fs=lfs)

    def line(self, p1, p2, ls="-", lw=LW):
        self.ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="black", linewidth=lw, linestyle=ls)

    def _overflow(self):
        """检出超出画布的元素，避免图面被静默截断。"""
        self.fig.canvas.draw()
        renderer = self.fig.canvas.get_renderer()
        inv = self.ax.transData.inverted()
        bad = []
        for artist in self.ax.get_children():
            if isinstance(artist, plt.Axes):
                continue
            try:
                bb = artist.get_window_extent(renderer=renderer)
                (x0, y0), (x1, y1) = inv.transform([[bb.x0, bb.y0], [bb.x1, bb.y1]])
            except Exception:
                continue
            if x0 < -0.5 or y0 < -0.5 or x1 > self.w + 0.5 or y1 > self.h + 0.5:
                label = getattr(artist, "get_text", lambda: "")()[:18]
                bad.append(f"{type(artist).__name__}{'(' + label + ')' if label else ''}"
                           f" x[{x0:.0f},{x1:.0f}] y[{y0:.0f},{y1:.0f}]")
        return bad

    def save(self, stem: Path):
        stem.parent.mkdir(parents=True, exist_ok=True)
        bad = self._overflow()
        if bad:
            print(f"  [警告] {stem.name} 有 {len(bad)} 个元素越界：{'；'.join(bad[:3])}")
        self.fig.savefig(stem.with_suffix(".png"), dpi=300, facecolor="white")
        self.fig.savefig(stem.with_suffix(".svg"), facecolor="white")
        plt.close(self.fig)


def vchain(c: Canvas, steps, x=34.0, top=64.0, bw=46.0, bh=8.6, gap=2.6,
           dashed=(), note_x=70.0, notes=None):
    """竖向流程：steps 为 (文本, 旁注) 列表；dashed 中的序号用虚线框表示人工步骤。"""
    y = top
    centers = []
    for idx, item in enumerate(steps):
        text = item[0] if isinstance(item, (tuple, list)) else item
        note = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else ""
        ls = "--" if idx in dashed else "-"
        c.box(x, y, bw, bh, text, ls=ls)
        centers.append((x, y))
        if note:
            c.text(note_x, y, note, fs=FSS, ha="left")
        if idx < len(steps) - 1:
            c.arrow((x, y - bh / 2), (x, y - bh / 2 - gap))
        y -= bh + gap
    return centers


# ================================================================ 主案附图

def main_fig1(out: Path):
    """图1 系统总体结构图。"""
    c = Canvas(132, 118)
    c.text(66, 113, "图1  系统总体结构图", fs=10)
    c.box(66, 103, 76, 8, "异构案件材料：委托书 / 病历 / 影像报告 / 检查记录 / 量角器照片", fs=7.4)

    labels = [
        "证据处理模块（文本提取·识别·质量择优）",
        "候选证据对象（来源位置 + 核验状态）",
        "专业核验：确认 / 修改 / 拒绝 / 补充",
        "共享案件状态（已确认事实）",
        "确定性规则执行模块（阈值·查表·条件匹配）",
        "阳性 / 阴性 / 拒绝 三类规则对象",
        "专业审核队列（必须确认 / 建议确认）",
        "报告生成模块",
        "一致性门控 + 授权门控",
        "正式意见 + 审计记录",
    ]
    bh, gap = 6.8, 1.9
    top = 88.0
    y = top
    centers = []
    for i, t in enumerate(labels):
        ls = "--" if i == 2 else "-"
        c.box(66, y, 62, bh, t, fs=7.4, ls=ls)
        c.text(100, y, str(10 + i), fs=7.4, ha="left")
        centers.append(y)
        if i < len(labels) - 1:
            c.arrow((66, y - bh / 2), (66, y - bh / 2 - gap), head=6)
        y -= bh + gap
    c.arrow((66, 99), (66, 92))
    c.text(78, 95.5, "材料接入", fs=7.2, ha="left")

    # 修正写回起源状态，并按依赖关系选择性重算
    c.arrow((35, centers[2]), (35, centers[4]), rad=-0.48, ls="--", lw=1.0)
    c.text(21, (centers[2] + centers[4]) / 2, "修正写回起源状态\n按依赖关系选择性重算", fs=7.2, rot=90)
    c.save(out / "主案" / "图1_系统总体结构图")


def main_fig2(out: Path):
    """图2 方法流程图。"""
    c = Canvas(100, 104)
    c.text(50, 100, "图2  方法流程图", fs=10)
    steps = [
        ("S1  接收并登记异构材料", "机器"),
        ("S2  文本提取与自适应二次识别，生成统一证据表示", "机器"),
        ("S3  映射为带核验状态的候选事实", "机器"),
        ("S4  接收专业核验：确认 / 修改 / 拒绝 / 补充", "人工"),
        ("S5  对已确认事实执行确定性规则", "机器"),
        ("S6  不可自动判断事项进入专业审核队列", "人工"),
        ("S7  生成带依据关联的报告草稿", "机器"),
        ("S8  接收专业修正，按依赖关系选择性重算", "人工"),
        ("S9  一致性门控与授权门控，输出正式意见与审计记录", "机器"),
    ]
    y = 92
    bh, gap = 7.4, 2.4
    pos = []
    for i, (t, who) in enumerate(steps):
        ls = "--" if who == "人工" else "-"
        c.box(42, y, 66, bh, t, fs=FSS, ls=ls)
        c.text(82, y, who, fs=FSS, ha="left")
        pos.append(y)
        if i < len(steps) - 1:
            c.arrow((42, y - bh / 2), (42, y - bh / 2 - gap))
        y -= bh + gap
    # S8 回到 S5
    c.arrow((10, pos[7]), (10, pos[4]), rad=0.32, ls="--", lw=0.95)
    c.text(3.5, (pos[4] + pos[7]) / 2, "修正后重算", fs=FSS, rot=90)
    c.save(out / "主案" / "图2_方法流程图")


def main_fig3(out: Path):
    """图3 状态转换图。"""
    c = Canvas(110, 74)
    c.text(55, 70, "图3  状态转换图", fs=10)
    c.box(20, 48, 22, 10, "候选", rounded=True)
    c.box(56, 62, 22, 10, "已确认", rounded=True)
    c.box(56, 34, 22, 10, "已拒绝", rounded=True)
    c.box(56, 6, 22, 10, "未决", rounded=True)
    c.box(92, 62, 22, 10, "已授权", rounded=True)
    c.arrow((31, 51), (45, 59), label="专业人员确认", lfs=FSS, loff=(0, 3))
    c.arrow((31, 47), (45, 38), label="专业人员拒绝", lfs=FSS, loff=(0, -3.5))
    c.arrow((31, 43), (45, 10), label="证据不足 / 事实冲突", lfs=FSS, loff=(-12, 0))
    c.arrow((67, 57), (81, 59), label="授权签署", lfs=FSS, loff=(0, 3))
    c.arrow((67, 39), (30, 44), rad=0.35, ls="--", label="修正后重新进入候选", lfs=FSS, loff=(6, -12))
    c.arrow((67, 11), (30, 44), rad=-0.3, ls="--", label="补充事实", lfs=FSS, loff=(10, 6))
    c.text(55, 22, "机器产生候选；专业人员确认、修改、拒绝、补充；\n具备权限者授权签署", fs=FSS)
    c.save(out / "主案" / "图3_状态转换图")


def main_fig4(out: Path):
    """图4 数据结构关系图。"""
    c = Canvas(112, 70)
    c.text(56, 66, "图4  数据结构关系图", fs=10)
    c.box(22, 48, 30, 11, "证据对象\n标识·来源文件·页·内容·质量", fs=FSS)
    c.box(70, 48, 30, 11, "事实对象\n字段·值·来源·核验状态·版本", fs=FSS)
    c.box(70, 26, 30, 11, "规则结果对象\n条款·分支·等级·推理路径", fs=FSS)
    c.box(22, 26, 30, 11, "报告声明\n段落·依据的事实与规则", fs=FSS)
    c.box(46, 6, 34, 10, "审计事件\n操作者·前后值·理由·受影响范围", fs=FSS)
    c.arrow((37, 48), (55, 48), label="来源关系", lfs=FSS, loff=(0, 2.5))
    c.arrow((70, 42.5), (70, 31.5), label="输入关系", lfs=FSS, loff=(16, 0))
    c.arrow((55, 26), (37, 26), label="生成关系", lfs=FSS, loff=(0, 2.5))
    c.arrow((29, 20.5), (42, 11), label="", lfs=FSS)
    c.arrow((63, 11), (76, 20.5))
    c.text(8, 36, "变更关系", fs=FSS, rot=90)
    c.save(out / "主案" / "图4_数据结构关系图")


def main_fig5(out: Path):
    """图5 修正写回与选择性重算示意图。"""
    c = Canvas(116, 76)
    c.text(58, 72, "图5  修正写回与选择性重算示意图", fs=10)
    stages = [("证据处理", 26), ("事实核验", 48), ("规则裁决", 70), ("报告生成", 92)]
    for name, x in stages:
        c.box(x, 58, 18, 9, name, fs=FSS)
    for i in range(len(stages) - 1):
        c.arrow((stages[i][1] + 9, 58), (stages[i + 1][1] - 9, 58))

    c.box(58, 38, 74, 12,
          "依赖索引：事实字段 → 依赖该字段的条款集合\n"
          "受影响条款 25 条 → 重跑裁决器 2 个、跳过 3 个；其余条款复用上次结果",
          fs=FSS)
    c.arrow((48, 53.5), (48, 44.5), label="修正字段", lfs=FSS, loff=(10, 0))
    c.arrow((70, 32), (70, 26), label="选择性重算", lfs=FSS, loff=(20, 0))

    c.box(30, 18, 46, 11, "重新执行\n脊柱 / 肋骨 / 结构化特征 / 活动度 中受影响的裁决器", fs=FSS, ls="--")
    c.box(88, 18, 44, 11, "直接复用\n其余条款的上一次裁决结果", fs=FSS, ls="--")
    c.arrow((58, 32), (30, 23.5), rad=0.15)
    c.arrow((58, 32), (88, 23.5), rad=-0.15)
    c.text(58, 5, "选择性重算的输出与全量重算逐条一致；无法识别的回填目标退回全量重算", fs=FSS)
    c.save(out / "主案" / "图5_修正写回与选择性重算")


def main_fig6(out: Path):
    """图6 一致性门控与授权门控流程图。"""
    c = Canvas(104, 80)
    c.text(52, 76, "图6  一致性门控与授权门控流程图", fs=10)
    c.box(30, 65, 46, 10, "待出具报告草稿", fs=FSS)
    checks = [
        "① 占位与模板残留扫描",
        "② 实验元数据泄漏检测",
        "③ 诉状与法律辩论噪声检测",
        "④ 识别串接噪声检测",
        "⑤ 人工正文替换落地校验",
        "⑥ 被鉴定人姓名与事故日期必现",
        "⑦ 伤残结论一致性（等级与条款号）",
        "⑧ 三期结论一致性（逐项期限与条款号）",
        "⑨ 委托范围与结论范围一致性",
    ]
    y = 54
    for t in checks:
        c.box(30, y, 46, 5.4, t, fs=7.4)
        y -= 6.4
    c.arrow((30, 60), (30, 56.7))
    c.arrow((53, 65), (72, 65))
    c.box(87, 40, 30, 14, "一致性门控\n仅产生告警\n不重写结论", fs=FSS)
    c.arrow((72, 65), (87, 47))
    c.arrow((87, 33), (87, 24))
    c.box(72, 16, 40, 12, "授权门控\n审核事项未完成时\n限制或禁止出具", fs=FSS)
    c.arrow((52, 16), (30, 16))
    c.box(16, 16, 26, 12, "正式意见\n与审计记录", fs=FSS)
    c.save(out / "主案" / "图6_一致性门控与授权门控")


def main_fig7(out: Path):
    """图7 专业审核界面示意图。"""
    c = Canvas(116, 76)
    c.text(58, 72, "图7  专业审核界面示意图", fs=10)
    c.box(58, 40, 108, 58, "", fs=FSS)
    c.box(28, 56, 40, 26, "", fs=FSS)
    c.text(28, 66, "审核队列", fs=FSS)
    c.line((9, 63), (47, 63))
    for i, t in enumerate(["rom_manual_001  必须确认", "image_review_002  必须确认",
                           "no_goniometer_003  建议确认", "disability_confirm_007  必须确认"]):
        c.text(10, 59.5 - i * 4.6, t, fs=7.2, ha="left")
    c.box(84, 56, 40, 26, "", fs=FSS)
    c.text(84, 66, "事项详情", fs=FSS)
    c.line((65, 63), (103, 63))
    c.text(66, 59, "问题：未提供健侧活动度数据，\n健侧丧失值按 0 处理，请复核。", fs=7.2, ha="left")
    c.text(66, 52, "预计写入位置：\n第五部分 分析说明 / 第六部分 鉴定意见", fs=7.2, ha="left")
    for i, t in enumerate(["确认", "修改", "拒绝", "补充", "暂缓"]):
        c.box(20 + i * 19, 28, 16, 8, t, fs=FSS, rounded=True)
    c.box(84, 14, 44, 8, "授权签署（门控）", fs=FSS, rounded=True, ls="--")
    c.arrow((84, 24), (84, 18.5))
    c.text(30, 14, "专业人员操作", fs=FSS)
    c.save(out / "主案" / "图7_专业审核界面示意图")


# ================================================================ 分案附图

def sub_fig1(out: Path):
    c = Canvas(100, 104)
    c.text(50, 100, "图1  读数方法整体流程图", fs=10)
    steps = [
        "S1  对量角器图像执行三部件实例分割",
        "S2  由轴点掩膜确定轴心位置",
        "S3  对各臂掩膜执行点集提纯",
        "S4  多距离窗口多方法生成候选方向",
        "S5  一致性门控聚合为臂方向",
        "S6  计算两臂夹角得到候选角度",
        "S7  提取边缘平行性、宽度变化、轴心圆度与椭圆度",
        "S8  四维质量评分与风险分级",
        "S9  置信度门控；可用则分级补偿后输出",
    ]
    y = 92
    bh, gap = 7.4, 2.4
    for i, t in enumerate(steps):
        ls = "--" if i == 8 else "-"
        c.box(50, y, 82, bh, t, fs=FSS, ls=ls)
        if i < len(steps) - 1:
            c.arrow((50, y - bh / 2), (50, y - bh / 2 - gap))
        y -= bh + gap
    c.arrow((7, y + bh + gap + bh * 2), (7, y + bh + gap * 2), rad=0.25, ls="--", lw=0.95)
    c.text(2.5, y + bh + gap * 2 + bh, "证据不足则拒识", fs=FSS, rot=90)
    c.save(out / "分案" / "图1_读数方法整体流程图")


def sub_fig2(out: Path):
    c = Canvas(104, 60)
    c.text(52, 56, "图2  三部件分割与轴心定位示意图", fs=10)
    c.box(16, 30, 22, 26, "量角器图像", fs=FSS)
    c.arrow((27, 30), (37, 30))
    c.box(52, 30, 24, 26, "实例分割网络\n（三类别）", fs=FSS)
    c.arrow((64, 30), (74, 30))
    c.box(90, 44, 24, 10, "固定臂掩膜", fs=FSS)
    c.box(90, 30, 24, 10, "移动臂掩膜", fs=FSS)
    c.box(90, 16, 24, 10, "轴点掩膜", fs=FSS)
    c.arrow((74, 34), (78, 44))
    c.arrow((74, 30), (78, 30))
    c.arrow((74, 26), (78, 16))
    c.text(52, 10, "轴心 = 轴点掩膜全部前景像素坐标的算术均值", fs=FSS)
    c.save(out / "分案" / "图2_三部件分割与轴心定位")


def sub_fig3(out: Path):
    c = Canvas(112, 62)
    c.text(56, 58, "图3  点集提纯与多窗口候选方向生成示意图", fs=10)
    c.box(24, 30, 40, 42, "", fs=FSS)
    c.text(24, 48, "点集提纯", fs=FSS)
    c.line((6, 45), (42, 45))
    c.text(8, 40, "剔除距轴心 ≤ max(12px, 0.12·d)", fs=7.2, ha="left")
    c.text(8, 35, "保留距离 ≥ 35 分位数的远端像素", fs=7.2, ha="left")
    c.text(8, 30, "点数不足 20 时退回上一级结果", fs=7.2, ha="left")
    c.text(8, 24, "轴心位置以 ○ 表示，d 为像素到轴心距离", fs=7.2, ha="left")
    c.text(24, 18, "近端像素不携带方向信息", fs=7.2)
    c.box(82, 30, 44, 42, "", fs=FSS)
    c.text(82, 48, "七窗口 × 两方法 → 九候选", fs=FSS)
    c.line((62, 45), (102, 45))
    for i, w in enumerate(["(0.12,0.55)", "(0.18,0.65)", "(0.25,0.75)", "(0.35,0.90)",
                           "(0.45,0.98)", "(0.20,0.85)", "(0.30,0.95)"]):
        c.text(63, 40 - i * 4.0, w, fs=7.2, ha="left")
    c.text(88, 40, "加权主成分分析\n径向中位数\n（另加基线 PCA，得分 ×0.85）", fs=7.2, ha="left")
    c.save(out / "分案" / "图3_点集提纯与多窗口候选方向")


def sub_fig4(out: Path):
    c = Canvas(108, 62)
    c.text(54, 58, "图4  一致性门控聚合示意图", fs=10)
    c.box(20, 34, 30, 40, "", fs=FSS)
    c.text(20, 50, "九条候选方向得分", fs=FSS)
    c.line((6, 47), (34, 47))
    for i, (t, s) in enumerate([("候选1", "310.6"), ("候选2", "268.4"), ("候选3", "201.7"),
                                ("候选4", "170.5"), ("候选5", "96.2"), ("候选6", "88.0"),
                                ("候选7", "61.3"), ("候选8", "55.9"), ("候选9", "40.2")]):
        c.text(8, 43 - i * 4.0, t, fs=7.2, ha="left")
        c.text(33, 43 - i * 4.0, s, fs=7.2, ha="right")
    c.arrow((36, 34), (46, 34))
    c.box(66, 34, 32, 40, "", fs=FSS)
    c.text(66, 50, "双重门限", fs=FSS)
    c.line((52, 47), (80, 47))
    c.text(53, 42, "① 得分 ≥ 最高分 × 0.55", fs=7.2, ha="left")
    c.text(53, 37, "② 与最高分方向夹角 ≤ 12°", fs=7.2, ha="left")
    c.text(53, 31, "①② 同时满足才纳入", fs=7.2, ha="left")
    c.text(53, 25, "纳入者按得分加权求和后归一化", fs=7.2, ha="left")
    c.text(66, 18, "夹角 > 90° 者先翻转方向", fs=7.2)
    c.arrow((83, 34), (92, 34))
    c.text(99, 34, "臂方向", fs=FSS, rot=90)
    c.save(out / "分案" / "图4_一致性门控聚合")


def sub_fig5(out: Path):
    c = Canvas(122, 64)
    c.text(61, 60, "图5  几何质量特征提取示意图", fs=10)
    panels = [
        (19, "① 边缘平行性\n沿臂方向分 16 桶\n取 P8/P92 为上下边缘\n多项式回归取斜率"),
        (47, "② 宽度变化\n每桶宽度 = 上−下\n剔除宽于 P75 的桶\n宽度斜率×跨度/均宽"),
        (75, "③ 轴点圆度\n圆度 = 4πA / P²\n半径变异系数 σr/μr\n半径范围比 (P95−P5)/μr"),
        (103, "④ 轴点椭圆化\n拟合椭圆长短轴\n轴比 = 短轴 / 长轴\n残差 = |(x/a)²+(y/b)²−1|"),
    ]
    for x, t in panels:
        c.box(x, 30, 25, 34, "", fs=FSS)
        c.text(x, 30, t, fs=7.0)
    c.save(out / "分案" / "图5_几何质量特征提取")


def sub_fig6(out: Path):
    c = Canvas(112, 76)
    c.text(56, 72, "图6  四维质量评分与风险分级流程图", fs=10)
    c.box(28, 62, 44, 9, "几何特征与掩膜置信度", fs=FSS)
    for i, (t, x) in enumerate([("分割质量分", 16), ("几何拟合质量分", 45),
                                ("透视风险分", 74), ("补偿可行性分", 101)]):
        c.box(x, 47, 24, 11, t, fs=FSS)
        c.arrow((28, 57.5), (x, 52.5), rad=0.0)
    c.arrow((56, 41.5), (56, 36))
    c.diamond(56, 28, 52, 16, "分割分 < 55 或拟合分 < 45 ？", fs=7.2)
    c.arrow((30, 28), (14, 28))
    c.text(8, 22, "高风险\n拒识或重拍", fs=FSS)
    c.arrow((56, 20), (56, 15))
    c.diamond(56, 9, 56, 14, "风险 ≥ 45 且可行性 ≥ 55 ？", fs=7.2)
    c.arrow((28, 9), (14, 9))
    c.text(8, 3, "中风险\n施加补偿", fs=FSS)
    c.arrow((84, 9), (98, 9))
    c.text(105, 9, "否则\n采纳", fs=FSS, rot=90)
    c.save(out / "分案" / "图6_四维质量评分与风险分级")


def sub_fig7(out: Path):
    c = Canvas(122, 86)
    c.text(61, 81, "图7  置信度门控与分级透视补偿流程图", fs=10)
    c.box(34, 72, 52, 8, "通过分割的测量记录", fs=FSS)
    c.diamond(34, 55, 54, 16, "拒识判据（任一命中即拒识）", fs=7.4)
    c.arrow((34, 68), (34, 63.2))
    c.text(34, 42, "① 拟合分<35 且 风险≥55　② 分歧度≥50°　③ 固定臂宽度变化≥1.20", fs=6.8)

    # 拒识分支
    c.box(17, 27, 30, 11, "拒不读数\n转人工复核", fs=7.2, ls="--")
    c.arrow((21, 47.5), (17, 32.8))
    c.text(4, 40, "拒识", fs=7.0, ha="left")

    # 补偿分支
    c.arrow((61.2, 55), (69, 58))
    c.box(93, 60, 42, 17, "前置否决\n拟合分<35 或可行性<35\n弧线补偿量不在 0.5～4.5°\n分歧度>50°", fs=6.8)
    c.arrow((93, 51.3), (93, 45.2))
    c.box(93, 38, 42, 14, "风险分级\n<30→0　30–35→0.7\n35–65→0.95　≥65→1.05", fs=6.8)
    c.arrow((93, 30.8), (93, 24.2))
    c.box(93, 19, 42, 10, "折减：分歧>20° ×0.75\n宽度>0.55 ×0.65", fs=6.6)
    c.arrow((72, 19), (63, 16))
    c.box(36, 11, 50, 12, "最终角度 = 基线角度 + α × 弧线补偿量\n路由：基线 / 轻度 / 中度 / 强补偿", fs=7.0)
    c.save(out / "分案" / "图7_置信度门控与分级透视补偿")


def sub_fig8(out: Path):
    c = Canvas(96, 60)
    c.text(48, 56, "图8  可视化叠加图示意图", fs=10)
    c.box(24, 28, 34, 40, "", fs=FSS)
    c.text(24, 45, "原图", fs=FSS)
    c.line((20, 38), (34, 22), lw=1.6)
    c.line((20, 22), (34, 38), lw=1.6)
    c.text(24, 30, "○ 轴心", fs=FSS)
    c.text(24, 24, "— 两臂方向线", fs=FSS)
    c.arrow((42, 28), (54, 28))
    c.box(74, 28, 34, 40, "", fs=FSS)
    c.text(74, 45, "叠加图", fs=FSS)
    c.line((66, 38), (82, 22), lw=1.6)
    c.line((66, 22), (82, 38), lw=1.6)
    c.text(74, 30, "三个部件掩膜（以不同纹理区分）\n部件类别名 · 状态 · 角度\n拒识原因 · 轴心与方向标注", fs=6.8)
    c.text(48, 5, "同一关节、侧别、动作的多张图按角度中位数取代表值；\n被拒识的图像保留拒识原因，不退出证据链", fs=FSS)
    c.save(out / "分案" / "图8_可视化叠加图")


def sub_fig9(out: Path):
    c = Canvas(112, 78)
    c.text(56, 74, "图9  装置结构示意图", fs=10)
    mods = ["图像分割模块", "轴心定位模块", "点集提纯模块",
            "候选方向生成与聚合模块", "几何质量特征提取模块",
            "质量评分与风险分级模块", "置信度门控模块",
            "分级补偿模块", "可视化证据模块"]
    bh, gap = 5.6, 1.4
    y = 66.0
    ys = []
    for i, m in enumerate(mods):
        c.box(40, y, 48, bh, m, fs=7.4)
        c.text(68, y, str(91 + i), fs=7.4, ha="left")
        ys.append(y)
        if i < len(mods) - 1:
            c.arrow((40, y - bh / 2), (40, y - bh / 2 - gap), head=5.5, lw=0.95)
        y -= bh + gap
    c.box(94, (ys[0] + ys[-1]) / 2, 26, 22,
          "量角器读数装置\n\n（软件、硬件\n或软硬件结合实现）", fs=7.4)
    c.arrow((64, (ys[0] + ys[-1]) / 2), (80, (ys[0] + ys[-1]) / 2))
    c.save(out / "分案" / "图9_装置结构示意图")


def main(argv: list) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("附图")
    builders = [
        main_fig1, main_fig2, main_fig3, main_fig4, main_fig5, main_fig6, main_fig7,
        sub_fig1, sub_fig2, sub_fig3, sub_fig4, sub_fig5, sub_fig6, sub_fig7, sub_fig8, sub_fig9,
    ]
    for fn in builders:
        fn(out)
        print(f"  {fn.__doc__ or fn.__name__}")
    pngs = sorted(out.rglob("*.png"))
    svgs = sorted(out.rglob("*.svg"))
    print(f"共生成 {len(pngs)} 张 PNG、{len(svgs)} 张 SVG → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
