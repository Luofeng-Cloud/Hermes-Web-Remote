@echo off
rem ============================================================================
rem  Hermes Web Remote - 启动服务
rem ============================================================================
cd /d "%~dp0"
set PYTHONPATH=%~dp0

echo 正在启动 Hermes 远程 Web 服务 (端口: 9220)...

rem 启动后台静默进程
start "" pythonw "%~dp0server.py"

echo.
echo ============================================================================
echo  Hermes 远程 Web 服务已在后台静默运行！
echo  本地测试地址: http://127.0.0.1:9220
echo.
echo  【手机远程连接提示】
echo  请在终端执行以下命令开启 Tailscale 安全隧道:
echo    tailscale serve --bg --set-path /app http://127.0.0.1:9220
echo.
echo  随后在手机浏览器打开终端给出的专属 HTTPS 链接即可直接使用。
echo ============================================================================
echo.
timeout /t 5 >nul
