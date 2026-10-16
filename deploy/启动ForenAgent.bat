@echo off
chcp 65001 >nul
title ForenAgent 1.0 — 鉴定意见书生成
setlocal

rem ============================================================
rem  ForenAgent 一键启动
rem  双击本文件即可。所有检查失败都会用中文提示，不会黑框一闪而过。
rem ============================================================

set "ROOT=%~dp0"
set "APP=%ROOT%app"
set "ENV=%ROOT%env"

echo.
echo   ============================================
echo    ForenAgent 1.0  鉴定意见书生成系统
echo   ============================================
echo.

rem ---------- 1. 检查 Python 环境 ----------
if not exist "%ENV%\python.exe" (
    echo   [错误] 未找到运行环境：%ENV%\python.exe
    echo.
    echo   说明：本程序需要先运行一次「安装.bat」。
    echo         如果已经装过，请确认 env 文件夹与 app 文件夹在同一目录下。
    echo.
    pause
    exit /b 1
)

rem ---------- 2. 检查程序本体 ----------
if not exist "%APP%\agents\boss_agent\boss_agent_main.py" (
    echo   [错误] 未找到程序文件：%APP%\agents\boss_agent\boss_agent_main.py
    echo.
    echo   说明：app 文件夹不完整，请重新从安装包拷贝。
    echo.
    pause
    exit /b 1
)

rem ---------- 3. 检查视觉模型权重 ----------
if not exist "%APP%\models\goniometer_seg_best.pt" (
    echo   [错误] 缺少量角器视觉模型：app\models\goniometer_seg_best.pt
    echo.
    echo   说明：该文件约 23MB，安装包内应带有；缺失时检查照片无法自动读数，
    echo         但文字部分仍可运行。请重新拷贝 app\models 文件夹。
    echo.
    pause
    exit /b 1
)

rem ---------- 4. 检查本地大模型服务 ----------
curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo   [提醒] 未检测到本地大模型服务（Ollama）。
    echo.
    echo   不影响启动，但材料结构化抽取会退化，报告质量会明显下降。
    echo   请先双击桌面的「Ollama」启动它，再重新运行本程序。
    echo.
    echo   按任意键继续启动，或直接关掉本窗口去启动 Ollama。
    pause >nul
)

rem ---------- 5. 环境变量 ----------
set "FORENAGENT_PYTHON=%ENV%\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KMP_DUPLICATE_LIB_OK=TRUE"

cd /d "%APP%"

echo   正在启动，请稍候……
echo   （首次启动要加载 OCR 与视觉模型，可能需要 1-2 分钟，属正常现象）
echo.

"%ENV%\python.exe" "%APP%\agents\boss_agent\boss_agent_main.py"

if errorlevel 1 (
    echo.
    echo   ============================================
    echo    [程序异常退出] 上面是本程序的报错信息。
    echo    请把整个窗口截图发给技术人员。
    echo   ============================================
    echo.
    pause
    exit /b 1
)

endlocal
