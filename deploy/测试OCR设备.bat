@echo off
chcp 936 >nul
title ForenAgent OCR 设备自检
setlocal

rem ============================================================
rem  测一下这台机器的 OCR 能不能走 GPU。
rem  不动案件数据，只造一张图跑一次，十几秒出结果。
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

"%ENV%\python.exe" "%ROOT%测试OCR设备.py"

echo.
pause
endlocal
