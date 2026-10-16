"""在真实量角器图像上验证弧线修正量与候选分歧度的分布。

用法：
    KMP_DUPLICATE_LIB_OK=TRUE D:/CondaData/envs/forenagent/python.exe \
        tools/verify_arc_real.py --limit 40
"""

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "visual_agent" / "goniometer"))

import cv2  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from predict_angle import (  # noqa: E402
    collect_masks,
    estimate_angle,
    fill_missing_masks,
)

DEFAULT_MODEL = ROOT / "models" / "goniometer_seg_best.pt"
DEFAULT_SOURCE = Path("E:/Users/a/Desktop/ForenAgent_1.0_archive_20261013/files/training/raw_image")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--fallback-conf", type=float, default=0.05)
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"图像目录不存在：{source}")
        return 1
    model_path = Path(args.model)
    if not model_path.exists():
        print(f"模型不存在：{model_path}")
        return 1

    images = sorted([p for p in source.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    images = images[: args.limit]
    print(f"模型：{model_path.name}　图像：{len(images)} 张")

    model = YOLO(str(model_path))
    rows = []
    failed = 0
    for path in images:
        image = cv2.imread(str(path))
        if image is None:
            failed += 1
            continue
        result = model.predict(str(path), conf=min(args.conf, args.fallback_conf), verbose=False)[0]
        primary = collect_masks(result, image.shape, args.conf)
        fallback = collect_masks(result, image.shape, args.fallback_conf)
        masks_by_cls, _used = fill_missing_masks(primary, fallback)
        try:
            angle, debug = estimate_angle(image, masks_by_cls, fixed_fit="robust")
        except Exception as exc:
            failed += 1
            print(f"  {path.name}: 推理异常 {exc}")
            continue
        if angle is None or not debug or debug.get("error"):
            failed += 1
            continue
        rows.append({
            "image": path.name,
            "angle": angle,
            "arc_delta": float(debug.get("arc_delta") or 0.0),
            "spread": float(debug.get("candidate_delta_spread") or 0.0),
            "arc_windows": (debug.get("arc_meta") or {}).get("arc_windows", 0),
            "candidates": (debug.get("spread_meta") or {}).get("candidate_count", 0),
        })

    if not rows:
        print(f"无有效读数（失败 {failed} 张）")
        return 1

    def stats(values):
        values = sorted(values)
        n = len(values)
        return {
            "min": values[0], "max": values[-1],
            "median": values[n // 2], "mean": sum(values) / n,
        }

    deltas = [r["arc_delta"] for r in rows]
    spreads = [r["spread"] for r in rows]
    nonzero = sum(1 for d in deltas if abs(d) > 1e-9)
    in_policy = sum(1 for d in deltas if 0.5 <= abs(d) <= 4.5)
    spread_ok = sum(1 for s in spreads if s <= 20.0)

    print(f"有效读数：{len(rows)}　失败/跳过：{failed}")
    print(f"arc_delta   : " + "  ".join(f"{k}={v:.2f}" for k, v in stats(deltas).items()))
    print(f"spread      : " + "  ".join(f"{k}={v:.2f}" for k, v in stats(spreads).items()))
    print(f"arc_delta 非零：{nonzero}/{len(rows)}"
          f"　落入策略区间[0.5,4.5]：{in_policy}/{len(rows)}")
    print(f"spread <= 20（可参与补偿档）：{spread_ok}/{len(rows)}")
    print()
    print("示例：")
    for row in rows[:10]:
        print(f"  {row['image'][:24]:<24} angle={row['angle']:7.2f}"
              f"  arc_delta={row['arc_delta']:7.3f}  spread={row['spread']:7.3f}"
              f"  win={row['arc_windows']} cand={row['candidates']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
