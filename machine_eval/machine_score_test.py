import csv
import json
import re
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "test"
OUT_DIR = ROOT / "machine_eval" / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
LEVEL_MAP = {f"{k}级": v for k, v in CN_NUM.items()}
SEG_PREFIX = {"颈": "C", "胸": "T", "腰": "L"}

ITEMS = [
    ("disability_constitution", "是否构成伤残"),
    ("disability_levels", "伤残等级"),
    ("clauses", "适用条款"),
    ("miswork_days", "误工期"),
    ("nursing_days", "护理期"),
    ("nutrition_days", "营养期"),
    ("medical_fee_reasonable", "医疗费合理性"),
    ("rib_fracture_count", "肋骨骨折数量"),
    ("vertebra_segments", "椎体节段"),
    ("rom_loss_percent", "关节/功能丧失率"),
    ("scar_burn_percent", "烧伤/瘢痕面积比例"),
    ("injury_date", "损伤日期"),
]

KEY_ITEMS = {
    "disability_constitution",
    "disability_levels",
    "clauses",
    "miswork_days",
    "nursing_days",
    "nutrition_days",
    "medical_fee_reasonable",
}

FACT_ITEMS = {
    "rib_fracture_count",
    "vertebra_segments",
    "rom_loss_percent",
    "scar_burn_percent",
    "injury_date",
}


def read_docx_text(path: Path) -> str:
    with ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    paras = []
    for para in root.iter(W_NS + "p"):
        s = "".join((t.text or "") for t in para.iter(W_NS + "t")).strip()
        if s:
            paras.append(s)
    return "\n".join(paras)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def normalize_text(text: str) -> str:
    t = text
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("％", "%")
    t = t.replace("，", ",").replace("。", ".")
    t = t.replace("－", "-").replace("—", "-").replace("～", "~")
    return t


def first_section(text: str) -> str:
    markers = ["二、基本案情", "基本案情", "资料摘要", "三、资料摘要"]
    end = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx > 0:
            end = min(end, idx)
    return text[:end]


def final_section(text: str) -> str:
    lines = text.splitlines()
    idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if re.search(r"^(?:[一二三四五六七八九十]+、)?鉴定意见$", s):
            idx = i
    return "\n".join(lines[idx:]) if idx >= 0 else text


def analysis_section(text: str) -> str:
    lines = text.splitlines()
    start = -1
    end = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if re.search(r"^(?:[一二三四五六七八九十]+、)?分析说明$", s):
            start = i
        if start >= 0 and i > start and re.search(r"^(?:[一二三四五六七八九十]+、)?鉴定意见$", s):
            end = i
            break
    return "\n".join(lines[start:end]) if start >= 0 else text


def conclusion_text(text: str) -> str:
    return final_section(text) + "\n" + analysis_section(text)


def scope_flags(text: str):
    top = first_section(text)
    has_disability = bool(re.search(r"伤残|残疾", top))
    has_period = bool(re.search(r"误工|护理|营养|三期", top))
    has_medical = bool(re.search(r"医疗费|诊疗合理|合理性", top))
    return has_disability, has_period, has_medical


def chinese_date_to_iso(s: str):
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", s)
    if not m:
        m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if not m:
        return ""
    y, mo, d = map(int, m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}"


def extract_injury_date(text: str):
    patterns = [
        r"于\s*((?:20\d{2})年\s*\d{1,2}月\s*\d{1,2}日)[^。；;\n]{0,25}?(?:因|发生|外伤|交通事故|车祸|受伤|致)",
        r"((?:20\d{2})年\s*\d{1,2}月\s*\d{1,2}日)[^。；;\n]{0,25}?(?:因|发生|外伤|交通事故|车祸|碰撞|摔倒|爆炸|受伤|致)",
        r"(?:事故|外伤|受伤|车祸|爆炸)[^。；;\n]{0,25}?((?:20\d{2})年\s*\d{1,2}月\s*\d{1,2}日)",
    ]
    for part in [final_section(text), analysis_section(text), text]:
        for p in patterns:
            for m in re.finditer(p, part):
                span = part[max(0, m.start()-20):m.end()+20]
                if re.search(r"受理|鉴定地点|司法鉴定人|签名|委托书|起诉|出具|复查|检查", span):
                    # Do not discard if the same short span clearly says 因外伤/交通事故.
                    if not re.search(r"因(?:交通事故|外伤|故|车祸)|发生(?:交通事故|事故)|受伤", span):
                        continue
                iso = chinese_date_to_iso(m.group(1))
                if iso:
                    return iso
    return "NA"


def extract_period_days(text: str, label: str, enabled: bool):
    if not enabled:
        return []
    patterns = [
        rf"{label}(?:期限|期)?[^。；;\n]{{0,45}}?(\d+)\s*日",
        rf"{label}(?:期限|期)?[^。；;\n]{{0,45}}?(\d+)\s*个?月",
    ]
    for part in [final_section(text), analysis_section(text)]:
        vals = []
        for p in patterns:
            for m in re.finditer(p, part):
                raw = int(m.group(1))
                unit = "月" if "月" in m.group(0) else "日"
                vals.append(raw * 30 if unit == "月" else raw)
        if vals:
            return sorted(set(vals))
    return []


def extract_disability(text: str, enabled: bool):
    if not enabled:
        return "NA", []
    tail = conclusion_text(text)
    no_disability = bool(re.search(r"尚未构成.*?伤残|未构成.*?伤残|不构成.*?伤残", tail))
    levels = []
    for m in re.finditer(r"([一二三四五六七八九十]级)", tail):
        span = tail[max(0, m.start()-18):m.end()+18]
        if re.search(r"伤残|残疾|人体损伤致残", span):
            levels.append(LEVEL_MAP[m.group(1)])
    levels = sorted(set(levels))
    if levels:
        constitution = "构成"
    elif no_disability:
        constitution = "不构成"
    else:
        constitution = "NA"
    return constitution, levels


def extract_clauses(text: str):
    # Keep only cited legal/technical clauses. Avoid treating percentages such as 34.7% as clauses.
    part = conclusion_text(text)
    clauses = []
    clause_re = r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,2}(?:\.[a-zA-Z])?)(?![\d.%])"
    for m in re.finditer(clause_re, part):
        span = part[max(0, m.start()-35):m.end()+35]
        if re.search(r"条|标准|GA/T|规范|附录|之", span):
            clauses.append(m.group(1).lower())
    # Drop broad parent clauses when a more specific child clause is present, e.g. keep 5.10.6.11 over 5.10.
    uniq = sorted(set(clauses))
    specific = []
    for c in uniq:
        if not any(other != c and other.startswith(c + ".") for other in uniq):
            specific.append(c)
    return specific


def expand_num_spec(spec: str):
    spec = spec.replace("，", ",").replace("、", ",").replace("至", "-").replace("~", "-")
    nums = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                nums.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            nums.add(int(part))
    return nums


def extract_rib_count(text: str):
    part = conclusion_text(text)
    vals = []
    for m in re.finditer(r"肋骨[^。；;\n]{0,30}?(\d+)\s*根", part):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"共计?\s*(\d+)\s*根[^。；;\n]{0,8}肋", part):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"第([0-9、,\-至~]+)肋", part):
        nums = expand_num_spec(m.group(1))
        if nums:
            vals.append(len(nums))
    return sorted(set(vals))


def extract_vertebra_segments(text: str):
    part = conclusion_text(text)
    segs = set()
    for m in re.finditer(r"([颈胸腰])\s*([0-9、,\-至~]+)\s*椎", part):
        span = part[max(0, m.start()-20):m.end()+25]
        if not re.search(r"骨折|压缩|横突|损伤", span):
            continue
        prefix = SEG_PREFIX[m.group(1)]
        nums = expand_num_spec(m.group(2))
        for n in nums:
            segs.add(f"{prefix}{n}")
    for m in re.finditer(r"([颈胸腰])椎\s*([0-9、,\-至~]+)", part):
        span = part[max(0, m.start()-20):m.end()+25]
        if not re.search(r"骨折|压缩|横突|损伤", span):
            continue
        prefix = SEG_PREFIX[m.group(1)]
        nums = expand_num_spec(m.group(2))
        for n in nums:
            segs.add(f"{prefix}{n}")
    return sorted(segs)


def extract_percents(text: str, keywords):
    vals = []
    for part in [conclusion_text(text)]:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", part):
            span = part[max(0, m.start()-45):m.end()+45]
            if not any(k in span for k in keywords):
                continue
            val = round(float(m.group(1)), 1)
            # Broad threshold phrases are usually legal criteria, not the measured case value.
            if re.search(r"以上|未达|以下", span) and not re.search(r"经计算|经测算|瘢痕面积占体表面积的", span):
                continue
            vals.append(val)
    return sorted(set(vals))


def extract_medical_fee(text: str, enabled: bool):
    if not enabled:
        return "NA"
    part = final_section(text) + "\n" + analysis_section(text)
    if re.search(r"基本与本次.*相关|相关.*合理|可视为.*合理费用|合理费用", part):
        return "相关合理"
    if re.search(r"不合理|无关|不相关", part):
        return "不相关或不合理"
    if "医疗费" in part or "合理费用" in part or "诊疗合理" in part:
        return "提及但未明"
    return "NA"


def extract_record(case_id, source, path, text):
    t = normalize_text(text)
    has_disability, has_period, has_medical = scope_flags(t)
    constitution, levels = extract_disability(t, has_disability)
    rec = {
        "case_id": case_id,
        "source": source,
        "file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "disability_constitution": constitution,
        "disability_levels": ";".join(map(str, levels)) if levels else ("NA" if constitution == "NA" else ""),
        "clauses": ";".join(extract_clauses(t)) or "NA",
        "miswork_days": ";".join(map(str, extract_period_days(t, "误工", has_period))) or "NA",
        "nursing_days": ";".join(map(str, extract_period_days(t, "护理", has_period))) or "NA",
        "nutrition_days": ";".join(map(str, extract_period_days(t, "营养", has_period))) or "NA",
        "medical_fee_reasonable": extract_medical_fee(t, has_medical),
        "rib_fracture_count": ";".join(map(str, extract_rib_count(t))) or "NA",
        "vertebra_segments": ";".join(extract_vertebra_segments(t)) or "NA",
        "rom_loss_percent": ";".join(map(str, extract_percents(t, ["功能", "关节", "丧失", "活动"]))) or "NA",
        "scar_burn_percent": ";".join(map(str, extract_percents(t, ["烧伤", "瘢痕", "体表", "TBSA", "容貌"]))) or "NA",
        "injury_date": extract_injury_date(t),
    }
    return rec


def split_values(v):
    if v is None or v == "" or v == "NA":
        return []
    return [x.strip() for x in str(v).split(";") if x.strip()]


def numeric_set(v):
    vals = []
    for x in split_values(v):
        try:
            vals.append(float(x))
        except ValueError:
            pass
    return vals


def compare_clauses(gv, pv):
    gs, ps = set(split_values(gv)), set(split_values(pv))
    if not gs:
        return "NA", "gold未抽取条款"
    if not ps:
        return 0, "系统未抽取条款"
    g_dis = {x for x in gs if x.startswith("5.")}
    p_dis = {x for x in ps if x.startswith("5.")}
    if g_dis:
        missing = sorted(g_dis - p_dis)
        return (1 if not missing else 0), ("缺少伤残条款: " + ";".join(missing) if missing else "")
    missing = sorted(gs - ps)
    return (1 if not missing else 0), ("缺少条款: " + ";".join(missing) if missing else "")


def compare_field(field, gold, pred):
    gv, pv = gold.get(field, "NA"), pred.get(field, "NA")
    gold_empty = gv in (None, "", "NA")
    pred_empty = pv in (None, "", "NA")
    if gold_empty:
        if field in {"rib_fracture_count", "vertebra_segments", "rom_loss_percent", "scar_burn_percent"} and not pred_empty:
            return 0, "gold未见该关键事实，系统新增或抽取到该事实"
        return "NA", "gold不适用或未抽取"
    if pred_empty:
        return 0, "系统未抽取"
    if field in {"disability_constitution", "medical_fee_reasonable", "injury_date"}:
        return (1 if gv == pv else 0), ""
    if field in {"disability_levels", "vertebra_segments"}:
        gs, ps = set(split_values(gv)), set(split_values(pv))
        return (1 if gs == ps else 0), ""
    if field == "clauses":
        return compare_clauses(gv, pv)
    if field in {"miswork_days", "nursing_days", "nutrition_days", "rib_fracture_count"}:
        gs, ps = numeric_set(gv), numeric_set(pv)
        if not gs:
            return "NA", "gold未抽取数值"
        if not ps:
            return 0, "系统未抽取数值"
        return (1 if set(gs) & set(ps) else 0), ""
    if field in {"rom_loss_percent", "scar_burn_percent"}:
        gs, ps = numeric_set(gv), numeric_set(pv)
        if not gs:
            return "NA", "gold未抽取数值"
        if not ps:
            return 0, "系统未抽取数值"
        tolerance = 1.0 if field == "rom_loss_percent" else 3.0
        ok = any(abs(a - b) <= tolerance for a in gs for b in ps)
        return (1 if ok else 0), ""
    return (1 if gv == pv else 0), ""


def main():
    records = []
    case_dirs = [p for p in sorted(TEST_DIR.iterdir()) if p.is_dir()]
    for case_dir in case_dirs:
        case_id = case_dir.name
        golds = list(case_dir.glob("test_*.docx"))
        report = case_dir / "06_boss_reviewed.md"
        if not golds or not report.exists():
            continue
        gold_path = golds[0]
        records.append(extract_record(case_id, "gold", gold_path, read_docx_text(gold_path)))
        records.append(extract_record(case_id, "forenagent", report, read_text(report)))

    fields = ["case_id", "source", "file"] + [k for k, _ in ITEMS]
    with (OUT_DIR / "structured_extraction.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(records)

    score_rows = []
    by_case = {}
    for rec in records:
        by_case.setdefault(rec["case_id"], {})[rec["source"]] = rec
    for case_id, pair in sorted(by_case.items()):
        gold, pred = pair.get("gold"), pair.get("forenagent")
        if not gold or not pred:
            continue
        for field, name in ITEMS:
            score, note = compare_field(field, gold, pred)
            major = 0
            error_type = ""
            if score == 0 and field in KEY_ITEMS:
                major = 1
                error_type = "key_conclusion_error"
            elif score == 0 and field in FACT_ITEMS:
                major = 1 if field in {"rib_fracture_count", "vertebra_segments", "rom_loss_percent", "scar_burn_percent"} else 0
                error_type = "key_fact_error" if major else "fact_error"
            score_rows.append({
                "case_id": case_id,
                "model": "forenagent",
                "field": field,
                "item_name": name,
                "gold_value": gold.get(field, ""),
                "model_value": pred.get(field, ""),
                "score": score,
                "major_error": major,
                "error_type": error_type,
                "note": note,
            })

    with (OUT_DIR / "rule_item_scores.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(score_rows[0].keys()))
        w.writeheader()
        w.writerows(score_rows)

    summary_rows = []
    for case_id in sorted(by_case):
        rows = [r for r in score_rows if r["case_id"] == case_id]
        applicable = [r for r in rows if r["score"] != "NA"]
        key = [r for r in applicable if r["field"] in KEY_ITEMS]
        fact = [r for r in applicable if r["field"] in FACT_ITEMS]

        def rate(rs):
            return "" if not rs else round(sum(int(r["score"]) for r in rs) / len(rs), 4)

        summary_rows.append({
            "case_id": case_id,
            "model": "forenagent",
            "applicable_items": len(applicable),
            "overall_rule_score": rate(applicable),
            "key_conclusion_accuracy": rate(key),
            "fact_consistency": rate(fact),
            "major_error_items": sum(int(r["major_error"]) for r in rows),
            "major_error_case": 1 if any(int(r["major_error"]) for r in rows) else 0,
        })

    with (OUT_DIR / "rule_case_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)

    all_app = [r for r in score_rows if r["score"] != "NA"]
    all_key = [r for r in all_app if r["field"] in KEY_ITEMS]
    all_fact = [r for r in all_app if r["field"] in FACT_ITEMS]
    aggregate = {
        "model": "forenagent",
        "case_count": len(summary_rows),
        "applicable_items": len(all_app),
        "overall_rule_score": round(sum(int(r["score"]) for r in all_app) / len(all_app), 4) if all_app else "",
        "key_conclusion_accuracy": round(sum(int(r["score"]) for r in all_key) / len(all_key), 4) if all_key else "",
        "fact_consistency": round(sum(int(r["score"]) for r in all_fact) / len(all_fact), 4) if all_fact else "",
        "major_error_cases": sum(int(r["major_error_case"]) for r in summary_rows),
        "major_error_case_rate": round(sum(int(r["major_error_case"]) for r in summary_rows) / len(summary_rows), 4) if summary_rows else "",
    }
    with (OUT_DIR / "rule_model_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(aggregate.keys()))
        w.writeheader()
        w.writerow(aggregate)
    (OUT_DIR / "rule_model_summary.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
