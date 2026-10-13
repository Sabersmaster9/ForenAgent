"""Input adapter for Ruler Agent.

Converts Structuring/Judge JSON into a single deterministic fact object.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .rule_models import RulerFacts

UNKNOWN_VALUES = {"", "不详", "未查找到", "无", "None", "none", "null", None}
SURGERY_POSITIVE_TERMS = ["手术", "内固定", "切复", "置换", "修补术", "切除术", "植入", "取出术", "清创缝合"]
SURGERY_NEGATIVE_PATTERNS = [
    r"无.{0,6}手术",
    r"未.{0,6}手术",
    r"没有.{0,6}手术",
    r"未行.{0,6}",
    r"未予.{0,6}手术",
    r"手术史[:：]?\s*无",
    r"没有做手术",
    r"无住院手术",
]


def normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return None if stripped in UNKNOWN_VALUES else stripped
    if value in UNKNOWN_VALUES:
        return None
    return value


def flatten_strings(value: Any) -> List[str]:
    result: List[str] = []
    if value is None:
        return result
    if isinstance(value, str):
        text = value.strip()
        if text and text not in UNKNOWN_VALUES:
            result.append(text)
        return result
    if isinstance(value, dict):
        for v in value.values():
            result.extend(flatten_strings(v))
        return result
    if isinstance(value, list):
        for item in value:
            result.extend(flatten_strings(item))
        return result
    return result


def has_negated_surgery(text: str) -> bool:
    compact = (text or "").replace(" ", "")
    return any(re.search(pattern, compact) for pattern in SURGERY_NEGATIVE_PATTERNS)


def has_positive_surgery_text(texts: Iterable[str]) -> bool:
    for text in texts:
        if not text:
            continue
        compact = text.replace(" ", "")
        if has_negated_surgery(compact):
            continue
        if any(term in compact for term in SURGERY_POSITIVE_TERMS):
            return True
    return False


SPINE_CONTEXT_TERMS = ["椎", "椎体", "胸椎", "腰椎", "颈椎", "骶椎", "脊柱", "压缩", "楔形", "变扁", "骨折"]
CODE_CONTEXT_TERMS = ["ICD", "疾病编码", "诊断编码", "编码", "诊断代码"]
VALID_LEVEL_RANGES = {"C": (1, 7), "T": (1, 12), "L": (1, 5), "S": (1, 5)}


def _is_valid_vertebral_level(prefix: str, number: str) -> bool:
    prefix = (prefix or "").upper()
    try:
        value = int(number)
    except (TypeError, ValueError):
        return False
    low, high = VALID_LEVEL_RANGES.get(prefix, (0, -1))
    return low <= value <= high


def _normalize_vertebral_level_value(value: Any) -> str:
    text = str(value or "").strip().upper().replace("Ｌ", "L").replace("Ｔ", "T")
    match = re.match(r"^([CTLS])(\d{1,2})$", text)
    if match:
        prefix, number = match.groups()
        return f"{prefix}{int(number)}" if _is_valid_vertebral_level(prefix, number) else ""
    match = re.match(r"^(颈|胸|腰|骶)\s*(\d{1,2})", text)
    if match:
        region, number = match.groups()
        prefix = {"颈": "C", "胸": "T", "腰": "L", "骶": "S"}.get(region, "")
        return f"{prefix}{int(number)}" if _is_valid_vertebral_level(prefix, number) else ""
    return ""


def _filter_valid_vertebral_levels(levels: Any) -> List[str]:
    if isinstance(levels, str):
        levels = [levels]
    result: List[str] = []
    for level in levels or []:
        normalized = _normalize_vertebral_level_value(level)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _has_spine_context(text: str, start: int, end: int) -> bool:
    left = max(0, start - 20)
    right = min(len(text), end + 20)
    window = text[left:right]
    return any(term in window for term in SPINE_CONTEXT_TERMS)


def _has_code_context(text: str, start: int, end: int) -> bool:
    left = max(0, start - 12)
    right = min(len(text), end + 12)
    window = text[left:right]
    if any(term in window.upper() for term in CODE_CONTEXT_TERMS):
        return True
    token = text[start:end]
    return bool(re.search(rf"{re.escape(token)}[.．]\d", window, flags=re.I))


def _record_compression_evidence(vertebral: Dict[str, Any], level: str, snippet: str) -> None:
    if not level or not snippet:
        return
    evidence_by_level = vertebral.setdefault("compression_evidence_by_level", {})
    if not isinstance(evidence_by_level, dict):
        evidence_by_level = {}
        vertebral["compression_evidence_by_level"] = evidence_by_level
    bucket = evidence_by_level.setdefault(level, [])
    if isinstance(bucket, list) and snippet not in bucket:
        bucket.append(snippet[:180])
    evidence = vertebral.setdefault("evidence", [])
    if isinstance(evidence, list) and snippet not in evidence:
        evidence.append(snippet[:180])


def _infer_vertebral_features_from_text(vertebral: Dict[str, Any], text: str) -> None:
    """把机器抽取/审片文字中的关键椎体事实转成 Ruler 可裁决字段。"""
    compact = (text or "").replace(" ", "")
    if not compact:
        return
    if "锥体" in compact and any(term in compact for term in ["胸", "腰", "颈", "骶", "脊柱", "压缩", "骨折", "楔形", "变扁"]):
        compact = compact.replace("锥体", "椎体")

    fracture_terms = ["骨折", "压缩", "楔形", "变扁", "骨质不连", "透亮影"]

    if not vertebral.get("compression_degree"):
        if any(token in compact for token in ["未达1/3", "小于1/3", "不足1/3", "未达三分之一", "小于三分之一", "轻度压缩"]):
            vertebral["compression_degree"] = "未达1/3"
        elif any(token in compact for token in ["达1/3", "超过1/3", "大于1/3", "达三分之一", "超过三分之一"]):
            vertebral["compression_degree"] = "达1/3"
        elif re.search(r"(?:前|中|后)?\s*[12]/3", compact) or "三分之二" in compact:
            vertebral["compression_degree"] = "达1/3"

    if not vertebral.get("compression_fracture") and re.search(r"椎体[^。；,，]{0,20}(?:压缩|楔形|变扁)", compact):
        vertebral["compression_fracture"] = True

    levels = vertebral.get("levels") or vertebral.get("vertebra_levels") or []
    normalized_levels = _filter_valid_vertebral_levels(levels)
    if normalized_levels and vertebral.get("count"):
        vertebral["levels"] = normalized_levels
        vertebral["vertebra_levels"] = normalized_levels
        try:
            existing_count = int(vertebral.get("count") or len(normalized_levels))
        except (TypeError, ValueError):
            existing_count = len(normalized_levels)
        vertebral["count"] = min(existing_count, len(normalized_levels))
        return
    for match in re.finditer(r"([胸腰颈骶])\s*(\d{1,2})\s*椎?", compact):
        region, number = match.groups()
        if "检查部位" in compact[max(0, match.start() - 12):match.start() + 24] and not any(k in compact[max(0, match.start() - 20):match.end() + 40] for k in fracture_terms):
            continue
        if not any(k in compact[max(0, match.start() - 24):match.end() + 48] for k in fracture_terms):
            continue
        prefix = {"颈": "C", "胸": "T", "腰": "L", "骶": "S"}.get(region, "")
        if not _is_valid_vertebral_level(prefix, number):
            continue
        value = f"{prefix}{int(number)}"
        if value not in normalized_levels:
            normalized_levels.append(value)
    for match in re.finditer(r"\b([CTLS])\s*[-]?\s*(\d{1,2})\b", compact, flags=re.I):
        prefix, number = match.groups()
        prefix = prefix.upper()
        if not _is_valid_vertebral_level(prefix, number):
            continue
        if _has_code_context(compact, match.start(), match.end()) and not _has_spine_context(compact, match.start(), match.end()):
            continue
        if not _has_spine_context(compact, match.start(), match.end()):
            continue
        value = f"{prefix}{int(number)}"
        if value not in normalized_levels:
            normalized_levels.append(value)
    for level in normalized_levels:
        cn = {"C": "颈", "T": "胸", "L": "腰", "S": "骶"}.get(level[0], "") + level[1:]
        for pattern in [
            rf"(?:{re.escape(level)}|{re.escape(cn)}椎?)[^。；，,\n]{{0,50}}(?:压缩|楔形|变扁|骨折)",
            rf"(?:压缩|楔形|变扁|骨折)[^。；，,\n]{{0,50}}(?:{re.escape(level)}|{re.escape(cn)}椎?)",
        ]:
            m = re.search(pattern, compact, re.I)
            if m:
                _record_compression_evidence(vertebral, level, m.group(0))
                break
    if normalized_levels:
        vertebral["levels"] = normalized_levels
        vertebral.setdefault("vertebra_levels", normalized_levels)
        if not vertebral.get("count") or int(vertebral.get("count") or 0) < len(normalized_levels):
            vertebral["count"] = len(normalized_levels)
        evidence_by_level = vertebral.get("compression_evidence_by_level") if isinstance(vertebral.get("compression_evidence_by_level"), dict) else {}
        if len([level for level in normalized_levels if evidence_by_level.get(level)]) >= 2:
            vertebral["compression_fracture"] = True


def _get_vertebral_features(fracture_features: Dict[str, Any], rule_features: Dict[str, Any]) -> Dict[str, Any]:
    vertebral = fracture_features.get("vertebral") or {}
    if not isinstance(vertebral, dict):
        vertebral = {}
    levels = _filter_valid_vertebral_levels(vertebral.get("levels") or vertebral.get("vertebra_levels") or rule_features.get("vertebra_levels") or [])
    raw_count = vertebral.get("count") or vertebral.get("vertebral_count") or rule_features.get("vertebral_count")
    try:
        count = int(raw_count) if raw_count not in UNKNOWN_VALUES else len(levels)
    except (TypeError, ValueError):
        count = len(levels)
    if levels:
        count = min(count, len(levels)) if count else len(levels)
    else:
        count = 0
    compression = vertebral.get("compression_fracture")
    if not levels:
        compression = False
    elif compression is None:
        compression = bool(rule_features.get("two_vertebral_compression_fractures")) or "压缩" in " ".join(flatten_strings(vertebral))
    has_surgery = vertebral.get("has_spine_surgery")
    if has_surgery is None:
        has_surgery = bool(rule_features.get("one_vertebral_fracture_with_surgery"))
    evidence_by_level = vertebral.get("compression_evidence_by_level") if isinstance(vertebral.get("compression_evidence_by_level"), dict) else {}
    if levels and bool(rule_features.get("two_vertebral_compression_fractures")) and not evidence_by_level:
        evidence_text = "；".join(flatten_strings(vertebral))
        for level in levels:
            evidence_by_level[level] = [evidence_text[:180] or f"结构化规则特征提示{level}椎体压缩性骨折"]
    return {
        **vertebral,
        "levels": levels,
        "count": count,
        "compression_fracture": bool(compression),
        "compression_evidence_by_level": evidence_by_level,
        "has_spine_surgery": bool(has_surgery),
    }


def adapt_inputs(structuring_data: Dict[str, Any], judge_data: Dict[str, Any]) -> RulerFacts:
    forensic_facts = structuring_data.get("forensic_facts") or {}
    rule_features = forensic_facts.get("rule_features") or {}
    period_rule_features = forensic_facts.get("period_rule_features") or {}
    fracture_features = forensic_facts.get("fracture_features") or {}
    if not isinstance(fracture_features, dict):
        fracture_features = {}
    scar_burn = forensic_facts.get("scar_burn_facts") or forensic_facts.get("scar_features") or {}
    if isinstance(scar_burn, dict):
        fracture_features = {**fracture_features, "scar_burn": scar_burn}
    fracture_features = {**fracture_features, "vertebral": _get_vertebral_features(fracture_features, rule_features)}

    diagnoses: List[str] = []
    for mr in structuring_data.get("medical_records", []) or []:
        diag = normalize_value(mr.get("diagnosis", "")) if isinstance(mr, dict) else None
        if diag:
            diagnoses.append(str(diag))
    for item in forensic_facts.get("recognized_injuries", []) or []:
        if isinstance(item, dict):
            text = normalize_value(item.get("name") or item.get("injury") or item.get("description") or item.get("normalized_name"))
            if text:
                diagnoses.append(str(text))
        elif isinstance(item, str) and item.strip():
            diagnoses.append(item.strip())

    surgery_history = [s for s in flatten_strings(structuring_data.get("surgery_history", [])) if not has_negated_surgery(s)]
    treatment_events = forensic_facts.get("treatment_events", []) or []
    for event in treatment_events:
        if isinstance(event, dict):
            event_text = " ".join(flatten_strings(event))
            if event_text and not has_negated_surgery(event_text) and has_positive_surgery_text([event_text]):
                surgery_history.append(event_text)

    section4 = judge_data.get("section4", {}) or {}
    physical_exam = section4.get("physical_exam", {}) or {}
    structuring_rom = forensic_facts.get("structured_rom", {}) if isinstance(forensic_facts.get("structured_rom", {}), dict) else {}
    judge_rom = section4.get("structured_rom", {}) if isinstance(section4.get("structured_rom", {}), dict) else {}
    rom_data = {**structuring_rom}
    for joint, side_map in judge_rom.items():
        if isinstance(side_map, dict) and isinstance(rom_data.get(joint), dict):
            merged_side = dict(rom_data[joint])
            for side, direction_map in side_map.items():
                if isinstance(direction_map, dict) and isinstance(merged_side.get(side), dict):
                    merged_side[side] = {**merged_side[side], **direction_map}
                else:
                    merged_side[side] = direction_map
            rom_data[joint] = merged_side
        else:
            rom_data[joint] = side_map
    exam_findings = flatten_strings(physical_exam.get("exam_findings", []))
    aux_checks = section4.get("auxiliary_checks", {}) or {}
    if not aux_checks and isinstance(section4.get("auxiliary_check"), dict):
        aux_checks = section4.get("auxiliary_check") or {}
    aux_findings = []
    manual_findings = normalize_value(physical_exam.get("manual_findings"))
    if manual_findings:
        exam_findings.append(str(manual_findings))
    manual_aux = normalize_value(aux_checks.get("manual_review_opinion"))
    if manual_aux:
        aux_findings.append(str(manual_aux))
    for item in aux_checks.get("items", []) or []:
        if isinstance(item, dict):
            finding = normalize_value(item.get("findings", ""))
            if finding:
                aux_findings.append(str(finding))

    injury_summary = normalize_value(structuring_data.get("injury_summary", "")) or "不详"
    all_text_parts = []
    all_text_parts.extend(diagnoses)
    all_text_parts.extend(surgery_history)
    all_text_parts.extend(exam_findings)
    all_text_parts.extend(aux_findings)
    all_text_parts.extend(flatten_strings(injury_summary))
    all_text_parts.extend(flatten_strings(forensic_facts))
    all_text = " ".join(all_text_parts)
    _infer_vertebral_features_from_text(fracture_features.setdefault("vertebral", {}), all_text)

    has_positive_surgery = has_positive_surgery_text(surgery_history + flatten_strings(treatment_events))
    vertebral = fracture_features.get("vertebral", {})
    period_candidates = period_rule_features.get("candidate_clauses") if isinstance(period_rule_features, dict) else None
    if has_negated_surgery(all_text):
        has_positive_surgery = False
        vertebral["has_spine_surgery"] = False
        if isinstance(period_candidates, list):
            period_rule_features["candidate_clauses"] = [
                item for item in period_candidates
                if not (isinstance(item, dict) and str(item.get("clause") or "").startswith("9.1") and "手术" in str(item.get("label") or item.get("reason") or ""))
            ]
    ligament_features = forensic_facts.get("ligament_meniscus_features") if isinstance(forensic_facts, dict) else {}
    ligament_text = " ".join(flatten_strings(ligament_features)).replace(" ", "")
    if isinstance(period_candidates, list) and re.search(r"(?:未见|无|未发现|未检出|未提示)[^。；，,]{0,30}(?:撕裂|断裂|损伤|异常)", ligament_text):
        period_rule_features["candidate_clauses"] = [
            item for item in period_rule_features.get("candidate_clauses", [])
            if not (isinstance(item, dict) and str(item.get("clause") or "").startswith("10.4"))
        ]
    elif vertebral.get("has_spine_surgery"):
        has_positive_surgery = True

    eye_facts = forensic_facts.get("eye_facts") if isinstance(forensic_facts, dict) else {}
    eye_text = " ".join(flatten_strings(eye_facts)).replace(" ", "")
    has_objective_visual = bool(
        isinstance(eye_facts, dict)
        and (
            eye_facts.get("corrected_visual_acuity")
            or eye_facts.get("blindness_level")
            or eye_facts.get("blindness_level_num")
            or eye_facts.get("moderate_visual_impairment")
            or eye_facts.get("severe_visual_impairment")
            or eye_facts.get("visual_field_effective_pct")
            or re.search(r"视力|视野|盲目|眼球缺失|眼球摘除", eye_text)
        )
    )
    if isinstance(period_rule_features.get("candidate_clauses"), list) and not has_objective_visual:
        period_rule_features["candidate_clauses"] = [
            item for item in period_rule_features.get("candidate_clauses", [])
            if not (isinstance(item, dict) and str(item.get("clause") or "").startswith("5.1.11"))
        ]

    return RulerFacts(
        subject_name=normalize_value(structuring_data.get("subject_name")) or "XXX",
        incident_date=normalize_value(structuring_data.get("incident_date")) or "不详",
        incident_summary=normalize_value(structuring_data.get("incident_summary")) or "",
        diagnosis_overview=normalize_value(structuring_data.get("diagnosis_overview")) or "",
        injury_summary=injury_summary,
        entrustment_items=flatten_strings(structuring_data.get("entrustment_items", [])),
        diagnoses=diagnoses,
        surgery_history=surgery_history,
        exam_findings=exam_findings,
        aux_findings=aux_findings,
        rom_data=rom_data,
        forensic_facts=forensic_facts,
        rule_features=rule_features,
        period_rule_features=period_rule_features,
        fracture_features=fracture_features,
        treatment_events=treatment_events if isinstance(treatment_events, list) else [],
        recognized_injuries=forensic_facts.get("recognized_injuries", []) or [],
        negative_rule_features=forensic_facts.get("negative_rule_features", []) or [],
        uncertain_points=forensic_facts.get("uncertain_points", []) or [],
        all_text=all_text,
        has_positive_surgery=has_positive_surgery,
        input_mode="forensic_facts_first" if forensic_facts else "legacy_fallback",
    )
