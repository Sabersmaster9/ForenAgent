@echo off
chcp 936 >nul
title ForenAgent 1.0 安装
setlocal enabledelayedexpansion

rem ============================================================
rem  ForenAgent 一键安装（在目标电脑上运行一次）
rem  在安装包根目录双击本文件即可，全程无需联网。
rem ============================================================

set "ROOT=%~dp0"
set "ENV=%ROOT%env"
set "APP=%ROOT%app"

echo.
echo   ============================================================
echo    ForenAgent 1.0  安装程序
echo   ============================================================
echo.
echo   安装目录：%ROOT%
echo.
echo   本程序将完成：
echo     1. 释放 Python 运行环境
echo     2. 校验程序文件与模型
echo     3. 创建桌面快捷方式
echo.
echo   全程离线，不需要联网。预计 2-5 分钟。
echo.
pause

rem ---------- 1. 释放 conda 环境 ----------
echo.
echo   [1/4] 释放 Python 运行环境……

if exist "%ENV%\python.exe" (
    echo         已存在，跳过解压。
) else (
    rem U 盘多为 FAT32，单文件上限 4GB，因此 env.zip 以分卷形式分发（env.zip.part00…）。
    rem 若只找到分卷，先用 copy /b 拼回完整 zip。
    if not exist "%ROOT%env.zip" (
        if exist "%ROOT%env.zip.part00" (
            echo         检测到分卷文件，正在合并……
            copy /b "%ROOT%env.zip.part*" "%ROOT%env.zip" >nul
            if not exist "%ROOT%env.zip" (
                echo.
                echo   [错误] 分卷合并失败。
                echo.
                pause
                exit /b 1
            )
            echo         合并完成。
        )
    )
    if not exist "%ROOT%env.zip" (
        echo.
        echo   [错误] 未找到 env.zip 或 env.zip.part00，安装包不完整。
        echo          请确认 env.zip.part00 / part01 / part02 与「安装.bat」在同一目录下。
        echo.
        pause
        exit /b 1
    )
    echo         正在解压 env.zip（约 5GB，解压后约 9GB，请耐心等待）……
    rem conda-pack 产出的 zip 内容在根层（Lib/、DLLs/ …），必须显式解到 env\ 下，
    rem 否则会把环境文件散落到安装包根目录。
    if not exist "%ENV%" mkdir "%ENV%"
    rem 优先用 Windows 自带的 tar(bsdtar)：解 6 万多个文件只要几分钟，
    rem 而 PowerShell 的 Expand-Archive 属于纯托管实现，同样内容要跑几小时。
    where tar >nul 2>&1
    if not errorlevel 1 (
        tar -xf "%ROOT%env.zip" -C "%ENV%"
    ) else (
        echo         未找到 tar，改用 PowerShell 解压（会很慢，请耐心等待）……
        powershell -NoProfile -Command "Expand-Archive -LiteralPath '%ROOT%env.zip' -DestinationPath '%ENV%' -Force"
    )
    if errorlevel 1 (
        echo.
        echo   [错误] 解压失败。可能原因：
        echo          - U 盘或磁盘空间不足（需要至少 15GB）
        echo          - env.zip 拷贝过程中损坏，请重新拷贝
        echo.
        pause
        exit /b 1
    )
    if not exist "%ENV%\python.exe" (
        echo.
        echo   [错误] 解压后未找到 env\python.exe，压缩包内容异常。
        echo.
        pause
        exit /b 1
    )
    echo         解压完成。
)

rem ---------- 2. conda-unpack：改写环境内的绝对路径 ----------
echo.
echo   [2/4] 配置运行环境（conda-unpack）……
if exist "%ENV%\Scripts\conda-unpack.exe" (
    pushd "%ENV%"
    ".\Scripts\conda-unpack.exe" >nul 2>&1
    popd
    echo         完成。
) else (
    echo         未发现 conda-unpack，跳过（环境可能已是可移植形式）。
)

rem ---------- 3. 校验 ----------
echo.
echo   [3/4] 校验程序文件……

set "FAIL=0"
if not exist "%APP%\agents\boss_agent\boss_agent_main.py" (
    echo         [缺失] app\agents\boss_agent\boss_agent_main.py
    set "FAIL=1"
)
if not exist "%APP%\models\goniometer_seg_best.pt" (
    echo         [缺失] app\models\goniometer_seg_best.pt（量角器视觉模型）
    set "FAIL=1"
)
if not exist "%APP%\files\知识库\知识库_条例\人体损伤致残程度分级.md" (
    echo         [缺失] app\files\知识库\知识库_条例\人体损伤致残程度分级.md（规则引擎查表源）
    set "FAIL=1"
)
if not exist "%ENV%\Library\lib\tcl8.6\init.tcl" (
    echo         [缺失] env\Library\lib\tcl8.6\init.tcl（图形界面依赖）
    set "FAIL=1"
)
if "%FAIL%"=="1" (
    echo.
    echo   [错误] 程序文件不完整，请重新从安装包拷贝 app 文件夹。
    echo.
    pause
    exit /b 1
)
echo         程序文件完整。

rem ---------- 4. 创建桌面快捷方式 ----------
echo.
echo   [4/4] 创建桌面快捷方式……

set "LNK=%USERPROFILE%\Desktop\ForenAgent 鉴定意见书生成.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%ROOT%启动ForenAgent.bat';" ^
  "$s.WorkingDirectory='%ROOT%';" ^
  "$s.Description='ForenAgent 1.0 法医临床司法鉴定意见书生成';" ^
  "$s.IconLocation='%SystemRoot%\System32\shell32.dll,71';" ^
  "$s.Save()"
if errorlevel 1 (
    echo         [提醒] 快捷方式创建失败，可直接双击「启动ForenAgent.bat」使用。
) else (
    echo         已创建：桌面 →「ForenAgent 鉴定意见书生成」
)

rem ---------- 完成 ----------
echo.
echo   ============================================================
echo    安装完成
echo   ============================================================
echo.
echo    接下来还有一步（只需做一次）：
echo.
echo      1. 安装 Ollama：双击安装包里的 OllamaSetup.exe
echo      2. 把 ollama_models 文件夹的内容拷到：
echo         %USERPROFILE%\.ollama\models
echo.
echo    做完后，双击桌面「ForenAgent 鉴定意见书生成」即可开始工作。
echo.
echo    ※ 确认能正常出报告之前，请不要删除安装包和 U 盘内容。
echo.
pause

endlocal
