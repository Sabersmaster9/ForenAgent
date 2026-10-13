"""Shared data models for the Ruler Agent rule engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


CN_GRADE_NUMS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


@dataclass
class DisabilityResult:
    clause: str
    text: str
    grade: int
    joint: str = ""
    injured_side: str = ""
    loss_pct: float = 0.0
    match_type: str = ""
    details: List[Dict[str, Any]] = field(default_factory=list)
    branch_id: str = ""
    warnings: List[str] = field(default_factory=list)
    conclusion_type: str = ""

    @property
    def grade_cn(self) -> str:
        if self.grade <= 0:
            return "不构成伤残"
        return f"{CN_GRADE_NUMS.get(self.grade, str(self.grade))}级"


@dataclass
class PeriodResult:
    clause: str
    clause_text: str
    误工: int = 0
    护理: int = 0
    营养: int = 0
    sub_label: str = ""
    ranges: Dict[str, List[int]] = field(default_factory=dict)
    selected_value_policy: str = "upper_bound"
    selection_reasons: List[str] = field(default_factory=list)
    selected_from_items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    matched_items: List[Dict[str, Any]] = field(default_factory=list)
    requires_manual_period_assessment: bool = False


@dataclass
class RulerFacts:
    subject_name: str = "XXX"
    incident_date: str = "不详"
    incident_summary: str = ""
    diagnosis_overview: str = ""
    injury_summary: str = "不详"
    entrustment_items: List[str] = field(default_factory=list)
    diagnoses: List[str] = field(default_factory=list)
    surgery_history: List[str] = field(default_factory=list)
    exam_findings: List[str] = field(default_factory=list)
    aux_findings: List[str] = field(default_factory=list)
    rom_data: Dict[str, Any] = field(default_factory=dict)
    forensic_facts: Dict[str, Any] = field(default_factory=dict)
    rule_features: Dict[str, Any] = field(default_factory=dict)
    period_rule_features: Dict[str, Any] = field(default_factory=dict)
    fracture_features: Dict[str, Any] = field(default_factory=dict)
    treatment_events: List[Dict[str, Any]] = field(default_factory=list)
    recognized_injuries: List[Any] = field(default_factory=list)
    negative_rule_features: List[Any] = field(default_factory=list)
    uncertain_points: List[Any] = field(default_factory=list)
    all_text: str = ""
    has_positive_surgery: bool = False
    input_mode: str = "legacy_fallback"


@dataclass
class RuleEngineOutput:
    disability_results: List[DisabilityResult]
    period_result: PeriodResult
    audit: Dict[str, Any]
    validation: Dict[str, Any]
