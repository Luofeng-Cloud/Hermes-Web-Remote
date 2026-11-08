@echo off
rem ============================================================================
rem  Hermes Web Remote - 重启服务
rem ============================================================================
cd /d "%~dp0"
set PYTHONPATH=%~dp0

echo 正在重启 Hermes 远程 Web 服务...
python "%~dp0server.py" --stop >nul 2>&1
timeout /t 1 /nobreak >nul
start "" pythonw "%~dp0server.py"

echo.
echo ============================================================================
echo  Hermes 远程 Web 服务已重新启动！(端口: 9220)
echo ============================================================================
echo.
timeout /t 3 >nul
