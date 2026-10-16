"""构造完全合成的案件输入，用于生成专利实施例的实际产物。

案件为虚构：当事人、机构、日期、单位均为编制需要而设，不含任何真实信息。
量角器图像由程序绘制，不做任何真实照片的复制或变换。

用法：
    D:/CondaData/envs/forenagent/python.exe tools/build_synthetic_case.py [输出目录]
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np

CASE_ID = "CASE-SYN-001"

COMMISSION = """司法鉴定委托书

委托单位：示例市示例区人民法院（合成）
案号：（2026）示例民初0000号（合成）
受托单位：示例司法鉴定机构（合成）

委托事项：
一、对被鉴定人张某某的损伤致残程度进行鉴定；
二、对被鉴定人张某某的误工期、护理期、营养期进行评定。

案情摘要：被鉴定人于2026年3月因道路交通事故受伤，伤后就诊于示例医院。
委托方随案移送门诊病历、影像报告及检查记录等材料。
本案全部材料为合成材料，用于技术验证。

委托日期：2026年9月1日
"""

RECORDS = """检查记录

被鉴定人：张某某（合成）    性别：男    年龄：41（合成）

一、主诉
左膝关节外伤后疼痛、活动受限六月余。

二、体格检查
神志清，步入检查室，查体合作。
左膝关节外观无明显肿胀，关节间隙压痛（±），浮髌试验阴性，侧方应力试验阴性。
左膝关节活动度：屈曲0度到95度，伸展-10度。
右膝关节活动度：屈曲0度到135度，伸展0度。
双下肢肌力5级，肌张力正常。

三、辅助检查
左膝关节正侧位X线片（示例医院，2026年3月）：左膝关节未见明显骨折征象，
关节间隙未见明显狭窄，关节面光整。
左膝关节磁共振（示例医院，2026年3月）：左膝关节内侧副韧带损伤，
信号增高，连续性欠佳；半月板形态信号未见明显异常。

四、诊断
左膝关节内侧副韧带损伤；
左膝关节功能障碍。

测量工具：ZDSJ-A-002量角器，ZDSJ-A-003钢卷尺。
检查日期：2026年9月10日
"""

REPORT = """影像检查报告单

检查机构：示例医院（合成）    检查号：SYN-2026-0910
被鉴定人：张某某（合成）      检查日期：2026年9月10日

检查项目：左膝关节正侧位X线摄影

检查所见：
左膝关节骨质结构完整，未见明确骨折线及骨质破坏。
关节间隙宽度未见明显变窄，关节面尚光整。
周围软组织未见明显异常密度影。

检查结论：
左膝关节未见明显骨折征象；
左膝关节间隙未见明显狭窄。

报告医师：示例（合成）
"""


def draw_goniometer(angle_deg: float, size: int = 900) -> np.ndarray:
    """绘制一张合成量角器照片：固定臂 + 移动臂 + 刻度弧 + 轴点。"""
    img = np.full((size, size, 3), 245, np.uint8)
    cx, cy = size // 2, size // 2
    r = int(size * 0.34)

    # 刻度弧与刻度线
    cv2.ellipse(img, (cx, cy), (r, r), 0, 0, 180, (60, 60, 60), 3)
    for deg in range(0, 181, 5):
        t = math.radians(deg)
        long_tick = (deg % 10 == 0)
        r1 = r - (18 if long_tick else 9)
        p1 = (int(cx + r1 * math.cos(t)), int(cy - r1 * math.sin(t)))
        p2 = (int(cx + r * math.cos(t)), int(cy - r * math.sin(t)))
        cv2.line(img, p1, p2, (60, 60, 60), 3 if long_tick else 1)
        if long_tick and deg % 30 == 0:
            rl = r + 26
            cv2.putText(img, str(deg), (int(cx + rl * math.cos(t)) - 14, int(cy - rl * math.sin(t)) + 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, (40, 40, 40), 2)

    # 固定臂：指向右侧（0 度）
    cv2.line(img, (cx, cy), (int(cx + r + 40), cy), (30, 30, 30), 11)
    # 移动臂：按测量角度
    t = math.radians(angle_deg)
    cv2.line(img, (cx, cy), (int(cx + (r + 40) * math.cos(t)), int(cy - (r + 40) * math.sin(t))),
             (30, 30, 30), 11)
    # 轴点
    cv2.circle(img, (cx, cy), 15, (20, 20, 20), -1)

    # 轻微噪声，避免图像过于理想化
    noise = np.random.default_rng(7).integers(-3, 4, img.shape, dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return img


def main(argv: list) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("_synthetic_case")
    materials = root / "1 鉴定材料"
    exams = materials / "检查记录"
    photos = root / "3 检查照片"
    for d in (materials, exams, photos):
        d.mkdir(parents=True, exist_ok=True)

    (materials / "委托书.txt").write_text(COMMISSION, encoding="utf-8")
    (materials / "影像报告.txt").write_text(REPORT, encoding="utf-8")
    (exams / "检查记录.txt").write_text(RECORDS, encoding="utf-8")

    # 同一动作拍三张，角度略有差异，用于按中位数取代表值。
    # 注意：cv2.imwrite 在 Windows 上按本地代码页解释路径，无法处理中文文件名，
    # 因此改为先编码再用 Python 的文件接口写盘。
    for idx, angle in enumerate((94.6, 95.4, 95.0), start=1):
        img = draw_goniometer(angle)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            raise RuntimeError("PNG 编码失败")
        (photos / f"左膝屈曲{idx}.png").write_bytes(buf.tobytes())

    sys.stdout.reconfigure(encoding="utf-8")
    print(f"合成案件已生成：{root}")
    for f in sorted(root.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(root)}  ({f.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
