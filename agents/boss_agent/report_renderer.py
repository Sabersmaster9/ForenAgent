"""Expert-style report renderer for Boss Agent.

This module renders the final report from structured JSON outputs. It does not
make legal/medical decisions; Ruler remains the source of disability and period
conclusions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple


UNKNOWN = {None, "", "不详", "未查找到", "未知", "无", "None", "none", "null", "N/A", "n/a"}
# 上游在材料缺失时会填 xxx/XXX 形式的占位模板（如“xxx法院（xxxx年）…”），
# 这些也必须按“未知”处理，否则会原样出现在正式文书里。
_X_PLACEHOLDER_RE = re.compile(r"^[xX×]{2,}$")
MISSING_TEXT = "待补充"
GRADE_CN = {1: "一级", 2: "二级", 3: "三级", 4: "四级", 5: "五级", 6: "六级", 7: "七级", 8: "八级", 9: "九级", 10: "十级"}
DISABILITY_STANDARD_NAME = "《人体损伤致残程度分级》"
PERIOD_STANDARD_NAME = "《人身损害误工期、护理期、营养期评定规范》（GA/T1193-2014）"


def _is_unknown(value: Any) -> bool:
    if value in UNKNOWN:
        return True
    if isinstance(value, str):
        text = value.strip()
        return text in UNKNOWN or bool(_X_PLACEHOLDER_RE.match(text))
    return False


def _clean(value: Any, default: str = "") -> str:
    if _is_unknown(value):
        return default
    text = str(value).replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def _dedupe(items: Iterable[str], limit: int | None = None) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        text = _clean(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit and len(result) >= limit:
            break
    return result


def _flatten_strings(value: Any) -> List[str]:
    result: List[str] = []
    if value is None:
        return result
    if isinstance(value, str):
        text = value.strip()
        if text:
            result.append(text)
        return result
    if isinstance(value, dict):
        for v in value.values():
            result.extend(_flatten_strings(v))
    elif isinstance(value, list):
        for item in value:
            result.extend(_flatten_strings(item))
    return result


def _all_text(data: Dict[str, Any]) -> str:
    return "\n".join(_flatten_strings(data))


SUBJECT_TITLE = "被鉴定人"


def _subject_name(*candidates: Any) -> str:
    """解析被鉴定人姓名，只返回真实姓名。

    上游未抽取到姓名时返回空串，由 :func:`_subject_ref` 补上默认称谓。
    注意不要把 ``"被鉴定人"`` 当作姓名兜底值返回，否则会与模板里已有的
    “被鉴定人”前缀叠成“被鉴定人被鉴定人”。
    """
    for value in candidates:
        text = _clean(value)
        if text and text != SUBJECT_TITLE:
            return text
    return ""


def _subject_label(*candidates: Any) -> str:
    """独立的被鉴定人指称：有姓名用姓名，无姓名退回“被鉴定人”。

    用于句中不另带“被鉴定人”前缀的位置（原 ``_clean(x, "被鉴定人")`` 行为）。
    """
    return _subject_name(*candidates) or SUBJECT_TITLE


def _subject_ref(subject: str) -> str:
    """把姓名安全地接在“被鉴定人”称谓之后；无姓名时只留称谓。

    仅用于模板本身已含“被鉴定人”字样的位置，避免兜底值与前缀叠写。
    """
    return f"{SUBJECT_TITLE}{subject}" if subject else SUBJECT_TITLE


_PLACEHOLDER_WORDS = ("待补充", "需人工复核", "需复核", "xxx", "XXXX")


def _is_placeholder_stub(line: str) -> bool:
    """判断一行是否只是空占位行，而非“含实质内容但顺带提到占位词”的行。

    旧清理规则用 ``^.*待补充.*`` 整行删除，会把人工补充写在同一行的内容
    一并抹掉，表现为“改了但报告里没有”。这里改为：只有当整行去掉占位词、
    标签、数字与标点后几乎没有剩余文字时，才认定为可丢弃的空占位行。
    """
    text = (line or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if not any(word.lower() in lowered for word in _PLACEHOLDER_WORDS):
        return False
    residue = re.sub(r"[待补充需人工复核\s\d.、（）()：:，,。；;·\-—_/《》\[\]【】]", "", text)
    return len(residue) <= 2


def _clause_ref(clause: Any) -> str:
    """把条款号归一化成“X条”，避免渲染出“第5.9.6.2条条”。

    规则引擎输出裸号（``5.9.6.2``、``10.2.14.b``），人工确认界面可能填
    自然写法（``第5.9.6.2条``），两条链路在此统一到同一形态。
    """
    text = _clean(clause)
    if not text:
        return ""
    text = re.sub(r"^第\s*", "", text.strip())
    text = re.sub(r"条+\s*$", "", text).strip()
    if not text:
        return ""
    if text in {"相关", "其他", "无"} or not re.search(r"\d", text):
        return text
    return f"{text}条"


def _human_text_patches(structuring: Dict[str, Any]) -> Dict[str, str]:
    """人工针对正文具体行填写的替换文字，key 为该行的锚点文本。

    以锚点而非行号定位：报告每次都是从 JSON 重渲染出来的，行号会漂移，
    只有锚点能让人工修改在重跑后依然落到正确位置。
    """
    forensic = structuring.get("forensic_facts") or {}
    value = forensic.get("human_text_patches")
    if not isinstance(value, dict):
        return {}
    return {
        _clean(anchor): _clean(text)
        for anchor, text in value.items()
        if _clean(anchor) and _clean(text)
    }


def _squash(text: str) -> str:
    """去掉空白与常见标点，用于锚点的宽松匹配。"""
    return re.sub(r"[\s，。；：、（）()【】《》“”‘’·\-—_/]", "", text or "")


# 正文里的占位词：人工补丁应只替换这些词，而不是整行。
_PLACEHOLDER_TOKEN_RE = re.compile(r"待补充|需人工复核|需复核|不详|缺失|未查找到")


def _apply_human_text_patches(lines: List[str], patches: Dict[str, str]) -> List[str]:
    """把人工补丁替换回正文对应行；保留原有编号前缀。"""
    if not patches:
        return lines
    squashed = {_squash(anchor): text for anchor, text in patches.items()}
    used: set = set()
    result: List[str] = []
    for line in lines:
        replacement = None
        matched_key = ""
        for anchor, text in patches.items():
            if anchor in line:
                replacement, matched_key = text, _squash(anchor)
                break
        if replacement is None:
            squashed_line = _squash(line)
            for key, text in squashed.items():
                # 锚点过短时容易误伤其它行，只在足够长时才做宽松匹配。
                if len(key) >= 8 and key in squashed_line:
                    replacement, matched_key = text, key
                    break
        if replacement is None:
            result.append(line)
            continue
        used.add(matched_key)
        # 行内若只有占位词，就只替换占位词，保留“受理日期：”“在场人员：”这类标签；
        # 整行替换会把标签一并抹掉，读起来就成了一句没头没尾的话。
        token_match = _PLACEHOLDER_TOKEN_RE.search(line)
        if token_match and replacement not in token_match.group(0):
            result.append(line[:token_match.start()] + replacement + line[token_match.end():])
            continue
        prefix = re.match(r"^(\s*(?:\d+[.、]|[（(][一二三四五六七八九十]+[）)]))", line)
        if prefix and not replacement.startswith(prefix.group(1)):
            result.append(f"{prefix.group(1)}{replacement}")
        else:
            result.append(replacement)
    return result


def _normalize_date(text: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    text = re.sub(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", r"\1年\2月\3日", text)
    text = re.sub(r"(\d{1,2})点(\d{1,2})分", r"\1时\2分", text)
    return text


def _normalize_forensic_text(text: Any) -> str:
    text = _clean(text)
    if not text:
        return ""
    text = text.replace("胸L1", "胸11")
    text = text.replace("胸 T11", "胸11").replace("胸T11", "胸11")
    text = re.sub(r"(?<![A-Za-z0-9])T(\d+)(?![A-Za-z0-9])", r"胸\1", text)
    text = re.sub(r"(?<![A-Za-z0-9])L(\d+)(?![A-Za-z0-9])", r"腰\1", text)
    text = text.replace("腰椎压缩性骨折(L)", "腰椎压缩性骨折")
    text = text.replace("CT示：胸L", "CT示")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _level_to_cn(level: str) -> str:
    level = (level or "").strip().upper()
    m = re.match(r"([TLC])(\d+)", level)
    if not m:
        return level
    prefix = {"T": "胸", "L": "腰", "C": "颈"}.get(m.group(1), "")
    return f"{prefix}{m.group(2)}"


def _format_injury_name(name: str) -> str:
    return _normalize_forensic_text(name)


def _spine_sort_key(level: str) -> tuple[int, int]:
    m = re.match(r"([CTL])(\d+)", str(level).upper())
    if not m:
        return (9, 999)
    return ({"C": 0, "T": 1, "L": 2}.get(m.group(1), 9), int(m.group(2)))


def _valid_report_spine_level(level: str) -> bool:
    m = re.match(r"^([CTLS])(\d{1,2})$", str(level or "").upper())
    if not m:
        return False
    ranges = {"C": (1, 7), "T": (1, 12), "L": (1, 5), "S": (1, 5)}
    low, high = ranges.get(m.group(1), (0, -1))
    return low <= int(m.group(2)) <= high


def _spine_has_renderable_evidence(vertebral: Dict[str, Any], levels: List[str]) -> bool:
    if not levels:
        return False
    evidence_by_level = vertebral.get("compression_evidence_by_level") or {}
    if isinstance(evidence_by_level, dict) and all(evidence_by_level.get(level) for level in levels):
        return True
    if vertebral.get("compression_fracture") and int(vertebral.get("count") or 0) >= len(levels):
        return True
    evidence = "\n".join(str(x) for x in (vertebral.get("evidence") or []) if x)
    if not evidence:
        return False
    return bool(re.search(r"椎体[^。；，,\n]{0,20}(?:压缩|楔形|变扁|骨折)|(?:压缩|楔形|变扁|骨折)[^。；，,\n]{0,20}椎体", evidence))


def _injury_facts(structuring: Dict[str, Any], ruler: Dict[str, Any]) -> str:
    forensic = structuring.get("forensic_facts") or {}
    names: List[str] = []
    for item in forensic.get("recognized_injuries", []) or []:
        if isinstance(item, dict):
            names.append(item.get("name_normalized") or item.get("normalized_name") or item.get("name") or item.get("name_raw") or "")
        elif isinstance(item, str):
            names.append(item)
    names = [_format_injury_name(n) for n in _dedupe(names)]

    vertebral = ((forensic.get("fracture_features") or {}).get("vertebral") or {})
    levels = vertebral.get("levels") or vertebral.get("vertebra_levels") or []
    if isinstance(levels, str):
        levels = [levels]
    primary_spine_name = ""
    levels = [str(x).upper() for x in levels if _valid_report_spine_level(str(x))]
    if levels and vertebral.get("compression_fracture") and _spine_has_renderable_evidence(vertebral, levels):
        sorted_levels = sorted([str(x) for x in levels if x], key=_spine_sort_key)
        spine_name = "、".join(_level_to_cn(x) for x in sorted_levels)
        if spine_name:
            primary_spine_name = f"{spine_name}椎体压缩性骨折"
            names.insert(0, primary_spine_name)

    summary = _clean(structuring.get("injury_summary"))
    if summary and summary != primary_spine_name:
        names.insert(0, _format_injury_name(summary))

    scar_burn = forensic.get("scar_burn_facts") or forensic.get("scar_features") or {}
    if isinstance(scar_burn, dict):
        evidence = " ".join(_flatten_strings(scar_burn.get("evidence")))
        pct = _clean(scar_burn.get("burn_total_tbsa_percent") or scar_burn.get("scar_bsa_percent") or scar_burn.get("body_surface_pct"))
        # 只有确证存在烧伤/烫伤时才写“烧伤后瘢痕形成”。
        # “瘢痕/疤痕/植皮”本身只说明有瘢痕，外科手术和皮肤擦伤同样会留下瘢痕，
        # 把它们当作烧伤证据会凭空给案件加一条不存在的伤情。
        burn_confirmed = bool(re.search(r"烧伤|烫伤|热液|火焰|化学烧伤|电击伤|热压伤", evidence))
        if burn_confirmed and pct:
            names.insert(0, f"大面积烧伤后瘢痕形成（约{pct}%体表面积）")
        elif burn_confirmed:
            names.insert(0, "烧伤后瘢痕形成")

    positive = [r for r in (ruler.get("section5", {}).get("disability_results", []) or []) if r.get("grade", 0) > 0]
    for r in positive:
        text = _clean(r.get("text"))
        if text and ("骨折" in text or "瘢痕" in text or "容貌" in text) and not names:
            names.append(_format_injury_name(text))

    if primary_spine_name:
        extras = [n for n in names if n and n != primary_spine_name and "椎体压缩" not in n]
        extras = _dedupe(extras, limit=5)
        return "、".join([primary_spine_name] + extras)

    names = _dedupe([n for n in names if n and n not in {"上述损伤"}], limit=8)
    return "、".join(names) if names else "相关损伤"


def _incident_date(structuring: Dict[str, Any]) -> str:
    for key in ["accident_date", "traffic_accident_date", "incident_date"]:
        direct = _normalize_date(structuring.get(key, ""))
        if direct:
            return direct
    traffic_fact = _normalize_forensic_text(structuring.get("traffic_accident_fact_responsibility_text"))
    match = re.search(r"(\d{4}年\s*\d{1,2}月\s*\d{1,2}日(?:\s*\d{1,2}[时点](?:\s*\d{1,2}分)?许?)?)", traffic_fact)
    if match:
        return _normalize_date(match.group(1))
    fragment = _accident_fragment(structuring)
    m = re.search(r"(\d{4}年\s*\d{1,2}月\s*\d{1,2}日(?:\s*\d{1,2}[时点](?:\s*\d{1,2}分)?许?)?)", fragment)
    if m:
        return _normalize_date(m.group(1))
    visit_dates = []
    for record in structuring.get("medical_records", []) or []:
        if isinstance(record, dict):
            date = _normalize_date(record.get("visit_date", ""))
            if date:
                visit_dates.append(date)
    if visit_dates:
        return visit_dates[0]
    all_text = _all_text(structuring)
    m = re.search(r"(\d{4}年\s*\d{1,2}月\s*\d{1,2}日)[^。\n]{0,60}(?:受伤|外伤|碰撞|相撞|摔倒|撞伤|倒地)", all_text)
    return _normalize_date(m.group(1)) if m else ""


def _commission_scope_flags(structuring: Dict[str, Any]) -> Tuple[bool, bool, bool]:
    forensic = structuring.get("forensic_facts") or {}
    commission = forensic.get("commission") or {}
    if not isinstance(commission, dict):
        return True, False, False
    scope_type = str(commission.get("scope_type") or "").strip()
    period_features = forensic.get("period_rule_features") or {}
    has_period_candidates = bool(isinstance(period_features, dict) and period_features.get("candidate_clauses"))
    explicit = bool(commission.get("raw_items") or commission.get("scope_source") == "raw_commission")
    if scope_type == "disability_only":
        return True, bool((not explicit) and has_period_candidates), False
    if scope_type == "period_only":
        return False, True, False
    if scope_type == "disability_period":
        return True, True, False
    if scope_type == "disability_period_medical_fee_review":
        return True, True, True
    wants_disability = bool(commission.get("requires_disability"))
    wants_period = bool(commission.get("requires_work_loss_period") or commission.get("requires_nursing_period") or commission.get("requires_nutrition_period"))
    wants_fee = bool(commission.get("requires_medical_fee_review"))
    if has_period_candidates and not explicit:
        wants_period = True
    if not explicit and commission.get("scope_source") != "raw_commission":
        wants_disability = True
    return wants_disability, wants_period, wants_fee


def _entrustment_text(structuring: Dict[str, Any]) -> str:
    forensic = structuring.get("forensic_facts") or {}
    commission = forensic.get("commission") or {}
    subject = _subject_label(structuring.get("subject_name"))
    wants_disability, wants_period, wants_fee = _commission_scope_flags(structuring)
    action = "重新鉴定" if isinstance(commission, dict) and commission.get("is_reappraisal") else "鉴定"
    parts = []
    if wants_disability:
        parts.append("伤残等级")
    if wants_period:
        parts.extend(["误工期", "护理期", "营养期"])
    if commission.get("requires_care_dependency"):
        parts.append("护理依赖程度")
    if wants_fee:
        parts.append("医疗费合理性")
    if commission.get("requires_future_treatment_fee"):
        parts.append("后续治疗费")
    if parts:
        return f"对{subject}的{'、'.join(_dedupe(parts, limit=8))}进行{action}。"

    items = structuring.get("entrustment_items") or []
    text = "、".join(_dedupe([re.sub(r"依照《[^》]+》(?:（[^）]+）)?", "", str(x)).replace("进行鉴定", "").replace("进行重新鉴定", "") for x in items if x], limit=5))
    if text:
        text = text.replace(f"对{subject}的", "")
        return f"对{subject}的{text}进行{action}。"
    return "对被鉴定人的相关委托事项进行鉴定。"


def _extract_identifiers(text: str, limit: int = 4) -> List[str]:
    results: List[str] = []
    patterns = [
        ("病案号", r"病案号[:：\s]*([A-Za-z0-9-]{4,})"),
        ("住院号", r"住院号[:：\s]*([A-Za-z0-9-]{4,})"),
        ("门诊号", r"门诊号[:：\s]*([A-Za-z0-9-]{4,})"),
        ("电子图像号", r"(?:电子图像号|图像号|影像号|检查号)[:：\s]*([A-Z]{0,4}[-]?\d{5,})"),
        ("电子图像号", r"\b((?:CT|MR|DR|CR|DX|XR)[-]?\d{5,})\b"),
        ("电脑号", r"电脑号[:：\s]*([A-Za-z0-9-]{4,})"),
    ]
    seen = set()
    for label, pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1).strip()
            key = (label, value)
            if key in seen:
                continue
            seen.add(key)
            results.append(f"{label}：{value}")
            if len(results) >= limit:
                return results
    return results


def _identifier_suffix(text: str) -> str:
    ids = _extract_identifiers(text, limit=3)
    return f"（{'；'.join(ids)}）" if ids else ""


def _extract_exam_names(text: str, limit: int = 4) -> List[str]:
    text = _normalize_forensic_text(text)
    names: List[str] = []
    patterns = [
        r"(?:胸腰椎|胸椎|腰椎|颈椎|脊柱|头颅|胸部|腹部|骨盆|左膝关节|右膝关节|膝关节|肩关节|肘关节|腕关节|髋关节|踝关节)(?:CT|MRI|MR|X线|DR)",
        r"(?:CT|MRI|MR|X线|DR)电子图像",
    ]
    for pattern in patterns:
        names.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return _dedupe(names, limit=limit)


def _image_material_line(structuring: Dict[str, Any], injury_text: str) -> str:
    material_items = [str(x).strip("；。 ") for x in (structuring.get("material_summary") or []) if _clean(x)]
    material_text = "\n".join(material_items)
    all_text = _all_text(structuring)
    bad_body_part = "左膝" in material_text and any(k in injury_text for k in ["椎", "脊柱", "腰", "胸"])
    if not bad_body_part:
        for item in material_items:
            if any(k in item for k in ["U盘", "u盘", "光盘", "影像", "电子图像", "电子影像", "CT", "MR", "MRI"]):
                if "其中包括" in item or "电子图像" in item or "电子影像" in item:
                    return item
    carrier = ""
    if not bad_body_part:
        m = re.search(r"(?:影像学)?[Uu]盘\s*\d*\s*个|(?:影像学)?光盘\s*\d*\s*张|[Uu]盘\s*\d*\s*个", material_text)
        if m:
            carrier = re.sub(r"\s+", "", m.group(0))
    exams = _extract_exam_names(all_text)
    exams = [exam[:-4] if exam.endswith("电子图像") else exam for exam in exams]
    if bad_body_part and any(k in injury_text for k in ["椎", "脊柱", "腰", "胸"]):
        exams = [exam for exam in exams if "膝" not in exam]
    if not exams and any(k in injury_text for k in ["椎", "脊柱"]):
        exams = ["胸腰椎CT"]
    ids = _extract_identifiers(all_text, limit=3)
    carrier_text = carrier or "送检影像学资料载体"
    exam_text = "、".join(_dedupe(exams, limit=4)) if exams else "相关部位影像学资料"
    id_text = f"（{'；'.join(ids)}）" if ids else ""
    return f"{carrier_text}，其中包括{exam_text}电子图像{id_text}等"


def _materials_lines(structuring: Dict[str, Any], injury_text: str) -> List[str]:
    materials = [_normalize_forensic_text(item).rstrip("；。") for item in (structuring.get("material_summary") or []) if _clean(item)]
    normal_lines: List[str] = []
    image_line = ""
    for item in materials:
        if len(item) > 180 or any(k in item for k in ["另外我还要补充", "事实和理由", "特具状起诉", "原告为维护", "请求法院", "公民身份号码中华人民共和国"]):
            continue
        if any(k in item for k in ["影像", "光盘", "U盘", "u盘", "电子图像", "电子影像", "CT", "MRI", "MR"]):
            if not image_line:
                image_line = item if "其中包括" in item and len(item) <= 140 else _image_material_line(structuring, injury_text)
            continue
        normal_lines.append(item)
    if not image_line and any(k in _all_text(structuring) for k in ["CT", "MRI", "MR", "影像", "电子图像", "电子影像"]):
        image_line = _image_material_line(structuring, injury_text)
    lines = normal_lines + ([image_line] if image_line else [])
    if not lines:
        lines.append("司法鉴定委托书等送检材料")
    return [f"{i}.{item}；" for i, item in enumerate(lines, 1)]


def _valid_subject_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{17}[0-9Xx]", _clean(value)))


def _stable_address(value: str) -> bool:
    text = _clean(value)
    if not text or len(text) > 80:
        return False
    if re.search(r"身份证|公民|号码|中华人民共和国|\d{12,}", text):
        return False
    return any(k in text for k in ["省", "市", "区", "县", "镇", "街道", "村", "号"])


def _subject_sentence(structuring: Dict[str, Any]) -> str:
    # 该句直接跟在“被鉴定人：”标签之后，因此这里只能放姓名本身；
    # 姓名缺失时用待补充标记暴露问题，由审计报告提示人工补录。
    name = _subject_name(structuring.get("subject_name")) or MISSING_TEXT
    gender = _clean(structuring.get("subject_gender"))
    ethnicity = _clean(structuring.get("subject_ethnicity"))
    birth = _clean(structuring.get("subject_birth"))
    address = _clean(structuring.get("subject_address"))
    subject_id = _clean(structuring.get("subject_id"))
    parts = [name]
    if gender in {"男", "女"}:
        parts.append(gender)
    if ethnicity and len(ethnicity) <= 4 and not ethnicity.endswith("族"):
        parts.append(f"{ethnicity}族")
    elif ethnicity and len(ethnicity) <= 5:
        parts.append(ethnicity)
    if birth and re.search(r"\d{4}年\d{1,2}月\d{1,2}日", birth):
        parts.append(f"{birth}出生")
    if _stable_address(address):
        parts.append(f"住址：{address}")
    if _valid_subject_id(subject_id):
        parts.append(f"公民身份号码：{subject_id}")
    return "，".join(parts) + "。"


def _primary_treatment_hospital(structuring: Dict[str, Any]) -> str:
    for item in (structuring.get("medical_summary_items") or []):
        if isinstance(item, dict):
            hospital = _clean(item.get("hospital_name"))
            if hospital and "医院" in hospital:
                return hospital
    for record in (structuring.get("medical_records") or []):
        if isinstance(record, dict):
            hospital = _clean(record.get("hospital_name"))
            if hospital and "医院" in hospital:
                return hospital
    text = _all_text(structuring)
    match = re.search(r"([一-龥]{2,}(?:大学|学院|附属|省|市|区|县)?[一-龥]{0,20}医院)", text)
    return match.group(1) if match else "医院"


def _core_diagnosis_text(structuring: Dict[str, Any], injury_text: str) -> str:
    candidates: List[str] = []
    for item in (structuring.get("medical_summary_items") or []):
        if not isinstance(item, dict):
            continue
        candidates.extend([
            _clean(item.get("discharge_diagnosis")),
            _clean(item.get("imaging_findings")),
            _clean(item.get("chief_complaint")),
        ])
    for record in (structuring.get("medical_records") or []):
        if isinstance(record, dict):
            candidates.append(_clean(record.get("diagnosis")))
    candidates.append(_clean(structuring.get("injury_summary")))
    candidates.append(injury_text)
    for text in candidates:
        value = _normalize_forensic_text(text)
        if not value or any(k in value for k in ["起诉", "诉请", "委托", "鉴定"]):
            continue
        value = re.split(r"[。；;\n]|诊疗经过|治疗经过|处理意见|建议", value)[0].strip("：:，, ")
        value = re.sub(r"^(?:诊断|出院诊断|入院诊断|门诊诊断|影像诊断|印象)[:：]", "", value).strip()
        parts = [p.strip(" ，,、") for p in re.split(r"[，,、]", value) if p.strip(" ，,、")]
        parts = [p for p in parts if not re.search(r"待查|复查|诉讼|鉴定", p)]
        if parts:
            return "、".join(_dedupe(parts, limit=3))
    return injury_text or "相关损伤"


def _diagnosis_sentence(structuring: Dict[str, Any], injury_text: str, subject: str = "被鉴定人") -> str:
    hospital = _primary_treatment_hospital(structuring)
    diagnosis = _core_diagnosis_text(structuring, injury_text)
    return f"{subject}伤后在{hospital}治疗，诊断为{diagnosis}等。经治疗，{subject}目前伤情已稳定。"


def _accident_fragment(structuring: Dict[str, Any]) -> str:
    candidates = _flatten_strings((structuring.get("forensic_facts") or {}).get("negative_rule_features"))
    candidates.extend(_flatten_strings(structuring))
    for text in candidates:
        text = _normalize_forensic_text(text)
        if not re.search(r"20\d{2}年\d{1,2}月\d{1,2}日", text):
            continue
        if not any(k in text for k in ["撞", "碰撞", "摔", "倒地", "受伤", "事故", "送往医院"]):
            continue
        m = re.search(r"(20\d{2}年\d{1,2}月\d{1,2}日[^。]{0,260})", text)
        if not m:
            continue
        fragment = _strip_case_form_noise(m.group(1))
        fragment = re.split(r"事情发生后|事发后|后经\s*120|由\s*120|伤后", fragment)[0]
        fragment = _normalize_date(fragment.strip("，；。 "))
        # 剔完版式噪声后仍像表格选项的，直接跳过换下一个候选。
        if len(fragment) >= 18 and not _is_form_noise_text(fragment):
            return fragment
    return ""


def _lawsuit_date(structuring: Dict[str, Any]) -> str:
    direct = _clean(structuring.get("lawsuit_date"))
    if direct:
        return _normalize_date(direct)
    all_text = _all_text(structuring)
    patterns = [
        r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)[^。\n]{0,80}(?:起诉|诉至|诉请)",
        r"(?:起诉|诉至|诉请)[^。\n]{0,80}(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)",
    ]
    for pattern in patterns:
        m = re.search(pattern, all_text)
        if m:
            return _normalize_date(m.group(1))
    return ""


def _contains_responsibility_text(text: str) -> bool:
    text = _clean(text)
    if not text:
        return False
    return bool(re.search(r"责任认定|事故认定|交通事故认定书|认定[:：]|负[^。；，,]{0,20}责任|承担[^。；，,]{0,20}责任|主要责任|次要责任|全部责任|同等责任|无责任", text))


def _strip_source_prefix(text: str) -> str:
    return re.sub(r"^据(?:委托人提供的|送检的|送检的相关|相关)?鉴定资料记载[:：]", "", text or "").strip()


def _should_append_responsibility(fragment: str, first: str, responsibility: str) -> bool:
    if not responsibility or _is_unknown(responsibility):
        return False
    return not (_contains_responsibility_text(fragment) or _contains_responsibility_text(first))


# 事故认定书、伤情鉴定表等表格材料被 OCR 拉平后混进来的版式噪声。
# 典型如“故。杭州交通修正章（6）√财产损失事故√受伤当事人伤势轻微，
# 各方当事人一致同意适用简易程序处理”——这是表格标签，不是案情叙述。
_CASE_FORM_NOISE_RE = re.compile(
    # 只匹配表格表头本身；“交通事”会误伤正文里的“交通事故”，不能放进这里。
    r"[√□■☑]|交通事故事实及责任|损害赔偿调解结果|交通警察|交通修正章"
)
_CASE_FORM_NOISE_WORDS = (
    "财产损失事故", "受伤当事人伤势轻微", "适用简易程序", "存档份", "无碳复写纸", "当事人：", "当事人:",
)


def _is_form_noise_text(text: str) -> bool:
    value = _clean(text)
    if not value:
        return True
    if _CASE_FORM_NOISE_RE.search(value):
        return True
    return sum(1 for word in _CASE_FORM_NOISE_WORDS if word in value) >= 2


def _strip_case_form_noise(text: str) -> str:
    """剔除表格版式噪声；遇到复选框即截断（其后基本是勾选项而非事实描述）。"""
    value = re.split(r"[√□■☑]", str(text or ""))[0]
    for token in ["交通事故事实及责任", "损害赔偿调解结果", "交通警察", "交通修正章"]:
        value = value.replace(token, "")
    value = re.sub(r"当事人[:：]?$", "", value)
    value = re.sub(r"[，、；]{2,}", "，", value)
    return value.strip("，、； ")


def _is_admissible_case_fragment(text: str) -> bool:
    value = _clean(text)
    if not value:
        return False
    if any(k in value for k in ["另外我还要补充", "事实和理由", "特具状起诉", "原告为维护", "请求法院", "此致", "代理人", "具状人", "签章"]):
        return False
    if _is_form_noise_text(value):
        return False
    if len(value) > 320 and not re.search(r"交通事故认定书|事故认定|责任认定", value):
        return False
    return True


def _case_narrative(structuring: Dict[str, Any], injury_text: str, incident_date: str) -> List[str]:
    subject = _subject_label(structuring.get("subject_name"))
    incident = _normalize_forensic_text(structuring.get("incident_summary"))
    defendants = "、".join(structuring.get("defendants") or [])
    court = _clean(structuring.get("client_name"), "委托人")
    responsibility = _clean(structuring.get("responsibility"))

    lines: List[str] = []
    machine_case = (((structuring.get("forensic_facts") or {}).get("machine_ablation_replacements") or {}).get("case_summary") or {})
    fragment = _clean(machine_case.get("text")) if isinstance(machine_case, dict) else ""
    if not fragment:
        fragment = "" if _is_unknown(incident) else incident
    traffic_fact = _normalize_forensic_text(structuring.get("traffic_accident_fact_responsibility_text"))
    if traffic_fact and not _is_unknown(traffic_fact):
        fragment = traffic_fact
    fragment = _strip_source_prefix(fragment)
    if not _is_admissible_case_fragment(fragment) or "完毕" in fragment:
        fragment = _accident_fragment(structuring)
    if not fragment:
        fragment = _accident_fragment(structuring)
    if fragment:
        fragment = re.split(r"原告为维护|现被告|完毕|特具状起诉", fragment)[0].strip("。；，, ")
        fragment = re.sub(r"^(20\d{2}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分)，?\s*\1许?", r"\1许", fragment)
        if traffic_fact:
            first = f"据送检的相关鉴定资料记载：{fragment.rstrip('。')}。"
        else:
            accident = fragment.rstrip("。")
            if not re.search(r"伤后|送往|送至|医院|治疗", accident):
                accident = f"{accident}，伤后立即送往医院治疗"
            first = f"据委托人提供的鉴定资料记载：{accident.rstrip('。')}。"
        if traffic_fact and _should_append_responsibility(fragment, first, responsibility):
            first += f"该起事故经相关部门认定，{responsibility.rstrip('。')}。"
        if traffic_fact:
            first += _diagnosis_sentence(structuring, injury_text, subject)
    else:
        date_part = f"{incident_date}，" if incident_date else ""
        first = f"据委托人提供的鉴定资料记载：{date_part}{subject}发生意外事故，伤后立即送往医院治疗。"
    lines.append(first)

    lawsuit = _lawsuit_date(structuring)
    lawsuit_part = f"{lawsuit}，" if lawsuit else "因相关民事纠纷，"
    entrustment = _entrustment_text(structuring)
    if defendants:
        second = f"{lawsuit_part}{subject}将{defendants}起诉至{court}，诉请获得相应赔偿，并申请司法鉴定。现{court}委托本中心{entrustment}"
    else:
        second = f"现{court}委托本中心{entrustment}"
    lines.append(second)
    # 人工对存疑案情填写的事实核对说明：按“可直接入文的事实说明”补入基本案情。
    case_notes = _clean((structuring.get("forensic_facts") or {}).get("case_unclear_manual_notes"))
    if case_notes and case_notes not in "\n".join(lines):
        for part in re.split(r"[\n；;]+", case_notes):
            part = part.strip().rstrip("。")
            if part:
                lines.append(f"{part}。")
    return lines


def _record_source(item: Dict[str, Any]) -> str:
    hospital = _clean(item.get("hospital_name"))
    medical_type = _clean(item.get("medical_type"), "病历")
    if hospital and "医院" in hospital:
        return f"{hospital}{medical_type}病历"
    if medical_type and medical_type != "病历":
        return f"送检{medical_type}病历"
    return "送检病历"


def _evidence_medical_summary(structuring: Dict[str, Any], incident_date: str) -> List[str]:
    all_text = _normalize_forensic_text(_all_text(structuring))
    lines: List[str] = []
    entry = ""
    m = re.search(r"入室情况[:：](患者因[^。]{3,120}。查体[:：][^\n。]{10,360}。)", all_text)
    if m:
        entry = m.group(1)
    imaging = ""
    for pattern in [
        r"胸腰椎曲度存在[^\n。]{10,220}。",
        r"胸11、腰1椎体楔形压缩[^\n。]{10,220}。",
        r"CT(?:示|提示)[:：]?[^\n。]{5,160}。",
    ]:
        m = re.search(pattern, all_text)
        if m:
            imaging = m.group(0)
            break
    diagnosis = ""
    for pattern in [r"入室诊断[:：]\s*([^\n。]{5,120})", r"出室诊断[:：]\s*([^\n。]{5,120})"]:
        m = re.search(pattern, all_text)
        if m:
            diagnosis = m.group(1).strip("。； ")
            break
    if entry or imaging or diagnosis:
        date = incident_date or _incident_date(structuring)
        parts = []
        if date:
            parts.append(date)
        if entry:
            parts.append(entry.rstrip("。"))
        if imaging:
            parts.append(imaging.rstrip("。"))
        if diagnosis:
            diagnosis = re.split(r"诊疗经过|目前情况|出室诊断", diagnosis)[0].strip("。； ，")
            parts.append(f"诊断：{diagnosis.rstrip('。')}等")
        source = "送检急诊病历" + _identifier_suffix(all_text)
        lines.append(f"1.据{source}记载：" + "。".join(parts).rstrip("。") + "。")
    if imaging and "胸11、腰1" not in imaging:
        m = re.search(r"胸11、腰1椎体楔形压缩[^\n。]{10,220}。", all_text)
        if m:
            lines.append(f"2.据送检影像学检查资料记载：{m.group(0)}")
    return lines


def _summary_item_kind(item: Dict[str, Any]) -> str:
    material = _clean(item.get("material_name")) or _clean(item.get("medical_type"))
    text = "\n".join(_flatten_strings(item))
    if "诊断证明" in text or ("证明" in material and any(k in text for k in ["经诊断", "建议", "无需取出"])):
        return "diagnosis_certificate"
    if any(k in text for k in ["住院病历", "住院号", "住院时间", "入院", "出院诊断", "手术"]):
        return "inpatient"
    if "急诊" in text or "入室" in text:
        return "emergency"
    if "门诊" in text or "就诊" in text:
        return "outpatient"
    if any(k in text for k in ["CT", "DR", "MRI", "MR", "磁共振", "X线", "影像", "检查报告"]):
        return "imaging"
    return "medical"


def _clean_summary_sentence_text(text: str) -> str:
    value = _normalize_forensic_text(text)
    if not value:
        return ""
    value = re.sub(r"^(?:西医诊断|中医诊断|初步诊断|诊断|出院诊断)[:：]\s*", "", value)
    value = re.sub(r"\|[^\n]*\|", " ", value)
    value = re.sub(r"[-|:]{4,}", " ", value)
    boundaries = r"主诉|现病史|先病史|入院情况|专科检查|专科情况|体格检查|查体|辅助检查|诊疗经过|治疗经过|处理|出院情况|出院诊断|初步诊断|诊断|处理意见|出院医嘱|医嘱|建议"
    for label in ["婚育史", "月经史", "个人史", "家族史", "过敏史", "传染病史", "预防接种史", "医疗付费", "费用", "中医诊断", "主要中医诊断", "实验室检查", "检验报告"]:
        value = re.sub(rf"{label}[:：]?[^。；\n]*?(?=(?:{boundaries})[:：]|\n|$)", " ", value)
    value = re.sub(r"\s+", " ", value)
    parts = re.split(r"(?<=[。；;])", value)
    if len(parts) > 1:
        kept = []
        for part in parts:
            stripped = part.strip("。；;，, ")
            if not stripped:
                continue
            if re.search(r"未见明显外伤性改变|年度复查|纤维灶|结节灶|肺结节|骨密度|T值|脂肪肝|囊肿|返流", stripped) and not re.search(r"骨折|脱位|牙折|牙脱位|压缩|内固定|损伤|挫伤|擦伤|瘢痕", stripped):
                continue
            kept.append(stripped)
        value = "。".join(kept)
    return value.strip("。；;，, ")


def _ensure_sentence(text: str) -> str:
    value = _clean_summary_sentence_text(text)
    if not value:
        return ""
    return value if value.endswith(("。", "！", "？")) else f"{value}。"


def _summary_record_number_suffix(item: Dict[str, Any]) -> str:
    number = _clean(item.get("record_number"))
    if not number:
        return ""
    material = _clean(item.get("material_name")) or _clean(item.get("medical_type"))
    if "住院" in material or str(number).upper().startswith("Z"):
        label = "住院号"
    elif "门诊" in material or "诊断证明" in material:
        label = "就诊卡号"
    elif "急诊出室小结" in material:
        label = "病历号"
    elif "急诊" in material:
        label = "病案号"
    elif any(k in material for k in ["影像", "检查报告"]):
        label = "检查号"
    else:
        label = "病历号"
    return f"（{label}：{number}）"


def _summary_source_title(item: Dict[str, Any], subject: str) -> str:
    material = _clean(item.get("material_name")) or _clean(item.get("medical_type"), "病历")
    if material in {"急诊", "门诊", "住院", "证明", "影像", "其他"}:
        material = f"{material}病历" if material not in {"证明", "影像"} else ("诊断证明书" if material == "证明" else "影像检查报告")
    hospital = _clean(item.get("hospital_name"))
    record_date = _normalize_date(_clean(item.get("record_date")))
    suffix = _summary_record_number_suffix(item)
    kind = _summary_item_kind(item)

    if kind == "diagnosis_certificate":
        if hospital and subject:
            prefix = f"{subject}{record_date}在{hospital}治疗的" if record_date else f"{subject}在{hospital}治疗的"
        elif subject:
            prefix = f"{subject}{record_date}" if record_date else subject
        else:
            prefix = "送检"
        return f"据{prefix}{material}{suffix}记载："

    if hospital and subject:
        return f"据{subject}在{hospital}治疗的{material}{suffix}记载："
    if hospital:
        return f"据{hospital}{material}{suffix}记载："
    return f"据送检{material}{suffix}记载："


def _summary_is_low_value_for_render(item: Dict[str, Any]) -> bool:
    text = "\n".join(_flatten_strings(item))
    kind = _summary_item_kind(item)
    if any(k in text for k in ["婚育史", "月经史", "个人史", "家族史", "过敏史", "中医诊断", "实验室检查"]):
        useful = re.search(r"主诉|现病史|入院情况|专科检查|诊疗经过|手术|出院诊断|牙折|牙脱位|内固定无需取出", text)
        if useful is None:
            return True
    if kind == "imaging" and re.search(r"未见明显外伤性改变|年度复查|纤维灶|结节灶|脂肪肝|囊肿|返流|骨密度|T值", text):
        return True
    if kind == "inpatient" and re.search(r"出院情况|切口|要求出院|检验报告|尿分析|带药", text) and not re.search(r"入院情况|诊疗经过|手术|出院诊断", text):
        return True
    return False


def _summary_detail_parts(item: Dict[str, Any]) -> List[str]:
    kind = _summary_item_kind(item)
    parts: List[str] = []
    hospitalization = _clean(item.get("hospitalization_period"))
    record_date = _normalize_date(_clean(item.get("record_date")))
    if hospitalization:
        parts.append(f"住院时间：{_ensure_sentence(hospitalization)}")
    elif record_date and kind != "diagnosis_certificate":
        label = "入室时间" if kind == "emergency" and "出室小结" in _clean(item.get("material_name")) else "就诊时间"
        parts.append(f"{label}：{_ensure_sentence(record_date)}")

    field_plan = []
    if kind == "diagnosis_certificate":
        field_plan = [("诊断", "discharge_diagnosis"), ("建议", "treatment_advice")]
    elif kind == "inpatient":
        field_plan = [
            ("主诉", "chief_complaint"),
            ("现病史", "present_history"),
            ("专科情况", "specialist_findings"),
            ("体格检查", "physical_exam"),
            ("诊疗经过", "treatment_course"),
            ("术后复查拍片示", "post_op_review"),
            ("出院诊断", "discharge_diagnosis"),
            ("出院医嘱", "treatment_advice"),
        ]
    else:
        field_plan = [
            ("主诉", "chief_complaint"),
            ("现病史", "present_history"),
            ("体格检查", "physical_exam"),
            ("专科检查", "specialist_findings"),
            ("处理", "treatment_course"),
            ("诊断", "discharge_diagnosis"),
            ("处理意见", "treatment_advice"),
        ]

    imaging = _clean(item.get("imaging_findings"))
    if imaging and kind != "diagnosis_certificate":
        if not re.search(r"预约|办理登记|年度复查|纤维灶|结节灶|未见明显外伤性改变", imaging):
            if re.match(r"^(?:CT|DR|MRI|MR|X线|影像)(?:示|提示|诊断)[:：]", imaging):
                parts.append(_ensure_sentence(imaging))
            else:
                method = "MR" if re.search(r"MR|MRI|磁共振", "\n".join(_flatten_strings(item)), re.I) else "CT" if re.search(r"CT", "\n".join(_flatten_strings(item)), re.I) else "影像"
                parts.append(f"{method}示：{_ensure_sentence(imaging)}")

    for label, key in field_plan:
        value = _clean(item.get(key))
        if not value:
            continue
        if key == "post_op_review" and re.search(r"^(?:现病史|主诉|史当时住院治疗)", value):
            continue
        parts.append(f"{label}：{_ensure_sentence(value)}")
    return [p for p in parts if p and not any(bad in p for bad in ["不详", "未查找到", "待补充"])]


def _format_medical_summary_item(idx: int, item: Dict[str, Any], subject: str) -> str:
    title = _summary_source_title(item, subject)
    parts = _summary_detail_parts(item)
    if not parts:
        return ""
    text = "".join(parts).rstrip("。") + "。"
    text = re.sub(r"。+", "。", text)
    return f"{idx}.{title}{text}"


def _fallback_record_to_summary_item(record: Dict[str, Any]) -> Dict[str, Any]:
    diagnosis = _clean(record.get("diagnosis"))
    treatment = _clean(record.get("treatment_summary"))
    exam = _clean(record.get("key_exam_findings"))
    return {
        "subject_name": "",
        "record_date": _clean(record.get("visit_date")),
        "hospital_name": _clean(record.get("hospital_name")),
        "material_name": f"{_clean(record.get('medical_type'), '病历')}病历" if _clean(record.get("medical_type")) not in {"病历", "证明", "影像"} else _clean(record.get("medical_type"), "病历"),
        "record_number": "",
        "hospitalization_period": "",
        "chief_complaint": _clean(record.get("complaint_and_history")),
        "present_history": "",
        "imaging_findings": exam,
        "physical_exam": "",
        "specialist_findings": "",
        "treatment_course": treatment,
        "post_op_review": "",
        "discharge_diagnosis": diagnosis,
        "treatment_advice": "",
    }


def _format_other_summary_item(idx: int, item: Dict[str, Any], subject: str) -> str:
    institution = _clean(item.get("institution"), "外部司法鉴定机构")
    document = _clean(item.get("document_name"), "司法鉴定意见书")
    document_no = _clean(item.get("document_no"))
    document_date = _normalize_date(_clean(item.get("document_date")))
    entrusting = _clean(item.get("entrusting_party"), subject or "被鉴定人")
    opinion = _ensure_sentence(_clean(item.get("opinion_text")))
    if not opinion:
        return ""
    date_part = f"{document_date}，" if document_date else ""
    no_part = f"（{document_no}）" if document_no else ""
    return f"{idx}.因{entrusting}委托，{date_part}{institution}出具{document}{no_part}，鉴定意见：{opinion}"


def _medical_summary(structuring: Dict[str, Any], incident_date: str) -> List[str]:
    subject = _clean(structuring.get("subject_name"), "")
    summary_items = [item for item in (structuring.get("medical_summary_items") or []) if isinstance(item, dict)]
    if not summary_items:
        summary_items = [_fallback_record_to_summary_item(item) for item in (structuring.get("medical_records") or []) if isinstance(item, dict)]

    medical_lines: List[str] = []
    for item in summary_items[:10]:
        if _summary_is_low_value_for_render(item):
            continue
        line = _format_medical_summary_item(len(medical_lines) + 1, item, subject)
        if line and "影像检查资料记载" not in line:
            medical_lines.append(line)

    if not medical_lines:
        evidence_lines = [line for line in _evidence_medical_summary(structuring, incident_date) if "影像学检查资料" not in line]
        medical_lines = [re.sub(r"^\d+\.", f"{idx}.", line) for idx, line in enumerate(evidence_lines, 1)]

    if not medical_lines:
        medical_lines = ["1.据送检病历材料记载：相关诊疗经过详见送检材料。"]

    other_items = [item for item in (structuring.get("other_summary_items") or []) if isinstance(item, dict)]
    other_lines = []
    seen_other = set()
    for item in other_items[:8]:
        key = re.sub(r"\s+", "", "|".join([
            _clean(item.get("institution")),
            _clean(item.get("document_no")),
            _normalize_date(_clean(item.get("document_date"))),
            _clean(item.get("opinion_text"))[:80],
        ]))
        if key in seen_other:
            continue
        seen_other.add(key)
        line = _format_other_summary_item(len(other_lines) + 1, item, subject)
        if line:
            other_lines.append(line)
        if len(other_lines) >= 6:
            break

    if other_lines:
        return ["（一）病史摘要", *medical_lines, "", "（二）其他信息摘要", *other_lines]
    return medical_lines


def _has_rom(judge: Dict[str, Any], structuring: Dict[str, Any]) -> bool:
    section4 = judge.get("section4") or {}
    forensic_facts = structuring.get("forensic_facts") or {}
    return bool(section4.get("structured_rom") or judge.get("structured_rom") or forensic_facts.get("structured_rom"))


def _format_angle(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value or "").strip()
    return f"{number:g}°"


def _rom_lines_from_structured(rom: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for joint, side_map in (rom or {}).items():
        if not isinstance(side_map, dict):
            continue
        for side, direction_map in side_map.items():
            if not isinstance(direction_map, dict):
                continue
            parts = [f"{direction}约{_format_angle(angle)}" for direction, angle in direction_map.items() if not _is_unknown(angle)]
            if parts:
                lines.append(f"{side}{joint}" + "、".join(parts))
    return lines


def _rom_manual_findings(physical: Dict[str, Any]) -> List[str]:
    value = physical.get("rom_findings_manual")
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _has_length_measurement(judge: Dict[str, Any], structuring: Dict[str, Any]) -> bool:
    text = _all_text({"judge": judge, "structuring": structuring})
    return bool(re.search(r"(瘢痕|疤痕|创口|伤痕|长度|面积).{0,40}\d+(?:\.\d+)?\s*(?:cm|厘米|mm|毫米|%)", text))


def _tool_sentence(judge: Dict[str, Any], structuring: Dict[str, Any]) -> str:
    tools: List[str] = []
    if _has_rom(judge, structuring):
        tools.append("人体关节测量器（ZDSJ-A-002）")
    if _has_length_measurement(judge, structuring):
        tools.append("钢卷尺（ZDSJ-A-003）")
    if any(k in _all_text({"judge": judge, "structuring": structuring}) for k in ["CT", "MRI", "MR", "X线", "影像", "椎体", "骨折"]):
        tools.append("影像学阅片软件")
    if not tools:
        tools.append("影像学阅片软件")
    return "使用" + "、".join(_dedupe(tools)) + "等工具进行检验。"


def _imaging_auxiliary_lines(structuring: Dict[str, Any], injury_text: str) -> List[str]:
    all_text = _normalize_forensic_text(_all_text(structuring))
    events = (structuring.get("forensic_facts") or {}).get("imaging_events") or []
    event_findings: List[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        raw = _normalize_forensic_text(event.get("findings_raw"))
        if not raw or any(k in raw for k in ["鉴定材料", "U盘", "材料清单", "报告时间", "检查项目", "检查部位", "检查号", "CT号"]):
            continue
        if any(k in raw for k in ["对，因为", "我先", "第二次复查", "没有做手术", "诊疗经过", "现病史", "主诉", "处理内容", "门诊", "病历", "不详", "摔伤致", "原告-证据材料", "屈子豪"]):
            continue
        if not any(k in raw for k in ["影像所见", "影像诊断", "诊断结果", "CT示", "MRI提示", "MR提示", "胸腰椎曲度", "胸11、腰1椎体楔形压缩"]):
            continue
        if not any(k in raw for k in ["骨折", "压缩", "楔形", "变扁", "椎管", "硬膜囊"]):
            continue
        event_findings.append(raw.rstrip("。"))
    findings: List[str] = event_findings
    for pattern in [
        r"胸腰椎曲度存在[^\n。]{10,220}。",
        r"胸11、腰1椎体楔形压缩[^\n。]{10,220}。",
        r"CT(?:示|提示)[:：]?[^\n。]{5,160}。",
    ]:
        for match in re.finditer(pattern, all_text):
            findings.append(match.group(0).rstrip("。"))
    clean_findings: List[str] = []
    for finding in findings:
        if any(k in finding for k in ["原告-证据材料", "门诊 不详", "摔伤致腰椎骨折20余天；", "屈子豪", "患者要求转至上级医院"]):
            continue
        clean_findings.append(finding)
    findings = _dedupe(clean_findings, limit=3)
    lines = []
    for idx, finding in enumerate(findings, 1):
        finding = re.sub(r"^(?:CT|MRI|MR|X线|DR)(?:示|提示)[:：]?", "", finding).strip("：:， -")
        lines.append(f"{idx}.送检影像学检查资料：示{finding}。")
    if not lines:
        lines.append(f"1.送检材料提示{injury_text}。")
    return lines


def _section4(judge: Dict[str, Any], structuring: Dict[str, Any], injury_text: str) -> List[str]:
    section4 = judge.get("section4") or {}
    physical = section4.get("physical_exam") or {}
    aux = section4.get("auxiliary_checks") or {}
    if not aux and isinstance(section4.get("auxiliary_check"), dict):
        aux = section4.get("auxiliary_check") or {}
    subject = _subject_name(structuring.get("subject_name"), physical.get("subject_name"))
    subject_ref = _subject_ref(subject)
    subject_label = _subject_label(structuring.get("subject_name"), physical.get("subject_name"))

    lines = [
        "四、鉴定过程",
        "（一）检验方法",
        "参照司法鉴定技术规范《法医临床检验规范》（SF/T 0111-2021）和《法医临床影像学检验实施规范》（SF/T 0112-2021）等进行法医学检验及核实。",
        "（二）使用工具",
        _tool_sentence(judge, structuring),
        "（三）体格检查",
    ]
    complaint = _normalize_forensic_text(physical.get("chief_complaint"))
    manual_findings = _normalize_forensic_text(physical.get("manual_findings"))
    machine_findings = physical.get("machine_findings") or []
    findings = _dedupe([_normalize_forensic_text(x) for x in (physical.get("exam_findings") or [])] + [_normalize_forensic_text(x) for x in machine_findings], limit=6)
    forensic_facts = structuring.get("forensic_facts") or {}
    structured_rom = section4.get("structured_rom") or judge.get("structured_rom") or forensic_facts.get("structured_rom") or {}
    rom_lines = _rom_lines_from_structured(structured_rom)
    rom_manual = _rom_manual_findings(physical)
    # 人工已经写进现场检查的活动度数据，优先于人工作为唯一来源：
    # 再追加机器/视觉读数会在同一段里出现两套互相矛盾的角度
    # （曾出现人工写“屈曲 0°-120°”、下面又跟一行“屈曲约180°”）。
    rom_already_in_manual = bool(
        re.search(r"活动度|屈曲|伸展|背屈|跖屈|内收|外展|旋前|旋后|内旋|外旋|\d+\s*°", manual_findings or "")
    )
    if manual_findings:
        lines.append(manual_findings if manual_findings.endswith("。") else f"{manual_findings}。")
        if rom_already_in_manual:
            pass
        elif rom_lines:
            lines.append("关节活动度检查：" + "；".join(rom_lines) + "。")
        elif rom_manual:
            lines.append("关节活动度检查：" + "；".join(rom_manual) + "。")
    elif complaint or findings or rom_lines or rom_manual:
        intro = f"{subject_ref}{_clean(physical.get('entry_description'), '步入检查室')}。"
        lines.append(intro)
        if complaint:
            lines.append(f"主诉：{complaint}。")
        if findings:
            lines.append("查体：" + "；".join(findings).rstrip("。") + "。")
        if rom_lines:
            lines.append("关节活动度检查：" + "；".join(rom_lines) + "。")
        elif rom_manual:
            lines.append("关节活动度检查：" + "；".join(rom_manual) + "。")
    else:
        lines.append(f"本次未能从材料中形成稳定的现场体格检查摘录，建议结合原始检查记录或现场检验记录复核。")

    lines.append("（四）辅助检查")
    manual_aux_raw = aux.get("manual_review_opinion")
    manual_aux = str(manual_aux_raw).strip() if not _is_unknown(manual_aux_raw) else ""
    machine_aux = _clean(aux.get("machine_review_opinion"))
    if manual_aux:
        lines.extend(line.rstrip() for line in manual_aux.replace("\r", "\n").splitlines() if line.strip())
        return lines
    if machine_aux:
        lines.append(f"阅读送检的所有{subject_label}影像学及辅助检查资料，其中：")
        lines.extend(line.rstrip() for line in machine_aux.replace("\r", "\n").splitlines() if line.strip())
        return lines

    lines.append(f"阅读送检的所有{subject_label}影像学及辅助检查资料，其中：")
    aux_lines = []
    for item in aux.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        finding = _normalize_forensic_text(item.get("findings"))
        finding = re.split(r"\n\s*###|\n\s*##", finding)[0].strip()
        if not finding:
            continue
        if any(k in finding for k in ["###", "##", "附注", "诊疗经过", "诊断/结论", "患者要求转至上级医院", "鉴定材料", "材料清单", "U盘"]):
            continue
        hospital = _clean(item.get("hospital"))
        date = _normalize_date(item.get("exam_date"))
        exam_name = _clean(item.get("exam_name"))
        prefix = "".join([hospital, date, exam_name]) or "送检辅助检查资料"
        aux_lines.append(f"{len(aux_lines)+1}.{prefix}：示{finding.rstrip('。')}。")
    if not aux_lines:
        aux_lines = _imaging_auxiliary_lines(structuring, injury_text)
    lines.extend(aux_lines[:6])
    return lines


def _surgery_sentence(structuring: Dict[str, Any]) -> str:
    surgeries = _dedupe(structuring.get("surgery_history") or [], limit=3)
    if not surgeries:
        return ""
    subject = _subject_label(structuring.get("subject_name"))
    parts = []
    for item in surgeries:
        parts.append(item if "行" in item else f"行{item}")
    return f"{subject}伤后" + "；".join(parts) + "。"


def _positive_grade_text(result: Dict[str, Any]) -> str:
    grade = result.get("grade")
    return result.get("grade_cn") or GRADE_CN.get(grade, f"{grade}级")


def _int_value(value: Any) -> Any:
    if _is_unknown(value):
        return None
    text = str(value).strip()
    if any(token in text for token in ["不构成伤残", "未构成伤残", "不构成", "未构成"]):
        return 0
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


# 系统状态说明（而非可入文的条款内容）。这些文字一旦被当成条款原文或后遗表现
# 写进正式文书，就会读成“目前遗留事实不足，暂不能自动判断伤残相关后遗表现”。
_STATUS_TEXT_RE = re.compile(r"事实不足|不能自动判断|无法判断|暂不宜|需人工|待补充|未形成|不足以|未查找到")


def _looks_like_status_text(value: Any) -> bool:
    """判断一段文字是“系统状态说明”还是可入文的正文内容。"""
    text = _clean(value)
    if not text:
        return True
    return bool(_STATUS_TEXT_RE.search(text)) or text == "相关伤残等级依据"


def _looks_like_disability_conclusion(value: Any) -> bool:
    """判断人工填写的文字能否落到“……规定，<结论>。”这个结论位置上。

    判定刻意保守：只有确实在谈“伤残/等级”的句子才放行。该字段历史上语义混乱——
    阴性流程存结论句、阳性流程存条款原文、早期版本还存过整段伤情清单和条款标签
    （实测 005 为“……膝前皮肤擦伤等损伤。”、037 为“开颅术后”），
    这些值落到结论位置会写出没有谓语的病句，一律回退到固定表述。
    """
    text = _clean(value)
    if not text or _looks_like_status_text(text):
        return False
    return "伤残" in text or "等级" in text


def _authoritative_clause_text(clause: Any) -> str:
    """从 Ruler 的条款索引取该条款的权威表述。

    鉴定人确认伤残时往往只填条款号；没有条款内容的话，正文只能写成
    “标准之5.10.6.13条之规定”，读起来缺了最关键的一句。这里用标准原文补全。
    """
    key = re.sub(r"\s+", "", _clean(clause)).replace("第", "").rstrip("条")
    if not key:
        return ""
    try:
        from agents.ruler_agent.ruler_agent_main import DISABILITY_CLAUSE_INDEX
    except Exception:
        return ""
    for item in DISABILITY_CLAUSE_INDEX:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        candidate = re.sub(r"\s+", "", str(item.get("clause") or "")).replace("第", "").rstrip("条")
        if candidate == key:
            return _clean(item.get("text"))
    return ""


def _human_disability_confirmation(structuring: Dict[str, Any]) -> Dict[str, Any]:
    forensic = structuring.get("forensic_facts") or {}
    value = forensic.get("human_disability_confirmation") or {}
    return value if isinstance(value, dict) else {}


def _apply_human_disability_confirmation(structuring: Dict[str, Any], positives: List[Dict[str, Any]], negatives: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    confirmation = _human_disability_confirmation(structuring)
    conclusion_type = _clean(confirmation.get("conclusion_type"))
    grade = _int_value(confirmation.get("grade_number"))
    reason = _clean(confirmation.get("reason"))
    standard_name = _clean(confirmation.get("standard_name"))
    clause = _clean(confirmation.get("clause"))
    clause_text = _clean(confirmation.get("clause_text"))
    if conclusion_type == "no_disability":
        grade = 0
    if grade is None:
        return positives, negatives, reason
    if grade <= 0:
        negative = dict(negatives[0]) if negatives else {"grade": 0, "text": "其损伤及后遗症尚未构成伤残等级"}
        negative["grade"] = 0
        negative["conclusion_type"] = "no_disability"
        if "人工" in str(negative.get("text") or ""):
            negative["text"] = "其损伤及后遗症尚未构成伤残等级"
        if standard_name:
            negative["standard_name"] = standard_name
        if clause:
            negative["clause"] = clause
        # 人工填写的结论表述写进 clause_text 作为“确实由人工填写”的标记（Ruler 自身的
        # 阴性结果不含该键），_section5_6 据此覆盖固定表述。状态说明不进正文。
        if clause_text and not _looks_like_status_text(clause_text):
            negative["text"] = clause_text
            negative["clause_text"] = clause_text
        return [], [negative], reason
    source = dict(positives[0]) if positives else {}
    source["grade"] = grade
    source["grade_cn"] = GRADE_CN.get(grade, f"{grade}级")
    source.setdefault("text", "相关伤残等级依据")
    source.setdefault("clause", "相关")
    if standard_name:
        source["standard_name"] = standard_name
    if clause:
        source["clause"] = clause
    # 只采信真正像条款内容的文字；状态说明不进正文。
    if clause_text and not _looks_like_status_text(clause_text):
        source["text"] = clause_text
        source["clause_text"] = clause_text
    elif clause:
        authoritative = _authoritative_clause_text(clause)
        if authoritative:
            source["text"] = authoritative
            source["clause_text"] = authoritative
    return [source], [], reason


def _confirmed_period_result(structuring: Dict[str, Any], period: Dict[str, Any]) -> Dict[str, Any]:
    forensic = structuring.get("forensic_facts") or {}
    features = forensic.get("period_rule_features") or {}
    confirmed = features.get("confirmed_period_result") or {}
    if not isinstance(confirmed, dict):
        return period
    mapping = {
        "误工": ["work_loss_days", "误工", "误工期"],
        "护理": ["nursing_days", "护理", "护理期"],
        "营养": ["nutrition_days", "营养", "营养期"],
    }
    merged = dict(period or {})
    changed = False
    for report_key, keys in mapping.items():
        for key in keys:
            parsed = _int_value(confirmed.get(key))
            if parsed is not None:
                merged[report_key] = parsed
                changed = True
                break
    confirmed_clause = _clean(confirmed.get("clause"))
    for key in ["standard_name", "clause", "clause_text", "sub_label"]:
        text = _clean(confirmed.get(key))
        if key == "sub_label" and confirmed_clause and not confirmed_clause.startswith("9.1") and "脊柱" in text:
            continue
        if text:
            merged[key] = text
            changed = True
    if confirmed_clause and not confirmed_clause.startswith("9.1") and "脊柱" in _clean(merged.get("sub_label")):
        merged.pop("sub_label", None)
        changed = True
    if confirmed.get("reason"):
        merged["human_confirm_reason"] = _clean(confirmed.get("reason"))
        changed = True
    return merged if changed else period


def _final_metadata(structuring: Dict[str, Any]) -> Dict[str, Any]:
    metadata = structuring.get("final_metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _footer_value(metadata: Dict[str, Any], key: str, default: str = MISSING_TEXT) -> str:
    value = metadata.get(key)
    if _is_unknown(value):
        return default
    text = str(value).strip()
    return text or default


def _report_document_no_line(structuring: Dict[str, Any]) -> str:
    metadata = _final_metadata(structuring)
    year = _clean(metadata.get("report_doc_year"))
    serial = _clean(metadata.get("report_doc_serial"))
    full_no = _clean(metadata.get("report_doc_no"))
    if full_no:
        return full_no
    if year or serial:
        return f"浙大中心[{year or 'xxxx'}]临鉴字第{serial or 'xxxx'}号"
    return ""


def _section7_footer(structuring: Dict[str, Any], subject: str, report_date: str) -> List[str]:
    metadata = _final_metadata(structuring)
    fixed_keys = [
        "exam_photo_count",
        "imaging_photo_count",
        "appraiser1_name",
        "appraiser1_license_no",
        "appraiser2_name",
        "appraiser2_license_no",
        "report_date",
    ]
    has_fixed_footer = any(not _is_unknown(metadata.get(key)) for key in fixed_keys)
    if has_fixed_footer:
        date_text = _clean(metadata.get("report_date"), report_date or MISSING_TEXT)
        return [
            "七、附件",
            f"附{_subject_ref(subject)}检查照片{_footer_value(metadata, 'exam_photo_count')}张，影像学扫描照片{_footer_value(metadata, 'imaging_photo_count')}张。",
            "",
            f"司法鉴定人签名：{_footer_value(metadata, 'appraiser1_name')}",
            f"《司法鉴定人执业证》证号：{_footer_value(metadata, 'appraiser1_license_no')}",
            f"司法鉴定人签名：{_footer_value(metadata, 'appraiser2_name')}",
            f"《司法鉴定人执业证》证号：{_footer_value(metadata, 'appraiser2_license_no')}",
            "",
            "浙江大学司法鉴定中心",
            f"                             {date_text}",
        ]

    footer_raw = metadata.get("footer_block")
    footer_block = str(footer_raw).strip() if not _is_unknown(footer_raw) else ""
    if footer_block:
        lines = [line.rstrip() for line in footer_block.replace("\r", "\n").splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines or not lines[0].strip().startswith("七、附件"):
            lines.insert(0, "七、附件")
        return lines

    return [
        "七、附件",
        f"附{_subject_ref(subject)}检查照片、影像学扫描照片等。",
    ]


def _section5_6(structuring: Dict[str, Any], ruler: Dict[str, Any], injury_text: str, incident_date: str) -> Tuple[List[str], List[str]]:
    subject = _subject_name(structuring.get("subject_name"))
    subject_ref = _subject_ref(subject)
    subject_label = _subject_label(structuring.get("subject_name"))
    section5 = ruler.get("section5") or {}
    results = section5.get("disability_results") or []
    positives = [r for r in results if r.get("grade", 0) > 0]
    negatives = [r for r in results if r.get("grade", 0) == 0]
    positives, negatives, disability_reason = _apply_human_disability_confirmation(structuring, positives, negatives)
    wants_disability, wants_period, wants_fee = _commission_scope_flags(structuring)
    period = section5.get("period_result") or {}
    has_ruler_period = bool(isinstance(period, dict) and (period.get("clause") or period.get("误工") or period.get("护理") or period.get("营养")))
    if wants_period or has_ruler_period:
        period = _confirmed_period_result(structuring, period)
    else:
        period = {}
    surgery = _surgery_sentence(structuring)

    analysis = [
        "五、分析说明",
        "经审核所送的鉴定材料，结合本中心检验所见，分析如下：",
        f"根据{subject_ref}于{incident_date or '本次'}的外伤史，损伤后的病史记载，结合审阅其损伤后的影像学及相关检查资料，其因本次外伤致{injury_text}的损伤事实可予认定。",
    ]
    if surgery:
        analysis.append(surgery)
    analysis.append(f"经治疗，目前{subject_label}伤情已稳定，符合鉴定时机要求。")

    opinion: List[str] = ["六、鉴定意见"]

    if wants_disability and positives:
        analysis.append("1.伤残等级：")
        for r in positives:
            clause = _clean(r.get("clause"))
            text = _clean(r.get("clause_text"), _clean(r.get("text")))
            standard_name = _clean(r.get("standard_name"), DISABILITY_STANDARD_NAME)
            grade_text = _positive_grade_text(r)
            match_type = _clean(r.get("match_type"))
            if "spine" in match_type or "椎体" in text:
                details = (r.get("details") or [{}])[0] if isinstance(r.get("details"), list) and r.get("details") else {}
                levels = details.get("vertebra_levels") or []
                if isinstance(levels, str):
                    levels = [levels]
                levels = sorted([str(x) for x in levels if x], key=_spine_sort_key)
                level_text = "、".join(_level_to_cn(str(x)) for x in levels if x)
                evidence = f"结构化影像事实显示{level_text}椎体压缩性骨折" if level_text else f"结构化事实显示{text}"
                analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}。{evidence}。因此，依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，{subject_label}已构成{grade_text}伤残。")
            elif match_type == "joint_function_loss":
                details = r.get("details") or []
                detail_parts = []
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    direction = _clean(item.get("direction"))
                    angle = item.get("injured_angle")
                    net_loss = item.get("net_loss")
                    if direction and not _is_unknown(angle):
                        detail_parts.append(f"{direction}{_format_angle(angle)}（净丧失{net_loss}%）")
                joint_ref = f"{_clean(r.get('injured_side'))}{_clean(r.get('joint'))}".strip() or "相关关节"
                detail_text = "、".join(detail_parts) if detail_parts else "活动度测量结果"
                loss_pct = r.get("loss_pct", 0)
                analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}，目前遗留{joint_ref}活动功能受限。经关节活动度测算，{detail_text}，功能丧失为{loss_pct}%。依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，评定为{grade_text}伤残。")
            elif "scar" in match_type or "burn" in match_type or "disfigurement" in match_type or any(k in text for k in ["瘢痕", "烧伤", "容貌毁损"]):
                details = (r.get("details") or [{}])[0] if isinstance(r.get("details"), list) and r.get("details") else {}
                evidence = "；".join(_dedupe(_flatten_strings(details.get("evidence")), limit=3)) if isinstance(details, dict) else ""
                fact_part = f"送检材料及检验所见提示{evidence}。" if evidence else f"送检材料及检验所见提示{injury_text}。"
                analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}，目前遗留烧伤后瘢痕/容貌损害等后遗表现。{fact_part}依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，评定为{grade_text}伤残。")
            elif "eye" in match_type or "visual" in match_type or "眼" in text:
                analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}，目前遗留眼部功能损害。依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，评定为{grade_text}伤残。")
            elif _looks_like_status_text(text):
                # 只有条款号、没有条款内容时，不要硬套“目前遗留……相关后遗表现”句式，
                # 否则系统状态说明会被拼成病句。
                analysis.append(
                    f"经检验，{subject_ref}因本次外伤致{injury_text}。"
                    f"依照{standard_name}标准之{_clause_ref(clause)}之规定，评定为{grade_text}伤残。"
                )
            elif text and text == _authoritative_clause_text(clause):
                # text 是标准条款原文（而非“后遗表现”），套进“目前遗留……相关后遗表现”会读成病句。
                analysis.append(
                    f"经检验，{subject_ref}因本次外伤致{injury_text}。"
                    f"依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，评定为{grade_text}伤残。"
                )
            else:
                residual = text
                analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}，目前遗留{residual}相关后遗表现。依照{standard_name}标准之{_clause_ref(clause)}“{text}”之规定，评定为{grade_text}伤残。")
        first = positives[0]
        grade_text = _positive_grade_text(first)
        opinion_standard = _clean(first.get("standard_name"), DISABILITY_STANDARD_NAME)
        surgery_part = "经手术治疗，" if structuring.get("surgery_history") else ""
        opinion.append(f"{subject_ref}于{incident_date or '本次'}因外伤致{injury_text}，{surgery_part}依据{opinion_standard}标准之相关规定，已构成{grade_text}伤残。")
        if negatives:
            analysis.append(f"经检验，{subject_ref}其余损伤及后遗症尚未构成伤残等级。")
    elif wants_disability:
        analysis.append("1.伤残等级：")
        # Ruler 用 conclusion_status 区分“确实不构成”与“系统判不出”。二者绝不能混为一谈：
        # 把 insufficient_facts / manual_review 写成“尚未构成伤残等级”，等于把“不知道”当成“否”，
        # 在司法鉴定场景属于错误结论，必须改写成非结论性表述并转人工。
        conclusion_status = _clean(section5.get("disability_conclusion_status"))
        # 只有“Ruler 判不出 且 人工未表态”时才写成非结论性表述。
        # 人工一旦明确确认了等级（含确认不构成伤残），就以人工为准；
        # 否则鉴定人刚填的结论会被 Ruler 的 insufficient_facts 状态静默盖掉。
        human_decided = _int_value(_human_disability_confirmation(structuring).get("grade_number")) is not None
        if conclusion_status in {"insufficient_facts", "manual_review"} and not human_decided:
            reason = _clean(section5.get("disability_conclusion_label")) or "现有材料尚不足以对伤残等级作出判断"
            analysis.append(
                f"经检验，{subject_ref}因本次外伤致{injury_text}，经治疗后目前伤情稳定。"
                f"但{reason}。需补充关节活动度测量、影像学所见等检验资料后，由鉴定人评定其伤残等级。"
            )
            opinion.append(
                f"{subject_ref}于{incident_date or '本次'}因外伤致{injury_text}，"
                f"{reason}，需补充相关检验资料后由鉴定人评定伤残等级。"
            )
        else:
            negative = negatives[0] if negatives else {}
            negative_standard = _clean(negative.get("standard_name"), DISABILITY_STANDARD_NAME)
            negative_clause = _clean(negative.get("clause"))
            clause_part = f"之{_clause_ref(negative_clause)}" if negative_clause else "相关"
            # 人工在审计窗口填写的结论表述优先。
            # 不能读 negative["text"]：未填人工内容时它可能是 Ruler 的内部标签，
            # 实测出现过“开颅术后”“尚未构成伤残等级”和整段伤情清单，直接入文会写出病句。
            # 只有人工路径会写入 clause_text（见 _apply_human_disability_confirmation）。
            human_negative_text = _clean(negative.get("clause_text"))
            negative_text = (
                human_negative_text
                if _looks_like_disability_conclusion(human_negative_text)
                else "其损伤及后遗症尚未构成伤残等级"
            )
            analysis.append(f"经检验，{subject_ref}因本次外伤致{injury_text}，经治疗后目前伤情稳定。依照{negative_standard}标准{clause_part}规定，{negative_text.rstrip('。；;')}。")
            opinion.append(f"{subject_ref}于{incident_date or '本次'}因外伤致{injury_text}，其损伤及后遗症尚未构成伤残等级。")

    if wants_disability and disability_reason and (positives or negatives):
        analysis.append(f"经复核，{disability_reason.rstrip('。；;')}。")

    has_period_opinion = bool(period.get("误工", 0) or period.get("护理", 0) or period.get("营养", 0))
    has_period_section = False
    if has_period_opinion:
        clause = _clean(period.get("clause"))
        label = _clean(period.get("sub_label"))
        standard_name = _clean(period.get("standard_name"), PERIOD_STANDARD_NAME)
        clause_label = f"之{clause}（{label}）" if clause and label else f"之{clause}" if clause else ""
        period_number = "2" if wants_disability else "1"
        period_sentence = (
            f"{subject_ref}损伤后，因治疗和康复需要而无法参加工作，需要休息，存在误工。"
            "生活自理能力下降，存在一定时期内生活不能完全自理，需要他人帮助的情况。"
            "因创伤可导致机体代谢异常，短期内可能单纯依靠日常饮食不能满足受损机体对热能和各种营养的需求，"
            "须从其他食品中获得营养。因此，"
            f"依照{standard_name}{clause_label}及附录A等相关条款规定，结合其损伤程度、治疗和实际恢复情况，"
            f"建议其所需的误工期限以{period.get('误工', 0)}日左右为宜，"
            f"护理期限以{period.get('护理', 0)}日左右为宜，营养期限以{period.get('营养', 0)}日左右为宜。"
        )
        # 鉴定人工复核三期时填写的理由此前被静默丢弃，这里补进分析说明。
        period_reason = _clean(period.get("human_confirm_reason"))
        if period_reason:
            period_sentence = period_sentence.rstrip("。") + f"。经复核，{period_reason.rstrip('。')}。"
        analysis.extend([
            f"{period_number}.误工期限、护理期限、营养期限：",
            period_sentence,
        ])
        opinion.append(f"建议{subject_ref}因本次损伤所需的误工期限以{period.get('误工', 0)}日左右为宜，护理期限以{period.get('护理', 0)}日左右为宜，营养期限以{period.get('营养', 0)}日左右为宜。")
        has_period_section = True
    elif wants_period:
        period_number = "2" if wants_disability else "1"
        analysis.extend([
            f"{period_number}.误工期限、护理期限、营养期限：",
            f"现有送检材料中有关{subject_label}休治、护理和营养支持期限的依据尚不充分，不能据此形成明确期限意见；建议结合完整病历、诊断证明、出院记录及损伤恢复情况综合评定。",
        ])
        opinion.append(f"{subject_ref}误工期限、护理期限、营养期限因现有依据尚不充分，暂不宜作出明确天数评定。")
        has_period_section = True

    if wants_fee:
        number = str(1 + int(wants_disability) + int(has_period_section))
        analysis.extend([
            f"{number}.医疗费合理性：",
            "送检材料中涉及医疗费合理性审查事项。因医疗费用合理性需结合完整票据、费用清单、诊疗必要性及与本次外伤关联性逐项核对，现有材料不足以由系统自动形成最终费用审查意见，需由鉴定人员结合补充费用材料后评定。",
        ])
        opinion.append(f"{subject_ref}医疗费合理性事项，需结合完整医疗费票据、费用清单及诊疗关联性材料另行审查确认。")

    return analysis, opinion


def render_report(structuring: Dict[str, Any], judge: Dict[str, Any], ruler: Dict[str, Any], report_date: str = "") -> str:
    injury_text = _injury_facts(structuring, ruler)
    incident_date = _incident_date(structuring)
    # 受理日期与报告落款日期是两回事，缺失时留待补充标记，不能用报告日期顶替。
    accepted_date = _clean(structuring.get("acceptance_date"), MISSING_TEXT)
    present_raw = structuring.get("present_personnel")
    if isinstance(present_raw, list):
        present_personnel = "、".join(_clean(x) for x in present_raw if _clean(x)) or MISSING_TEXT
    else:
        present_personnel = _clean(present_raw, MISSING_TEXT)

    header_lines = ["浙江大学司法鉴定中心", "司法鉴定意见书"]
    document_no_line = _report_document_no_line(structuring)
    if document_no_line:
        header_lines.append(document_no_line)

    lines: List[str] = [
        *header_lines,
        "",
        "一、基本情况",
        f"委托人：{_clean(structuring.get('client_name'), MISSING_TEXT)}",
        f"委托鉴定事项：{_entrustment_text(structuring)}",
        f"受理日期：{accepted_date or MISSING_TEXT}",
        "鉴定材料：",
    ]
    lines.extend(_materials_lines(structuring, injury_text))
    lines.extend([
        f"鉴定地点：{_clean(structuring.get('appraisal_location'), '浙江大学司法鉴定中心')}",
        f"在场人员：{present_personnel}",
        f"被鉴定人：{_subject_sentence(structuring)}",
        "",
        "二、基本案情",
    ])
    lines.extend(_case_narrative(structuring, injury_text, incident_date))
    lines.extend(["", "三、资料摘要"])
    lines.extend(_medical_summary(structuring, incident_date))
    lines.append("")
    lines.extend(_section4(judge, structuring, injury_text))
    lines.append("")
    section5, section6 = _section5_6(structuring, ruler, injury_text, incident_date)
    lines.extend(section5)
    lines.append("")
    lines.extend(section6)
    lines.append("")
    lines.extend(_section7_footer(structuring, _subject_name(structuring.get("subject_name")), report_date))
    # 人工针对正文具体行填写的文字在此替换回去，保证重渲染后人工修改不丢。
    lines = _apply_human_text_patches(lines, _human_text_patches(structuring))
    rendered = "\n".join(lines).strip() + "\n"
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    rendered = rendered.replace("UNKNOWN", "需复核").replace("N/A", "需复核")
    rendered = re.sub(r"[xX×]{3,}", "需复核", rendered)
    # 只丢弃“整行就是占位符”的空行；含实质内容的行一律保留，
    # 缺失项以可见的待补充标记暴露，并同步进入 06_boss_audit.md。
    rendered = "\n".join(
        line for line in rendered.split("\n") if not _is_placeholder_stub(line)
    )
    # 兜底归一化：即使上游再出现兜底值与模板前缀叠写，也不会把病句发出去。
    rendered = rendered.replace(f"{SUBJECT_TITLE}{SUBJECT_TITLE}", SUBJECT_TITLE)
    rendered = re.sub(r"条{2,}", "条", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"
    return rendered
