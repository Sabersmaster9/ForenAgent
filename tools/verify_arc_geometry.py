"""验证弧线几何与候选分歧度：合成掩膜单元测试。

不依赖模型权重，只用 cv2/numpy 构造已知几何的量角器掩膜，
检查 arc_delta 与 candidate_delta_spread 是否按预期产出。

用法：
    D:/CondaData/envs/forenagent/python.exe tools/verify_arc_geometry.py
"""

import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "visual_agent" / "goniometer"))

from predict_angle import (  # noqa: E402
    angle_between,
    arc_correction_delta,
    arc_direction_candidates,
    candidate_angle_spread,
    contour_points,
    mask_center,
)


def make_goniometer(fixed_deg: float, moving_deg: float, size: int = 640,
                    arm_len: float = 210.0, arm_half_width: float = 9.0,
                    arc_asymmetry: float = 0.0):
    """构造一个量角器：轴心在中心，两条矩形臂，外加一段刻度圆弧。

    arc_asymmetry 用于模拟透视：>0 时弧线沿垂直方向被压扁，
    使弧线推断出的固定臂方向偏离掩膜拟合方向。
    """
    cx = cy = size / 2.0
    fixed = np.zeros((size, size), np.uint8)
    moving = np.zeros((size, size), np.uint8)
    pivot = np.zeros((size, size), np.uint8)
    arc = np.zeros((size, size), np.uint8)

    def arm(angle_deg):
        theta = math.radians(angle_deg)
        dx, dy = math.cos(theta), math.sin(theta)
        px, py = -dy, dx
        p1 = (cx + dx * 18, cy + dy * 18)
        p2 = (cx + dx * arm_len, cy + dy * arm_len)
        pts = np.array([
            [p1[0] + px * arm_half_width, p1[1] + py * arm_half_width],
            [p2[0] + px * arm_half_width, p2[1] + py * arm_half_width],
            [p2[0] - px * arm_half_width, p2[1] - py * arm_half_width],
            [p1[0] - px * arm_half_width, p1[1] - py * arm_half_width],
        ], dtype=np.int32)
        return pts

    cv2.fillPoly(fixed, [arm(fixed_deg)], 1)
    cv2.fillPoly(moving, [arm(moving_deg)], 1)
    cv2.circle(pivot, (int(cx), int(cy)), 22, 1, -1)

    # 刻度圆弧：以轴心为圆心的一段粗弧，弧心角覆盖两个臂
    lo = min(fixed_deg, moving_deg)
    hi = max(fixed_deg, moving_deg)
    axes = (int(arm_len * 0.92), int(arm_len * 0.92 * (1.0 - arc_asymmetry)))
    cv2.ellipse(arc, (int(cx), int(cy)), axes, 0, lo, hi, 1, 7)

    return {"fixed_arm": fixed, "moving_arm": moving, "pivot": pivot}, arc


def main() -> int:
    failures = []

    def check(name, ok, detail=""):
        print(f"  [{'OK' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    print("1) 轴心定位")
    masks, arc = make_goniometer(0.0, 60.0)
    pivot_xy = mask_center(masks["pivot"])
    check("轴心接近几何中心", abs(pivot_xy[0] - 320) < 3 and abs(pivot_xy[1] - 320) < 3,
          f"pivot={pivot_xy.round(2).tolist()}")

    print("2) 无透视时弧线修正量应接近 0")
    masks, arc = make_goniometer(0.0, 60.0, arc_asymmetry=0.0)
    pivot_xy = mask_center(masks["pivot"])
    from predict_angle import fit_direction_pca, select_arm_points
    fixed_dir = fit_direction_pca(select_arm_points(masks["fixed_arm"], pivot_xy), pivot_xy)
    moving_dir = fit_direction_pca(select_arm_points(masks["moving_arm"], pivot_xy), pivot_xy)
    base = angle_between(fixed_dir, moving_dir)
    check("基线夹角约为 60 度", abs(base - 60.0) < 3.0, f"angle={base:.2f}")
    delta, meta = arc_correction_delta(fixed_dir, moving_dir, masks["fixed_arm"], pivot_xy)
    check("窗口数 >= 1", meta.get("arc_windows", 0) >= 1, f"windows={meta.get('arc_windows')}")
    check("对称情形修正量接近 0", abs(delta) < 3.0, f"arc_delta={delta:.3f}")

    print("3) 候选分歧度")
    spread, smeta = candidate_angle_spread(masks["moving_arm"], pivot_xy, fixed_dir)
    check("候选数 >= 2", smeta.get("candidate_count", 0) >= 2, f"count={smeta.get('candidate_count')}")
    check("分歧度为有限值", isinstance(spread, float) and spread >= 0.0, f"spread={spread:.3f}")
    check("规则形状分歧度小", spread < 12.0, f"spread={spread:.3f}")

    print("4) 退化输入不得抛异常")
    empty = np.zeros((100, 100), np.uint8)
    d0, m0 = arc_correction_delta(fixed_dir, moving_dir, empty, pivot_xy)
    check("空掩膜返回 0", d0 == 0.0, f"delta={d0}")
    s0, _ = candidate_angle_spread(empty, pivot_xy, fixed_dir)
    check("空掩膜分歧度为 0", s0 == 0.0, f"spread={s0}")
    check("空掩膜轮廓为 None", contour_points(empty) is None)

    print("5) arc_direction_candidates 在真实轮廓上返回方向")
    cands = arc_direction_candidates(masks["fixed_arm"], pivot_xy)
    check("至少一个弧线窗口产出方向", len(cands) >= 1, f"windows={list(cands)}")

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
