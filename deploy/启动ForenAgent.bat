@echo off
chcp 936 >nul
title ForenAgent 1.0 - 鉴定意见书生成
setlocal

rem ============================================================
rem  ForenAgent 一键启动
rem  双击本文件即开工。运行中若失败，窗口会停住并显示原因，
rem  把整个窗口截图发给技术人员即可。
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
    echo         如果已经装好，请确认 env 文件夹和 app 文件夹在同一个目录下。
    echo.
    pause
    exit /b 1
)

rem ---------- 2. 检查程序 ----------
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
    echo   说明：该文件约 23MB，安装包中应有。缺失时关节活动度照片无法自动测量，
    echo         请从安装包的 app\models 文件夹中补齐。
    echo.
    pause
    exit /b 1
)

rem ---------- 4. 检查本地大模型服务 ----------
rem 注意：这里必须写在 PATH 改动之前，否则可能调用到环境自带的 curl。
curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo   [提示] 未检测到本地大模型服务（Ollama）。
    echo.
    echo   不影响程序启动，但材料结构化抽取、人机复核窗口将无法使用。
    echo   建议双击桌面「Ollama」图标启动，等托盘出现羊驼图标后重试。
    echo.
    echo   若只想先把界面打开，按任意键继续。
    pause >nul
)

rem ---------- 5. 运行参数 ----------
set "FORENAGENT_PYTHON=%ENV%\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KMP_DUPLICATE_LIB_OK=TRUE"

rem ============================================================
rem  OCR 相关设置
rem ============================================================
rem ---- (1) 模型缓存必须放在纯英文路径 ----
rem Paddle 的 C++ 推理层用窄字符 API 打开模型文件：路径里只要有中文，文件就打不开，
rem 它拿到一个空流，抛 [json.exception.parse_error.101] attempting to parse an
rem empty input。现象是所有照片 OCR 全部失败，而 PDF 因为有文字层走文本抽取、
rem 不经过 OCR，看起来「只有一个文件成功」，极容易被误判成显卡问题。
rem 所以模型缓存统一搬到 %ProgramData%\ForenAgent（固定英文路径）下。
set "PADDLE_CACHE=%ProgramData%\ForenAgent\models\cache"
if not exist "%PADDLE_CACHE%\paddlex\official_models\PP-OCRv5_mobile_det\inference.pdiparams" (
    echo   首次运行：正在把 OCR 模型缓存复制到英文路径（约 200MB），请稍候……
    if not exist "%APP%\models\cache\paddlex" (
        echo   [错误] 程序目录下找不到 OCR 模型缓存：%APP%\models\cache\paddlex
        echo          请重新从安装包拷贝 app 文件夹。
        echo.
        pause
        exit /b 1
    )
    if not exist "%PADDLE_CACHE%" mkdir "%PADDLE_CACHE%"
    xcopy "%APP%\models\cache\*" "%PADDLE_CACHE%\" /E /I /Q /Y >nul
    echo   完成。
)
set "PADDLE_PDX_CACHE_HOME=%PADDLE_CACHE%\paddlex"
set "PADDLEOCR_HOME=%PADDLE_CACHE%\paddleocr"
set "PADDLE_HOME=%PADDLE_CACHE%\paddle"
set "XDG_CACHE_HOME=%PADDLE_CACHE%\xdg"

rem ---- (2) OCR 设备 ----
rem cpu = 强制走 CPU（慢，但一定可用）；auto = 有 CUDA 就优先用 GPU。
rem 目标机是 RTX 5060 Ti（sm_120），环境里的 Paddle 是 cu126 构建，很可能不含
rem sm_120 的 kernel。先按 cpu 把流程跑通，确认无误后再试着改回 auto 看 GPU 能否用。
set "FORENAGENT_LOCAL_OCR_DEVICE=cpu"
rem CPU 跑 OCR 的线程数。程序默认只给 2，60 多张图的案子会等到天荒地老，给到 8。
set "FORENAGENT_LOCAL_OCR_CPU_THREADS=8"

rem Tcl/Tk 库目录（关键，缺了图形界面起不来）
rem 运行环境是 conda 打包出来的。直接调用 env\python.exe 时不会执行 conda 的
rem activate 脚本，Tcl 找不到自己的库目录，界面会以
rem   _tkinter.TclError: Can't find a usable init.tcl ...
rem 报错退出。下面既把库目录显式告诉 Tcl，也把 conda 本来会加的几个目录补进 PATH。
set "CONDA_PREFIX=%ENV%"
if exist "%ENV%\etc\conda\activate.d" (
    for %%F in ("%ENV%\etc\conda\activate.d\*.bat") do call "%%F" >nul 2>&1
)
if exist "%ENV%\Library\lib\tcl8.6" set "TCL_LIBRARY=%ENV%\Library\lib\tcl8.6"
if not defined TCL_LIBRARY if exist "%ENV%\tcl\tcl8.6" set "TCL_LIBRARY=%ENV%\tcl\tcl8.6"
if not defined TCL_LIBRARY if exist "%ENV%\lib\tcl8.6" set "TCL_LIBRARY=%ENV%\lib\tcl8.6"
if exist "%ENV%\Library\lib\tk8.6" set "TK_LIBRARY=%ENV%\Library\lib\tk8.6"
if not defined TK_LIBRARY if exist "%ENV%\tcl\tk8.6" set "TK_LIBRARY=%ENV%\tcl\tk8.6"
if not defined TK_LIBRARY if exist "%ENV%\lib\tk8.6" set "TK_LIBRARY=%ENV%\lib\tk8.6"
set "PATH=%ENV%\Library\bin;%ENV%\Library\lib;%ENV%\Scripts;%ENV%;%PATH%"

rem 图形界面自检：初始化 Tcl（不弹窗口），失败就直接给出人话提示，
rem 省得看到一个看不懂的 Traceback。
"%ENV%\python.exe" -c "import tkinter; tkinter.Tcl()" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 图形界面组件（Tcl/Tk）加载失败。
    echo.
    echo   当前识别的库目录：
    echo     TCL_LIBRARY = %TCL_LIBRARY%
    echo     TK_LIBRARY  = %TK_LIBRARY%
    echo.
    echo   请把整个窗口截图发给技术人员。
    echo.
    pause
    exit /b 1
)

rem ---------- 6. 启动 ----------
cd /d "%APP%"

echo   正在启动，请稍候……
echo   首次启动要加载 OCR 与视觉模型，可能需要 1-2 分钟（属正常现象）
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
