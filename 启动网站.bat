@echo off
rem ============================================================================
rem  Hermes Web Remote - 全自动自适应启动脚本 (集成 Tailscale 自动穿透)
rem ============================================================================
chcp 936 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0

echo [1/3] 正在检查端口占用情况...
netstat -ano 2>nul | findstr ":9220 " | findstr "LISTENING" >nul
if not errorlevel 1 goto :already_running

echo [2/3] 正在自动探测 Python 运行环境...
set PY_EXE=

rem 1. 探测系统 PATH 中的 pythonw
for /f "delims=" %%I in ('where pythonw 2^>nul') do if not defined PY_EXE set "PY_EXE=%%I"
if defined PY_EXE goto :found_python

rem 2. 探测 Hermes 官方标准虚拟环境
if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\pythonw.exe" (
    set "PY_EXE=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\pythonw.exe"
    goto :found_python
)

rem 3. 探测 Hermes .venv 独立环境
if exist "%LOCALAPPDATA%\hermes\hermes-agent\.venv\Scripts\pythonw.exe" (
    set "PY_EXE=%LOCALAPPDATA%\hermes\hermes-agent\.venv\Scripts\pythonw.exe"
    goto :found_python
)

rem 4. 探测 Hermes tools 独立运行包
for /d %%D in ("%LOCALAPPDATA%\hermes\tools\python*") do (
    if exist "%%D\pythonw.exe" (
        set "PY_EXE=%%D\pythonw.exe"
        goto :found_python
    )
)

rem 5. 探测用户本地标准 Python 安装路径
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%D\pythonw.exe" (
        set "PY_EXE=%%D\pythonw.exe"
        goto :found_python
    )
)

rem 6. 兜底探测真实 python.exe (过滤 WindowsApps 空桩)
for /f "delims=" %%I in ('where python 2^>nul') do (
    call :check_real_python "%%I"
    if defined PY_EXE goto :found_python
)

:no_python
echo.
echo ============================================================================
echo  [错误] 未检测到可用的 Python 环境！
echo  请确保已安装 Python 3.10+ (并勾选 Add to PATH) 或已安装 Hermes Agent。
echo ============================================================================
echo.
pause
exit /b 1

:check_real_python
echo %~1 | findstr /i "WindowsApps" >nul
if errorlevel 1 set "PY_EXE=%~1"
exit /b 0

:found_python
echo [3/3] 检测到 Python 核心: %PY_EXE%
echo 正在后台静默拉起 server.py...
start "" "%PY_EXE%" "%~dp0server.py"

rem 等待 2 秒验活
ping -n 3 127.0.0.1 >nul
netstat -ano 2>nul | findstr ":9220 " | findstr "LISTENING" >nul
if errorlevel 1 goto :start_warn

goto :show_success

:already_running
echo.
echo [提示] Hermes 远程 Web 服务已经在运行中 [端口: 9220]

:show_success
rem 探测并配置 Tailscale 自动穿透
set TS_EXE=
where tailscale >nul 2>&1 && set "TS_EXE=tailscale"
if not defined TS_EXE (
    if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS_EXE=%ProgramFiles%\Tailscale\tailscale.exe"
)
if not defined TS_EXE (
    if exist "%ProgramFiles(x86)%\Tailscale\tailscale.exe" set "TS_EXE=%ProgramFiles(x86)%\Tailscale\tailscale.exe"
)

echo.
echo ============================================================================
echo  Hermes 远程 Web 服务运行正常！
echo  本地测试地址: http://127.0.0.1:9220
echo.

if not defined TS_EXE goto :no_tailscale

echo  【手机专属访问链接 - Tailscale 安全穿透】
echo  正在自动更新并打通 Tailscale 隧道通道:
echo ----------------------------------------------------------------------------
"%TS_EXE%" serve --bg --set-path /app http://127.0.0.1:9220
echo ----------------------------------------------------------------------------
echo  请在手机浏览器 [Safari 或 Chrome] 直接打开上方给出的专属 HTTPS 链接即可！
goto :after_tailscale

:no_tailscale
echo  [提示] 未检测到 Tailscale 客户端，当前仅可通过本机或局域网访问。
echo  若需手机外出远程使用，请在电脑与手机上安装并登录 Tailscale。

:after_tailscale
echo ============================================================================
echo.
timeout /t 8 >nul 2>&1 || ping -n 9 127.0.0.1 >nul
exit /b 0

:start_warn
echo.
echo ============================================================================
echo  [提示] 服务已发起启动指令，正在等待端口就绪...
echo  若无法访问，请查看 server.log 了解详细状态。
echo ============================================================================
echo.
timeout /t 5 >nul 2>&1 || ping -n 6 127.0.0.1 >nul
exit /b 0
