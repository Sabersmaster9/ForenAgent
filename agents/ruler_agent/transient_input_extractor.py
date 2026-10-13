"""Minimal transient rule-input extractor for Study 2 NoStruct ablation.

This deliberately avoids rebuilding Structuring Agent outputs. It extracts only the
fields needed by the deterministic Ruler from cached OCR text plus Judge/Visual
outputs, and marks the facts as transient.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

from .fact_adapter import has_negated_surgery, has_positive_surgery_text
from .rule_models import RulerFacts

UNKNOWN_VALUES = {"", "不详", "未查找到", "无", "None", "none", "null", None}


class TransientRuleInputExtractor:
    """Extract minimal RulerFacts without persistent structured evidence."""

    def extract(self, extractor_markdown: str, judge_data: Dict[str, Any], visual_data: Optional[Dict[str, Any]] = None) -> RulerFacts:
        text = extractor_markdown or ""
        section4 = judge_data.get("section4", {}) or {}
        physical_exam = section4.get("physical_exam", {}) or {}
        aux_checks = section4.get("auxiliary_checks", {}) or {}
        if not aux_checks and isinstance(section4.get("auxiliary_check"), dict):
            aux_checks = section4.get("auxiliary_check") or {}

        exam_findings = _flatten_strings(physical_exam.get("exam_findings", []))
        manual_findings = _clean(physical_exam.get("manual_findings"))
        if manual_findings:
            exam_findings.append(manual_findings)

        aux_findings = []
        for item in aux_checks.get("items", []) or []:
            if isinstance(item, dict):
                finding = _clean(item.get("findings"))
                if finding:
                    aux_findings.append(finding)
        manual_aux = _clean(aux_checks.get("manual_review_opinion")) if isinstance(aux_checks, dict) else ""
        if manual_aux:
            aux_findings.append(manual_aux)

        diagnoses = _extract_diagnoses(text)
        surgery_history = [s for s in _extract_surgery_history(text) if not has_negated_surgery(s)]
        injury_summary = _extract_injury_summary(text, diagnoses)
        entrustment_items = _extract_entrustment_items(text)
        fracture_features = _extract_fracture_features(text)
        scar_burn = _extract_scar_burn_features(text)
        if scar_burn:
            fracture_features["scar_burn"] = scar_burn
        rule_features = _build_rule_features(fracture_features, text)
        period_rule_features = _build_period_rule_features(text, diagnoses, surgery_history, injury_summary)
        recognized_injuries = _recognized_injuries(diagnoses, fracture_features)
        treatment_events = _extract_treatment_events(text, surgery_history)
        negative_rule_features = _extract_negative_rule_features(text)
        uncertain_points = []
        if not diagnoses:
            uncertain_points.append("transient extractor未稳定抽取到诊断列表。")

        rom_data = _merge_rom(section4, judge_data, visual_data or {})
        all_text_parts = [text, *diagnoses, *surgery_history, *exam_findings, *aux_findings, injury_summary]
        all_text = " ".join(part for part in all_text_parts if part)
        has_positive_surgery = has_positive_surgery_text(surgery_history + _flatten_strings(treatment_events))
        if has_negated_surgery(all_text):
            has_positive_surgery = False
            vertebral = fracture_features.setdefault("vertebral", {})
            vertebral["has_spine_surgery"] = False

        return RulerFacts(
            subject_name=_extract_subject_name(text),
            incident_date=_extract_incident_date(text),
            incident_summary=_extract_incident_summary(text),
            diagnosis_overview="；".join(diagnoses[:8]),
            injury_summary=injury_summary or "不详",
            entrustment_items=entrustment_items,
            diagnoses=diagnoses,
            surgery_history=surgery_history,
            exam_findings=exam_findings,
            aux_findings=aux_findings,
            rom_data=rom_data,
            forensic_facts={"scar_burn_facts": scar_burn} if scar_burn else {},
            rule_features=rule_features,
            period_rule_features=period_rule_features,
            fracture_features=fracture_features,
            treatment_events=treatment_events,
            recognized_injuries=recognized_injuries,
            negative_rule_features=negative_rule_features,
            uncertain_points=uncertain_points,
            all_text=all_text,
            has_positive_surgery=has_positive_surgery,
            input_mode="transient_no_struct",
        )


def facts_to_jsonable(facts: RulerFacts) -> Dict[str, Any]:
    return asdict(facts)


def synthetic_structuring_from_facts(facts: RulerFacts) -> Dict[str, Any]:
    """Build a minimal renderer payload without persistent structured evidence."""
    return {
        "subject_name": facts.subject_name,
        "incident_date": facts.incident_date,
        "incident_summary": facts.incident_summary,
        "diagnosis_overview": facts.diagnosis_overview,
        "injury_summary": facts.injury_summary,
        "entrustment_items": facts.entrustment_items,
        "surgery_history": facts.surgery_history,
        "medical_records": [],
        "medical_summary_items": [],
        "other_summary_items": [],
        "forensic_facts": {},
        "client_name": "待补充",
        "acceptance_date": "待补充",
        "appraisal_location": "浙江大学司法鉴定中心",
        "present_personnel": "待补充",
        "_study2_ablation_note": "NoStruct synthetic renderer payload; persistent structured evidence intentionally bypassed.",
    }


def _clean(value: Any) -> str:
    if value in UNKNOWN_VALUES:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _flatten_strings(value: Any) -> List[str]:
    result: List[str] = []
    if value is None:
        return result
    if isinstance(value, str):
        text = _clean(value)
        if text:
            result.append(text)
    elif isinstance(value, dict):
        for v in value.values():
            result.extend(_flatten_strings(v))
    elif isinstance(value, list):
        for item in value:
            result.extend(_flatten_strings(item))
    return result


def _dedupe(items: Iterable[str], limit: int = 20) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        text = _clean(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _extract_subject_name(text: str) -> str:
    for pattern in [r"(?:被鉴定人|伤者|患者|姓名)[:：]\s*([一-龥]{2,4})", r"CASE FOLDER[:：]?\s*\d{3}[-_\s]*([一-龥]{2,4})"]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return "XXX"


def _extract_incident_date(text: str) -> str:
    match = re.search(r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)[^。\n]{0,80}(?:事故|受伤|外伤|摔倒|碰撞|撞伤|交通)", text)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = re.search(r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})[^。\n]{0,80}(?:事故|受伤|外伤|摔倒|碰撞|撞伤|交通)", text)
    return match.group(1) if match else "不详"


def _extract_incident_summary(text: str) -> str:
    for pattern in [
        r"([^。\n]{0,80}(?:交通事故|车祸|碰撞|相撞|摔倒|跌倒|外伤|受伤)[^。\n]{0,120})",
        r"(?:基本案情|案情摘要)[:：]?\s*([^\n]{20,180})",
    ]:
        match = re.search(pattern, text)
        if match:
            return _clean(match.group(1))[:220]
    return ""


def _extract_diagnoses(text: str) -> List[str]:
    diagnoses: List[str] = []
    for match in re.finditer(r"(?:诊断|出院诊断|入院诊断|临床诊断|影像诊断)[:：]\s*([^\n。；]{2,160})", text):
        fragment = match.group(1)
        diagnoses.extend(re.split(r"[；;、,，]", fragment))
    injury_terms = [
        r"[胸腰颈骶]?\d{0,2}\s*椎体?[^。；，,\n]{0,12}(?:压缩性|粉碎性)?骨折",
        r"第?[一二三四五六七八九十0-9、\-至~]+肋骨骨折",
        r"(?:胫骨|腓骨|股骨|肱骨|桡骨|尺骨|锁骨|肩胛骨|跟骨|跖骨|掌骨|骨盆)[^。；，,\n]{0,18}骨折",
        r"(?:半月板|交叉韧带|侧副韧带)[^。；，,\n]{0,18}(?:损伤|撕裂|断裂)",
        r"(?:头皮|面部|体表)[^。；，,\n]{0,12}(?:瘢痕|疤痕)",
        r"(?:烧伤|烫伤|瘢痕|疤痕)[^。；，,\n]{0,40}(?:TBSA|体表面积|\d{1,3}(?:\.\d+)?\s*%)",
    ]
    for pattern in injury_terms:
        diagnoses.extend(match.group(0) for match in re.finditer(pattern, text))
    return _dedupe(diagnoses, limit=30)


def _extract_surgery_history(text: str) -> List[str]:
    surgeries = []
    for match in re.finditer(r"([^。；\n]{0,40}(?:手术|内固定|切开复位|切复|置换|修补|重建|缝合|取出术|清创|植皮|削痂|皮瓣)[^。；\n]{0,80})", text):
        surgeries.append(match.group(1))
    return _dedupe(surgeries, limit=12)


def _extract_scar_burn_features(text: str) -> Dict[str, Any]:
    compact = text.replace(" ", "")
    if not any(k in compact for k in ["烧伤", "烫伤", "瘢痕", "疤痕", "植皮", "削痂", "睑外翻", "小口"]):
        return {}
    pct_values = []
    for m in re.finditer(r"(?:烧伤|烫伤|TBSA|总面积|体表面积)[^。；，,\n]{0,30}(\d{1,3}(?:\.\d+)?)\s*%", compact, flags=re.I):
        pct_values.append(float(m.group(1)))
    for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%[^。；，,\n]{0,20}(?:TBSA|烧伤|烫伤|体表面积)", compact, flags=re.I):
        pct_values.append(float(m.group(1)))
    range_match = re.search(r"(\d{1,3})\s*%\s*[-至~～]\s*(\d{1,3})\s*%?[^。；，,\n]{0,20}(?:烧伤|烫伤|TBSA|体表面积)", compact, flags=re.I)
    if range_match:
        pct_values.append(float(range_match.group(2)))
    evidence = _dedupe(re.findall(r"[^。；\n]{0,50}(?:烧伤|烫伤|瘢痕|疤痕|植皮|削痂|清创|睑外翻|小口畸形|体表面积|TBSA)[^。；\n]{0,80}", text, flags=re.I), limit=8)
    max_pct = max(pct_values) if pct_values else 0
    return {
        "burn_total_tbsa_percent": max_pct or None,
        "scar_bsa_percent": max_pct if any(k in compact for k in ["瘢痕", "疤痕"]) else None,
        "body_surface_pct": max_pct if any(k in compact for k in ["瘢痕", "疤痕"]) else None,
        "severe_face_disfigurement": bool(any(k in compact for k in ["重度容貌毁损", "严重容貌毁损", "毁容", "睑外翻", "小口畸形"])),
        "evidence": evidence,
        "source": "transient_regex",
    }


def _extract_injury_summary(text: str, diagnoses: List[str]) -> str:
    if diagnoses:
        return "、".join(diagnoses[:5])
    match = re.search(r"(?:损伤|伤情)[:：]\s*([^\n。]{2,120})", text)
    return _clean(match.group(1)) if match else "不详"


def _extract_entrustment_items(text: str) -> List[str]:
    items = []
    for match in re.finditer(r"(?:委托鉴定事项|鉴定事项|委托事项)[:：]\s*([^\n。]{2,160})", text):
        items.append(match.group(1))
    if any(term in text for term in ["伤残", "残疾等级", "致残程度"]):
        items.append("伤残程度鉴定")
    if any(term in text for term in ["误工期", "护理期", "营养期", "三期"]):
        items.append("误工期、护理期、营养期鉴定")
    return _dedupe(items, limit=8)


def _extract_fracture_features(text: str) -> Dict[str, Any]:
    features: Dict[str, Any] = {"vertebral": {}, "rib": {}, "limb": []}
    compact = text.replace(" ", "")

    levels = []
    for region, number in re.findall(r"([胸腰颈骶])\s*(\d{1,2})\s*椎?", compact):
        prefix = {"颈": "C", "胸": "T", "腰": "L", "骶": "S"}.get(region, "")
        value = f"{prefix}{int(number)}"
        if value not in levels and re.search(rf"{region}\s*{number}\s*椎?[^。；，,\n]{{0,30}}(?:骨折|压缩|楔形|变扁)|(?:骨折|压缩|楔形|变扁)[^。；，,\n]{{0,30}}{region}\s*{number}\s*椎?", compact):
            levels.append(value)
    for prefix, number in re.findall(r"\b([CTLS])\s*-?\s*(\d{1,2})\b", compact, flags=re.I):
        value = f"{prefix.upper()}{int(number)}"
        if value not in levels and re.search(rf"{re.escape(prefix)}\s*-?\s*{number}[^。；，,\n]{{0,30}}(?:骨折|压缩|楔形|变扁)", compact, flags=re.I):
            levels.append(value)
    if levels:
        spine_evidence = _dedupe(re.findall(r"[^。；\n]{0,40}(?:椎体|胸椎|腰椎|颈椎|骶椎)[^。；\n]{0,60}(?:骨折|压缩|楔形|变扁)[^。；\n]{0,40}", text), limit=8)
        features["vertebral"] = {
            "levels": levels,
            "vertebra_levels": levels,
            "count": len(levels),
            "compression_fracture": bool(re.search(r"(?:压缩|楔形|变扁)", compact)),
            "compression_degree": _extract_compression_degree(compact),
            "has_spine_surgery": bool(re.search(r"椎[^。；\n]{0,30}(?:手术|内固定|置入|取出术)", compact)),
            "evidence": spine_evidence,
        }

    rib_count = _extract_rib_count(compact)
    if rib_count:
        features["rib"] = {
            "count": rib_count,
            "rib_fracture_count": rib_count,
            "evidence": _dedupe(re.findall(r"[^。；\n]{0,40}肋骨[^。；\n]{0,60}骨折[^。；\n]{0,30}", text), limit=6),
        }

    limb_items = []
    for match in re.finditer(r"(?:胫骨|腓骨|股骨|肱骨|桡骨|尺骨|锁骨|肩胛骨|跟骨|跖骨|掌骨|骨盆)[^。；，,\n]{0,18}骨折", text):
        limb_items.append({"description": match.group(0), "evidence": [match.group(0)]})
    features["limb"] = limb_items[:12]
    return features


def _extract_compression_degree(compact: str) -> str:
    if any(token in compact for token in ["未达1/3", "小于1/3", "不足1/3", "未达三分之一", "小于三分之一", "轻度压缩"]):
        return "未达1/3"
    if any(token in compact for token in ["达1/3", "超过1/3", "大于1/3", "达三分之一", "超过三分之一"]):
        return "达1/3"
    return ""


def _extract_rib_count(compact: str) -> int:
    digit_match = re.search(r"肋骨骨折\s*(\d+)\s*根|([0-9]+)\s*根肋骨骨折", compact)
    if digit_match:
        return int(next(g for g in digit_match.groups() if g))
    ribs = set()
    for fragment in re.findall(r"第?([0-9一二三四五六七八九十、,，\-至~]+)肋", compact):
        ribs.update(_parse_number_list(fragment))
    return len(ribs)


def _parse_number_list(fragment: str) -> set[int]:
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    normalized = fragment.replace("，", "、").replace(",", "、")
    result: set[int] = set()
    for part in re.split(r"[、]", normalized):
        if not part:
            continue
        if re.search(r"[-至~]", part):
            ends = re.split(r"[-至~]", part)
            nums = [_cn_or_int(x, cn) for x in ends[:2]]
            if all(nums):
                result.update(range(min(nums), max(nums) + 1))
        else:
            num = _cn_or_int(part, cn)
            if num:
                result.add(num)
    return result


def _cn_or_int(value: str, cn: Dict[str, int]) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        return 10 + cn.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return cn.get(value[0], 0) * 10
    if "十" in value and len(value) == 3:
        return cn.get(value[0], 0) * 10 + cn.get(value[2], 0)
    return cn.get(value, 0)


def _build_rule_features(fracture_features: Dict[str, Any], text: str) -> Dict[str, Any]:
    vertebral = fracture_features.get("vertebral") or {}
    rib = fracture_features.get("rib") or {}
    scar_burn = fracture_features.get("scar_burn") or {}
    return {
        "vertebra_levels": vertebral.get("levels", []),
        "vertebral_count": vertebral.get("count", 0),
        "two_vertebral_compression_fractures": bool(vertebral.get("compression_fracture") and int(vertebral.get("count") or 0) >= 2),
        "one_vertebral_fracture_with_surgery": bool(vertebral.get("has_spine_surgery") and int(vertebral.get("count") or 0) >= 1),
        "rib_fracture_count": rib.get("count", 0),
        "scar": {
            "body_surface_pct": scar_burn.get("body_surface_pct") or scar_burn.get("scar_bsa_percent"),
            "burn_total_tbsa_percent": scar_burn.get("burn_total_tbsa_percent"),
            "severe_face_disfigurement": scar_burn.get("severe_face_disfigurement"),
            "evidence": scar_burn.get("evidence", []),
        } if scar_burn else {},
        "body_scar_pct": scar_burn.get("body_surface_pct") or scar_burn.get("scar_bsa_percent"),
        "burn_total_tbsa_percent": scar_burn.get("burn_total_tbsa_percent"),
        "severe_face_disfigurement": scar_burn.get("severe_face_disfigurement"),
        "raw_feature_source": "transient_regex",
    }


def _build_period_rule_features(text: str, diagnoses: List[str], surgery_history: List[str], injury_summary: str) -> Dict[str, Any]:
    candidates = []
    all_text = " ".join([text, *diagnoses, *surgery_history, injury_summary])
    if any(term in all_text for term in ["骨折", "压缩", "肋骨", "椎体"]):
        candidates.append({"clause": "骨折相关", "label": "骨折损伤", "reason": "transient extractor detected fracture terms"})
    if any(term in all_text for term in ["手术", "内固定", "切开复位", "置换"]):
        candidates.append({"clause": "手术相关", "label": "手术治疗", "reason": "transient extractor detected surgery terms"})
    if any(term in all_text for term in ["烧伤", "烫伤", "瘢痕", "疤痕", "植皮", "削痂", "清创"]):
        candidates.append({"clause": "11.1", "label": "烧烫伤/瘢痕相关损伤", "误工": "180-365日", "护理": "120-365日", "营养": "90-180日", "reason": "transient extractor detected burn/scar terms"})
    return {"candidate_clauses": candidates, "source": "transient_regex"}


def _recognized_injuries(diagnoses: List[str], fracture_features: Dict[str, Any]) -> List[Dict[str, Any]]:
    injuries = [{"name": item, "name_normalized": item, "source": "transient_regex"} for item in diagnoses[:20]]
    vertebral = fracture_features.get("vertebral") or {}
    if vertebral.get("levels") and not any("椎" in x.get("name", "") for x in injuries):
        injuries.append({"name": "椎体骨折", "name_normalized": "椎体骨折", "source": "transient_regex"})
    return injuries


def _extract_treatment_events(text: str, surgery_history: List[str]) -> List[Dict[str, Any]]:
    return [{"event_type": "surgery_or_treatment", "description": s, "source": "transient_regex"} for s in surgery_history]


def _extract_negative_rule_features(text: str) -> List[Dict[str, Any]]:
    negatives = []
    for match in re.finditer(r"(?:未见|无|未发现|未检出|未提示)[^。；，,\n]{0,40}(?:骨折|脱位|韧带|半月板|异常|手术)", text):
        negatives.append({"text": match.group(0), "source": "transient_regex"})
    return negatives[:20]


def _merge_rom(section4: Dict[str, Any], judge_data: Dict[str, Any], visual_data: Dict[str, Any]) -> Dict[str, Any]:
    rom_data = {}
    for candidate in [
        section4.get("structured_rom"),
        judge_data.get("structured_rom"),
        visual_data.get("structured_rom"),
    ]:
        if isinstance(candidate, dict):
            rom_data.update(candidate)
    return rom_data
