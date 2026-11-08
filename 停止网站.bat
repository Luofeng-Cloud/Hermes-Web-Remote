@echo off
rem ============================================================================
rem  Hermes Web Remote - 智能停止脚本
rem ============================================================================
chcp 936 >nul
cd /d "%~dp0"

echo 正在停止 Hermes 远程 Web 服务 [端口: 9220]...

rem 1. 尝试使用 Python 优雅下线
set PY_CLI=
for /f "delims=" %%I in ('where python 2^>nul') do (
    call :check_real_python "%%I"
    if defined PY_CLI goto :do_stop
)
if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" (
    set "PY_CLI=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
)

:do_stop
if defined PY_CLI (
    "%PY_CLI%" "%~dp0server.py" --stop >nul 2>&1
)

rem 2. 读取 server.pid 兜底终止
if exist "%~dp0server.pid" (
    for /f "usebackq tokens=*" %%A in ("%~dp0server.pid") do (
        taskkill /f /pid %%A >nul 2>&1
    )
    del "%~dp0server.pid" >nul 2>&1
)

rem 3. 强力探测 9220 端口清理残留
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr ":9220 " ^| findstr "LISTENING"') do (
    if not "%%P"=="" if not "%%P"=="0" (
        taskkill /f /pid %%P >nul 2>&1
    )
)

timeout /t 1 /nobreak >nul 2>&1 || ping -n 2 127.0.0.1 >nul
netstat -ano 2>nul | findstr ":9220 " | findstr "LISTENING" >nul
if not errorlevel 1 goto :stop_warn

echo.
echo ============================================================================
echo  Hermes 远程 Web 服务已彻底停止！[端口 9220 已释放]
echo ============================================================================
echo.
timeout /t 3 >nul 2>&1 || ping -n 4 127.0.0.1 >nul
exit /b 0

:stop_warn
echo [警告] 端口 9220 仍有占用，请手动检查任务管理器。
timeout /t 3 >nul 2>&1 || ping -n 4 127.0.0.1 >nul
exit /b 1

:check_real_python
echo %~1 | findstr /i "WindowsApps" >nul
if errorlevel 1 set "PY_CLI=%~1"
exit /b 0
