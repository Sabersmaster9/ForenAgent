"""
ruler_agent — 司法鉴定意见书第五、六部分生成器

输入：structuring_agent 输出的 JSON + judge_agent 输出的 JSON
输出：section56_result.json + section56_report.md（分析说明 + 鉴定意见）
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.common.output_paths import resolve_numbered_output_dir

try:
    from .coverage import coverage_snapshot
    from .disability_engine import match_disability_v2
    from .fact_adapter import adapt_inputs
    from .period_engine import match_period_v2
except ImportError:  # 支持直接以脚本路径运行
    from coverage import coverage_snapshot
    from disability_engine import match_disability_v2
    from fact_adapter import adapt_inputs
    from period_engine import match_period_v2

# ============================================================
# 模块1：法规结构化索引
# ============================================================

# --- 伤残等级条款索引（基于100案审计的31条） ---

DISABILITY_CLAUSE_INDEX = [
    # ===== 路径1: joint_function_loss（需ROM计算）=====
    {
        "clause": "5.10.6.11",
        "text": "四肢任一大关节（踝关节除外）功能丧失25%以上",
        "type": "joint_function_loss",
        "applies_to": ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节"],
        "grade": 10,
        "min_loss_pct": 25.0,
        "max_loss_pct": 49.9,
    },
    {
        "clause": "5.9.6.9",
        "text": "四肢任一大关节（踝关节除外）功能丧失50%以上",
        "type": "joint_function_loss",
        "applies_to": ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节"],
        "grade": 9,
        "min_loss_pct": 50.0,
        "max_loss_pct": 74.9,
    },
    {
        "clause": "5.8.6.8",
        "text": "四肢任一大关节（踝关节除外）功能丧失75%以上",
        "type": "joint_function_loss",
        "applies_to": ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节"],
        "grade": 8,
        "min_loss_pct": 75.0,
        "max_loss_pct": 89.9,
    },
    {
        "clause": "5.10.6.12",
        "text": "一踝关节功能丧失50%以上",
        "type": "joint_function_loss",
        "applies_to": ["踝关节"],
        "grade": 10,
        "min_loss_pct": 50.0,
        "max_loss_pct": 74.9,
    },
    {
        "clause": "5.9.6.10",
        "text": "一踝关节功能丧失75%以上",
        "type": "joint_function_loss",
        "applies_to": ["踝关节"],
        "grade": 9,
        "min_loss_pct": 75.0,
        "max_loss_pct": 89.9,
    },
    # ===== 路径2: diagnosis_match（诊断关键词匹配）=====
    {
        "clause": "5.10.6.13",
        "text": "下肢任一大关节骨折后遗创伤性关节炎",
        "type": "diagnosis_match",
        "applies_to": ["髋关节", "膝关节", "踝关节"],
        "grade": 10,
        "keywords": ["创伤性关节炎"],
        "prerequisite": ["骨折"],
    },
    # ===== 路径2b: fracture_surgery（长骨骨折内固定术后）=====
    {
        "clause": "5.10.6.7",
        "text": "四肢长骨骨折内固定术后",
        "type": "fracture_surgery",
        "applies_to": [],
        "grade": 10,
        "fracture_keywords": [
            "肱骨骨折", "桡骨骨折", "尺骨骨折", "股骨骨折", "胫骨骨折", "腓骨骨折",
            "肱骨干骨折", "肱骨近端骨折", "肱骨髁上骨折", "肱骨髁间骨折",
            "股骨颈骨折", "股骨粗隆间骨折", "股骨干骨折", "股骨远端骨折",
            "股骨髁骨折", "股骨外侧髁骨折", "股骨内侧髁骨折",
            "胫骨平台骨折", "胫腓骨骨折", "胫骨下端骨折", "胫骨远端骨折",
            "尺桡骨骨折", "尺桡骨双骨折", "前臂骨折",
        ],
        "surgery_keywords": ["内固定", "切复内固定", "骨折内固定术", "钢板内固定", "髓内钉"],
    },
    # ===== 路径2c: diagnosis_surgery（膝关节交叉韧带/半月板）=====
    {
        "clause": "5.10.6.6",
        "text": "一侧膝关节交叉韧带、半月板伴侧副韧带撕裂经手术治疗后遗功能障碍",
        "type": "diagnosis_surgery",
        "applies_to": ["膝关节"],
        "grade": 10,
        "diagnosis_keywords": ["交叉韧带", "半月板", "侧副韧带", "韧带撕裂"],
        "surgery_keywords": ["手术", "修补", "重建", "缝合"],
    },
    # ===== 路径3: surgery_match（手术事实匹配）=====
    {
        "clause": "5.9.6.5",
        "text": "四肢任一大关节行关节假体置换术后",
        "type": "surgery_match",
        "applies_to": ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节", "踝关节"],
        "grade": 9,
        "keywords": ["关节置换", "假体置换", "人工关节", "半髋置换", "全髋置换", "股骨头置换"],
    },
    # ===== 路径4: diagnosis_match（强直固定）=====
    {
        "clause": "5.7.6.3",
        "text": "四肢任一大关节（踝关节除外）强直固定于非功能位",
        "type": "diagnosis_match",
        "applies_to": ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节"],
        "grade": 7,
        "keywords": ["强直固定于非功能位", "强直于非功能位", "关节强直固定"],
    },
    {
        "clause": "5.8.6.9",
        "text": "一踝关节强直固定于非功能位",
        "type": "diagnosis_match",
        "applies_to": ["踝关节"],
        "grade": 8,
        "keywords": ["踝关节强直固定", "踝关节强直于非功能位"],
    },
    # ===== 路径5: 其他骨折/损伤条款（直接匹配）=====
    # -- 肋骨 --
    {
        "clause": "5.10.3.7",
        "text": "肋骨骨折6根以上，或者肋骨部分缺失2根以上",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["肋骨骨折"],
        "prerequisite": [],
    },
    {
        "clause": "5.9.3.11",
        "text": "肋骨骨折12根以上，或者肋骨部分缺失4根以上",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["肋骨骨折"],
        "prerequisite": [],
    },
    # -- 骨盆 --
    {
        "clause": "5.10.6.4",
        "text": "骨盆两处以上骨折或者粉碎性骨折，畸形愈合",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["骨盆骨折", "骨盆粉碎性骨折"],
        "prerequisite": [],
    },
    {
        "clause": "5.9.6.3",
        "text": "骨盆两处以上骨折或者粉碎性骨折，严重畸形愈合",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["骨盆骨折", "骨盆粉碎性骨折"],
        "prerequisite": [],
    },
    # -- 脊柱 --
    {
        "clause": "5.10.6.2",
        "text": "一椎体压缩性骨折（压缩程度达1/3）或者粉碎性骨折；一椎体骨折经手术治疗后",
        "type": "diagnosis_surgery",
        "applies_to": [],
        "grade": 10,
        "diagnosis_keywords": ["椎体骨折", "椎体压缩性骨折", "椎体粉碎性骨折"],
        "surgery_keywords": ["椎体手术", "椎体骨折手术"],
    },
    {
        "clause": "5.10.6.3",
        "text": "四处以上横突、棘突或者椎弓根骨折，影响功能",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["横突骨折", "棘突骨折", "椎弓根骨折"],
    },
    {
        "clause": "5.9.6.2",
        "text": "一椎体并相应附件骨折，经手术治疗后；二椎体压缩性骨折",
        "type": "diagnosis_surgery",
        "applies_to": [],
        "grade": 9,
        "diagnosis_keywords": ["椎体骨折", "椎体附件骨折"],
        "surgery_keywords": ["椎体手术"],
    },
    # -- 颅脑 --
    {
        "clause": "5.10.1.8",
        "text": "开颅术后",
        "type": "surgery_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["开颅术", "开颅手术", "颅骨修补"],
    },
    {
        "clause": "5.9.1.3",
        "text": "脑叶切除术后",
        "type": "surgery_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["脑叶切除"],
    },
    # -- 面部 --
    {
        "clause": "5.10.2.4",
        "text": "面部条状瘢痕形成，累计长度达10.0cm",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["面部瘢痕", "面部条状瘢痕"],
    },
    {
        "clause": "5.10.2.5",
        "text": "面部块状瘢痕形成",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["面部块状瘢痕"],
    },
    {
        "clause": "5.10.2.10",
        "text": "复视或者斜视",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["复视", "斜视"],
    },
    {
        "clause": "5.10.2.15",
        "text": "一眼中度视力损害",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["视力损害"],
    },
    {
        "clause": "5.9.2.16",
        "text": "一眼眶骨折后遗眼球内陷5mm以上",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["眼眶骨折", "眼球内陷"],
    },
    {
        "clause": "5.9.2.24",
        "text": "张口受限II度",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["张口受限"],
    },
    {
        "clause": "5.10.2.26",
        "text": "牙齿缺失或者折断7枚以上",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["牙齿缺失", "牙齿折断"],
    },
    # -- 腹部 --
    {
        "clause": "5.10.4.3",
        "text": "胃、肠或者胆道修补术后",
        "type": "surgery_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["胃修补", "肠修补", "胆道修补"],
    },
    {
        "clause": "5.9.4.6",
        "text": "肠部分切除术后",
        "type": "surgery_match",
        "applies_to": [],
        "grade": 9,
        "keywords": ["肠切除"],
    },
    {
        "clause": "5.10.5.1",
        "text": "肾、输尿管或者膀胱修补术后",
        "type": "surgery_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["肾修补", "输尿管修补", "膀胱修补"],
    },
    # -- 其他肢体 --
    {
        "clause": "5.7.6.2",
        "text": "一下肢踝关节以上缺失",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 7,
        "keywords": ["下肢缺失", "踝关节以上缺失"],
    },
    {
        "clause": "5.10.6.14",
        "text": "肢体重要血管循环障碍，影响功能",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["血管循环障碍"],
    },
    {
        "clause": "5.10.6.18",
        "text": "一足足弓结构部分破坏",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 10,
        "keywords": ["足弓结构破坏", "足弓破坏"],
    },
    # -- 神经 --
    {
        "clause": "5.5.1.7",
        "text": "非肢体瘫运动障碍（中度）",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 5,
        "keywords": ["非肢体瘫运动障碍"],
    },
    {
        "clause": "5.7.1.4",
        "text": "偏瘫（肌力4级以下）",
        "type": "diagnosis_match",
        "applies_to": [],
        "grade": 7,
        "keywords": ["偏瘫"],
    },
]

# --- 三期评定条款索引（基于100案审计的44条基础条款） ---

PERIOD_CLAUSE_INDEX = [
    # ===== 第10章：肢体与关节损伤（最高频）=====
    {
        "clause": "10.2.1",
        "keywords": ["锁骨骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 120], "护理": [30, 60], "营养": [60, 90]},
            {"label": "b)手术治疗", "误工": [90, 120], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.2",
        "keywords": ["肩胛骨骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 120], "护理": [30, 60], "营养": [30, 60]},
            {"label": "b)手术治疗", "误工": [90, 180], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.3",
        "keywords": ["肱骨骨折", "肱骨外髁骨折", "肱骨髁上骨折", "肱骨干骨折", "肱骨近端骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 180], "护理": [30, 60], "营养": [60, 90]},
            {"label": "b)手术治疗", "误工": [90, 270], "护理": [60, 90], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.4",
        "keywords": ["尺骨鹰嘴骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 90], "护理": [30, 60], "营养": [30, 60]},
            {"label": "b)手术治疗", "误工": [90, 120], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.5",
        "keywords": ["尺桡骨骨折", "尺骨骨折", "桡骨骨折", "尺桡骨双骨折", "前臂骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [90, 120], "护理": [30, 60], "营养": [60, 90]},
            {"label": "b)手术治疗", "误工": [90, 180], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.6",
        "keywords": ["腕骨骨折"],
        "sub_items": [
            {"label": "a)骨折", "误工": [90, 180], "护理": [30, 60], "营养": [20, 30]},
            {"label": "b)脱位", "误工": [120, 180], "护理": [30, 60], "营养": [20, 30]},
        ],
    },
    {
        "clause": "10.2.8",
        "keywords": ["股骨颈骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [240, 365], "护理": [120, 180], "营养": [30, 90]},
            {"label": "b)手术治疗", "误工": [180, 365], "护理": [90, 150], "营养": [90, 180]},
        ],
    },
    {
        "clause": "10.2.9",
        "keywords": ["股骨粗隆间骨折", "粗隆间骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [180, 270], "护理": [120, 180], "营养": [30, 90]},
            {"label": "b)手术治疗", "误工": [180, 270], "护理": [90, 180], "营养": [90, 180]},
        ],
    },
    {
        "clause": "10.2.10",
        "keywords": ["股骨干骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [90, 180], "护理": [60, 120], "营养": [30, 90]},
            {"label": "b)手术治疗", "误工": [90, 300], "护理": [60, 120], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.11",
        "keywords": ["股骨远端骨折", "股骨髁骨折", "股骨外侧髁骨折", "股骨内侧髁骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [90, 180], "护理": [60, 120], "营养": [30, 90]},
            {"label": "b)手术治疗", "误工": [120, 270], "护理": [60, 120], "营养": [30, 90]},
        ],
    },
    {
        "clause": "10.2.12",
        "keywords": ["髌骨骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [120, 150], "护理": [30, 60], "营养": [30, 60]},
            {"label": "b)手术治疗", "误工": [120, 180], "护理": [60, 90], "营养": [30, 60]},
        ],
    },
    {
        "clause": "10.2.13",
        "keywords": ["胫骨平台骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [90, 150], "护理": [60, 90], "营养": [30, 60]},
            {"label": "b)手术治疗", "误工": [120, 180], "护理": [60, 90], "营养": [30, 60]},
        ],
    },
    {
        "clause": "10.2.14",
        "keywords": ["胫腓骨骨折", "胫骨骨折", "腓骨骨折", "胫腓骨双骨折", "胫骨远端骨折", "胫骨下段骨折", "腓骨下段骨折", "胫腓骨下段骨折"],
        "sub_items": [
            {"label": "a)胫骨骨折", "误工": [120, 180], "护理": [30, 90], "营养": [60, 90]},
            {"label": "b)腓骨骨折", "误工": [60, 90], "护理": [30, 60], "营养": [30, 60]},
            {"label": "c)胫腓骨双骨折", "误工": [120, 180], "护理": [30, 90], "营养": [60, 90]},
            {"label": "d)开放性骨折", "误工": [150, 180], "护理": [60, 90], "营养": [60, 90]},
            {"label": "e)胫骨远端粉碎性骨折", "误工": [150, 180], "护理": [60, 90], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.15",
        "keywords": ["踝部骨折", "踝关节骨折", "内踝骨折", "外踝骨折", "双踝骨折", "三踝骨折", "单踝骨折"],
        "sub_items": [
            {"label": "a)单踝骨折", "误工": [90, 120], "护理": [30, 60], "营养": [60, 90]},
            {"label": "b)双踝骨折", "误工": [90, 180], "护理": [30, 60], "营养": [60, 90]},
            {"label": "c)三踝骨折", "误工": [90, 180], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.16",
        "keywords": ["舟骨骨折", "楔骨骨折"],
        "sub_items": [
            {"label": "", "误工": [120, 120], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.17",
        "keywords": ["跟骨骨折", "距骨骨折"],
        "sub_items": [
            {"label": "a)单纯骨折", "误工": [90, 180], "护理": [60, 90], "营养": [60, 90]},
            {"label": "b)累及关节面", "误工": [90, 240], "护理": [60, 90], "营养": [90, 120]},
            {"label": "c)手术治疗", "误工": [90, 240], "护理": [60, 90], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.2.18",
        "keywords": ["跖骨骨折", "趾骨骨折", "跗骨骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [90, 120], "护理": [30, 60], "营养": [60, 90]},
            {"label": "b)手术治疗", "误工": [120, 150], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    # ===== 第10章：关节脱位/韧带/神经 =====
    {
        "clause": "10.3.1",
        "keywords": ["肩关节脱位"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 90], "护理": [30, 60], "营养": [20, 30]},
            {"label": "b)手术治疗", "误工": [60, 180], "护理": [30, 60], "营养": [20, 30]},
        ],
    },
    {
        "clause": "10.3.2",
        "keywords": ["肘关节脱位"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 90], "护理": [30, 60], "营养": [20, 30]},
            {"label": "b)手术治疗", "误工": [60, 180], "护理": [30, 60], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.3.4",
        "keywords": ["肩锁关节脱位", "胸锁关节脱位", "腕部脱位", "掌指关节脱位", "指间关节脱位", "距骨脱位", "跗骨间脱位", "跗跖关节脱位", "跖趾关节脱位", "趾间关节脱位"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 180], "护理": [30, 60], "营养": [20, 30]},
            {"label": "b)手术治疗", "误工": [60, 180], "护理": [30, 90], "营养": [60, 90]},
        ],
    },
    {
        "clause": "10.4",
        "keywords": ["韧带损伤", "韧带撕裂", "侧副韧带", "交叉韧带", "半月板损伤"],
        "sub_items": [
            {"label": "", "误工": [60, 120], "护理": [60, 90], "营养": [30, 60]},
        ],
    },
    {
        "clause": "10.5",
        "keywords": ["肌腱断裂", "跟腱断裂", "肌腱损伤"],
        "sub_items": [
            {"label": "", "误工": [60, 150], "护理": [30, 60], "营养": [30, 60]},
        ],
    },
    {
        "clause": "10.8",
        "keywords": ["桡神经损伤", "尺神经损伤", "正中神经损伤", "坐骨神经损伤", "腓总神经损伤", "胫神经损伤", "臂丛神经损伤", "神经损伤"],
        "sub_items": [
            {"label": "a)上肢", "误工": [180, 365], "护理": [30, 150], "营养": [30, 60]},
            {"label": "b)下肢", "误工": [180, 365], "护理": [30, 150], "营养": [30, 60]},
        ],
    },
    {
        "clause": "10.9",
        "keywords": ["血管损伤", "动脉损伤", "静脉损伤"],
        "sub_items": [
            {"label": "", "误工": [90, 180], "护理": [30, 60], "营养": [30, 60]},
        ],
    },
    # ===== 第9章：脊柱、骨盆 =====
    {
        "clause": "9.1",
        "keywords": ["脊柱骨折", "椎体骨折", "腰椎骨折", "胸椎骨折", "颈椎骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [45, 150], "护理": [45, 60], "营养": [45, 60]},
            {"label": "b)手术治疗", "误工": [120, 180], "护理": [60, 90], "营养": [60, 90]},
        ],
    },
    {
        "clause": "9.5",
        "keywords": ["骨盆骨折", "骨盆粉碎性骨折"],
        "sub_items": [
            {"label": "a)稳定型骨折", "误工": [60, 120], "护理": [20, 30], "营养": [30, 60]},
            {"label": "b)不稳定型骨折", "误工": [120, 180], "护理": [60, 90], "营养": [60, 90]},
        ],
    },
    # ===== 第7章：胸部 =====
    {
        "clause": "7.2",
        "keywords": ["肋骨骨折"],
        "sub_items": [
            {"label": "a)一处骨折", "误工": [30, 45], "护理": [7, 15], "营养": [15, 30]},
            {"label": "b)多根、多处骨折", "误工": [60, 120], "护理": [30, 60], "营养": [30, 60]},
        ],
    },
    # ===== 第4章：颅脑 =====
    {
        "clause": "4.7",
        "keywords": ["颅脑损伤", "颅骨骨折", "脑挫裂伤", "硬膜下血肿", "硬膜外血肿", "蛛网膜下腔出血", "脑内血肿"],
        "sub_items": [
            {"label": "a)轻型", "误工": [30, 45], "护理": [0, 0], "营养": [0, 0]},
            {"label": "b)中型", "误工": [90, 180], "护理": [30, 60], "营养": [30, 60]},
        ],
    },
    {
        "clause": "4.9",
        "keywords": ["颅脑损伤并发症", "颅脑损伤后遗症"],
        "sub_items": [
            {"label": "", "误工": [90, 180], "护理": [30, 90], "营养": [30, 90]},
        ],
    },
    # ===== 第5章：面部 =====
    {
        "clause": "5.1.17",
        "keywords": ["眶壁骨折", "眼眶骨折"],
        "sub_items": [
            {"label": "a)非手术治疗", "误工": [60, 90], "护理": [15, 30], "营养": [30, 45]},
            {"label": "b)手术治疗", "误工": [90, 150], "护理": [30, 60], "营养": [45, 60]},
        ],
    },
    # ===== 第8章：腹部 =====
    {
        "clause": "8.6.2",
        "keywords": ["空腔脏器损伤", "肠损伤", "胃损伤"],
        "sub_items": [
            {"label": "a)修补术", "误工": [60, 90], "护理": [30, 60], "营养": [60, 120]},
            {"label": "b)部分切除术", "误工": [90, 120], "护理": [30, 60], "营养": [60, 120]},
        ],
    },
    # ===== 附录A总则 =====
    {
        "clause": "附录A",
        "keywords": [],
        "sub_items": [
            {"label": "多处损伤", "误工": [0, 0], "护理": [0, 0], "营养": [0, 0]},
        ],
        "note": "多处损伤不简单累加，以最长的损伤为主，必要时酌情延长",
    },
]


# ============================================================
# 模块2：ROM查表引擎（附录C表 C-4 ~ C-9）
# ============================================================
# 每个方向的表格式: [(阈值角度, {"M1":x, "M2":x, "M3":x, "M4":x, "M5":x}), ...]
# 角度按降序排列，查表找第一个 ≤ 实际角度的阈值

# --- 表C-4：肩关节 ---
_SHOULDER_TABLE = {
    "前屈上举": [
        (171, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (151, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (131, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (111, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (91,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (71,  {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (51,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (31,  {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,   {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "后伸": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "外展上举": [
        (171, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (151, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (131, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (111, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (91,  {"M1":100,"M2":85,"M3":70,"M4":65,"M5":40}),
        (71,  {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (51,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (31,  {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,   {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "内收": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "水平位内旋": [
        (81, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (71, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (61, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (51, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (41, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (31, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (21, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (11, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "水平位外旋": [
        (81, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (71, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (61, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (51, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (41, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (31, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (21, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (11, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

# --- 表C-5：肘关节 ---
# 注：肘关节以屈曲90度为中立位0度
_ELBOW_TABLE = {
    "屈曲": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (36, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (26, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (16, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (6,  {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "伸展": [
        (90, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (81, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (71, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (61, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (51, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (41, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (31, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (21, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (11, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

# --- 表C-6：腕关节 ---
_WRIST_TABLE = {
    "掌屈": [
        (61, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (51, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (41, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (31, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (26, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (21, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (16, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (11, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "背伸": [
        (61, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (51, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (41, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (31, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (26, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (21, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (16, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (11, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "桡偏": [
        (21, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (16, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (11, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (6,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "尺偏": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

# --- 表C-7：髋关节 ---
_HIP_TABLE = {
    "前屈": [
        (121, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (106, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (91,  {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (76,  {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (61,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (46,  {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (31,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (16,  {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,   {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "后伸": [
        (11, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (6,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":20}),
        (1,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":50}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "外展": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "内收": [
        (16, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (11, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (6,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (1,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "外旋": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "内旋": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

# --- 表C-8：膝关节 ---
# 注：负值表示伸展时到达功能位（直立位）所差的度数
# 膝关节功能丧失 = 屈曲丧失 + 伸展丧失（求和，非平均），上限100%
_KNEE_TABLE = {
    "屈曲": [
        (130, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (116, {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (101, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (86,  {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (71,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (61,  {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (46,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (31,  {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (0,   {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "伸展": [
        (-5,  {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (-6,  {"M1":100,"M2":77,"M3":55,"M4":32,"M5":10}),
        (-11, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (-21, {"M1":100,"M2":82,"M3":65,"M4":47,"M5":30}),
        (-26, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (-31, {"M1":100,"M2":87,"M3":75,"M4":62,"M5":50}),
        (-36, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (-41, {"M1":100,"M2":92,"M3":85,"M4":77,"M5":70}),
        (-46, {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

# --- 表C-9：踝关节 ---
_ANKLE_TABLE = {
    "背屈": [
        (16, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (11, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (6,  {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (1,  {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
    "跖屈": [
        (41, {"M1":100,"M2":75,"M3":50,"M4":25,"M5":0}),
        (31, {"M1":100,"M2":80,"M3":60,"M4":40,"M5":20}),
        (21, {"M1":100,"M2":85,"M3":70,"M4":55,"M5":40}),
        (11, {"M1":100,"M2":90,"M3":80,"M4":70,"M5":60}),
        (0,  {"M1":100,"M2":95,"M3":90,"M4":85,"M5":80}),
    ],
}

ROM_TABLES = {
    "肩关节": _SHOULDER_TABLE,
    "肘关节": _ELBOW_TABLE,
    "腕关节": _WRIST_TABLE,
    "髋关节": _HIP_TABLE,
    "膝关节": _KNEE_TABLE,
    "踝关节": _ANKLE_TABLE,
}

# 关节方向映射（judge_agent的方向名 → ROM表的方向名）
DIRECTION_ALIAS = {
    "肩关节": {"前屈上举": "前屈上举", "后伸": "后伸", "外展上举": "外展上举", "内收": "内收", "水平位内旋": "水平位内旋", "水平位外旋": "水平位外旋"},
    "肘关节": {"屈曲": "屈曲", "伸展": "伸展"},
    "腕关节": {"掌屈": "掌屈", "背伸": "背伸", "桡偏": "桡偏", "尺偏": "尺偏"},
    "髋关节": {"前屈": "前屈", "后伸": "后伸", "外展": "外展", "内收": "内收", "内旋": "内旋", "外旋": "外旋"},
    "膝关节": {"屈曲": "屈曲", "伸展": "伸展"},
    "踝关节": {"背屈": "背屈", "跖屈": "跖屈"},
}


# ============================================================
# 模块2续：ROM查表核心函数
# ============================================================

def lookup_loss_pct(joint: str, direction: str, angle: float, muscle_grade: str = "M5") -> float:
    """查表得到单个方向的功能丧失值（百分比）。

    angle: 关节运动活动度（度）。膝关节伸展为负值。
    muscle_grade: M1~M5，默认M5（法医检查通常记录肌力5级）。
    """
    table = ROM_TABLES.get(joint)
    if table is None:
        return 0.0

    dir_name = DIRECTION_ALIAS.get(joint, {}).get(direction, direction)
    rows = table.get(dir_name)
    if rows is None:
        return 0.0

    # 膝关节伸展：角度为负值，表的阈值也是负值，按降序匹配
    # 其他关节：角度为正值，表的阈值为正值，按降序匹配
    for threshold, grades in rows:
        if angle >= threshold:
            return float(grades.get(muscle_grade, 0))

    # 角度低于所有阈值，取最后一行
    return float(rows[-1][1].get(muscle_grade, 0))


def _convert_angle_for_table(joint: str, direction: str, angle: float) -> float:
    """将原始角度转换为ROM查表所需的角度。

    肘关节特殊：以屈曲90度为中立位0度。
    - 屈曲：table_angle = actual_angle - 90
    - 伸展：table_angle = 90 + actual_angle（actual为负值表示伸展受限）
    """
    if joint == "肘关节":
        if direction == "屈曲":
            return angle - 90.0
        elif direction == "伸展":
            return 90.0 + angle
    return angle


def calc_joint_loss(joint: str, rom_data: dict, muscle_grade: str = "M5") -> dict:
    """计算某关节的功能丧失值。

    rom_data: {"左": {"屈曲": 120.0, "伸展": -5.0}, "右": {"屈曲": 140.0, "伸展": 0.0}}
    屈曲值为最大屈曲角度（如100°）；伸展值对膝关节为负值（如-25°表示伸展受限25°），
    对肘关节同理（如-25°表示伸展受限25°）。
    返回: {"loss_pct": float, "details": [...], "injured_side": str}
    """
    directions = list(DIRECTION_ALIAS.get(joint, {}).keys())
    if not directions:
        return {"loss_pct": 0.0, "details": [], "injured_side": "不详"}

    # 确定伤侧（数据方向少的一侧，因为伤侧通常ROM数据更少）
    left_dirs = rom_data.get("左", {})
    right_dirs = rom_data.get("右", {})
    if len(left_dirs) <= len(right_dirs) and left_dirs:
        injured_side = "左"
    elif right_dirs:
        injured_side = "右"
    elif left_dirs:
        injured_side = "左"
    else:
        return {"loss_pct": 0.0, "details": [], "injured_side": "不详"}

    healthy_side = "右" if injured_side == "左" else "左"
    injured_rom = rom_data.get(injured_side, {})
    healthy_rom = rom_data.get(healthy_side, {})

    details = []
    total_loss = 0.0

    for direction in directions:
        injured_angle = injured_rom.get(direction)
        healthy_angle = healthy_rom.get(direction)

        if injured_angle is None:
            continue

        # 角度转换为查表所需格式
        table_angle_injured = _convert_angle_for_table(joint, direction, injured_angle)

        # 伤侧丧失值
        injured_loss = lookup_loss_pct(joint, direction, table_angle_injured, muscle_grade)

        # 健侧丧失值（有数据时减去，无数据时视为0）
        if healthy_angle is not None:
            table_angle_healthy = _convert_angle_for_table(joint, direction, healthy_angle)
            healthy_loss = lookup_loss_pct(joint, direction, table_angle_healthy, muscle_grade)
        else:
            healthy_loss = 0.0

        net_loss = max(0.0, injured_loss - healthy_loss)
        details.append({
            "direction": direction,
            "injured_side": injured_side,
            "healthy_side": healthy_side,
            "injured_angle": injured_angle,
            "healthy_angle": healthy_angle,
            "table_angle_injured": table_angle_injured,
            "table_angle_healthy": table_angle_healthy if healthy_angle is not None else None,
            "injured_loss": injured_loss,
            "healthy_loss": healthy_loss,
            "net_loss": net_loss,
            "healthy_side_used": healthy_angle is not None,
        })
        total_loss += net_loss

    # 膝关节特殊：屈曲丧失 + 伸展丧失（求和），上限100%
    if joint == "膝关节":
        loss_pct = min(100.0, total_loss)
    else:
        # 其他关节：各方向丧失值之和 / 方位数
        num_dirs = len(details) if details else 1
        loss_pct = total_loss / num_dirs

    return {
        "loss_pct": round(loss_pct, 1),
        "details": details,
        "injured_side": injured_side,
        "healthy_side": healthy_side,
        "calculation_note": "膝关节按屈曲与伸展丧失值求和（上限100%）；其他关节按已测方向净丧失值平均。",
        "warnings": [] if healthy_rom else ["未提供健侧活动度数据，健侧丧失值按0处理，建议人工复核。"],
    }


def _parse_rom_from_findings(rom_findings: List[str]) -> dict:
    """从 rom_findings 字符串列表解析为结构化 ROM 数据。

    输入格式如：["左膝屈曲0度到120度", "右踝背屈0度到20度", ...]
    输出格式如：{"膝关节": {"左": {"屈曲": 120.0, "伸展": 0.0}, "右": {...}}, ...}
    """
    joint_prefix_map = {
        "肩": "肩关节", "肘": "肘关节", "腕": "腕关节",
        "髋": "髋关节", "膝": "膝关节", "踝": "踝关节",
    }

    result: Dict[str, Dict[str, Dict[str, float]]] = {}

    for finding in rom_findings:
        m = re.match(
            r'([左右])(肩|肘|腕|髋|膝|踝)([一-鿿]+?)([-\d.]+)度(?:到([-.\d]+)度)?',
            finding,
        )
        if not m:
            continue

        side = m.group(1)
        joint_prefix = m.group(2)
        direction = m.group(3)
        start_angle = float(m.group(4))
        end_angle = float(m.group(5)) if m.group(5) else start_angle

        joint_name = joint_prefix_map.get(joint_prefix)
        if not joint_name:
            continue

        if joint_name not in result:
            result[joint_name] = {}
        if side not in result[joint_name]:
            result[joint_name][side] = {}

        result[joint_name][side][direction] = end_angle

    return result


# ============================================================
# 模块3：条款匹配引擎
# ============================================================

@dataclass
class DisabilityResult:
    clause: str
    text: str
    grade: int
    joint: str = ""
    injured_side: str = ""
    loss_pct: float = 0.0
    match_type: str = ""  # joint_function_loss / diagnosis_match / surgery_match / diagnosis_surgery
    details: list = field(default_factory=list)
    branch_id: str = ""
    warnings: list = field(default_factory=list)
    conclusion_type: str = ""

    @property
    def grade_cn(self) -> str:
        if self.grade <= 0:
            return "不构成伤残"
        cn_nums = {1:"一", 2:"二", 3:"三", 4:"四", 5:"五", 6:"六", 7:"七", 8:"八", 9:"九", 10:"十"}
        return f"{cn_nums.get(self.grade, str(self.grade))}级"


@dataclass
class PeriodResult:
    clause: str
    clause_text: str
    误工: int = 0
    护理: int = 0
    营养: int = 0
    sub_label: str = ""


# 长骨名称 → 对应关节映射（用于确定骨折影响的关节）
_BONE_TO_JOINT = {
    "肱骨": "肩关节",
    "桡骨": "腕关节",
    "尺骨": "腕关节",
    "股骨": "膝关节",
    "胫骨": "踝关节",
    "腓骨": "踝关节",
}


def _detect_joints_from_text(text: str) -> List[str]:
    """从文本中检测涉及的关节名称。"""
    joint_names = ["肩关节", "肘关节", "腕关节", "髋关节", "膝关节", "踝关节"]
    return [j for j in joint_names if j in text]


def match_disability(
    diagnoses: List[str],
    rom_data: dict,
    surgery_history: List[str],
    exam_findings: List[str],
    aux_findings: List[str],
) -> List[DisabilityResult]:
    """多路径伤残条款匹配。"""
    results: List[DisabilityResult] = []
    matched_joints: set = set()  # 已匹配的关节，避免重复定级

    all_text = " ".join(diagnoses + surgery_history + exam_findings + aux_findings)
    detected_joints = _detect_joints_from_text(all_text)
    # 也从ROM数据中补充检测关节
    for joint_name in rom_data:
        if joint_name not in detected_joints:
            detected_joints.append(joint_name)

    # --- 路径3：手术匹配（关节置换）---
    for item in DISABILITY_CLAUSE_INDEX:
        if item["type"] != "surgery_match":
            continue
        for kw in item.get("keywords", []):
            if any(kw in s for s in surgery_history):
                applies = item.get("applies_to", [])
                if not applies or any(j in detected_joints for j in applies):
                    joint = ""
                    for j in applies:
                        if j in detected_joints:
                            joint = j
                            matched_joints.add(j)
                            break
                    results.append(DisabilityResult(
                        clause=item["clause"],
                        text=item["text"],
                        grade=item["grade"],
                        joint=joint,
                        match_type="surgery_match",
                    ))
                break

    # --- 路径4：诊断匹配（强直固定）---
    for item in DISABILITY_CLAUSE_INDEX:
        if item["type"] != "diagnosis_match":
            continue
        if item["grade"] not in (7, 8):
            continue  # 只检查强直固定条款（七级/八级）
        applies = item.get("applies_to", [])
        if applies and not any(j in detected_joints for j in applies):
            continue
        for kw in item.get("keywords", []):
            if kw in all_text:
                joint = ""
                for j in applies:
                    if j in detected_joints:
                        joint = j
                        matched_joints.add(j)
                        break
                results.append(DisabilityResult(
                    clause=item["clause"],
                    text=item["text"],
                    grade=item["grade"],
                    joint=joint,
                    match_type="diagnosis_match",
                ))
                break

    # --- 路径2：诊断匹配（创伤性关节炎等）---
    for item in DISABILITY_CLAUSE_INDEX:
        if item["type"] != "diagnosis_match":
            continue
        if item["grade"] in (7, 8):
            continue  # 已在上面处理
        applies = item.get("applies_to", [])
        keywords = item.get("keywords", [])
        prereq = item.get("prerequisite", [])

        # 检查关键词是否匹配
        kw_matched = any(kw in all_text for kw in keywords)
        if not kw_matched:
            continue

        # 检查前提条件
        if prereq and not any(p in all_text for p in prereq):
            continue

        joint = ""
        if applies:
            matched = False
            for j in applies:
                if j in detected_joints:
                    joint = j
                    matched_joints.add(j)
                    matched = True
                    break
            if not matched:
                continue

        # 避免与已有结果重复（附则6.2）
        if joint and joint in [r.joint for r in results]:
            continue

        results.append(DisabilityResult(
            clause=item["clause"],
            text=item["text"],
            grade=item["grade"],
            joint=joint,
            match_type="diagnosis_match",
        ))

    # --- 路径2b：骨折+内固定手术（5.10.6.7 长骨骨折内固定术后）---
    for item in DISABILITY_CLAUSE_INDEX:
        if item["type"] != "fracture_surgery":
            continue
        fracture_kw = item.get("fracture_keywords", [])
        surgery_kw = item.get("surgery_keywords", [])

        # 检查长骨骨折
        has_fracture = any(kw in all_text for kw in fracture_kw)

        # 检查内固定手术
        has_surgery_kw = False
        for s in surgery_history:
            if any(kw in s for kw in surgery_kw):
                has_surgery_kw = True
                break
        if not has_surgery_kw:
            has_surgery_kw = any(kw in all_text for kw in surgery_kw)

        if not (has_fracture and has_surgery_kw):
            continue

        # 找到匹配的骨折名称，推断伤侧和关联关节
        matched_fracture = ""
        injured_side = ""
        joint = ""
        for kw in fracture_kw:
            if kw in all_text:
                matched_fracture = kw
                # 推断伤侧
                for side in ["左", "右"]:
                    if side + kw[:2] in all_text:
                        injured_side = side
                        break
                # 推断关联关节
                for bone_prefix, joint_name in _BONE_TO_JOINT.items():
                    if bone_prefix in kw:
                        joint = joint_name
                        break
                break

        # 避免同一关节已有更高级别判定（仅比较等级，不阻止ROM报告）
        if joint and joint in matched_joints:
            existing_grades = [r.grade for r in results if r.joint == joint and r.grade > 0]
            if existing_grades and min(existing_grades) <= item["grade"]:
                continue

        # 注意：fracture_surgery条款针对的是骨骼而非关节，
        # 不将关联关节加入matched_joints，以便ROM路径仍可报告功能丧失数据

        results.append(DisabilityResult(
            clause=item["clause"],
            text=item["text"],
            grade=item["grade"],
            joint=joint,
            injured_side=injured_side,
            match_type="fracture_surgery",
        ))

    # --- 路径1：关节功能丧失（ROM计算）---
    for joint_name in detected_joints:
        if joint_name in matched_joints:
            continue  # 该关节已有更高级别判定
        joint_rom = rom_data.get(joint_name)
        if not joint_rom:
            continue

        calc = calc_joint_loss(joint_name, joint_rom)
        if calc["loss_pct"] <= 0:
            continue

        # 按功能丧失阈值稳定匹配条款；同时检查 min/max，避免高比例先命中低等级条款。
        best_match = None
        joint_items = sorted(
            [item for item in DISABILITY_CLAUSE_INDEX if item.get("type") == "joint_function_loss" and joint_name in item.get("applies_to", [])],
            key=lambda item: (-float(item.get("min_loss_pct", 0) or 0), int(item.get("grade", 99) or 99)),
        )
        for item in joint_items:
            min_loss = float(item.get("min_loss_pct", 999) or 999)
            max_loss = float(item.get("max_loss_pct", 100.0) or 100.0)
            if min_loss <= float(calc["loss_pct"]) <= max_loss:
                best_match = item
                break

        if best_match:
            results.append(DisabilityResult(
                clause=best_match["clause"],
                text=best_match["text"],
                grade=best_match["grade"],
                joint=joint_name,
                injured_side=calc["injured_side"],
                loss_pct=calc["loss_pct"],
                match_type="joint_function_loss",
                details=calc["details"],
            ))
        elif calc["loss_pct"] > 0:
            # 功能丧失存在但未达到最低阈值
            results.append(DisabilityResult(
                clause="",
                text=f"{joint_name}功能丧失{calc['loss_pct']}%（未达伤残标准）",
                grade=0,
                joint=joint_name,
                injured_side=calc["injured_side"],
                loss_pct=calc["loss_pct"],
                match_type="below_threshold",
                details=calc["details"],
            ))

    # --- 路径5：其他骨折/损伤条款 ---
    for item in DISABILITY_CLAUSE_INDEX:
        if item["type"] not in ("diagnosis_surgery",):
            continue
        if item["type"] == "diagnosis_surgery":
            diag_kw = item.get("diagnosis_keywords", [])
            surg_kw = item.get("surgery_keywords", [])
            diag_matched = any(kw in all_text for kw in diag_kw)
            surg_matched = any(kw in all_text for kw in surg_kw)
            has_surgery = any(kw in s for s in surgery_history for kw in ["手术", "内固定", "切复"])
            if diag_matched and (surg_matched or has_surgery):
                results.append(DisabilityResult(
                    clause=item["clause"],
                    text=item["text"],
                    grade=item["grade"],
                    match_type="diagnosis_surgery",
                ))

    # 按等级排序（等级数越小 = 等级越高，排前面）
    results.sort(key=lambda r: r.grade if r.grade > 0 else 99)

    return results


def _select_period_sub(rule: dict, has_surgery: bool, all_text: str) -> Optional[dict]:
    """根据是否手术选择三期条款的子项。"""
    sub = None
    if has_surgery:
        for s in rule.get("sub_items", []):
            label = s.get("label", "")
            if "手术" in label and "非手术" not in label:
                sub = s
                break
        if sub is None:
            for s in rule.get("sub_items", []):
                label = s.get("label", "")
                if re.match(r"[b-e]\)", label):
                    sub = s
                    break
    else:
        for s in rule.get("sub_items", []):
            label = s.get("label", "")
            if "a)" in label or "非手术" in label:
                sub = s
                break
    if sub is None and rule.get("sub_items"):
        sub = rule["sub_items"][0]
    return sub


def _period_midpoint(rng: List[int]) -> int:
    """计算期限范围的中值。上下限相同时直接取该值。"""
    if rng[0] == rng[1]:
        return rng[0]
    return (rng[0] + rng[1]) // 2


def match_period(
    diagnoses: List[str],
    surgery_history: List[str],
    injury_summary: str = "",
) -> PeriodResult:
    """匹配三期评定条款。多处损伤匹配多条条款，取各期最长值（附录A.4）。"""
    all_text = " ".join(diagnoses + surgery_history) + " " + injury_summary
    has_surgery = any(kw in all_text for kw in ["手术", "内固定", "切复", "置换", "修补术", "切除术"])

    all_matches: List[Tuple[dict, dict]] = []  # (rule, sub_item)

    for rule in PERIOD_CLAUSE_INDEX:
        if not rule.get("keywords"):
            continue
        kw_matched = any(kw in all_text for kw in rule["keywords"])
        if not kw_matched:
            continue

        sub = _select_period_sub(rule, has_surgery, all_text)
        if sub:
            all_matches.append((rule, sub))

    if not all_matches:
        return PeriodResult(clause="", clause_text="", 误工=0, 护理=0, 营养=0)

    # 多处损伤：取各期最长值（附录A.4：以较长的损伤为主，酌情延长）
    max_误工 = 0
    max_护理 = 0
    max_营养 = 0
    primary_rule = None
    primary_sub = None
    primary_period_sum = 0
    matched_clauses = []

    for rule, sub in all_matches:
        # 取上限（最长值），非中值
        误工_val = sub.get("误工", [0, 0])[1]
        护理_val = sub.get("护理", [0, 0])[1]
        营养_val = sub.get("营养", [0, 0])[1]

        max_误工 = max(max_误工, 误工_val)
        max_护理 = max(max_护理, 护理_val)
        max_营养 = max(max_营养, 营养_val)

        matched_clauses.append(rule["clause"])

        # 记录误工最长的条款作为主条款
        if 误工_val > primary_period_sum:
            primary_period_sum = 误工_val
            primary_rule = rule
            primary_sub = sub

    # 条款号：如多条匹配则合并，如 "10.2.11、10.2.14"
    if len(matched_clauses) > 1:
        clause_str = "、".join(matched_clauses)
    else:
        clause_str = matched_clauses[0]

    return PeriodResult(
        clause=clause_str,
        clause_text=f"《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）之{clause_str}条及附录A等相关条款",
        误工=max_误工,
        护理=max_护理,
        营养=max_营养,
        sub_label=primary_sub.get("label", "") if primary_sub else "",
    )


# ============================================================
# 模块4辅助：遗留问题提取
# ============================================================

_IMAGING_RESIDUAL_KEYWORDS = [
    "骨质缺损", "边缘硬化", "断端硬化", "骨质硬化",
    "关节面欠平整", "关节面不平整", "关节面毛糙", "关节面隆起",
    "骨皮质欠平整",
    "关节间隙欠对称", "关节间隙欠规则", "关节间隙变窄",
    "关节间隙增宽", "关节间隙不对称", "关节间隙不等宽",
    "游离骨质", "游离小骨片", "软骨缺损", "骨质中断",
    "断端不连续", "骨折线模糊",
]

JOINT_ACTIVITY_DESC = {
    "肩关节": "肩关节活动功能障碍",
    "肘关节": "肘关节活动功能障碍",
    "腕关节": "腕关节活动功能障碍",
    "髋关节": "髋关节活动功能障碍",
    "膝关节": "膝关节活动功能障碍",
    "踝关节": "踝关节活动功能障碍",
}

# 用于过滤辅助检查中的手术/术后描述，保留影像学异常
_AUX_SKIP_PATTERNS = [
    r'术后', r'取出术后', r'内固定在位', r'钉道影',
    r'骨折.*?术后', r'内固定.*?术后', r'石膏.*?在位',
    r'^复查$', r'骨折愈合', r'愈合可', r'愈合良好',
    r'^提示[：:]?',
]


def _extract_residual_findings(
    joint_name: str,
    injured_side: str,
    aux_findings: List[str],
    exam_findings: List[str],
) -> Tuple[str, bool]:
    """从辅助检查发现中提取遗留问题的影像学描述。

    策略：找到与该关节相关的最新辅助检查，提取"示"之后的内容，
    过滤手术/术后描述，保留影像学异常关键词。

    Returns: (residual_text, has_traumatic_arthritis)
    """
    joint_short = joint_name.replace("关节", "")  # 膝/踝/肩/肘/腕/髋
    side_prefix = injured_side if injured_side else ""

    # 筛选与该关节相关的辅助检查
    relevant = []
    for f in aux_findings:
        if joint_short in f or joint_name in f:
            relevant.append(f)
    if not relevant and side_prefix:
        for f in aux_findings:
            if side_prefix in f:
                relevant.append(f)

    if not relevant:
        return "", False

    # 取最后一条（通常是最新的复查报告）
    latest = relevant[-1]

    # 提取"示"之后的内容
    m = re.search(r'[：:]示(.+)', latest)
    if m:
        desc = m.group(1).strip().rstrip('。')
    else:
        desc = latest

    has_traumatic_arthritis = "创伤性关节炎" in desc

    # 按逗号分割，过滤手术/术后相关分句，保留异常发现
    parts = re.split(r'[，；]', desc)
    filtered = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 跳过手术/术后相关描述（除非同时含异常关键词）
        if any(re.search(p, part) for p in _AUX_SKIP_PATTERNS):
            if not any(kw in part for kw in _IMAGING_RESIDUAL_KEYWORDS):
                continue
        # 保留包含异常关键词的分句
        if any(kw in part for kw in _IMAGING_RESIDUAL_KEYWORDS):
            filtered.append(part)

    residual_text = "，".join(filtered)
    return residual_text, has_traumatic_arthritis


def _infer_injured_side(joint_name: str, text: str) -> str:
    """从文本中推断某关节的受伤侧别。"""
    joint_short = joint_name.replace("关节", "")
    for side in ["左", "右"]:
        # 匹配"左膝"、"左股骨"（膝关节损伤常关联股骨）等
        if side + joint_short in text:
            return side
    # 扩展匹配：膝关节 → 查"左股"/"左胫"，踝关节 → 查"左胫"/"左距"
    related = {
        "膝关节": ["股骨", "胫骨", "髌骨"],
        "踝关节": ["胫骨下端", "距骨", "内踝", "外踝"],
        "髋关节": ["股骨粗隆", "股骨颈", "髋臼"],
    }
    for keyword in related.get(joint_name, []):
        for side in ["左", "右"]:
            if side + keyword in text:
                return side
    return ""


# ============================================================
# 模块4：模板引擎
# ============================================================

def compose_section5(
    disability_results: List[DisabilityResult],
    period_result: PeriodResult,
    case_data: dict,
) -> str:
    """生成第五部分：分析说明。"""
    subject_name = case_data.get("subject_name", "XXX")
    incident_date = case_data.get("incident_date", "不详")
    injury_summary = case_data.get("injury_summary", "不详")
    surgery_history = case_data.get("surgery_history", [])
    aux_findings = case_data.get("aux_findings", [])
    exam_findings = case_data.get("exam_findings", [])

    lines: List[str] = []

    # --- 固定开头 ---
    lines.append("经审核所送的鉴定材料，结合本中心检验所见，分析如下：")
    lines.append("")

    # --- 总述段：伤情认定 + 手术经过 + 鉴定时机 ---
    injury_facts = injury_summary if injury_summary not in ("不详", "") else "上述损伤"
    lines.append(
        f"根据被鉴定人{subject_name}于{incident_date}发生的外伤史，"
        f"损伤后的病史记载，结合审阅其损伤后的影像学资料，"
        f"其因本次外伤致{injury_facts}等损伤事实可予认定。"
    )

    if surgery_history:
        surg_parts = []
        for s in surgery_history[:3]:
            if "行" not in s:
                surg_parts.append(f"行{s}")
            else:
                surg_parts.append(s)
        lines.append(f"{subject_name}伤后{'；'.join(surg_parts)}。")

    lines.append(f"目前{subject_name}伤情基本稳定，符合鉴定时机要求。")
    lines.append("")

    # --- 1. 伤残等级评定 ---
    lines.append("1.伤残等级评定：")

    has_disability = False
    below_threshold_joints = []
    has_surgery = bool(surgery_history)

    for r in disability_results:
        if r.grade > 0:
            has_disability = True

            # 提取遗留问题
            imaging_residual, has_arthritis = _extract_residual_findings(
                r.joint, r.injured_side, aux_findings, exam_findings,
            )
            residual_parts = []
            if imaging_residual:
                residual_parts.append(imaging_residual)
            if r.joint:
                activity_desc = JOINT_ACTIVITY_DESC.get(r.joint, f"{r.joint}活动功能障碍")
                residual_parts.append(
                    f"{r.injured_side}{activity_desc}" if r.injured_side else activity_desc,
                )
            residual_text = "，".join(residual_parts)

            # 影像学表现
            imaging_text = ""
            if has_arthritis and r.joint:
                joint_ref = f"{r.injured_side}{r.joint}" if r.injured_side else r.joint
                imaging_text = f"，符合{joint_ref}创伤性关节炎的影像学表现"
            elif imaging_residual and r.joint:
                joint_ref = f"{r.injured_side}{r.joint}" if r.injured_side else r.joint
                imaging_text = f"，符合{joint_ref}损伤后的影像学表现"

            # 损伤描述
            if r.match_type == "fracture_surgery":
                # 长骨骨折内固定术后：描述骨折+手术事实
                injury_desc = injury_facts
                surg_desc = "，经手术治疗" if has_surgery else ""
            elif r.joint:
                injury_desc = f"{r.injured_side}{r.joint}损伤" if r.injured_side else f"{r.joint}损伤"
                surg_desc = "，经手术治疗" if has_surgery else ""
            else:
                injury_desc = injury_facts
                surg_desc = "，经手术治疗" if has_surgery else ""

            # 完整段落
            para = (
                f"经检验，被鉴定人{subject_name}因本次外伤致{injury_desc}{surg_desc}，"
                f"目前遗留{residual_text}{imaging_text}。"
            )

            # ROM计算结果（关节功能丧失类）
            if r.match_type == "joint_function_loss" and r.loss_pct > 0:
                joint_ref = f"{r.injured_side}{r.joint}" if r.injured_side else r.joint
                para += f"经测算，其{joint_ref}活动功能丧失达{r.loss_pct}%。"

            para += (
                f"依照《人体损伤致残程度分级》第{r.clause}条"
                f"\"{r.text}\"之规定，{subject_name}已构成{r.grade_cn}伤残。"
            )
            lines.append(para)

        elif r.match_type == "below_threshold":
            below_threshold_joints.append(r)

    if not has_disability:
        # 即使未构成伤残，也要描述伤情和遗留问题
        # 收集所有涉及的关节（从below_threshold结果、ROM数据、损伤文本中检测）
        all_joints = []
        for r in below_threshold_joints:
            if r.joint and r.joint not in all_joints:
                all_joints.append(r.joint)
        if not all_joints:
            detected = _detect_joints_from_text(
                injury_facts + " " + " ".join(aux_findings),
            )
            for j in detected:
                if j not in all_joints:
                    all_joints.append(j)

        # 构建遗留问题描述
        residual_parts = []
        imaging_texts = []
        for joint_name in all_joints:
            # 判定伤侧
            injured_side = ""
            for r in below_threshold_joints:
                if r.joint == joint_name and r.injured_side:
                    injured_side = r.injured_side
                    break
            if not injured_side:
                injured_side = _infer_injured_side(joint_name, injury_facts)

            imaging_residual, has_arthritis = _extract_residual_findings(
                joint_name, injured_side, aux_findings, exam_findings,
            )
            if imaging_residual:
                residual_parts.append(imaging_residual)
            activity_desc = JOINT_ACTIVITY_DESC.get(joint_name, f"{joint_name}活动功能障碍")
            residual_parts.append(
                f"{injured_side}{activity_desc}" if injured_side else activity_desc,
            )
            if has_arthritis:
                joint_ref = f"{injured_side}{joint_name}" if injured_side else joint_name
                imaging_texts.append(f"符合{joint_ref}创伤性关节炎的影像学表现")
            elif imaging_residual:
                joint_ref = f"{injured_side}{joint_name}" if injured_side else joint_name
                imaging_texts.append(f"符合{joint_ref}损伤后的影像学表现")

        residual_text = "，".join(residual_parts)
        imaging_text = "，".join(imaging_texts)

        # 损伤描述
        if all_joints:
            injury_desc = "、".join(
                f"{_infer_injured_side(j, injury_facts)}{j}损伤" for j in all_joints
            )
        else:
            injury_desc = injury_facts

        surg_desc = "，经手术治疗" if has_surgery else ""

        para = (
            f"经检验，被鉴定人{subject_name}因本次外伤致{injury_desc}{surg_desc}，"
            f"目前遗留{residual_text}"
        )
        if imaging_text:
            para += f"，{imaging_text}"
        para += "。"

        # 如果有ROM计算结果（below_threshold），附上百分比
        rom_parts = []
        for r in below_threshold_joints:
            if r.loss_pct > 0:
                joint_ref = f"{r.injured_side}{r.joint}" if r.injured_side else r.joint
                rom_parts.append(f"{joint_ref}活动功能丧失达{r.loss_pct}%")
        if rom_parts:
            para += f"经测算，其{'，'.join(rom_parts)}。"

        para += "依照《人体损伤致残程度分级》标准相关条款之规定，未构成人体损伤伤残等级。"
        lines.append(para)

    elif below_threshold_joints:
        # 有伤残但还有功能丧失未达阈值的关节
        rom_parts = []
        for r in below_threshold_joints:
            if r.loss_pct > 0:
                joint_ref = f"{r.injured_side}{r.joint}" if r.injured_side else r.joint
                rom_parts.append(f"{joint_ref}活动功能丧失达{r.loss_pct}%")
        if rom_parts:
            lines.append(
                f"经测算，其{'，'.join(rom_parts)}，"
                f"未达上述条款规定的功能丧失标准，不构成伤残等级。"
            )
        else:
            joint_names = "、".join(r.joint for r in below_threshold_joints)
            lines.append(
                f"被鉴定人{subject_name}的其余损伤（{joint_names}功能丧失未达25%），"
                f"尚未构成法医临床伤残等级。"
            )

    lines.append("")

    # --- 2. 三期评定 ---
    if period_result.误工 > 0 or period_result.护理 > 0 or period_result.营养 > 0:
        lines.append("2.误工期限、护理期限、营养期限：")
        lines.append(
            f"被鉴定人{subject_name}损伤后，因治疗和康复需要而无法参加工作，需要休息，存在误工；"
            f"损伤后生活自理能力下降，存在短期内生活不能完全自理，需要他人帮助的情况；"
            f"因创伤可导致机体代谢异常，短期内可能单纯依靠日常饮食不能满足受损机体对热能和各种营养的需求，必须从其他食品中获得营养。"
            f"因此，参照《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）"
            f"第{period_result.clause}条及附录A等相关条款之规定，"
            f"结合其损伤程度、治疗和实际恢复情况，"
            f"建议所需的误工期限以{period_result.误工}日左右为宜，"
            f"护理期限以{period_result.护理}日左右为宜，"
            f"营养期限以{period_result.营养}日左右为宜。"
        )

    return "\n".join(lines)


def compose_section6(
    disability_results: List[DisabilityResult],
    period_result: PeriodResult,
    case_data: dict,
) -> str:
    """生成第六部分：鉴定意见。"""
    subject_name = case_data.get("subject_name", "XXX")
    incident_date = case_data.get("incident_date", "不详")
    injury_summary = case_data.get("injury_summary", "不详")
    surgery_history = case_data.get("surgery_history", [])

    lines: List[str] = []
    injury_facts = injury_summary if injury_summary not in ("不详", "") else "上述损伤"
    has_surgery = bool(surgery_history)

    # 伤残结论
    disability_clauses = [r for r in disability_results if r.grade > 0]
    if disability_clauses:
        # 遗留问题摘要
        residual_parts = []
        for r in disability_clauses:
            if r.match_type == "fracture_surgery":
                # 长骨骨折内固定术后：无需列举关节活动障碍
                continue
            if r.joint:
                activity_desc = JOINT_ACTIVITY_DESC.get(r.joint, f"{r.joint}活动功能障碍")
                residual_parts.append(
                    f"{r.injured_side}{activity_desc}" if r.injured_side else activity_desc,
                )

        # 伤残等级描述
        grades = [r.grade for r in disability_clauses]
        if len(set(grades)) == 1 and len(grades) > 1:
            grade_desc = f"{len(grades)}项{disability_clauses[0].grade_cn}"
        else:
            grade_desc = disability_clauses[0].grade_cn

        residual_text = "、".join(residual_parts)
        if residual_text:
            lines.append(
                f"被鉴定人{subject_name}于{incident_date}因事故致{injury_facts}等，"
                f"{'经手术治疗，' if has_surgery else ''}"
                f"目前遗留{residual_text}，"
                f"依照《人体损伤致残程度分级》，已构成{grade_desc}伤残。"
            )
        else:
            lines.append(
                f"被鉴定人{subject_name}于{incident_date}因事故致{injury_facts}等，"
                f"{'经手术治疗，' if has_surgery else ''}"
                f"依照《人体损伤致残程度分级》，已构成{grade_desc}伤残。"
            )
    else:
        lines.append(
            f"被鉴定人{subject_name}于{incident_date}因事故致{injury_facts}等，"
            f"{'经手术治疗，' if has_surgery else ''}"
            f"依照《人体损伤致残程度分级》，未构成伤残。"
        )

    # 三期结论
    if period_result.误工 > 0:
        lines.append("")
        lines.append(
            f"建议被鉴定人{subject_name}因本次损伤所需的"
            f"误工期限以{period_result.误工}日左右为宜，"
            f"护理期限以{period_result.护理}日左右为宜，"
            f"营养期限以{period_result.营养}日左右为宜。"
        )

    return "\n".join(lines)


def _is_unknown_rule_value(value) -> bool:
    if value is None:
        return True
    if value is False:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    text = str(value).strip()
    if not text:
        return True
    return any(k in text for k in ["不详", "待补充", "未见", "无", "否", "无法判断", "需人工", "未查"])


def _has_meaningful_adl_score(value) -> bool:
    if _is_unknown_rule_value(value):
        return False
    try:
        score = float(str(value).strip())
    except Exception:
        return False
    return 0 <= score <= 100


def _has_meaningful_positive_value(value) -> bool:
    if _is_unknown_rule_value(value):
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value > 0
    text = str(value).strip()
    return any(k in text for k in ["有", "存在", "阳性", "受限", "障碍", "缺损", "减退", "异常"])


def _manual_review_markers_from_facts(facts) -> List[dict]:
    markers = []
    commission = facts.forensic_facts.get("commission", {}) if isinstance(facts.forensic_facts, dict) else {}
    for key, label in [
        ("requires_causation", "因果关系/参与度"),
        ("requires_medical_fee_review", "医疗费或诊疗合理性"),
        ("requires_future_treatment_fee", "后续治疗费"),
        ("requires_care_dependency", "护理依赖"),
    ]:
        if commission.get(key):
            markers.append({"category": label, "source": f"forensic_facts.commission.{key}", "reason": "委托事项或材料提示该项需人工审查。"})
    rf = facts.rule_features or {}
    if _has_meaningful_positive_value(rf.get("objective_neuro_deficit")) or _has_meaningful_adl_score(rf.get("adl_score")):
        markers.append({"category": "神经功能/ADL", "source": "forensic_facts.rule_features", "reason": "仅在存在明确神经功能缺损或可采纳 ADL/Barthel 评分时需人工复核；普通骨折案件可不处理。", "priority": "recommended"})
    if rf.get("delayed_union_or_nonunion"):
        markers.append({"category": "骨折延迟愈合/骨不连", "source": "forensic_facts.fracture_healing_status", "reason": "普通延迟愈合或骨不连需结合病程、因果关系和功能后果人工审查。"})
    return markers


def _disability_conclusion_status(disability_results: List[DisabilityResult], disability_audit: dict, manual_items: List[dict], facts) -> dict:
    positive = [r for r in disability_results if r.grade > 0]
    negative = [r for r in disability_results if r.grade == 0 and (r.match_type.startswith("negative") or r.match_type == "below_threshold")]
    manual_markers = list(manual_items or []) + _manual_review_markers_from_facts(facts)
    insufficient = [item for item in (facts.uncertain_points or []) if isinstance(item, str)]

    human_confirmed_negative = any(getattr(r, "match_type", "") == "negative_human_confirmed_no_disability" or getattr(r, "conclusion_type", "") == "no_disability" and getattr(r, "match_type", "").startswith("negative_human") for r in negative)

    if positive and manual_markers:
        status = "mixed_manual"
        label = "自动阳性伤残结论 + 人工审查项"
    elif positive:
        status = "positive_auto"
        label = "自动阳性伤残结论"
    elif human_confirmed_negative:
        status = "no_disability_confirmed"
        label = "人工确认不构成伤残"
    elif negative and manual_markers:
        status = "mixed_manual"
        label = "自动阴性伤残结论 + 人工审查项"
    elif negative:
        status = "negative_auto"
        label = "自动阴性伤残结论"
    elif manual_markers:
        status = "manual_review"
        label = "主要需人工审查"
    else:
        status = "insufficient_facts"
        label = "事实不足，暂不能自动判断伤残"

    return {
        "status": status,
        "label": label,
        "positive_count": len(positive),
        "negative_evidence_count": len(negative),
        "manual_review_count": len(manual_markers),
        "insufficient_fact_count": 0 if status != "insufficient_facts" else len(insufficient),
        "negative_evidence": disability_audit.get("negative_disability_clauses", []),
        "manual_review_markers": manual_markers,
    }


# ============================================================
# 模块5：主流程 + CLI
# ============================================================

def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _commission_requests_period_from_facts(facts) -> bool:
    forensic = getattr(facts, "forensic_facts", {}) or {}
    commission = forensic.get("commission") or {}
    if not isinstance(commission, dict):
        commission = {}
    scope_type = str(commission.get("scope_type") or "").strip()
    wants_period = bool(
        commission.get("requires_work_loss_period")
        or commission.get("requires_nursing_period")
        or commission.get("requires_nutrition_period")
    )
    explicit = bool(commission.get("raw_items") or commission.get("scope_source") == "raw_commission")
    text = "\n".join(str(item) for item in (getattr(facts, "entrustment_items", []) or []) if item)
    text_wants_period = any(k in text for k in ["误工期", "护理期", "营养期", "三期"])
    has_machine_period_candidates = bool((getattr(facts, "period_rule_features", {}) or {}).get("candidate_clauses"))
    fracture_features = getattr(facts, "fracture_features", {}) or {}
    has_period_fact_features = bool(
        any(fracture_features.get(key) for key in ["vertebral", "rib", "limb", "pelvis", "scar_burn"])
        or (getattr(facts, "rule_features", {}) or {}).get("scar")
        or forensic.get("scar_burn_facts")
    )
    if scope_type == "disability_only":
        # If the raw commission was explicit and only requested disability, respect it. If the scope is merely
        # an unresolved/heuristic by-product, allow machine period candidates to keep the report complete.
        return bool((not explicit) and (has_machine_period_candidates or text_wants_period))
    if scope_type in {"period_only", "disability_period", "disability_period_medical_fee_review"}:
        return True
    if explicit:
        return wants_period or text_wants_period
    return bool(text_wants_period or has_machine_period_candidates or has_period_fact_features)


def run_from_facts(
    facts,
    judge_data: Dict,
    output_dir: str,
    muscle_grade: str = "M5",
    meta_extra: Optional[Dict] = None,
) -> Tuple[str, str]:
    """从已适配的 RulerFacts 运行确定性规则并写入第五六部分。

    这是 Study 2 NoStruct 消融使用的窄入口；默认 Boss/Ruler 流程仍通过
    run_pipeline 读取 structuring/judge JSON 后调用本函数。
    """
    # 如果 structured_rom 为空，仍保留旧的 rom_findings 解析兜底
    if not facts.rom_data:
        section4 = judge_data.get("section4", {})
        physical_exam = section4.get("physical_exam", {})
        rom_findings = physical_exam.get("rom_findings", [])
        if rom_findings:
            facts.rom_data = _parse_rom_from_findings(rom_findings)

    injury_summary = facts.injury_summary
    surgery_history = facts.surgery_history
    subject_name = facts.subject_name
    incident_date = facts.incident_date
    incident_summary = facts.incident_summary
    diagnosis_overview = facts.diagnosis_overview
    entrustment_items = facts.entrustment_items
    exam_findings = facts.exam_findings
    aux_findings = facts.aux_findings

    # --- 伤残条款匹配 ---
    disability_results, disability_audit = match_disability_v2(
        facts=facts,
        legacy_index=DISABILITY_CLAUSE_INDEX,
        legacy_matcher=match_disability,
        calc_joint_loss=calc_joint_loss,
        muscle_grade=muscle_grade,
    )

    # --- 三期条款匹配：仅在委托事项明确包含三期时启动 ---
    wants_period = _commission_requests_period_from_facts(facts)
    if wants_period:
        period_result, period_audit = match_period_v2(
            facts=facts,
            period_index=PERIOD_CLAUSE_INDEX,
            select_period_sub=_select_period_sub,
        )
    else:
        period_result = PeriodResult(clause="", clause_text="", 误工=0, 护理=0, 营养=0)
        period_audit = {
            "candidate_period_clauses": [],
            "matched_period_clauses": [],
            "period_interval_candidates": [],
            "period_selection": {"skipped": True, "reason": "委托事项未包含误工期、护理期、营养期（三期）鉴定。"},
            "manual_review_items": [],
        }

    # --- 生成第五六部分 ---
    case_data = {
        "subject_name": subject_name,
        "incident_date": incident_date,
        "incident_summary": incident_summary,
        "diagnosis_overview": diagnosis_overview,
        "injury_summary": injury_summary,
        "surgery_history": surgery_history,
        "entrustment_items": entrustment_items,
        "aux_findings": aux_findings,
        "exam_findings": exam_findings,
    }

    section5_md = compose_section5(disability_results, period_result, case_data)
    section6_md = compose_section6(disability_results, period_result, case_data)

    # --- 输出 ---
    full_md = f"# 司法鉴定意见书结构化提要（第五、六部分，AI草稿）\n\n"
    full_md += f"## 五、分析说明\n\n{section5_md}\n\n"
    full_md += f"## 六、鉴定意见\n\n{section6_md}\n"

    coverage = coverage_snapshot()
    manual_review_items = period_audit.get("manual_review_items", [])
    disability_status = _disability_conclusion_status(disability_results, disability_audit, manual_review_items, facts)

    audit = {
        "facts_used": {
            "input_mode": facts.input_mode,
            "forensic_facts_schema_version": facts.forensic_facts.get("schema_version", "") if isinstance(facts.forensic_facts, dict) else "",
            "recognized_injuries": facts.recognized_injuries,
            "fracture_features": facts.fracture_features,
            "rule_features": facts.rule_features,
            "period_rule_features": facts.period_rule_features,
        },
        "candidate_disability_clauses": disability_audit.get("candidate_disability_clauses", []),
        "matched_disability_clauses": disability_audit.get("matched_disability_clauses", []),
        "negative_disability_clauses": disability_audit.get("negative_disability_clauses", []),
        "rejected_disability_clauses": disability_audit.get("rejected_disability_clauses", []),
        "candidate_period_clauses": period_audit.get("candidate_period_clauses", []),
        "matched_period_clauses": period_audit.get("matched_period_clauses", []),
        "period_interval_candidates": period_audit.get("period_interval_candidates", []),
        "period_selection": period_audit.get("period_selection", {}),
        "manual_review_items": manual_review_items,
    }
    validation = {
        "status": "pass",
        "warnings": list(facts.uncertain_points or []),
        "coverage_snapshot": coverage,
    }

    meta = {
        "muscle_grade": muscle_grade,
        "input_mode": facts.input_mode,
        "rule_engine_version": "ruler_rules_v2",
    }
    if meta_extra:
        meta.update(meta_extra)

    payload = {
        "section5": {
            "disability_conclusion_status": disability_status["status"],
            "disability_conclusion_label": disability_status["label"],
            "disability_conclusion_detail": disability_status,
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
                "selected_value_policy": getattr(period_result, "selected_value_policy", "upper_bound"),
                "selection_reasons": getattr(period_result, "selection_reasons", []),
                "selected_from_items": getattr(period_result, "selected_from_items", {}),
                "requires_manual_period_assessment": getattr(period_result, "requires_manual_period_assessment", False),
            },
            "markdown": section5_md,
        },
        "section6": {
            "markdown": section6_md,
        },
        "audit": audit,
        "validation": validation,
        "meta": meta,
    }

    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "section56_result.json")
    md_path = os.path.join(output_dir, "section56_report.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_md)

    return json_path, md_path


def run_pipeline_from_facts(
    facts,
    judge_data: Dict,
    output_dir: str,
    muscle_grade: str = "M5",
    meta_extra: Optional[Dict] = None,
) -> Tuple[str, str]:
    """Study 2 消融入口：直接使用 transient RulerFacts 运行确定性规则。"""
    return run_from_facts(facts, judge_data, output_dir, muscle_grade=muscle_grade, meta_extra=meta_extra)


def run_pipeline(
    structuring_json_path: str,
    judge_json_path: str,
    output_dir: Optional[str] = None,
    muscle_grade: str = "M5",
) -> Tuple[str, str]:
    """主流程：读取两个JSON → 推理 → 输出第五六部分。"""
    structuring_data = load_json(structuring_json_path)
    judge_data = load_json(judge_json_path)

    # --- forensic_facts 优先的事实适配 ---
    facts = adapt_inputs(structuring_data, judge_data)

    # 决定输出目录：优先写入 test_result/<案件编号>；Boss 传入 output_dir 时沿用 Boss 目录。
    if output_dir:
        resolved_output_dir = output_dir
    else:
        parent_dir = os.path.dirname(structuring_json_path)
        resolved_output_dir = resolve_numbered_output_dir(parent_dir, fallback_name=os.path.basename(parent_dir))

    return run_from_facts(
        facts,
        judge_data,
        resolved_output_dir,
        muscle_grade=muscle_grade,
        meta_extra={
            "structuring_json": structuring_json_path,
            "judge_json": judge_json_path,
        },
    )


def main():
    parser = argparse.ArgumentParser(description="ruler_agent — 第五、六部分生成器")
    parser.add_argument("--structuring-json", required=True, help="structuring_agent 输出的 JSON 文件路径")
    parser.add_argument("--judge-json", required=True, help="judge_agent 输出的 JSON 文件路径")
    parser.add_argument("--output-dir", default=None, help="输出目录，默认 test_result/<案件编号>")
    parser.add_argument("--muscle-grade", default="M5", choices=["M1","M2","M3","M4","M5"], help="肌力等级（默认M5）")

    args = parser.parse_args()

    if not os.path.isfile(args.structuring_json):
        print(f"[X] 文件不存在: {args.structuring_json}")
        raise SystemExit(1)
    if not os.path.isfile(args.judge_json):
        print(f"[X] 文件不存在: {args.judge_json}")
        raise SystemExit(1)

    print("========== ruler_agent 启动 ==========")
    json_path, md_path = run_pipeline(
        structuring_json_path=args.structuring_json,
        judge_json_path=args.judge_json,
        output_dir=args.output_dir,
        muscle_grade=args.muscle_grade,
    )

    print(f"[*] JSON输出: {json_path}")
    print(f"[*] Markdown输出: {md_path}")


if __name__ == "__main__":
    main()
