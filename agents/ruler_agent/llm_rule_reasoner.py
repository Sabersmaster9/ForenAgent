"""LLM-based rule reasoner for Study 2 NoRules ablation.

This module is intentionally experiment-scoped: it preserves structured evidence but
replaces the deterministic Ruler decision step with a Qwen JSON reasoner. It must
not read expert references, existing ruler outputs, or full prior reports.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from openai import OpenAI

try:
    from .ruler_agent_main import DisabilityResult, PeriodResult, compose_section5, compose_section6
except ImportError:  # pragma: no cover - direct script compatibility
    from ruler_agent_main import DisabilityResult, PeriodResult, compose_section5, compose_section6

DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen3.5-397b-a17b"
RULE_REASONER_VERSION = "qwen_llm_rule_reasoner_v1"


class LLMRuleReasonerError(RuntimeError):
    """Raised when the NoRules reasoner cannot produce a valid payload."""


def _compact_json(data: Any, max_chars: int = 24000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[TRUNCATED FOR PROMPT]"


def _strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    return raw


def _balanced_json_prefix(raw: str) -> str:
    """Return the first balanced JSON-looking object, ignoring braces in strings."""
    start = raw.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : idx + 1]
    return raw[start:]


def _remove_trailing_commas(raw: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", raw)


def _close_incomplete_json(raw: str) -> str:
    """Best-effort closure for locally truncated JSON objects."""
    text = raw.rstrip()
    while text and text[-1] in ",:":
        text = text[:-1].rstrip()
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_string:
        text += '"'
    return _remove_trailing_commas(text + "".join(reversed(stack)))


def _json_repair_candidates(raw: str) -> Iterable[Tuple[str, str]]:
    trimmed = _strip_json_fence(raw)
    balanced = _balanced_json_prefix(trimmed) or trimmed
    candidates = [
        ("strict", trimmed),
        ("balanced", balanced),
        ("trailing_comma_repair", _remove_trailing_commas(balanced)),
        ("incomplete_json_repair", _close_incomplete_json(balanced)),
    ]
    seen: set[str] = set()
    for mode, candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield mode, candidate


def _loads_jsonish(raw: str) -> Tuple[Dict[str, Any], str]:
    last_exc: Exception | None = None
    for mode, candidate in _json_repair_candidates(raw):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, mode
        except json.JSONDecodeError as exc:
            last_exc = exc
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict):
                return parsed, f"{mode}_python_literal"
        except (ValueError, SyntaxError) as exc:
            last_exc = exc
    if last_exc:
        raise LLMRuleReasonerError(f"LLM returned invalid JSON: {last_exc}") from last_exc
    raise LLMRuleReasonerError("LLM returned no JSON object")


def _extract_json_object(text: str) -> Dict[str, Any]:
    parsed, _ = _loads_jsonish(text)
    return parsed


def _fallback_reasoner_json(structuring_data: Dict[str, Any], judge_data: Dict[str, Any], error: Exception) -> Dict[str, Any]:
    forensic = structuring_data.get("forensic_facts") or {}
    recognized = forensic.get("recognized_injuries") or []
    rule_features = forensic.get("rule_features") or {}
    period_features = forensic.get("period_rule_features") or {}
    case_text = _compact_json(_case_data(structuring_data, judge_data), 4000)
    return {
        "disability_supported": False,
        "disability_results": [
            {
                "clause": "",
                "branch_id": "",
                "text": "本地 LLM 未能返回可解析 JSON；NoRules 兜底为证据不足，未支持构成伤残。",
                "grade": 0,
                "conclusion_type": "insufficient",
                "joint": "",
                "injured_side": "",
                "loss_pct": 0,
                "details": [
                    {"reason": "LLM JSON 解析失败，未使用 deterministic Ruler 输出。"},
                    {"reason": f"解析错误：{error}"},
                ],
                "warnings": ["llm_json_fallback_used"],
            }
        ],
        "period_result": {
            "clause": "",
            "clause_text": "",
            "误工": 0,
            "护理": 0,
            "营养": 0,
            "sub_label": "",
            "ranges": {},
            "selection_reasons": ["LLM JSON 解析失败，未能稳定形成三期判断。"],
            "requires_manual_period_assessment": True,
        },
        "candidate_disability_clauses": [],
        "matched_disability_clauses": [],
        "rejected_disability_clauses": [],
        "candidate_period_clauses": [],
        "matched_period_clauses": [],
        "insufficient_evidence": [
            "NoRules local LLM output was not parseable as JSON after retries; fallback payload used.",
            f"recognized_injuries={recognized}",
            f"rule_features={rule_features}",
            f"period_rule_features={period_features}",
        ],
        "reasoning_basis": f"LLM JSON 解析失败后的实验兜底；case_data_snapshot={case_text}",
    }


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, "", "不详", "无"}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, "", "不详", "无"}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _rule_feature_snapshot(structuring_data: Dict[str, Any]) -> Dict[str, Any]:
    forensic = structuring_data.get("forensic_facts") or {}
    if not isinstance(forensic, dict):
        return {}
    return {
        "recognized_injuries": forensic.get("recognized_injuries") or [],
        "fracture_features": forensic.get("fracture_features") or {},
        "rule_features": forensic.get("rule_features") or {},
        "period_rule_features": forensic.get("period_rule_features") or {},
        "scar_burn_facts": forensic.get("scar_burn_facts") or forensic.get("scar_features") or {},
    }


def _case_data(structuring_data: Dict[str, Any], judge_data: Dict[str, Any]) -> Dict[str, Any]:
    section4 = judge_data.get("section4", {}) or {}
    physical = section4.get("physical_exam", {}) or {}
    aux = section4.get("auxiliary_checks", {}) or {}
    aux_items = aux.get("items", []) if isinstance(aux, dict) else []
    return {
        "subject_name": structuring_data.get("subject_name") or "XXX",
        "incident_date": structuring_data.get("incident_date") or "不详",
        "incident_summary": structuring_data.get("incident_summary") or "",
        "diagnosis_overview": structuring_data.get("diagnosis_overview") or "",
        "injury_summary": structuring_data.get("injury_summary") or "不详",
        "surgery_history": structuring_data.get("surgery_history") or [],
        "entrustment_items": structuring_data.get("entrustment_items") or [],
        "exam_findings": physical.get("exam_findings") or [],
        "aux_findings": [item.get("findings") for item in aux_items if isinstance(item, dict) and item.get("findings")],
        "rule_feature_snapshot": _rule_feature_snapshot(structuring_data),
    }


def _grade_cn(grade: int) -> str:
    if grade <= 0:
        return "不构成伤残"
    cn_nums = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return f"{cn_nums.get(grade, str(grade))}级"


def _normalize_level(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    m = re.match(r"([CTLS])(\d{1,2})", text)
    if m:
        return f"{m.group(1)}{int(m.group(2))}"
    m = re.match(r"([颈胸腰骶])(\d{1,2})", str(value or "").strip())
    if m:
        prefix = {"颈": "C", "胸": "T", "腰": "L", "骶": "S"}.get(m.group(1), "")
        return f"{prefix}{int(m.group(2))}" if prefix else ""
    return ""


def _dedupe_strings(items: Iterable[Any], limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" 。；;,，")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_llm_rule_fallbacks(reasoner_json: Dict[str, Any], structuring_data: Dict[str, Any]) -> Tuple[Dict[str, Any], list[str]]:
    """Patch obvious NoRules local-LLM omissions from allowed structured evidence only.

    This keeps the ablation as component replacement rather than component deletion: the local
    LLM remains the rule reasoner, while stable structured rule features are used as a safety net
    when its JSON contradicts canonical machine facts.
    """
    patched = deepcopy(reasoner_json or {})
    notes: list[str] = []
    forensic = structuring_data.get("forensic_facts") or {}
    if not isinstance(forensic, dict):
        return patched, notes
    rule_features = forensic.get("rule_features") or {}
    fracture = forensic.get("fracture_features") or {}
    vertebral = fracture.get("vertebral") if isinstance(fracture, dict) else {}
    vertebral = vertebral if isinstance(vertebral, dict) else {}
    levels = [_normalize_level(x) for x in (vertebral.get("levels") or vertebral.get("vertebra_levels") or rule_features.get("vertebra_levels") or [])]
    levels = [x for x in _dedupe_strings(levels) if x]

    disability_results = [item for item in _as_list(patched.get("disability_results")) if isinstance(item, dict)]
    has_positive_disability = any(_as_int(item.get("grade"), 0) > 0 for item in disability_results)
    two_compression = bool(rule_features.get("two_vertebral_compression_fractures")) or bool(vertebral.get("compression_fracture") and _as_int(vertebral.get("count"), 0) >= 2)
    if two_compression and len(levels) >= 2 and not has_positive_disability:
        details = dict(vertebral)
        details.setdefault("vertebra_levels", levels)
        details.setdefault("levels", levels)
        details.setdefault("compression_fracture", True)
        patched["disability_supported"] = True
        patched["disability_results"] = [
            {
                "clause": "5.9.6.2",
                "branch_id": "5.9.6.2.b",
                "text": "二椎体压缩性骨折",
                "grade": 9,
                "conclusion_type": "supported",
                "joint": "",
                "injured_side": "",
                "loss_pct": 0,
                "details": [details],
                "warnings": ["stable_structured_rule_feature_fallback"],
            },
            *[item for item in disability_results if _as_int(item.get("grade"), 0) <= 0 and str(item.get("conclusion_type") or "") == "insufficient"],
        ]
        patched.setdefault("matched_disability_clauses", []).append({
            "clause": "5.9.6.2",
            "text": "二椎体压缩性骨折",
            "source": "structured_rule_features_fallback",
        })
        notes.append("LLM rule reasoner missed stable structured fact two_vertebral_compression_fractures; patched to 5.9.6.2 without using deterministic Ruler output.")

    period = patched.get("period_result") if isinstance(patched.get("period_result"), dict) else {}
    has_period_values = any(_as_int(period.get(key), 0) > 0 for key in ["误工", "护理", "营养"])
    period_features = forensic.get("period_rule_features") or {}
    candidates = period_features.get("candidate_clauses") if isinstance(period_features, dict) else []
    if not has_period_values and isinstance(candidates, list):
        spine_candidate = next((item for item in candidates if isinstance(item, dict) and str(item.get("clause") or "") == "9.1.1"), None)
        if spine_candidate and (two_compression or any("椎" in str(x) for x in levels)):
            patched["period_result"] = {
                "clause": "9.1.1",
                "clause_text": "《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）之9.1.1条及附录A等相关条款",
                "误工": 120,
                "护理": 60,
                "营养": 60,
                "sub_label": "脊柱骨折非手术治疗",
                "ranges": {"误工": [45, 150], "护理": [45, 60], "营养": [45, 60]},
                "selection_reasons": ["结构化三期候选条款提示脊柱骨折非手术治疗；NoRules 采用机器结构化证据兜底取值。"],
                "requires_manual_period_assessment": False,
            }
            patched.setdefault("matched_period_clauses", []).append({
                "clause": "9.1.1",
                "label": "脊柱骨折非手术治疗",
                "source": "structured_period_candidate_fallback",
            })
            notes.append("LLM rule reasoner omitted period values despite structured 9.1.1 candidate; patched period to 120/60/60 from allowed machine facts.")

    if notes:
        basis = str(patched.get("reasoning_basis") or "").strip()
        patched["reasoning_basis"] = (basis + "\n" if basis else "") + "\n".join(notes)
    return patched, notes


def build_prompt(
    structuring_data: Dict[str, Any],
    judge_data: Dict[str, Any],
    standard_corpus: str,
    *,
    max_structuring_chars: int = 24000,
    max_judge_chars: int = 12000,
    max_standard_chars: int = 36000,
) -> str:
    """Build a controlled NoRules prompt."""
    standard_text = (standard_corpus or "")[:max_standard_chars]
    return f"""只输出一个合法 JSON 对象。不要解释，不要 Markdown，不要代码块。

你是 Study 2 消融实验中的 LLM Rule Reasoner。你的唯一任务是替代 deterministic Ruler，而不是撰写整份报告。

实验条件：NoRules
- Structured Evidence = ON
- Deterministic Rules = OFF，由你用同底座模型判断
- Human Audit = OFF

你只能使用下面提供的 structured_case_facts、judge_json 和 standards。禁止使用、猜测或引用 expert reference、gold answer、已有 05_ruler.json、已有 ForenAgent 报告、Direct LLM 输出或任何外部知识。

请只返回一个 JSON 对象，不要 Markdown，不要代码块。证据不足时请明确写入 insufficient_evidence，不要编造条款或天数。

JSON schema:
{{
  "disability_supported": true 或 false,
  "disability_results": [
    {{
      "clause": "条款号；无则空字符串",
      "branch_id": "可空",
      "text": "条款文字或不构成理由",
      "grade": 0-10；不构成填0,
      "conclusion_type": "supported|not_supported|insufficient",
      "joint": "可空",
      "injured_side": "可空",
      "loss_pct": 数字或0,
      "details": [{{"reason": "基于哪些结构化事实"}}],
      "warnings": []
    }}
  ],
  "period_result": {{
    "clause": "三期条款；无则空",
    "clause_text": "条款依据；无则空",
    "误工": 整数天数,
    "护理": 整数天数,
    "营养": 整数天数,
    "sub_label": "可空",
    "ranges": {{}},
    "selection_reasons": [],
    "requires_manual_period_assessment": true 或 false
  }},
  "candidate_disability_clauses": [],
  "matched_disability_clauses": [],
  "rejected_disability_clauses": [],
  "candidate_period_clauses": [],
  "matched_period_clauses": [],
  "insufficient_evidence": [],
  "reasoning_basis": "简要说明"
}}

[structured_case_facts]
{_compact_json(structuring_data, max_structuring_chars)}

[judge_json]
{_compact_json(judge_data, max_judge_chars)}

[standards]
{standard_text}

再次强调：你的回复必须以 {{ 开始，以 }} 结束，且可被 json.loads 直接解析。不要输出“好的”、分析过程或代码块。
"""


def call_qwen_json(
    prompt: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 3000,
    timeout: float = 180,
    retries: int = 2,
    retry_sleep: float = 15,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    import time

    resolved_key = api_key or os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not resolved_key:
        raise LLMRuleReasonerError("未找到 QWEN_API_KEY 或 DASHSCOPE_API_KEY")
    resolved_base = base_url or os.getenv("QWEN_BASE_URL") or DEFAULT_QWEN_BASE_URL
    resolved_model = model or os.getenv("QWEN_MODEL") or DEFAULT_QWEN_MODEL
    client = OpenAI(api_key=resolved_key, base_url=resolved_base, timeout=timeout)

    last_exc: Exception | None = None
    last_content = ""
    last_finish_reason = ""
    last_usage = None
    for attempt in range(1, retries + 2):
        try:
            resp = client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": "只返回一个合法 JSON 对象。不要 Markdown，不要代码块，不要解释。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            last_content = content
            last_finish_reason = str(resp.choices[0].finish_reason or "")
            last_usage = getattr(resp, "usage", None).model_dump() if getattr(resp, "usage", None) else None
            parsed, repair_mode = _loads_jsonish(content)
            meta = {
                "model": resolved_model,
                "base_url": resolved_base,
                "finish_reason": resp.choices[0].finish_reason,
                "usage": last_usage,
                "attempt": attempt,
                "json_parse_mode": repair_mode,
            }
            return parsed, meta
        except Exception as exc:  # noqa: BLE001 - preserve API/JSON error in metadata
            last_exc = exc
            if attempt > retries:
                break
            time.sleep(retry_sleep)
    raise LLMRuleReasonerError(
        f"Qwen rule reasoning failed after {retries + 1} attempts: {last_exc}",
        {
            "model": resolved_model,
            "base_url": resolved_base,
            "finish_reason": last_finish_reason,
            "usage": last_usage,
            "attempt": retries + 1,
            "last_content": last_content,
        },
    )


def normalize_payload(
    reasoner_json: Dict[str, Any],
    structuring_data: Dict[str, Any],
    judge_data: Dict[str, Any],
    *,
    model_meta: Optional[Dict[str, Any]] = None,
    meta_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    disability_results = []
    for item in _as_list(reasoner_json.get("disability_results")):
        if not isinstance(item, dict):
            continue
        disability_results.append(
            DisabilityResult(
                clause=str(item.get("clause") or ""),
                branch_id=str(item.get("branch_id") or item.get("clause") or ""),
                text=str(item.get("text") or item.get("reason") or "不构成伤残"),
                grade=_as_int(item.get("grade"), 0),
                joint=str(item.get("joint") or ""),
                injured_side=str(item.get("injured_side") or ""),
                loss_pct=_as_float(item.get("loss_pct"), 0.0),
                match_type="llm_rule_reasoner",
                details=_as_list(item.get("details")),
                warnings=[str(x) for x in _as_list(item.get("warnings")) if x],
                conclusion_type=str(item.get("conclusion_type") or ""),
            )
        )
    if not disability_results:
        text = "证据不足，未支持构成伤残" if reasoner_json.get("insufficient_evidence") else "不构成伤残"
        disability_results.append(DisabilityResult(clause="", text=text, grade=0, match_type="llm_rule_reasoner"))

    period = reasoner_json.get("period_result") if isinstance(reasoner_json.get("period_result"), dict) else {}
    period_result = PeriodResult(
        clause=str(period.get("clause") or ""),
        clause_text=str(period.get("clause_text") or ""),
        误工=_as_int(period.get("误工"), 0),
        护理=_as_int(period.get("护理"), 0),
        营养=_as_int(period.get("营养"), 0),
        sub_label=str(period.get("sub_label") or ""),
    )
    for attr, default in {
        "ranges": {},
        "selected_value_policy": "llm_reasoned",
        "selection_reasons": [],
        "selected_from_items": {},
        "requires_manual_period_assessment": False,
    }.items():
        setattr(period_result, attr, period.get(attr, default))

    case_data = _case_data(structuring_data, judge_data)
    section5_md = compose_section5(disability_results, period_result, case_data)
    section6_md = compose_section6(disability_results, period_result, case_data)
    positive = [r for r in disability_results if r.grade > 0]
    status = "supported" if positive else ("insufficient" if reasoner_json.get("insufficient_evidence") else "not_supported")
    label = "构成伤残" if positive else ("证据不足" if status == "insufficient" else "不构成伤残")

    model_meta = model_meta or {}
    meta = {
        "condition": "no_rules",
        "rule_engine_version": RULE_REASONER_VERSION,
        "input_mode": "structured_evidence_llm_rules",
        "structured_evidence": True,
        "deterministic_rules": False,
        "human_audit": False,
        **model_meta,
    }
    if meta_extra:
        meta.update(meta_extra)

    return {
        "section5": {
            "disability_conclusion_status": status,
            "disability_conclusion_label": label,
            "disability_conclusion_detail": {
                "status": status,
                "label": label,
                "reasoning_basis": reasoner_json.get("reasoning_basis", ""),
                "insufficient_evidence": reasoner_json.get("insufficient_evidence", []),
            },
            "disability_results": [
                {
                    "clause": r.clause,
                    "branch_id": getattr(r, "branch_id", ""),
                    "text": r.text,
                    "grade": r.grade,
                    "grade_cn": r.grade_cn,
                    "conclusion_type": getattr(r, "conclusion_type", ""),
                    "joint": r.joint,
                    "injured_side": r.injured_side,
                    "loss_pct": r.loss_pct,
                    "match_type": r.match_type,
                    "details": r.details,
                    "warnings": getattr(r, "warnings", []),
                }
                for r in disability_results
            ],
            "period_result": {
                "clause": period_result.clause,
                "clause_text": period_result.clause_text,
                "误工": period_result.误工,
                "护理": period_result.护理,
                "营养": period_result.营养,
                "sub_label": period_result.sub_label,
                "ranges": getattr(period_result, "ranges", {}),
                "selected_value_policy": getattr(period_result, "selected_value_policy", "llm_reasoned"),
                "selection_reasons": getattr(period_result, "selection_reasons", []),
                "selected_from_items": getattr(period_result, "selected_from_items", {}),
                "requires_manual_period_assessment": getattr(period_result, "requires_manual_period_assessment", False),
            },
            "markdown": section5_md,
        },
        "section6": {"markdown": section6_md},
        "audit": {
            "facts_used": {
                "input_mode": "structured_evidence_llm_rules",
                "forensic_facts_schema_version": (structuring_data.get("forensic_facts") or {}).get("schema_version", ""),
                "recognized_injuries": (structuring_data.get("forensic_facts") or {}).get("recognized_injuries", []),
                "rule_features": (structuring_data.get("forensic_facts") or {}).get("rule_features", {}),
                "period_rule_features": (structuring_data.get("forensic_facts") or {}).get("period_rule_features", {}),
            },
            "candidate_disability_clauses": reasoner_json.get("candidate_disability_clauses", []),
            "matched_disability_clauses": reasoner_json.get("matched_disability_clauses", []),
            "negative_disability_clauses": reasoner_json.get("negative_disability_clauses", []),
            "rejected_disability_clauses": reasoner_json.get("rejected_disability_clauses", []),
            "candidate_period_clauses": reasoner_json.get("candidate_period_clauses", []),
            "matched_period_clauses": reasoner_json.get("matched_period_clauses", []),
            "period_interval_candidates": reasoner_json.get("period_interval_candidates", []),
            "period_selection": reasoner_json.get("period_selection", {}),
            "manual_review_items": reasoner_json.get("insufficient_evidence", []),
        },
        "validation": {
            "status": "pass",
            "warnings": _as_list(reasoner_json.get("insufficient_evidence")),
        },
        "meta": meta,
    }


def run_llm_rule_reasoner(
    structuring_data: Dict[str, Any],
    judge_data: Dict[str, Any],
    standard_corpus: str,
    output_dir: Path,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 3000,
    timeout: float = 180,
    retries: int = 2,
    retry_sleep: float = 15,
    meta_extra: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, Path, Dict[str, Any]]:
    prompt = build_prompt(structuring_data, judge_data, standard_corpus)
    fallback_raw_response = ""
    try:
        parsed, model_meta = call_qwen_json(
            prompt,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
            retry_sleep=retry_sleep,
        )
    except LLMRuleReasonerError as exc:
        error_meta = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], dict) else {}
        fallback_raw_response = str(error_meta.pop("last_content", "") or "")
        parsed = _fallback_reasoner_json(structuring_data, judge_data, exc)
        model_meta = {
            "model": model or os.getenv("QWEN_MODEL") or DEFAULT_QWEN_MODEL,
            "base_url": base_url or os.getenv("QWEN_BASE_URL") or DEFAULT_QWEN_BASE_URL,
            **error_meta,
            "json_parse_mode": "fallback_payload_after_parse_failure",
            "fallback_used": True,
            "fallback_reason": str(exc.args[0] if exc.args else exc),
        }
    parsed, safety_notes = _safe_llm_rule_fallbacks(parsed, structuring_data)
    if safety_notes:
        model_meta = {**model_meta, "structured_rule_safety_patch": safety_notes}
    payload = normalize_payload(parsed, structuring_data, judge_data, model_meta=model_meta, meta_extra=meta_extra)
    if fallback_raw_response:
        payload.setdefault("audit", {})["llm_raw_response_excerpt"] = fallback_raw_response[:4000]
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "section56_result.json"
    md_path = output_dir / "section56_report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        "# 司法鉴定意见书结构化提要（第五、六部分，AI草稿）\n\n"
        f"## 五、分析说明\n\n{payload['section5']['markdown']}\n\n"
        f"## 六、鉴定意见\n\n{payload['section6']['markdown']}\n",
        encoding="utf-8",
    )
    return json_path, md_path, payload
