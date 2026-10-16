# -*- coding: utf-8 -*-
"""测这台机器在指定设备下能不能真的识别出文字。

用法：由「测试OCR设备.bat」调用，参数是设备名（cpu / auto），例如
    测试OCR设备.py cpu

为什么要按设备分进程跑：PaddleX 一个进程只能初始化一次，第二次会报
「PDX has already been initialized. Reinitialization is not supported.」

还要注意：脚本里绝不能自己去 import paddle 或调 paddle.device.*，
那同样会提前初始化 PaddleX。设备信息一律从引擎对象的
_forenagent_device_status 取。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time

# 控制台是 cp936（启动脚本里 chcp 936），输出必须跟着用 gbk，否则中文全是乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="gbk", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
DEVICE = (sys.argv[1] if len(sys.argv) > 1 else "cpu").strip().lower()

os.environ["FORENAGENT_LOCAL_OCR_DEVICE"] = DEVICE

if not os.path.isdir(APP):
    print("[错误] 同目录下找不到 app 文件夹：%s" % APP)
    raise SystemExit(1)
sys.path.insert(0, APP)
os.chdir(APP)


def line(text=""):
    print(text)


line("-" * 62)
line("  设备：%s" % DEVICE)
line("-" * 62)

# ---------- 造测试图 ----------
import cv2
import numpy as np

img = np.full((300, 900, 3), 255, dtype=np.uint8)
cv2.putText(img, "FORENAGENT OCR TEST", (30, 130),
            cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 0), 4, cv2.LINE_AA)
cv2.putText(img, "2026-10-16  1234567890", (30, 220),
            cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3, cv2.LINE_AA)

# 真实案卷里的图片文件名都是中文（原告-身份证.jpg、原告证据-门诊病历…），
# 所以除了数组输入，还要专门走一遍「中文文件名 + 传路径」这条路，
# 这正是主程序 engine.predict(file_path) 的用法。
tmpdir = tempfile.mkdtemp(prefix="forenagent_ocr_")
cn_path = os.path.join(tmpdir, "原告-身份证.jpg")
cv2.imencode(".jpg", img)[1].tofile(cn_path)
line("  测试图已生成，中文文件名样例：%s" % os.path.basename(cn_path))
line()

# ---------- 建引擎 ----------
from agents.common.ocr_client import _ocr_result_to_text, build_paddle_ocr
from agents.common.runtime_config import get_runtime_config

cfg = get_runtime_config()
line("  正在加载 OCR 模型……")
started = time.perf_counter()
try:
    engine = build_paddle_ocr(cfg)
except Exception as exc:
    line("  [失败] 引擎加载不了：%s: %s" % (type(exc).__name__, exc))
    raise SystemExit(1)

status = getattr(engine, "_forenagent_device_status", {}) or {}
line("  加载完成 %.1f 秒，实际使用设备：%s" % (time.perf_counter() - started, status.get("resolved")))
line("  %s" % status.get("reason"))
line()

# ---------- 两种输入各跑一次 ----------
cases = [
    ("数组输入", lambda: engine.predict(img)),
    ("中文文件名+路径输入", lambda: engine.predict(cn_path)),
]
verdict = {}
for label, call in cases:
    try:
        started = time.perf_counter()
        text = _ocr_result_to_text(call())
        elapsed = time.perf_counter() - started
        ok = bool(text.strip())
        verdict[label] = ok
        line("  %-16s %6.2f 秒   %s" % (
            label, elapsed,
            ("识别到：" + text.replace("\n", " | ")[:60]) if ok else "**识别结果为空**"))
    except Exception as exc:
        verdict[label] = False
        line("  %-16s 失败：%s: %s" % (label, type(exc).__name__, exc))

line()
if all(verdict.values()):
    line("  结论：这台机器用 %s 能正常识别。" % DEVICE)
else:
    bad = [k for k, v in verdict.items() if not v]
    line("  结论：%s 识别不出内容，这个设备不能用。" % "、".join(bad))
line()

try:
    os.remove(cn_path)
    os.rmdir(tmpdir)
except OSError:
    pass
