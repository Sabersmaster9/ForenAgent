"""
YOLO-Seg inference + mask-to-line geometry for goniometer angle reading.

Usage:
    conda activate yolo_train
    python predict_angle.py --source images/test
    python predict_angle.py --source ../raw_image/1.jpg --model runs/goniometer_seg/weights/best.pt
"""

import argparse
import csv
import math
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

CLASS_NAMES = ["fixed_arm", "moving_arm", "pivot"]
FIXED_ARM = 0
MOVING_ARM = 1
PIVOT = 2
COLORS = {
    FIXED_ARM: (0, 180, 0),
    MOVING_ARM: (255, 120, 0),
    PIVOT: (0, 0, 255),
}
BASELINE_COLOR = (255, 255, 255)
FIXED_ARM_WINDOWS = [
    (0.12, 0.55),
    (0.18, 0.65),
    (0.25, 0.75),
    (0.35, 0.90),
    (0.45, 0.98),
    (0.20, 0.85),
    (0.30, 0.95),
]


def list_images(source):
    source = Path(source)
    if source.is_dir():
        exts = {".jpg", ".jpeg", ".png", ".bmp"}
        return sorted(p for p in source.iterdir() if p.suffix.lower() in exts)
    return [source]


def normalize_vector(vector):
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        return None
    return vector / norm


def mask_points(mask):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    return np.column_stack([xs, ys]).astype(np.float32)


def mask_center(mask):
    pts = mask_points(mask)
    if pts is None:
        return None
    return pts.mean(axis=0).astype(np.float32)


def orient_direction(direction, points, pivot):
    direction = normalize_vector(direction)
    if direction is None:
        return None
    projected = np.dot(points - pivot[None, :], direction)
    if len(projected) and abs(np.percentile(projected, 5)) > abs(np.percentile(projected, 95)):
        direction = -direction
    return direction.astype(np.float32)


def select_arm_points(mask, pivot, min_radius_ratio=0.12, far_quantile=0.35):
    pts = mask_points(mask)
    if pts is None or len(pts) < 20:
        return None
    dists = np.linalg.norm(pts - pivot[None, :], axis=1)
    max_dist = float(dists.max())
    if max_dist <= 1:
        return None

    keep = dists > max(12.0, max_dist * min_radius_ratio)
    pts = pts[keep]
    dists = dists[keep]
    if len(pts) < 20:
        return None

    threshold = np.quantile(dists, far_quantile)
    far_pts = pts[dists >= threshold]
    if len(far_pts) >= 20:
        pts = far_pts
    return pts


def fit_direction_pca(points, pivot):
    centered = points - points.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return orient_direction(vt[0], points, pivot)


def fit_direction_weighted_pca(points, pivot, weights):
    weights = np.asarray(weights, dtype=np.float32)
    weight_sum = float(weights.sum())
    if len(points) < 20 or weight_sum <= 1e-8:
        return None
    mean = (points * weights[:, None]).sum(axis=0, keepdims=True) / weight_sum
    centered = points - mean
    cov = (centered * weights[:, None]).T @ centered / weight_sum
    eigvals, eigvecs = np.linalg.eigh(cov)
    direction = eigvecs[:, int(np.argmax(eigvals))]
    return orient_direction(direction, points, pivot)


def weighted_median(values, weights):
    order = np.argsort(values)
    values = values[order]
    weights = weights[order]
    cutoff = weights.sum() * 0.5
    idx = int(np.searchsorted(np.cumsum(weights), cutoff, side="left"))
    idx = min(idx, len(values) - 1)
    return float(values[idx])


def fit_direction_radial_median(points, pivot):
    vecs = points - pivot[None, :]
    dists = np.linalg.norm(vecs, axis=1)
    keep = dists > 1.0
    if keep.sum() < 20:
        return None
    vecs = vecs[keep]
    dists = dists[keep]
    unit = vecs / dists[:, None]
    weights = np.maximum((dists / (dists.max() + 1e-8)) ** 1.5, 1e-3)
    base_vec = normalize_vector((unit * weights[:, None]).sum(axis=0))
    if base_vec is None:
        return None
    angles = np.arctan2(unit[:, 1], unit[:, 0])
    base = math.atan2(float(base_vec[1]), float(base_vec[0]))
    deltas = np.arctan2(np.sin(angles - base), np.cos(angles - base))
    theta = base + weighted_median(deltas, weights)
    return normalize_vector(np.array([math.cos(theta), math.sin(theta)], dtype=np.float32))


def angle_between(v1, v2):
    v1 = v1 / (np.linalg.norm(v1) + 1e-8)
    v2 = v2 / (np.linalg.norm(v2) + 1e-8)
    cos_a = float(np.clip(np.dot(v1, v2), -1.0, 1.0))
    angle = math.degrees(math.acos(cos_a))
    return angle


def score_direction(points, pivot, direction):
    direction = normalize_vector(direction)
    if direction is None:
        return None
    vecs = points - pivot[None, :]
    proj = np.dot(vecs, direction)
    positive = proj > 0
    if positive.sum() < 20:
        return None
    vecs = vecs[positive]
    proj = proj[positive]
    perp = np.abs(vecs[:, 0] * direction[1] - vecs[:, 1] * direction[0])
    span = float(np.percentile(proj, 96) - np.percentile(proj, 4))
    if span <= 1:
        return None
    lateral = float(np.percentile(perp, 65) + 0.5 * np.percentile(perp, 85))
    max_proj = float(np.percentile(proj, 98))
    near_width = max(6.0, min(28.0, max_proj * 0.04))
    near_ratio = float((perp <= near_width).sum()) / float(len(points))
    positive_ratio = float(positive.sum()) / float(len(points))
    score = span * math.sqrt(float(positive.sum())) * (0.35 + near_ratio) * positive_ratio / (lateral + 4.0)
    return score


def candidate_angle_diff(a, b):
    return min(angle_between(a, b), angle_between(a, -b))


def fit_fixed_arm_robust(mask, pivot):
    pts = mask_points(mask)
    if pts is None or len(pts) < 30:
        return None, None, {"fixed_fit": "robust_failed_empty"}

    dists = np.linalg.norm(pts - pivot[None, :], axis=1)
    max_dist = float(dists.max())
    if max_dist <= 1:
        return None, None, {"fixed_fit": "robust_failed_short"}

    near = max(12.0, max_dist * 0.10)
    fit_pts = pts[dists > near]
    fit_dists = dists[dists > near]
    if len(fit_pts) < 30:
        return None, None, {"fixed_fit": "robust_failed_near"}

    candidates = []
    for low, high in FIXED_ARM_WINDOWS:
        low_dist = max(near, max_dist * low)
        high_dist = max_dist * high
        segment = fit_pts[(fit_dists >= low_dist) & (fit_dists <= high_dist)]
        if len(segment) < 30:
            continue
        segment_dists = np.linalg.norm(segment - pivot[None, :], axis=1)
        weights = np.maximum((segment_dists / (max_dist + 1e-8)) ** 1.8, 1e-3)
        for method, direction in (
            ("weighted_pca", fit_direction_weighted_pca(segment, pivot, weights)),
            ("radial_median", fit_direction_radial_median(segment, pivot)),
        ):
            if direction is None:
                continue
            score = score_direction(fit_pts, pivot, direction)
            if score is None:
                continue
            candidates.append({"direction": direction, "score": score, "points": segment, "method": method})

    baseline_pts = select_arm_points(mask, pivot)
    baseline_direction = fit_direction_pca(baseline_pts, pivot) if baseline_pts is not None else None
    if baseline_direction is not None:
        score = score_direction(fit_pts, pivot, baseline_direction)
        if score is not None:
            candidates.append({"direction": baseline_direction, "score": score * 0.85, "points": baseline_pts, "method": "baseline_pca"})

    if not candidates:
        return baseline_direction, baseline_pts, {"fixed_fit": "baseline_fallback", "baseline_direction": baseline_direction}

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]
    consensus = [
        item
        for item in candidates
        if item["score"] >= best["score"] * 0.55 and candidate_angle_diff(item["direction"], best["direction"]) <= 12.0
    ]
    vec = np.zeros(2, dtype=np.float32)
    for item in consensus:
        direction = item["direction"]
        if np.dot(direction, best["direction"]) < 0:
            direction = -direction
        vec += direction * float(item["score"])
    direction = normalize_vector(vec)
    if direction is None:
        direction = best["direction"]

    debug = {
        "fixed_fit": "robust",
        "fixed_candidates": len(candidates),
        "fixed_consensus": len(consensus),
        "fixed_best_score": float(best["score"]),
        "fixed_best_method": best["method"],
        "baseline_direction": baseline_direction,
    }
    return direction, best["points"], debug


def resize_mask(mask, shape_hw):
    h, w = shape_hw
    if mask.shape[:2] == (h, w):
        return mask
    return cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)


def collect_masks(result, image_shape, conf_threshold):
    masks_by_cls = {}
    if result.masks is None or result.boxes is None:
        return masks_by_cls

    h, w = image_shape[:2]
    masks = result.masks.data.cpu().numpy()
    classes = result.boxes.cls.cpu().numpy().astype(int)
    confs = result.boxes.conf.cpu().numpy()

    for mask, cls_id, conf in zip(masks, classes, confs):
        if cls_id not in (FIXED_ARM, MOVING_ARM, PIVOT):
            continue
        if conf < conf_threshold:
            continue
        mask = resize_mask(mask > 0.5, (h, w)).astype(np.uint8)
        current = masks_by_cls.get(cls_id)
        if current is None or conf > current["conf"]:
            masks_by_cls[cls_id] = {"mask": mask, "conf": float(conf)}
    return masks_by_cls


def fill_missing_masks(primary_masks, fallback_masks):
    masks_by_cls = dict(primary_masks)
    fallback_used = []
    for cls_id in (FIXED_ARM, MOVING_ARM, PIVOT):
        if cls_id in masks_by_cls or cls_id not in fallback_masks:
            continue
        masks_by_cls[cls_id] = fallback_masks[cls_id]
        fallback_used.append(CLASS_NAMES[cls_id])
    return masks_by_cls, fallback_used


def estimate_angle(image, masks_by_cls, fixed_fit="robust"):
    required = [FIXED_ARM, MOVING_ARM, PIVOT]
    missing = [CLASS_NAMES[i] for i in required if i not in masks_by_cls]
    if missing:
        return None, {"error": f"missing masks: {missing}"}

    pivot = mask_center(masks_by_cls[PIVOT]["mask"])
    if pivot is None:
        return None, {"error": "empty pivot mask"}

    directions = {}
    used_points = {}
    baseline_directions = {}

    fixed_pts = select_arm_points(masks_by_cls[FIXED_ARM]["mask"], pivot)
    baseline_fixed = fit_direction_pca(fixed_pts, pivot) if fixed_pts is not None else None
    if baseline_fixed is not None:
        baseline_directions[FIXED_ARM] = baseline_fixed

    fixed_debug = {}
    if fixed_fit == "pca":
        if fixed_pts is None or baseline_fixed is None:
            return None, {"error": "not enough fixed_arm points"}
        directions[FIXED_ARM] = baseline_fixed
        used_points[FIXED_ARM] = fixed_pts
        fixed_debug["fixed_fit"] = "pca"
    else:
        direction, pts, fixed_debug = fit_fixed_arm_robust(masks_by_cls[FIXED_ARM]["mask"], pivot)
        if direction is None or pts is None:
            return None, {"error": "not enough fixed_arm points"}
        directions[FIXED_ARM] = direction
        used_points[FIXED_ARM] = pts

    moving_pts = select_arm_points(masks_by_cls[MOVING_ARM]["mask"], pivot)
    if moving_pts is None:
        return None, {"error": "not enough moving_arm points"}
    moving_direction = fit_direction_pca(moving_pts, pivot)
    if moving_direction is None:
        return None, {"error": "not enough moving_arm points"}
    directions[MOVING_ARM] = moving_direction
    used_points[MOVING_ARM] = moving_pts

    angle = angle_between(directions[FIXED_ARM], directions[MOVING_ARM])
    debug = {
        "pivot": pivot,
        "directions": directions,
        "baseline_directions": baseline_directions,
        "used_points": used_points,
        "conf": {CLASS_NAMES[k]: v["conf"] for k, v in masks_by_cls.items()},
    }
    debug.update(fixed_debug)
    return angle, debug


def draw_overlay(image, masks_by_cls, angle, debug, error=None):
    out = image.copy()
    overlay = image.copy()

    for cls_id, item in masks_by_cls.items():
        color = COLORS[cls_id]
        mask = item["mask"].astype(bool)
        overlay[mask] = color
    out = cv2.addWeighted(overlay, 0.35, out, 0.65, 0)

    if debug and "pivot" in debug:
        pivot = debug["pivot"].astype(int)
        cv2.circle(out, tuple(pivot), 8, COLORS[PIVOT], -1, cv2.LINE_AA)
        line_len = int(max(image.shape[:2]) * 0.45)
        for cls_id, direction in debug.get("baseline_directions", {}).items():
            p2 = (debug["pivot"] + direction * line_len).astype(int)
            cv2.line(out, tuple(pivot), tuple(p2), BASELINE_COLOR, 2, cv2.LINE_AA)
            cv2.putText(
                out,
                "old_pca",
                tuple((debug["pivot"] + direction * min(line_len, 210)).astype(int)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                BASELINE_COLOR,
                2,
                cv2.LINE_AA,
            )
        for cls_id, direction in debug["directions"].items():
            p1 = pivot
            p2 = (debug["pivot"] + direction * line_len).astype(int)
            cv2.line(out, tuple(p1), tuple(p2), COLORS[cls_id], 5, cv2.LINE_AA)
            cv2.putText(
                out,
                CLASS_NAMES[cls_id],
                tuple((debug["pivot"] + direction * min(line_len, 260)).astype(int)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                COLORS[cls_id],
                2,
                cv2.LINE_AA,
            )

    if angle is not None:
        text = f"angle={angle:.1f} deg"
    else:
        text = f"ERROR: {error}"
    cv2.putText(out, text, (24, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3, cv2.LINE_AA)
    if debug and debug.get("fixed_fit"):
        cv2.putText(out, f"fixed_fit={debug['fixed_fit']}", (24, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
    if debug and debug.get("fallback_used"):
        cv2.putText(out, f"fallback={','.join(debug['fallback_used'])}", (24, 122), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
    return out


def load_gt(path):
    if not path:
        return {}
    gt = {}
    csv_path = Path(path)
    if not csv_path.exists():
        return gt
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            image_id = (row.get("image_id") or "").strip()
            angle_gt = (row.get("angle_gt") or "").strip()
            if not image_id or not angle_gt:
                continue
            try:
                gt[int(image_id)] = float(angle_gt)
            except ValueError:
                continue
    return gt


def image_id_from_name(path):
    try:
        return int(Path(path).stem)
    except ValueError:
        return None


def write_error_report(rows, save_dir):
    report_rows = [
        {
            "image_id": row["image_id"],
            "angle_gt": row["angle_gt"],
            "angle_pred": row["angle_pred"],
            "abs_error": row["abs_error"],
            "status": row["status"],
            "fallback_used": row["fallback_used"],
            "notes": "",
        }
        for row in rows
        if row["angle_gt"] or row["status"] != "ok"
    ]

    def sort_key(row):
        if row["abs_error"]:
            return (1, -float(row["abs_error"]))
        return (0, 0.0)

    report_rows.sort(key=sort_key)
    report_path = save_dir / "error_report_autosave.csv"
    with report_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["image_id", "angle_gt", "angle_pred", "abs_error", "status", "fallback_used", "notes"])
        writer.writeheader()
        writer.writerows(report_rows)
    return report_path


def main():
    parser = argparse.ArgumentParser(description="YOLO-Seg goniometer angle inference")
    parser.add_argument("--source", required=True, help="Image file or folder")
    parser.add_argument("--model", default="runs/goniometer_seg/weights/best.pt", help="Path to weights under seg_v1 or absolute path")
    parser.add_argument("--conf", type=float, default=0.35, help="Detection confidence threshold")
    parser.add_argument("--save-dir", default=None, help="Output folder")
    parser.add_argument("--gt", default="../angle_gt_sample_autosave.csv", help="Optional CSV with image_id,angle_gt,notes")
    parser.add_argument("--fallback-conf", type=float, default=0.05, help="Lower confidence threshold used only to fill missing masks")
    parser.add_argument("--fixed-fit", choices=["robust", "pca"], default="robust", help="fixed_arm line fitting method")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    model_path = Path(args.model)
    if not model_path.is_absolute():
        model_path = root / model_path
    if not model_path.exists():
        print(f"ERROR: Model not found at {model_path}")
        print("Train first: python train_seg.py")
        return

    save_dir = Path(args.save_dir) if args.save_dir else root / "runs" / "predict_angle"
    save_dir.mkdir(parents=True, exist_ok=True)
    gt_path = Path(args.gt)
    gt = load_gt(gt_path if gt_path.is_absolute() else root / gt_path)

    model = YOLO(str(model_path))
    images = list_images(args.source)
    rows = []

    print(f"Model: {model_path}")
    print(f"Images: {len(images)}")
    print(f"Save dir: {save_dir}")
    print(f"Fixed fit: {args.fixed_fit}")
    print(f"Fallback conf: {args.fallback_conf}")
    print("-" * 50)

    for img_path in images:
        image = cv2.imread(str(img_path))
        if image is None:
            print(f"SKIP read error: {img_path}")
            continue

        predict_conf = min(args.conf, args.fallback_conf)
        results = model.predict(str(img_path), conf=predict_conf, verbose=False)
        result = results[0]
        primary_masks = collect_masks(result, image.shape, args.conf)
        fallback_masks = collect_masks(result, image.shape, args.fallback_conf)
        masks_by_cls, fallback_used = fill_missing_masks(primary_masks, fallback_masks)
        angle, debug = estimate_angle(image, masks_by_cls, fixed_fit=args.fixed_fit)
        if debug is not None:
            debug["fallback_used"] = fallback_used
        error = debug.get("error") if debug else None

        image_id = image_id_from_name(img_path)
        gt_angle = gt.get(image_id)
        abs_err = abs(angle - gt_angle) if angle is not None and gt_angle is not None else None
        vis = draw_overlay(image, masks_by_cls, angle, debug if angle is not None else None, error)
        cv2.imwrite(str(save_dir / img_path.name), vis)

        rows.append(
            {
                "image": img_path.name,
                "image_id": image_id if image_id is not None else "",
                "angle_pred": f"{angle:.3f}" if angle is not None else "",
                "angle_gt": f"{gt_angle:.3f}" if gt_angle is not None else "",
                "abs_error": f"{abs_err:.3f}" if abs_err is not None else "",
                "status": "ok" if angle is not None else error,
                "fallback_used": ";".join(fallback_used),
            }
        )

        if angle is None:
            print(f"{img_path.name}: ERROR {error}")
        elif abs_err is None:
            print(f"{img_path.name}: angle={angle:.1f}")
        else:
            print(f"{img_path.name}: angle={angle:.1f}, gt={gt_angle:.1f}, err={abs_err:.1f}")

    csv_path = save_dir / "predictions.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["image", "image_id", "angle_pred", "angle_gt", "abs_error", "status", "fallback_used"])
        writer.writeheader()
        writer.writerows(rows)

    report_path = write_error_report(rows, save_dir)
    errors = [float(row["abs_error"]) for row in rows if row["abs_error"]]
    if errors:
        print("-" * 50)
        print(f"GT samples: {len(errors)}")
        print(f"MAE: {sum(errors) / len(errors):.2f} deg")
        print(f"<=5 deg: {sum(e <= 5 for e in errors)}/{len(errors)}")
    print(f"Results saved to: {save_dir}")
    print(f"CSV: {csv_path}")
    print(f"Error report: {report_path}")


if __name__ == "__main__":
    main()
