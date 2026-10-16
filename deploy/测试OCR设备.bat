@echo off
chcp 936 >nul
title ForenAgent OCR 设备自检
setlocal

rem ============================================================
rem  对比 CPU 与 GPU 两种设备下 OCR 能不能真的识别出文字。
rem  不动案件数据，自己造图，一两分钟出结果。
rem
rem  分两次调用，因为 PaddleX 一个进程只能初始化一次。
rem ============================================================

set "ROOT=%~dp0"
set "APP=%ROOT%app"
set "ENV=%ROOT%env"

echo.
echo   ============================================
echo    ForenAgent OCR 设备自检
echo   ============================================
echo.

if not exist "%ENV%\python.exe" (
    echo   [错误] 未找到运行环境：%ENV%\python.exe
    echo.
    pause
    exit /b 1
)

rem 与启动脚本保持一致：模型缓存必须放英文路径，否则 Paddle 打不开模型文件。
set "PADDLE_CACHE=%ProgramData%\ForenAgent\models\cache"
if not exist "%PADDLE_CACHE%\paddlex\official_models\PP-OCRv5_mobile_det\inference.pdiparams" (
    echo   [提示] OCR 模型缓存还没搬到英文路径。
    echo          请先双击「启动ForenAgent.bat」正常跑一次，再回来做这个测试。
    echo.
    pause
    exit /b 1
)
set "PADDLE_PDX_CACHE_HOME=%PADDLE_CACHE%\paddlex"
set "PADDLEOCR_HOME=%PADDLE_CACHE%\paddleocr"
set "PADDLE_HOME=%PADDLE_CACHE%\paddle"
set "XDG_CACHE_HOME=%PADDLE_CACHE%\xdg"

set "KMP_DUPLICATE_LIB_OK=TRUE"
cd /d "%APP%"

echo.
echo   ########## 第一轮：CPU ##########
echo.
"%ENV%\python.exe" "%ROOT%测试OCR设备.py" cpu

echo.
echo   ########## 第二轮：GPU（auto）##########
echo.
"%ENV%\python.exe" "%ROOT%测试OCR设备.py" auto

echo.
echo   ============================================
echo    哪一轮能识别出文字，启动脚本里就设哪个设备
echo   ============================================
echo.
pause
endlocal
