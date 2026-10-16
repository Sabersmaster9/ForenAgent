"""visual_agent — runtime goniometer image reader.

This module is the runtime bridge between ROM photos and the existing
Judge/Ruler schema. Geometry/quality helpers live in
``agents/visual_agent/goniometer``, weights in ``models/``; this module only
performs case-level inference and emits ``structured_rom`` for downstream agents.
"""

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.common.output_paths import resolve_numbered_output_dir

# Windows/Conda 下 OpenCV、torch、ultralytics 可能各自携带 OpenMP runtime；
# 这里在首次导入视觉依赖前设置，避免 GUI 一键运行时因 libiomp5md.dll 重复初始化而退出。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
GONIOMETER_RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goniometer")
DEFAULT_MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "goniometer_seg_best.pt")


def resolve_visual_device() -> str:
    """决定 YOLO 推理设备。

    auto（默认）→ 有 CUDA 就用 cuda:0，否则 cpu。可用 FORENAGENT_VISUAL_DEVICE
    强制指定（cuda/cpu/cuda:1…）。显式传 device 而不是让 ultralytics 自己猜，
    是为了能在日志里明确看到实际用了什么——torch 装成 +cpu 时它只会静默走 CPU。
    """
    requested = (os.getenv("FORENAGENT_VISUAL_DEVICE") or "auto").strip().lower()
    if requested in {"cpu"}:
        return "cpu"
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            if requested in {"auto", "gpu", "cuda"}:
                return "cuda:0"
            return requested
        return "cpu"
    except Exception:
        return "cpu"


VISUAL_DEVICE = resolve_visual_device()


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
JOINT_NAMES = ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节", "踝关节"]
JOINT_SHORT_TO_FULL = {"肩": "肩关节", "肘": "肘关节", "腕": "腕关节", "髋": "髋关节", "膝": "膝关节", "踝": "踝关节"}
DIRECTION_KEYWORDS = [
    "前屈上举", "外展上举", "水平位内旋", "水平位外旋",
    "前屈", "后伸", "外展", "内收", "内旋", "外旋",
    "屈曲", "伸展", "掌屈", "背伸", "桡偏", "尺偏", "背屈", "跖屈",
]

GRADED_POLICY = {
    "p1": 30.0,
    "p2": 35.0,
    "p3": 65.0,
    "a1": 0.7,
    "a2": 0.95,
    "a3": 1.05,
    "min_delta": 0.5,
    "max_delta": 4.5,
    "spread_soft": 20.0,
    "spread_penalty": 0.75,
    "fixed_width_soft": 0.55,
    "width_penalty": 0.65,
    "fit_soft": 55.0,
    "feas_soft": 55.0,
    "quality_penalty": 0.75,
    "overcorr_penalty": 0.25,
    "fit_min": 35.0,
    "feas_min": 35.0,
    "spread_max_for_arc": 50.0,
}


def _safe_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _collect_images(input_dir: str) -> List[str]:
    root = Path(input_dir)
    if not root.exists():
        return []
    if root.is_file():
        return [str(root)] if root.suffix.lower() in IMAGE_EXTENSIONS else []
    images = [str(path) for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    return sorted(images, key=lambda value: value.lower())


def infer_measurement_context(image_path: str) -> Dict[str, str]:
    """Infer joint/side/direction from folder and file names.

    This is intentionally conservative. The visual model reads only an angle;
    medical semantics must come from naming convention, nearby Word records, or
    later manual review.
    """
    path = Path(image_path)
    text = " ".join([part for part in path.parts[-4:]])

    joint = ""
    for joint_name in JOINT_NAMES:
        if joint_name in text or joint_name.replace("关节", "") in text:
            joint = joint_name
            break
    if not joint:
        for short, full in JOINT_SHORT_TO_FULL.items():
            if short in text:
                joint = full
                break

    side = ""
    if re.search(r"左侧|左[肩肘腕髋膝踝]?", text):
        side = "左"
    if re.search(r"右侧|右[肩肘腕髋膝踝]?", text):
        side = "右"

    direction = ""
    for keyword in DIRECTION_KEYWORDS:
        if keyword in text:
            direction = keyword
            break

    return {"joint": joint, "side": side, "direction": direction}


def _import_goniometer_runtime():
    if GONIOMETER_RUNTIME_DIR not in sys.path:
        sys.path.insert(0, GONIOMETER_RUNTIME_DIR)
    from predict_angle import collect_masks, estimate_angle, fill_missing_masks  # type: ignore
    from quality_analysis import (  # type: ignore
        CLASS_NAMES,
        FIXED_ARM,
        MOVING_ARM,
        PIVOT,
        angle_diff,
        blank_edge_features,
        ellipse_features,
        fixed_width_stability,
        line_stability,
        mask_points,
        parallel_edge_features,
        pivot_circularity_features,
        structured_quality_scores,
    )

    return {
        "collect_masks": collect_masks,
        "estimate_angle": estimate_angle,
        "fill_missing_masks": fill_missing_masks,
        "CLASS_NAMES": CLASS_NAMES,
        "FIXED_ARM": FIXED_ARM,
        "MOVING_ARM": MOVING_ARM,
        "PIVOT": PIVOT,
        "angle_diff": angle_diff,
        "blank_edge_features": blank_edge_features,
        "ellipse_features": ellipse_features,
        "fixed_width_stability": fixed_width_stability,
        "line_stability": line_stability,
        "mask_points": mask_points,
        "parallel_edge_features": parallel_edge_features,
        "pivot_circularity_features": pivot_circularity_features,
        "structured_quality_scores": structured_quality_scores,
    }


def _build_quality_row(image_id: str, angle: Optional[float], debug: Optional[dict], masks_by_cls: dict, fallback_used: List[str], runtime: dict) -> Dict[str, object]:
    class_names = runtime["CLASS_NAMES"]
    fixed_arm = runtime["FIXED_ARM"]
    moving_arm = runtime["MOVING_ARM"]
    pivot_cls = runtime["PIVOT"]
    mask_points = runtime["mask_points"]

    row: Dict[str, object] = {
        "image_id": image_id,
        "angle_pred": f"{angle:.3f}" if angle is not None else "",
        "fallback_used": ";".join(fallback_used),
        "status": "ok" if angle is not None else (debug or {}).get("error", "error"),
    }

    for cls_id, cls_name in enumerate(class_names):
        item = masks_by_cls.get(cls_id)
        points = mask_points(item["mask"]) if item else None
        row[f"{cls_name}_conf"] = f"{item['conf']:.4f}" if item else ""
        row[f"{cls_name}_area"] = int(points.shape[0]) if points is not None else ""

    if angle is not None and debug:
        fixed_dir = debug["directions"].get(fixed_arm)
        moving_dir = debug["directions"].get(moving_arm)
        baseline_fixed = debug.get("baseline_directions", {}).get(fixed_arm)
        row["fixed_candidate_count"] = debug.get("fixed_candidates", "")
        row["fixed_consensus_count"] = debug.get("fixed_consensus", "")
        row["fixed_best_score"] = f"{debug.get('fixed_best_score', 0):.3f}" if debug.get("fixed_best_score") is not None else ""
        row["fixed_old_vs_new_angle_diff"] = runtime["angle_diff"](fixed_dir, baseline_fixed)

        # 分级补偿路由依赖的两个量：弧线几何修正量与候选分歧度。
        row["arc_delta"] = round(float(debug.get("arc_delta") or 0.0), 3)
        row["candidate_delta_spread"] = round(float(debug.get("candidate_delta_spread") or 0.0), 3)
        row["arc_window_count"] = (debug.get("arc_meta") or {}).get("arc_windows", 0)
        row["moving_candidate_count"] = (debug.get("spread_meta") or {}).get("candidate_count", 0)
        row["baseline_angle"] = round(float(debug.get("baseline_angle") or 0.0), 3)

        fixed_points = debug.get("used_points", {}).get(fixed_arm)
        moving_points = debug.get("used_points", {}).get(moving_arm)
        pivot = debug.get("pivot")
        for prefix, points, direction in [("fixed", fixed_points, fixed_dir), ("moving", moving_points, moving_dir)]:
            stability = runtime["line_stability"](points, pivot, direction)
            row[f"{prefix}_span"] = stability["span"]
            row[f"{prefix}_perp_p75"] = stability["perp_p75"]
            row[f"{prefix}_perp_p90"] = stability["perp_p90"]
            row[f"{prefix}_slenderness"] = stability["slenderness"]

        if fixed_arm in masks_by_cls:
            row.update(runtime["fixed_width_stability"](masks_by_cls[fixed_arm]["mask"], pivot, fixed_dir))
            row.update(runtime["parallel_edge_features"](masks_by_cls[fixed_arm]["mask"], pivot, fixed_dir, "fixed"))
        else:
            row.update(runtime["blank_edge_features"]("fixed"))
        if moving_arm in masks_by_cls:
            row.update(runtime["parallel_edge_features"](masks_by_cls[moving_arm]["mask"], pivot, moving_dir, "moving"))
        else:
            row.update(runtime["blank_edge_features"]("moving"))
    else:
        row["fixed_candidate_count"] = ""
        row["fixed_consensus_count"] = ""
        row["fixed_best_score"] = ""
        row["fixed_old_vs_new_angle_diff"] = ""
        row["arc_delta"] = ""
        row["candidate_delta_spread"] = ""
        row["arc_window_count"] = ""
        row["moving_candidate_count"] = ""
        row["baseline_angle"] = ""
        for prefix in ["fixed", "moving"]:
            row[f"{prefix}_span"] = ""
            row[f"{prefix}_perp_p75"] = ""
            row[f"{prefix}_perp_p90"] = ""
            row[f"{prefix}_slenderness"] = ""
        row["fixed_width_cv"] = ""
        row["fixed_width_slope"] = ""
        row.update(runtime["blank_edge_features"]("fixed"))
        row.update(runtime["blank_edge_features"]("moving"))

    if pivot_cls in masks_by_cls:
        pivot_features = runtime["ellipse_features"](masks_by_cls[pivot_cls]["mask"])
        row["pivot_axis_ratio"] = pivot_features["axis_ratio"]
        row["pivot_ellipse_angle"] = pivot_features["ellipse_angle"]
        row["pivot_ellipse_residual"] = pivot_features["ellipse_residual"]
        row.update(runtime["pivot_circularity_features"](masks_by_cls[pivot_cls]["mask"]))
    else:
        row["pivot_axis_ratio"] = ""
        row["pivot_ellipse_angle"] = ""
        row["pivot_ellipse_residual"] = ""
        row.update(runtime["pivot_circularity_features"](None))

    row.update(runtime["structured_quality_scores"](row))
    return row


def _reject_decision(row: Dict[str, object]) -> Tuple[bool, List[str]]:
    risk = _safe_float(row.get("perspective_risk_score"), 0.0) or 0.0
    fitq = _safe_float(row.get("geometry_fit_quality_score"), 100.0) or 100.0
    spread = _safe_float(row.get("candidate_delta_spread"), 0.0) or 0.0
    fixed_width = _safe_float(row.get("fixed_edge_width_change"), 0.0) or 0.0

    reasons = []
    if fitq < 35 and risk >= 55:
        reasons.append("very_low_fit_quality_with_high_perspective_risk")
    if spread >= 50:
        reasons.append("candidate_predictions_extremely_disagree")
    if fixed_width >= 1.20:
        reasons.append("fixed_arm_width_change_extreme")
    return bool(reasons), reasons


def _alpha_from_policy(row: Dict[str, object]) -> Tuple[float, List[str]]:
    risk = _safe_float(row.get("perspective_risk_score"), 0.0) or 0.0
    fitq = _safe_float(row.get("geometry_fit_quality_score"), 100.0) or 100.0
    feas = _safe_float(row.get("correction_feasibility_score"), 100.0) or 100.0
    spread = _safe_float(row.get("candidate_delta_spread"), 0.0) or 0.0
    fixed_width = _safe_float(row.get("fixed_edge_width_change"), 0.0) or 0.0
    arc_delta = abs(_safe_float(row.get("arc_delta"), 0.0) or 0.0)

    if fitq < GRADED_POLICY["fit_min"] or feas < GRADED_POLICY["feas_min"]:
        return 0.0, ["insufficient_fit_or_feasibility"]
    if arc_delta < GRADED_POLICY["min_delta"] or arc_delta > GRADED_POLICY["max_delta"]:
        return 0.0, ["arc_delta_out_of_range"]
    if spread > GRADED_POLICY["spread_max_for_arc"]:
        return 0.0, ["candidate_spread_too_large_for_arc"]

    if risk < GRADED_POLICY["p1"]:
        alpha = 0.0
        reason = "low_perspective_risk_no_compensation"
    elif risk < GRADED_POLICY["p2"]:
        alpha = GRADED_POLICY["a1"]
        reason = "low_mid_risk_partial_compensation"
    elif risk < GRADED_POLICY["p3"]:
        alpha = GRADED_POLICY["a2"]
        reason = "mid_high_risk_partial_compensation"
    else:
        alpha = GRADED_POLICY["a3"]
        reason = "high_risk_strong_compensation"

    reasons = [reason]
    if spread > GRADED_POLICY["spread_soft"]:
        alpha *= GRADED_POLICY["spread_penalty"]
        reasons.append("large_spread_reduces_alpha")
    if fixed_width > GRADED_POLICY["fixed_width_soft"]:
        alpha *= GRADED_POLICY["width_penalty"]
        reasons.append("fixed_width_change_reduces_alpha")
    if fitq < GRADED_POLICY["fit_soft"] or feas < GRADED_POLICY["feas_soft"]:
        alpha *= GRADED_POLICY["quality_penalty"]
        reasons.append("borderline_quality_reduces_alpha")
    return alpha, reasons


def choose_final_prediction(row: Dict[str, object]) -> Tuple[Optional[float], str, float, List[str], bool]:
    baseline = _safe_float(row.get("angle_pred"))
    if baseline is None:
        return None, "missing", 0.0, ["missing_baseline_prediction"], True

    rejected, reject_reasons = _reject_decision(row)
    if rejected:
        return None, "reject", 0.0, reject_reasons, True

    arc_delta = _safe_float(row.get("arc_delta"), 0.0) or 0.0
    alpha, route_reasons = _alpha_from_policy(row)
    pred = baseline + alpha * arc_delta
    if alpha <= 0:
        route = "baseline"
    elif alpha < 0.75:
        route = "graded_arc_light"
    elif alpha < 1.0:
        route = "graded_arc_medium"
    else:
        route = "graded_arc_strong"
    return pred, route, alpha, route_reasons, False


def _normalize_direction_for_joint(joint: str, direction: str) -> str:
    """Normalize ambiguous motion words so Judge/Ruler use the same ROM keys."""
    joint = str(joint or "")
    direction = str(direction or "").strip()
    if joint == "踝关节" and direction == "背伸":
        return "背屈"
    if joint == "腕关节" and direction == "背屈":
        return "背伸"
    return direction


def _measurement_to_rom_line(item: Dict[str, object]) -> Optional[str]:
    joint = str(item.get("joint") or "")
    side = str(item.get("side") or "")
    direction = _normalize_direction_for_joint(joint, str(item.get("direction") or ""))
    angle = item.get("angle")
    if not joint or not side or not direction or angle in (None, ""):
        return None
    joint_short = joint.replace("关节", "")
    return f"{side}{joint_short}{direction}0度到{float(angle):.1f}度"


def _candidate_key(item: Dict[str, object], index: int) -> Tuple[str, str, str, str]:
    joint = str(item.get("joint") or "")
    side = str(item.get("side") or "")
    direction = _normalize_direction_for_joint(joint, str(item.get("direction") or ""))
    if joint and side and direction:
        return joint, side, direction, ""
    return joint, side, direction, str(index)


def _measurement_angle_candidate(item: Dict[str, object]) -> Optional[float]:
    """Return the best visible angle for GUI review, including rejected-but-detected images."""
    angle = item.get("angle")
    if angle not in (None, ""):
        return float(angle)
    baseline = item.get("baseline_angle")
    if baseline not in (None, ""):
        return float(baseline)
    return None


def _action_evidence_text(joint: str, side: str, direction: str, angle: float, count: int, complete_label: bool, review_only: bool = False) -> str:
    prefix = "视觉模块检出疑似量角器图片" if review_only else "视觉模块检出量角器图片"
    if complete_label:
        return f"{prefix}{count}张，候选读数：{side}{joint}{direction}约{angle:.1f}°；需司法人员复核后才写入报告。"
    return f"{prefix}{count}张，候选角度约{angle:.1f}°，但未能可靠确定关节、侧别或动作；需司法人员标注后才写入报告。"


def _build_visual_actions(measurements: List[Dict[str, object]]) -> List[Dict[str, Any]]:
    """Build GUI review actions from every image where the model saw enough goniometer geometry.

    `ok` images have an accepted angle. `reject` images still have mask/geometry evidence and
    an overlay, so they must be shown to the judicial user as "detected but needs review" rather
    than disappearing from the ROM workflow.
    """
    groups: Dict[Tuple[str, str, str, str], List[Dict[str, object]]] = {}
    for index, item in enumerate(measurements, start=1):
        status = item.get("status")
        if status not in {"ok", "reject"}:
            continue
        if _measurement_angle_candidate(item) is None:
            continue
        key = _candidate_key(item, index)
        if status == "reject" and not key[0:3]:
            key = ("", "", "", str(index))
        groups.setdefault(key, []).append(item)

    actions: List[Dict[str, Any]] = []
    for idx, ((joint, side, direction, _unique), items) in enumerate(groups.items(), start=1):
        angles = sorted(_measurement_angle_candidate(item) for item in items)
        angles = [float(angle) for angle in angles if angle is not None]
        if not angles:
            continue
        median = angles[len(angles) // 2]
        complete_label = bool(joint and side and direction)
        review_only = any(item.get("status") == "reject" for item in items)
        action_id = f"rom_{idx:03d}"
        for item in items:
            item["action_id"] = action_id
            item["requires_human_confirmation"] = True
            if direction:
                item["direction"] = direction
        overlay_paths = [str(item.get("overlay_path") or "") for item in items if item.get("overlay_path")]
        image_paths = [str(item.get("image_path") or "") for item in items if item.get("image_path")]
        actions.append({
            "action_id": action_id,
            "joint": joint,
            "side": side,
            "direction": direction,
            "status": "needs_human_image_review" if review_only else ("needs_human_confirmation" if complete_label else "needs_human_label"),
            "selected_angle": round(median, 3),
            "angle_range": {"min": round(min(angles), 3), "max": round(max(angles), 3)},
            "image_count": len(items),
            "images": [str(item.get("image_name") or "") for item in items],
            "image_paths": image_paths,
            "overlay_paths": overlay_paths,
            "preview_paths": overlay_paths or image_paths,
            "measurements": items,
            "requires_human_confirmation": True,
            "evidence_text": _action_evidence_text(joint, side, direction, median, len(items), complete_label, review_only=review_only),
        })
    return actions


def _build_structured_rom_candidates(actions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
    candidates: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for action in actions:
        joint = str(action.get("joint") or "")
        side = str(action.get("side") or "")
        direction = str(action.get("direction") or "")
        angle = action.get("selected_angle")
        if not joint or not side or not direction or angle in (None, ""):
            continue
        candidates.setdefault(joint, {}).setdefault(side, {})[direction] = {
            "angle": angle,
            "source": "visual_goniometer_candidate",
            "action_id": action.get("action_id"),
            "requires_human_confirmation": True,
            "overlay_paths": action.get("overlay_paths", []),
        }
    return candidates


def _build_structured_rom(measurements: List[Dict[str, object]], require_human_confirmation: bool = True) -> Dict[str, Dict[str, Dict[str, float]]]:
    if require_human_confirmation:
        return {}
    structured: Dict[str, Dict[str, Dict[str, float]]] = {}
    for item in measurements:
        if item.get("status") != "ok":
            continue
        joint = str(item.get("joint") or "")
        side = str(item.get("side") or "")
        direction = _normalize_direction_for_joint(joint, str(item.get("direction") or ""))
        angle = item.get("angle")
        if not joint or not side or not direction or angle in (None, ""):
            continue
        structured.setdefault(joint, {}).setdefault(side, {})[direction] = round(float(angle), 3)
    return structured


def _write_overlay(image, masks_by_cls: dict, angle: Optional[float], debug: Optional[dict], runtime: dict, output_path: Path, status: str, reasons: List[str]) -> Optional[str]:
    try:
        import cv2  # type: ignore

        class_names = runtime["CLASS_NAMES"]
        colors = {0: (0, 128, 255), 1: (0, 220, 0), 2: (255, 0, 180)}
        out = image.copy()
        overlay = image.copy()
        for cls_id, item in masks_by_cls.items():
            mask = item.get("mask")
            if mask is None:
                continue
            overlay[mask.astype(bool)] = colors.get(cls_id, (255, 255, 0))
        out = cv2.addWeighted(overlay, 0.35, out, 0.65, 0)
        if debug and debug.get("pivot") is not None:
            pivot_float = debug["pivot"]
            pivot = pivot_float.astype(int)
            cv2.circle(out, tuple(pivot), 8, colors.get(runtime["PIVOT"], (255, 0, 180)), -1, cv2.LINE_AA)
            line_len = int(max(image.shape[:2]) * 0.45)
            for cls_id, direction_vec in (debug.get("directions") or {}).items():
                p2 = (pivot_float + direction_vec * line_len).astype(int)
                cv2.line(out, tuple(pivot), tuple(p2), colors.get(cls_id, (255, 255, 0)), 5, cv2.LINE_AA)
                label = class_names[cls_id] if 0 <= cls_id < len(class_names) else str(cls_id)
                cv2.putText(out, label, tuple((pivot_float + direction_vec * min(line_len, 240)).astype(int)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, colors.get(cls_id, (255, 255, 0)), 2, cv2.LINE_AA)
        text = f"{status}: angle={angle:.1f} deg" if angle is not None else f"{status}: no reliable angle"
        cv2.putText(out, text, (24, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 3, cv2.LINE_AA)
        if reasons:
            cv2.putText(out, ";".join(reasons)[:90], (24, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2, cv2.LINE_AA)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), out)
        return str(output_path)
    except Exception:
        return None


def run_pipeline(
    input_dir: str,
    output_dir: Optional[str] = None,
    model_path: Optional[str] = None,
    conf: float = 0.35,
    fallback_conf: float = 0.05,
    skip_inference: bool = False,
    output_json_name: str = "visual_result.json",
    write_aux_outputs: bool = True,
    write_visual_evidence: bool = True,
) -> Tuple[str, str, str]:
    output_root = Path(resolve_numbered_output_dir(input_dir, output_dir))
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / output_json_name
    csv_path = output_root / output_json_name.replace(".json", ".csv")
    md_path = output_root / output_json_name.replace(".json", ".md")

    image_paths = _collect_images(input_dir)
    warnings: List[str] = []
    measurements: List[Dict[str, object]] = []

    visual_status = "ok"
    if not image_paths:
        warnings.append("未在损伤情况目录中发现可用于视觉读数的图片。")
        visual_status = "skipped"

    resolved_model = model_path or DEFAULT_MODEL_PATH
    can_infer = bool(image_paths) and not skip_inference
    runtime = None
    model = None
    if can_infer:
        if not os.path.isfile(resolved_model):
            warnings.append(f"视觉模型权重不存在，已跳过视觉推理: {resolved_model}")
            visual_status = "inference_unavailable"
            can_infer = False
        else:
            try:
                import cv2  # type: ignore
                from ultralytics import YOLO  # type: ignore

                runtime = _import_goniometer_runtime()
                model = YOLO(str(resolved_model))
            except Exception as exc:
                warnings.append(f"视觉依赖或训练脚本导入失败，已跳过视觉推理: {exc}")
                visual_status = "inference_unavailable"
                can_infer = False

    for idx, image_path in enumerate(image_paths, start=1):
        context = infer_measurement_context(image_path)
        base_item: Dict[str, object] = {
            "image_path": os.path.abspath(image_path),
            "image_name": os.path.basename(image_path),
            "joint": context["joint"],
            "side": context["side"],
            "direction": context["direction"],
            "status": "skipped",
            "route": "not_run",
            "angle": None,
            "alpha": 0.0,
            "quality_scores": {},
            "reasons": [],
        }

        if not can_infer or model is None or runtime is None:
            base_item["reasons"] = ["visual_inference_not_available"]
            measurements.append(base_item)
            continue

        try:
            import cv2  # type: ignore

            image = cv2.imread(image_path)
            if image is None:
                base_item["status"] = "error"
                base_item["reasons"] = ["image_read_failed"]
                measurements.append(base_item)
                continue

            predict_conf = min(conf, fallback_conf)
            result = model.predict(image_path, conf=predict_conf, verbose=False, device=VISUAL_DEVICE)[0]
            primary_masks = runtime["collect_masks"](result, image.shape, conf)
            fallback_masks = runtime["collect_masks"](result, image.shape, fallback_conf)
            masks_by_cls, fallback_used = runtime["fill_missing_masks"](primary_masks, fallback_masks)
            angle, debug = runtime["estimate_angle"](image, masks_by_cls, fixed_fit="robust")
            if debug is not None:
                debug["fallback_used"] = fallback_used

            quality_row = _build_quality_row(str(idx), angle, debug, masks_by_cls, fallback_used, runtime)
            final_angle, route, alpha, reasons, rejected = choose_final_prediction(quality_row)
            if final_angle is not None:
                item_status = "ok"
            elif route == "reject":
                item_status = "reject"
            elif route == "missing":
                item_status = "no_goniometer"
            else:
                item_status = "error"
            base_item["status"] = item_status
            base_item["route"] = route
            base_item["angle"] = round(float(final_angle), 3) if final_angle is not None else None
            base_item["baseline_angle"] = round(float(angle), 3) if angle is not None else None
            base_item["alpha"] = round(float(alpha), 3)
            base_item["quality_scores"] = {
                "segmentation_quality_score": quality_row.get("segmentation_quality_score", ""),
                "geometry_fit_quality_score": quality_row.get("geometry_fit_quality_score", ""),
                "perspective_risk_score": quality_row.get("perspective_risk_score", ""),
                "correction_feasibility_score": quality_row.get("correction_feasibility_score", ""),
                "final_risk_level": quality_row.get("final_risk_level", ""),
                "recommended_action": quality_row.get("recommended_action", ""),
            }
            base_item["reasons"] = reasons
            base_item["raw_quality_features"] = quality_row
            if write_visual_evidence:
                overlay_path = output_root / "visual_overlays" / f"{Path(image_path).stem}_overlay.png"
                overlay_angle = base_item.get("angle") if base_item.get("angle") not in (None, "") else base_item.get("baseline_angle")
                written_overlay = _write_overlay(image, masks_by_cls, overlay_angle, debug, runtime, overlay_path, item_status, reasons)
                if written_overlay:
                    base_item["overlay_path"] = written_overlay
            line = _measurement_to_rom_line(base_item)
            if line:
                base_item["rom_finding"] = line
            measurements.append(base_item)
        except Exception as exc:
            base_item["status"] = "error"
            base_item["reasons"] = [f"visual_runtime_error: {exc}"]
            measurements.append(base_item)

    accepted = [item for item in measurements if item.get("status") == "ok"]
    rejected_images = [item for item in measurements if item.get("status") == "reject"]
    no_goniometer_images = [item for item in measurements if item.get("status") == "no_goniometer"]
    visual_actions = _build_visual_actions(measurements)
    structured_rom_candidates = _build_structured_rom_candidates(visual_actions)
    structured_rom = _build_structured_rom(measurements, require_human_confirmation=True)
    has_goniometer_evidence = bool(accepted or rejected_images)
    if visual_actions:
        if accepted and all(action.get("status") == "needs_human_confirmation" for action in visual_actions):
            visual_status = "ok"
        else:
            visual_status = "partial"
        warnings.append("视觉读数仅作为关节活动度候选，需司法人员确认关节、侧别、动作和角度后才进入正式报告。")
    elif image_paths and visual_status not in {"skipped", "inference_unavailable"}:
        visual_status = "no_goniometer_detected"
        warnings.append("图片中未检出可采纳的量角器读数证据，视觉模块不参与关节活动度结论。")

    summary_map = {
        "skipped": "未运行视觉读数",
        "inference_unavailable": "视觉推理不可用，无法判断图片中是否存在量角器",
        "no_goniometer_detected": "未检出可采纳量角器读数",
        "partial": "检出量角器/疑似量角器图片，需人工选择关节动作并确认角度",
        "ok": "检出量角器读数候选，需人工复核确认",
    }
    payload = {
        "schema_version": "visual_rom_v1",
        "meta": {
            "input_dir": os.path.abspath(input_dir),
            "model_path": os.path.abspath(resolved_model),
            "inference_device": VISUAL_DEVICE,
            "conf": conf,
            "fallback_conf": fallback_conf,
            "status": visual_status,
            "image_count": len(image_paths),
            "measurement_count": len(measurements),
            "action_count": len(visual_actions),
            "accepted_count": len(accepted),
            "rejected_count": len(rejected_images),
            "no_goniometer_count": len(no_goniometer_images),
            "skip_inference": skip_inference,
            "needs_goniometer": has_goniometer_evidence if visual_status != "inference_unavailable" else None,
            "requires_human_image_review": bool(image_paths) and visual_status in {"inference_unavailable", "partial"},
            "summary": summary_map.get(visual_status, "视觉读数状态需人工复核"),
            "summary_for_judicial_user": summary_map.get(visual_status, "视觉读数状态需人工复核"),
            "conclusion_policy": "visual_reads_angles_only_never_grades",
        },
        "actions": visual_actions,
        "structured_rom_candidates": structured_rom_candidates,
        "structured_rom": structured_rom,
        "visual_measurements": measurements,
        "rejected_images": rejected_images,
        "no_goniometer_images": no_goniometer_images,
        "warnings": warnings,
        "judicial_explanation": {
            "plain_language": "视觉模块只读取量角器图片中的角度，并提供可视化证据；关节、侧别、动作和最终采纳角度需司法人员确认。",
            "limits": "视觉读数不直接形成伤残等级，也不在未检出量角器时输出关节活动度结论。",
        },
    }

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if write_aux_outputs:
        csv_fields = ["image_name", "joint", "side", "direction", "status", "route", "angle", "baseline_angle", "alpha", "reasons", "rom_finding", "overlay_path", "action_id"]
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            for item in measurements:
                row = dict(item)
                row["reasons"] = ";".join([str(x) for x in item.get("reasons", [])])
                writer.writerow(row)

        lines = ["# 视觉读数结果", ""]
        lines.append(f"- 结论：{payload['meta']['summary_for_judicial_user']}")
        lines.append(f"- 输入图片数：{len(image_paths)}")
        lines.append(f"- 成功读数：{len(accepted)}")
        lines.append(f"- 拒识图片：{len(payload['rejected_images'])}")
        if warnings:
            lines.append("- 警告：" + "；".join(warnings))
        lines.append("")
        lines.append("## 读数明细")
        for item in measurements:
            angle_text = f"{item['angle']}°" if item.get("angle") is not None else "-"
            label = " / ".join([str(item.get("joint") or "未识别关节"), str(item.get("side") or "未识别侧别"), str(item.get("direction") or "未识别方向")])
            overlay = f"，overlay={item.get('overlay_path')}" if item.get("overlay_path") else ""
            lines.append(f"- {item.get('image_name')}: {label}，status={item.get('status')}，route={item.get('route')}，angle={angle_text}{overlay}")
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return str(json_path), str(csv_path), str(md_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="visual_agent — 活动度/量角器图片读数")
    parser.add_argument("--input-dir", required=True, help="损伤情况目录或图片路径")
    parser.add_argument("--output-dir", default=None, help="输出目录，默认 test_result/<案件编号>")
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="YOLO-Seg权重路径")
    parser.add_argument("--conf", type=float, default=0.35, help="检测置信度")
    parser.add_argument("--fallback-conf", type=float, default=0.05, help="缺类fallback置信度")
    parser.add_argument("--skip-inference", action="store_true", help="仅生成图片清单与schema，不运行模型")
    parser.add_argument("--json-name", default="visual_result.json", help="JSON输出文件名")
    parser.add_argument("--json-only", action="store_true", help="只输出JSON，不写CSV/Markdown")
    parser.add_argument("--no-visual-evidence", action="store_true", help="不生成量角器读数可视化overlay")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    json_path, csv_path, md_path = run_pipeline(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        model_path=args.model,
        conf=args.conf,
        fallback_conf=args.fallback_conf,
        skip_inference=args.skip_inference,
        output_json_name=args.json_name,
        write_aux_outputs=not args.json_only,
        write_visual_evidence=not args.no_visual_evidence,
    )
    print(f"[*] JSON输出: {json_path}")
    if not args.json_only:
        print(f"[*] CSV输出: {csv_path}")
        print(f"[*] Markdown输出: {md_path}")


if __name__ == "__main__":
    main()
