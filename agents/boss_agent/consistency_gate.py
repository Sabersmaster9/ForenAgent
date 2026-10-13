"""Deterministic final-report consistency checks for Boss output.

The gate does not rewrite forensic conclusions. It extracts the canonical facts
that should already be fixed by Structuring/Judge/Ruler/human audit and reports
inconsistencies for audit/review.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

GRADE_CN = {
    1: "一级",
    2: "二级",
    3: "三级",
    4: "四级",
    5: "五级",
    6: "六级",
    7: "七级",
    8: "八级",
    9: "九级",
    10: "十级",
}
UNKNOWN = {None, "", "不详", "未查找到", "未知", "无", "None", "none", "null", "N/A", "n/a"}
PLACEHOLDER_PATTERNS = [
    "待补充",
    "UNKNOWN",
    "N/A",
    "???",
    "{{",
    "}}",
    "xxxx",
    "XXXX",
]


def _is_unknown(value: Any) -> bool:
    return value in UNKNOWN or (isinstance(value, str) and value.strip() in UNKNOWN)


def _clean(value: Any, default: str = "") -> str:
    if _is_unknown(value):
        return default
    return re.sub(r"\s+", " ", str(value).replace("\r", "\n")).strip()


def _int_value(value: Any) -> int | None:
    if _is_unknown(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value)
    if any(token in text for token in ["不构成伤残", "未构成伤残", "不构成", "未构成"]):
        return 0
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _normalize_date(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    match = re.search(r"(20\d{2})[年\-/\.\s]+(\d{1,2})[月\-/\.\s]+(\d{1,2})日?", text)
    if not match:
        return text
    year, month, day = match.groups()
    return f"{year}年{int(month)}月{int(day)}日"


def _flatten_strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: List[str] = []
        for item in value.values():
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    return [str(value)]


def _incident_date(structuring: Dict[str, Any]) -> str:
    for key in ["accident_date", "traffic_accident_date", "incident_date"]:
        value = _normalize_date(structuring.get(key))
        if value:
            return value
    text = "\n".join(_flatten_strings(structuring))
    match = re.search(r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)[^。\n]{0,80}(?:受伤|外伤|碰撞|相撞|摔倒|撞伤|倒地|事故)", text)
    return _normalize_date(match.group(1)) if match else ""


def _commission_scope_flags(structuring: Dict[str, Any]) -> Dict[str, bool]:
    forensic = structuring.get("forensic_facts") or {}
    commission = forensic.get("commission") or {}
    if not isinstance(commission, dict):
        return {"disability": True, "period": False, "fee": False}
    scope_type = str(commission.get("scope_type") or "").strip()
    period_features = forensic.get("period_rule_features") or {}
    has_period_candidates = bool(isinstance(period_features, dict) and period_features.get("candidate_clauses"))
    explicit = bool(commission.get("raw_items") or commission.get("scope_source") == "raw_commission")
    if scope_type == "disability_only":
        return {"disability": True, "period": bool((not explicit) and has_period_candidates), "fee": False}
    if scope_type == "period_only":
        return {"disability": False, "period": True, "fee": False}
    if scope_type == "disability_period":
        return {"disability": True, "period": True, "fee": False}
    if scope_type == "disability_period_medical_fee_review":
        return {"disability": True, "period": True, "fee": True}
    wants_disability = bool(commission.get("requires_disability"))
    wants_period = bool(
        commission.get("requires_work_loss_period")
        or commission.get("requires_nursing_period")
        or commission.get("requires_nutrition_period")
    )
    wants_fee = bool(commission.get("requires_medical_fee_review"))
    if has_period_candidates and not explicit:
        wants_period = True
    if not explicit and commission.get("scope_source") != "raw_commission":
        wants_disability = True
    return {"disability": wants_disability, "period": wants_period, "fee": wants_fee}


def _human_disability_confirmation(structuring: Dict[str, Any]) -> Dict[str, Any]:
    forensic = structuring.get("forensic_facts") or {}
    value = forensic.get("human_disability_confirmation") or {}
    return value if isinstance(value, dict) else {}


def _confirmed_period_result(structuring: Dict[str, Any], period: Dict[str, Any]) -> Dict[str, Any]:
    forensic = structuring.get("forensic_facts") or {}
    features = forensic.get("period_rule_features") or {}
    confirmed = features.get("confirmed_period_result") or {}
    if not isinstance(confirmed, dict):
        return dict(period or {})
    merged = dict(period or {})
    mapping = {
        "误工": ["work_loss_days", "误工", "误工期"],
        "护理": ["nursing_days", "护理", "护理期"],
        "营养": ["nutrition_days", "营养", "营养期"],
    }
    for report_key, keys in mapping.items():
        for key in keys:
            parsed = _int_value(confirmed.get(key))
            if parsed is not None:
                merged[report_key] = parsed
                break
    for key in ["standard_name", "clause", "clause_text", "sub_label"]:
        value = _clean(confirmed.get(key))
        if value:
            merged[key] = value
    return merged


def _canonical_disability(structuring: Dict[str, Any], ruler: Dict[str, Any]) -> Dict[str, Any]:
    section5 = ruler.get("section5") or {}
    results = [item for item in (section5.get("disability_results") or []) if isinstance(item, dict)]
    positives = [item for item in results if _int_value(item.get("grade")) and _int_value(item.get("grade")) > 0]
    negatives = [item for item in results if _int_value(item.get("grade")) == 0]

    confirmation = _human_disability_confirmation(structuring)
    confirmed_grade = _int_value(confirmation.get("grade_number"))
    conclusion_type = _clean(confirmation.get("conclusion_type"))
    if conclusion_type == "no_disability":
        confirmed_grade = 0
    if confirmed_grade is not None:
        if confirmed_grade > 0:
            source = dict(positives[0]) if positives else {}
            source["grade"] = confirmed_grade
            source["grade_cn"] = GRADE_CN.get(confirmed_grade, f"{confirmed_grade}级")
        else:
            source = dict(negatives[0]) if negatives else {"grade": 0, "grade_cn": "不构成伤残"}
            source["grade"] = 0
        for key in ["standard_name", "clause", "clause_text"]:
            if _clean(confirmation.get(key)):
                source[key] = _clean(confirmation.get(key))
        return source
    if positives:
        positives = sorted(positives, key=lambda item: _int_value(item.get("grade")) or 99)
        return dict(positives[0])
    if negatives:
        return dict(negatives[0])
    return {}


def build_canonical_report_facts(structuring: Dict[str, Any], judge: Dict[str, Any], ruler: Dict[str, Any]) -> Dict[str, Any]:
    """Return high-consequence facts that the final report should verbalize."""
    section5 = ruler.get("section5") or {}
    period = section5.get("period_result") or {}
    scope = _commission_scope_flags(structuring)
    if scope.get("period"):
        period = _confirmed_period_result(structuring, period)
    else:
        period = {}
    disability = _canonical_disability(structuring, ruler) if scope.get("disability") else {}
    grade = _int_value(disability.get("grade"))
    return {
        "subject_name": _clean(structuring.get("subject_name")),
        "subject_id": _clean(structuring.get("subject_id")),
        "incident_date": _incident_date(structuring),
        "commission_scope": scope,
        "disability": {
            "supported": grade is not None and grade > 0,
            "grade": grade,
            "grade_cn": disability.get("grade_cn") or (GRADE_CN.get(grade, f"{grade}级") if grade and grade > 0 else "不构成伤残" if grade == 0 else ""),
            "clause": _clean(disability.get("clause")),
            "clause_text": _clean(disability.get("clause_text") or disability.get("text")),
            "standard_name": _clean(disability.get("standard_name")),
        },
        "period": {
            "work_loss_days": _int_value(period.get("误工")),
            "nursing_days": _int_value(period.get("护理")),
            "nutrition_days": _int_value(period.get("营养")),
            "clause": _clean(period.get("clause")),
            "sub_label": _clean(period.get("sub_label")),
            "standard_name": _clean(period.get("standard_name")),
        },
        "section4": {
            "has_structured_rom": bool(
                ((judge.get("section4") or {}).get("structured_rom"))
                or judge.get("structured_rom")
                or ((structuring.get("forensic_facts") or {}).get("structured_rom"))
            ),
        },
    }


def _section_after_heading(report_text: str, heading: str) -> str:
    lines = report_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if heading in line:
            start = idx
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if re.match(r"^[一二三四五六七八九十]+、", lines[idx].strip()):
            end = idx
            break
    return "\n".join(lines[start:end])


def _warn(source: str, text: str, severity: str = "warning", kind: str = "consistency") -> Dict[str, str]:
    return {"source": source, "severity": severity, "kind": kind, "text": text}


def validate_report_consistency(
    structuring: Dict[str, Any],
    judge: Dict[str, Any],
    ruler: Dict[str, Any],
    report_text: str,
) -> List[Dict[str, str]]:
    """Flag final report inconsistencies against canonical facts.

    The returned items are audit warnings; callers decide whether to block.
    """
    warnings: List[Dict[str, str]] = []
    facts = build_canonical_report_facts(structuring, judge, ruler)
    compact_report = re.sub(r"\s+", "", report_text or "")
    section6 = _section_after_heading(report_text or "", "六、鉴定意见")
    compact_section6 = re.sub(r"\s+", "", section6)

    for marker in PLACEHOLDER_PATTERNS:
        if marker in (report_text or ""):
            warnings.append(_warn("FinalConsistencyGate", f"正式报告仍包含占位/模板残留：{marker}", kind="placeholder"))
    for marker in ["实验条件：", "Human Audit：", "运行时间："]:
        if marker in (report_text or ""):
            warnings.append(_warn("FinalConsistencyGate", f"实验元数据进入正式报告正文：{marker}", severity="error", kind="experiment_footer"))
    if re.search(r"另外我还要补充|事实和理由|特具状起诉|原告为维护|请求法院|此致", report_text or ""):
        warnings.append(_warn("FinalConsistencyGate", "基本案情/报告正文疑似包含诉状、第一人称陈述或法律论辩噪声。", kind="formal_noise"))
    if re.search(r"公民身份号码[:：]?\d{5,}.*中华人民共和国|住址[:：]?[^。\n]{80,}", report_text or ""):
        warnings.append(_warn("FinalConsistencyGate", "身份号码或住址字段疑似 OCR 串接噪声。", kind="formal_noise"))

    # 人工对正文行填写的替换文字，若在最终报告里找不到，说明这次人工修改没有进正文。
    patches = (structuring.get("forensic_facts") or {}).get("human_text_patches") or {}
    if isinstance(patches, dict):
        compact_text = re.sub(r"\s+", "", report_text or "")
        for anchor, text in patches.items():
            anchor_text = str(anchor or "").strip()
            value_text = re.sub(r"\s+", "", str(text or ""))
            if not anchor_text or not value_text or value_text in compact_text:
                continue
            warnings.append(_warn(
                "FinalConsistencyGate",
                f"人工填写的正文替换未落入最终报告：锚点“{anchor_text[:40]}”。该行可能在重渲染后已被改写或移除，需人工复核。",
                kind="manual_patch_unmatched",
            ))

    subject = facts.get("subject_name") or ""
    if subject and subject not in report_text:
        warnings.append(_warn("FinalConsistencyGate", f"被鉴定人姓名未出现在最终报告：{subject}", severity="error"))

    incident_date = facts.get("incident_date") or ""
    if incident_date and incident_date not in report_text:
        warnings.append(_warn("FinalConsistencyGate", f"事故/受伤日期未在最终报告中稳定呈现：{incident_date}"))

    disability_scope = (facts.get("commission_scope") or {}).get("disability")
    disability = facts.get("disability") or {}
    grade = disability.get("grade")
    grade_cn = disability.get("grade_cn") or ""
    clause = disability.get("clause") or ""
    if disability_scope and grade is not None:
        has_positive_phrase = bool(re.search(r"已构成[一二三四五六七八九十]+级伤残", compact_report))
        has_negative_phrase = "不构成伤残" in compact_report or "尚未构成伤残等级" in compact_report
        if grade > 0:
            if grade_cn and grade_cn not in compact_section6:
                warnings.append(_warn("FinalConsistencyGate", f"Section 6 未呈现 Ruler/人工确认的伤残等级：{grade_cn}", severity="error"))
            if clause and clause not in compact_report:
                warnings.append(_warn("FinalConsistencyGate", f"报告未呈现 Ruler/人工确认的伤残条款：{clause}"))
            if has_negative_phrase and has_positive_phrase:
                warnings.append(_warn("FinalConsistencyGate", "报告同时出现构成伤残与不构成伤残表述。", severity="error"))
        elif grade == 0:
            if not has_negative_phrase:
                warnings.append(_warn("FinalConsistencyGate", "Ruler/人工确认结果为不构成伤残，但 Section 6 未明确不构成伤残。", severity="error"))
            if has_positive_phrase:
                warnings.append(_warn("FinalConsistencyGate", "Ruler/人工确认结果为不构成伤残，但报告仍出现构成伤残表述。", severity="error"))

    period_scope = (facts.get("commission_scope") or {}).get("period")
    period = facts.get("period") or {}
    raw_period = ((ruler.get("section5") or {}).get("period_result") or {})
    if isinstance(raw_period, dict) and (raw_period.get("clause") or raw_period.get("误工") or raw_period.get("护理") or raw_period.get("营养")):
        period_scope = True
        period = {
            "work_loss_days": _int_value(raw_period.get("误工")),
            "nursing_days": _int_value(raw_period.get("护理")),
            "nutrition_days": _int_value(raw_period.get("营养")),
            "clause": _clean(raw_period.get("clause")),
            "sub_label": _clean(raw_period.get("sub_label")),
            "standard_name": _clean(raw_period.get("standard_name")),
        }
    if period_scope:
        for label, key in [("误工", "work_loss_days"), ("护理", "nursing_days"), ("营养", "nutrition_days")]:
            days = period.get(key)
            if days is not None and f"{label}期限以{days}日" not in compact_report:
                warnings.append(_warn("FinalConsistencyGate", f"报告未按 Ruler/人工确认结果呈现{label}期限：{days}日"))
        period_clause = period.get("clause") or ""
        if period_clause and period_clause not in compact_report:
            warnings.append(_warn("FinalConsistencyGate", f"报告未呈现三期评定条款：{period_clause}"))
    else:
        if re.search(r"误工期限以\d+日|护理期限以\d+日|营养期限以\d+日", compact_report):
            warnings.append(_warn("FinalConsistencyGate", "委托范围未要求三期，但报告中出现三期最终意见。"))

    return warnings
