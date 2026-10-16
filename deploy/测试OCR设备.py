# -*- coding: utf-8 -*-
"""测一下这台机器的 OCR 能不能走 GPU。不动案件数据，只造一张图试一次。

用法：双击同目录的「测试OCR设备.bat」。

注意：这个脚本刻意不自己去 import paddle 或调 paddle.device.*。
那样会在 build_paddle_ocr() 之前先初始化一次 PaddleX，之后引擎就报
「PDX has already been initialized. Reinitialization is not supported.」。
想知道设备情况，从引擎对象上的 _forenagent_device_status 取。
"""

from __future__ import annotations

import io
import os
import sys
import time

# 控制台是 cp936（启动脚本里 chcp 936），输出必须跟着用 gbk，否则中文全是乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="gbk", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")

# 默认按 auto 试；想强制指定可以自己改这一行
os.environ.setdefault("FORENAGENT_LOCAL_OCR_DEVICE", "auto")

if not os.path.isdir(APP):
    print("[错误] 同目录下找不到 app 文件夹：%s" % APP)
    raise SystemExit(1)
sys.path.insert(0, APP)
os.chdir(APP)

print("=" * 62)
print(" ForenAgent OCR 设备自检")
print("=" * 62)
print()


# ---------- 1. 造一张带文字的测试图 ----------
try:
    import cv2
    import numpy as np
except Exception as exc:
    print("[错误] 需要 cv2 / numpy：%s" % exc)
    raise SystemExit(1)

img = np.full((300, 900, 3), 255, dtype=np.uint8)
cv2.putText(img, "FORENAGENT OCR TEST", (30, 130),
            cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 0), 4, cv2.LINE_AA)
cv2.putText(img, "2026-10-16  1234567890", (30, 220),
            cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3, cv2.LINE_AA)
print("  已生成测试图 900x300")
print()

# ---------- 2. 构造引擎（这一步就能看出设备能不能用） ----------
from agents.common.ocr_client import build_paddle_ocr
from agents.common.runtime_config import get_runtime_config

cfg = get_runtime_config()
print("  请求的设备        : %s" % cfg.local_ocr_device)
print("  正在加载 OCR 模型……")

started = time.perf_counter()
try:
    engine = build_paddle_ocr(cfg)
except Exception as exc:
    print()
    print("  [失败] OCR 引擎加载不了")
    print("         %s: %s" % (type(exc).__name__, exc))
    print()
    print("  请把从「请求的设备」到最后这一整段截图发给技术人员。")
    raise SystemExit(1)

status = getattr(engine, "_forenagent_device_status", {}) or {}
print("  加载完成，用时 %.1f 秒" % (time.perf_counter() - started))
print()
print("  实际解析为        : %s" % status.get("resolved"))
print("  CUDA 可用         : %s" % status.get("cuda_available"))
print("  显卡              : %s" % (status.get("gpu_name") or "（未识别到）"))
print("  说明              : %s" % status.get("reason"))
print()

# ---------- 3. 跑两张，第二张才是真实速度 ----------
print("  正在识别（第一张含预热，会慢一些）……")
for i in (1, 2):
    started = time.perf_counter()
    try:
        result = engine.predict(img)
        texts = []
        for page in result:
            data = getattr(page, "json", None)
            if isinstance(data, dict):
                texts.extend((data.get("res") or {}).get("rec_texts") or [])
        elapsed = time.perf_counter() - started
        shown = " | ".join(str(t) for t in texts[:3]) or "（未识别到文字）"
        print("    第 %d 张：%.2f 秒   识别到：%s" % (i, elapsed, shown))
    except Exception as exc:
        print("    第 %d 张 失败：%s: %s" % (i, type(exc).__name__, exc))
        print()
        print("  [失败] 引擎能加载，但推理跑不起来。这说明当前 Paddle 构建")
        print("         不支持这张显卡的架构，只能继续用 CPU。")
        raise SystemExit(1)

print()
print("=" * 62)
print(" 结论")
print("=" * 62)
if status.get("resolved") == "gpu":
    print("  可以走 GPU。把启动脚本 启动ForenAgent.bat 里的")
    print('      set "FORENAGENT_LOCAL_OCR_DEVICE=cpu"')
    print("  改成")
    print('      set "FORENAGENT_LOCAL_OCR_DEVICE=auto"')
    print("  就能快很多（参考：开发机上 41 个文件约 31 秒）。")
else:
    print("  走的是 CPU。当前 Paddle 构建没认到这台机器的显卡。")
    print("  保持 CPU 也能正常出报告，只是慢；要提速得换支持该显卡")
    print("  架构的 Paddle 构建，属于后续优化，不影响使用。")
print()
