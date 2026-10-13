"""Geometry quality analysis for goniometer angle predictions."""

import argparse
import csv
import math
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from predict_angle import (
    CLASS_NAMES,
    FIXED_ARM,
    MOVING_ARM,
    PIVOT,
    angle_between,
    collect_masks,
    estimate_angle,
    fill_missing_masks,
    fit_direction_pca,
    image_id_from_name,
    list_images,
    load_gt,
    mask_points,
    select_arm_points,
)


def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except ValueError:
        return None


def ellipse_features(mask):
    pts = mask_points(mask)
    if pts is None or len(pts) < 20:
        return {"area": 0, "axis_ratio": "", "ellipse_angle": "", "ellipse_residual": ""}

    area = int(len(pts))
    if len(pts) < 5:
        return {"area": area, "axis_ratio": "", "ellipse_angle": "", "ellipse_residual": ""}

    contour_pts = pts.astype(np.float32).reshape(-1, 1, 2)
    ellipse = cv2.fitEllipse(contour_pts)
    (_, _), (a, b), angle = ellipse
    major = max(float(a), float(b))
    minor = min(float(a), float(b))
    axis_ratio = minor / major if major > 1e-6 else None

    center = np.array(ellipse[0], dtype=np.float32)
    theta = math.radians(float(angle))
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    rel = pts - center[None, :]
    xr = rel[:, 0] * cos_t + rel[:, 1] * sin_t
    yr = -rel[:, 0] * sin_t + rel[:, 1] * cos_t
    major_r = major / 2.0
    minor_r = minor / 2.0
    if major_r <= 1e-6 or minor_r <= 1e-6:
        residual = None
    else:
        residual = float(np.mean(np.abs((xr / major_r) ** 2 + (yr / minor_r) ** 2 - 1.0)))

    return {
        "area": area,
        "axis_ratio": f"{axis_ratio:.4f}" if axis_ratio is not None else "",
        "ellipse_angle": f"{float(angle):.3f}",
        "ellipse_residual": f"{residual:.4f}" if residual is not None else "",
    }


def line_stability(points, pivot, direction):
    if points is None or direction is None or len(points) < 20:
        return {"span": "", "perp_p75": "", "perp_p90": "", "slenderness": ""}
    direction = direction / (np.linalg.norm(direction) + 1e-8)
    vecs = points - pivot[None, :]
    proj = np.dot(vecs, direction)
    perp = np.abs(vecs[:, 0] * direction[1] - vecs[:, 1] * direction[0])
    pos = proj > 0
    if pos.sum() >= 20:
        proj = proj[pos]
        perp = perp[pos]
    span = float(np.percentile(proj, 95) - np.percentile(proj, 5))
    perp_p75 = float(np.percentile(perp, 75))
    perp_p90 = float(np.percentile(perp, 90))
    slenderness = span / (perp_p75 + 1e-6)
    return {
        "span": f"{span:.3f}",
        "perp_p75": f"{perp_p75:.3f}",
        "perp_p90": f"{perp_p90:.3f}",
        "slenderness": f"{slenderness:.3f}",
    }


def fixed_width_stability(mask, pivot, direction):
    pts = mask_points(mask)
    if pts is None or direction is None or len(pts) < 50:
        return {"fixed_width_cv": "", "fixed_width_slope": ""}
    direction = direction / (np.linalg.norm(direction) + 1e-8)
    vecs = pts - pivot[None, :]
    proj = np.dot(vecs, direction)
    perp = vecs[:, 0] * direction[1] - vecs[:, 1] * direction[0]
    keep = proj > max(8.0, np.percentile(proj, 10))
    proj = proj[keep]
    perp = perp[keep]
    if len(proj) < 50:
        return {"fixed_width_cv": "", "fixed_width_slope": ""}

    lo, hi = np.percentile(proj, [10, 90])
    bins = np.linspace(lo, hi, 9)
    centers = []
    widths = []
    for a, b in zip(bins[:-1], bins[1:]):
        seg = perp[(proj >= a) & (proj < b)]
        if len(seg) < 10:
            continue
        centers.append((a + b) / 2.0)
        widths.append(float(np.percentile(seg, 90) - np.percentile(seg, 10)))
    if len(widths) < 3:
        return {"fixed_width_cv": "", "fixed_width_slope": ""}
    widths = np.asarray(widths, dtype=np.float32)
    centers = np.asarray(centers, dtype=np.float32)
    cv = float(widths.std() / (widths.mean() + 1e-6))
    slope = float(np.polyfit(centers, widths, 1)[0])
    return {"fixed_width_cv": f"{cv:.4f}", "fixed_width_slope": f"{slope:.4f}"}


def blank_edge_features(prefix):
    return {
        f"{prefix}_edge_bins": "",
        f"{prefix}_edge_parallel_angle_diff": "",
        f"{prefix}_edge_width_cv": "",
        f"{prefix}_edge_width_change": "",
        f"{prefix}_edge_midline_slope": "",
    }


def parallel_edge_features(mask, pivot, direction, prefix):
    pts = mask_points(mask)
    if pts is None or pivot is None or direction is None or len(pts) < 120:
        return blank_edge_features(prefix)

    direction = direction / (np.linalg.norm(direction) + 1e-8)
    vecs = pts - pivot[None, :]
    proj = np.dot(vecs, direction)
    perp = vecs[:, 0] * direction[1] - vecs[:, 1] * direction[0]
    keep = proj > max(10.0, np.percentile(proj, 8))
    proj = proj[keep]
    perp = perp[keep]
    if len(proj) < 120:
        return blank_edge_features(prefix)

    lo, hi = np.percentile(proj, [8, 94])
    if hi <= lo + 1:
        return blank_edge_features(prefix)

    bin_edges = np.linspace(lo, hi, 17)
    centers = []
    lower = []
    upper = []
    widths = []
    for a, b in zip(bin_edges[:-1], bin_edges[1:]):
        seg = perp[(proj >= a) & (proj < b)]
        if len(seg) < 20:
            continue
        low = float(np.percentile(seg, 8))
        high = float(np.percentile(seg, 92))
        width = high - low
        if width <= 1:
            continue
        centers.append((a + b) / 2.0)
        lower.append(low)
        upper.append(high)
        widths.append(width)

    if len(widths) < 5:
        return blank_edge_features(prefix)

    centers = np.asarray(centers, dtype=np.float32)
    lower = np.asarray(lower, dtype=np.float32)
    upper = np.asarray(upper, dtype=np.float32)
    widths = np.asarray(widths, dtype=np.float32)

    width_limit = np.percentile(widths, 75)
    selected = widths <= width_limit
    if selected.sum() < 5:
        selected = np.ones_like(widths, dtype=bool)

    c = centers[selected]
    lo_edge = lower[selected]
    hi_edge = upper[selected]
    w = widths[selected]
    if len(w) < 5:
        return blank_edge_features(prefix)

    lower_slope, _ = np.polyfit(c, lo_edge, 1)
    upper_slope, _ = np.polyfit(c, hi_edge, 1)
    width_slope, _ = np.polyfit(c, w, 1)
    midline = (lo_edge + hi_edge) / 2.0
    midline_slope, _ = np.polyfit(c, midline, 1)

    parallel_angle = abs(math.degrees(math.atan(float(upper_slope))) - math.degrees(math.atan(float(lower_slope))))
    width_change = abs(float(width_slope)) * float(c.max() - c.min()) / (float(w.mean()) + 1e-6)
    width_cv = float(w.std() / (w.mean() + 1e-6))

    return {
        f"{prefix}_edge_bins": int(len(w)),
        f"{prefix}_edge_parallel_angle_diff": f"{parallel_angle:.3f}",
        f"{prefix}_edge_width_cv": f"{width_cv:.4f}",
        f"{prefix}_edge_width_change": f"{width_change:.4f}",
        f"{prefix}_edge_midline_slope": f"{float(midline_slope):.5f}",
    }


def pivot_circularity_features(mask):
    blank = {
        "pivot_contour_circularity": "",
        "pivot_radius_cv": "",
        "pivot_radius_range_ratio": "",
        "pivot_contour_points": "",
    }
    if mask is None:
        return blank
    mask_u8 = (mask > 0).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return blank
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    perimeter = float(cv2.arcLength(contour, True))
    if area <= 1 or perimeter <= 1:
        return blank
    circularity = 4.0 * math.pi * area / (perimeter * perimeter)
    pts = contour.reshape(-1, 2).astype(np.float32)
    center = pts.mean(axis=0)
    radii = np.linalg.norm(pts - center[None, :], axis=1)
    mean_radius = float(radii.mean())
    if mean_radius <= 1e-6:
        return blank
    radius_cv = float(radii.std() / mean_radius)
    radius_range = float((np.percentile(radii, 95) - np.percentile(radii, 5)) / mean_radius)
    return {
        "pivot_contour_circularity": f"{circularity:.4f}",
        "pivot_radius_cv": f"{radius_cv:.4f}",
        "pivot_radius_range_ratio": f"{radius_range:.4f}",
        "pivot_contour_points": int(len(pts)),
    }


def angle_diff(a, b):
    if a is None or b is None:
        return ""
    return f"{min(angle_between(a, b), angle_between(a, -b)):.3f}"


def load_predictions(path):
    rows = {}
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                image_id = int(row["image_id"])
            except (KeyError, ValueError):
                continue
            rows[image_id] = row
    return rows


def structured_quality_scores(row):
    reasons = []

    fixed_conf = safe_float(row.get("fixed_arm_conf"))
    moving_conf = safe_float(row.get("moving_arm_conf"))
    pivot_conf = safe_float(row.get("pivot_conf"))
    confs = [v for v in [fixed_conf, moving_conf, pivot_conf] if v is not None]
    min_conf = min(confs) if confs else 0.0
    segmentation_quality = 100.0
    if min_conf < 0.35:
        segmentation_quality -= 45
        reasons.append("very_low_mask_conf")
    elif min_conf < 0.55:
        segmentation_quality -= 25
        reasons.append("low_mask_conf")
    elif min_conf < 0.75:
        segmentation_quality -= 10
        reasons.append("moderate_mask_conf")
    if row.get("fallback_used", ""):
        segmentation_quality -= 25
        reasons.append("fallback_mask")
    segmentation_quality = max(0.0, min(100.0, segmentation_quality))

    geometry_fit_quality = 100.0
    consensus = safe_float(row.get("fixed_consensus_count"))
    candidates = safe_float(row.get("fixed_candidate_count"))
    if consensus is not None and candidates:
        consensus_ratio = consensus / candidates
        if consensus_ratio < 0.45:
            geometry_fit_quality -= 30
            reasons.append("low_fixed_consensus")
        elif consensus_ratio < 0.75:
            geometry_fit_quality -= 12
            reasons.append("moderate_fixed_consensus")
    old_new = safe_float(row.get("fixed_old_vs_new_angle_diff"))
    if old_new is not None:
        if old_new >= 8:
            geometry_fit_quality -= 25
            reasons.append("large_fixed_fit_disagreement")
        elif old_new >= 5:
            geometry_fit_quality -= 15
            reasons.append("fixed_fit_disagreement")
    fixed_edge_parallel = safe_float(row.get("fixed_edge_parallel_angle_diff"))
    fixed_edge_width_change = safe_float(row.get("fixed_edge_width_change"))
    moving_edge_parallel = safe_float(row.get("moving_edge_parallel_angle_diff"))
    moving_edge_width_change = safe_float(row.get("moving_edge_width_change"))
    if fixed_edge_parallel is not None and fixed_edge_parallel >= 3.5:
        geometry_fit_quality -= 18
        reasons.append("fixed_edges_not_parallel")
    if fixed_edge_width_change is not None and fixed_edge_width_change >= 0.16:
        geometry_fit_quality -= 18
        reasons.append("fixed_width_changes")
    if moving_edge_parallel is not None and moving_edge_parallel >= 1.0:
        geometry_fit_quality -= 10
        reasons.append("moving_edges_not_parallel")
    if moving_edge_width_change is not None and moving_edge_width_change >= 0.10:
        geometry_fit_quality -= 10
        reasons.append("moving_width_changes")
    geometry_fit_quality = max(0.0, min(100.0, geometry_fit_quality))

    perspective_risk = 0.0
    if fixed_edge_parallel is not None:
        perspective_risk += min(25.0, fixed_edge_parallel / 5.0 * 25.0)
    if fixed_edge_width_change is not None:
        perspective_risk += min(25.0, fixed_edge_width_change / 0.35 * 25.0)
    if moving_edge_parallel is not None:
        perspective_risk += min(15.0, moving_edge_parallel / 3.0 * 15.0)
    if moving_edge_width_change is not None:
        perspective_risk += min(15.0, moving_edge_width_change / 0.25 * 15.0)
    pivot_axis = safe_float(row.get("pivot_axis_ratio"))
    if pivot_axis is not None:
        perspective_risk += min(15.0, max(0.0, (0.95 - pivot_axis) / 0.30 * 15.0))
    pivot_circularity = safe_float(row.get("pivot_contour_circularity"))
    if pivot_circularity is not None:
        perspective_risk += min(10.0, max(0.0, (0.75 - pivot_circularity) / 0.25 * 10.0))
    perspective_risk = max(0.0, min(100.0, perspective_risk))

    correction_feasibility = min(segmentation_quality, geometry_fit_quality)
    if perspective_risk < 20:
        correction_feasibility = min(correction_feasibility, 60.0)
    if row.get("fallback_used", ""):
        correction_feasibility -= 15
    correction_feasibility = max(0.0, min(100.0, correction_feasibility))

    if segmentation_quality < 55 or geometry_fit_quality < 45:
        final_risk_level = "high"
        recommended_action = "reject_or_retake"
    elif perspective_risk >= 45 and correction_feasibility >= 55:
        final_risk_level = "medium"
        recommended_action = "apply_or_test_correction"
    elif perspective_risk >= 25:
        final_risk_level = "medium"
        recommended_action = "review_or_warn"
    else:
        final_risk_level = "low"
        recommended_action = "accept"

    return {
        "segmentation_quality_score": f"{segmentation_quality:.1f}",
        "geometry_fit_quality_score": f"{geometry_fit_quality:.1f}",
        "perspective_risk_score": f"{perspective_risk:.1f}",
        "correction_feasibility_score": f"{correction_feasibility:.1f}",
        "final_risk_level": final_risk_level,
        "recommended_action": recommended_action,
        "structured_risk_reasons": ";".join(dict.fromkeys(reasons)),
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze geometric quality and uncertainty features")
    parser.add_argument("--source", required=True, help="Image file or folder")
    parser.add_argument("--model", default="runs/goniometer_seg-5/weights/best.pt")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--fallback-conf", type=float, default=0.05)
    parser.add_argument("--gt", default="../angle_gt_sample_autosave.csv")
    parser.add_argument("--predictions", default="runs/predict_angle_final/predictions.csv")
    parser.add_argument("--out", default="runs/quality_analysis/quality_features.csv")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    model_path = Path(args.model)
    if not model_path.is_absolute():
        model_path = root / model_path
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pred_path = Path(args.predictions)
    if not pred_path.is_absolute():
        pred_path = root / pred_path
    gt_path = Path(args.gt)
    gt = load_gt(gt_path if gt_path.is_absolute() else root / gt_path)
    predictions = load_predictions(pred_path)

    model = YOLO(str(model_path))
    images = list_images(args.source)
    rows = []

    print(f"Images: {len(images)}")
    print(f"Output: {out_path}")

    for img_path in images:
        image = cv2.imread(str(img_path))
        if image is None:
            continue
        image_id = image_id_from_name(img_path)
        predict_conf = min(args.conf, args.fallback_conf)
        result = model.predict(str(img_path), conf=predict_conf, verbose=False)[0]
        primary_masks = collect_masks(result, image.shape, args.conf)
        fallback_masks = collect_masks(result, image.shape, args.fallback_conf)
        masks_by_cls, fallback_used = fill_missing_masks(primary_masks, fallback_masks)
        angle, debug = estimate_angle(image, masks_by_cls, fixed_fit="robust")
        pred_row = predictions.get(image_id, {})

        row = {
            "image_id": image_id if image_id is not None else "",
            "angle_pred": pred_row.get("angle_pred", f"{angle:.3f}" if angle is not None else ""),
            "angle_gt": f"{gt[image_id]:.3f}" if image_id in gt else "",
            "abs_error": pred_row.get("abs_error", ""),
            "fallback_used": ";".join(fallback_used),
            "status": pred_row.get("status", "ok" if angle is not None else (debug or {}).get("error", "error")),
        }

        for cls_id, cls_name in enumerate(CLASS_NAMES):
            item = masks_by_cls.get(cls_id)
            row[f"{cls_name}_conf"] = f"{item['conf']:.4f}" if item else ""
            row[f"{cls_name}_area"] = int(mask_points(item["mask"]).shape[0]) if item and mask_points(item["mask"]) is not None else ""

        if angle is not None and debug:
            fixed_dir = debug["directions"].get(FIXED_ARM)
            moving_dir = debug["directions"].get(MOVING_ARM)
            baseline_fixed = debug.get("baseline_directions", {}).get(FIXED_ARM)
            row["fixed_candidate_count"] = debug.get("fixed_candidates", "")
            row["fixed_consensus_count"] = debug.get("fixed_consensus", "")
            row["fixed_best_score"] = f"{debug.get('fixed_best_score', 0):.3f}" if debug.get("fixed_best_score") is not None else ""
            row["fixed_old_vs_new_angle_diff"] = angle_diff(fixed_dir, baseline_fixed)

            fixed_points = debug.get("used_points", {}).get(FIXED_ARM)
            moving_points = debug.get("used_points", {}).get(MOVING_ARM)
            pivot = debug.get("pivot")
            for prefix, points, direction in [("fixed", fixed_points, fixed_dir), ("moving", moving_points, moving_dir)]:
                stability = line_stability(points, pivot, direction)
                row[f"{prefix}_span"] = stability["span"]
                row[f"{prefix}_perp_p75"] = stability["perp_p75"]
                row[f"{prefix}_perp_p90"] = stability["perp_p90"]
                row[f"{prefix}_slenderness"] = stability["slenderness"]

            if FIXED_ARM in masks_by_cls:
                row.update(fixed_width_stability(masks_by_cls[FIXED_ARM]["mask"], pivot, fixed_dir))
                row.update(parallel_edge_features(masks_by_cls[FIXED_ARM]["mask"], pivot, fixed_dir, "fixed"))
            else:
                row.update(blank_edge_features("fixed"))
            if MOVING_ARM in masks_by_cls:
                row.update(parallel_edge_features(masks_by_cls[MOVING_ARM]["mask"], pivot, moving_dir, "moving"))
            else:
                row.update(blank_edge_features("moving"))
        else:
            row["fixed_candidate_count"] = ""
            row["fixed_consensus_count"] = ""
            row["fixed_best_score"] = ""
            row["fixed_old_vs_new_angle_diff"] = ""
            for prefix in ["fixed", "moving"]:
                row[f"{prefix}_span"] = ""
                row[f"{prefix}_perp_p75"] = ""
                row[f"{prefix}_perp_p90"] = ""
                row[f"{prefix}_slenderness"] = ""
            row["fixed_width_cv"] = ""
            row["fixed_width_slope"] = ""
            row.update(blank_edge_features("fixed"))
            row.update(blank_edge_features("moving"))

        if PIVOT in masks_by_cls:
            pivot_feats = ellipse_features(masks_by_cls[PIVOT]["mask"])
            row["pivot_axis_ratio"] = pivot_feats["axis_ratio"]
            row["pivot_ellipse_angle"] = pivot_feats["ellipse_angle"]
            row["pivot_ellipse_residual"] = pivot_feats["ellipse_residual"]
            row.update(pivot_circularity_features(masks_by_cls[PIVOT]["mask"]))
        else:
            row["pivot_axis_ratio"] = ""
            row["pivot_ellipse_angle"] = ""
            row["pivot_ellipse_residual"] = ""
            row.update(pivot_circularity_features(None))

        row.update(structured_quality_scores(row))
        rows.append(row)

    fieldnames = [
        "image_id",
        "angle_pred",
        "angle_gt",
        "abs_error",
        "status",
        "fallback_used",
        "fixed_arm_conf",
        "moving_arm_conf",
        "pivot_conf",
        "fixed_arm_area",
        "moving_arm_area",
        "pivot_area",
        "fixed_candidate_count",
        "fixed_consensus_count",
        "fixed_best_score",
        "fixed_old_vs_new_angle_diff",
        "fixed_span",
        "fixed_perp_p75",
        "fixed_perp_p90",
        "fixed_slenderness",
        "moving_span",
        "moving_perp_p75",
        "moving_perp_p90",
        "moving_slenderness",
        "fixed_width_cv",
        "fixed_width_slope",
        "fixed_edge_bins",
        "fixed_edge_parallel_angle_diff",
        "fixed_edge_width_cv",
        "fixed_edge_width_change",
        "fixed_edge_midline_slope",
        "moving_edge_bins",
        "moving_edge_parallel_angle_diff",
        "moving_edge_width_cv",
        "moving_edge_width_change",
        "moving_edge_midline_slope",
        "pivot_axis_ratio",
        "pivot_ellipse_angle",
        "pivot_ellipse_residual",
        "pivot_contour_circularity",
        "pivot_radius_cv",
        "pivot_radius_range_ratio",
        "pivot_contour_points",
        "segmentation_quality_score",
        "geometry_fit_quality_score",
        "perspective_risk_score",
        "correction_feasibility_score",
        "final_risk_level",
        "recommended_action",
        "structured_risk_reasons",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
