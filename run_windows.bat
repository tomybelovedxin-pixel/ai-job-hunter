@echo off
chcp 65001 >nul
title AI Job Hunter - 本地智能求职管理系统

echo ========================================================
echo          AI Job Hunter 本地智能求职系统
echo ========================================================
echo.

:: 检查 Python 环境
set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [错误] 未检测到 Python 环境！
    echo 请先运行 setup_windows.ps1 安装依赖或手动安装 Python 3.10+。
    echo.
    pause
    exit /b 1
)

:: 初始化检查用户数据目录与配置文件
"%PYTHON_CMD%" -m jobagent init >nul 2>nul

:MENU
echo 请选择要执行的操作:
echo   [1] 启动专属求职 Chrome 浏览器 (独立隔离配置)
echo   [2] 启动本地 WebUI 求职管理面板 (http://127.0.0.1:8765)
echo   [3] 启动后台 HR 聊天与简历请求自动同意守护 (Daemon)
echo   [4] 运行一轮职位抓取与匹配分析 (Pipeline)
echo   [5] 查看当前运行配置与本地数据目录路径
echo   [0] 退出
echo ========================================================
set /p choice="请输入选项 [0-5]: "

if "%choice%"=="1" goto START_BROWSER
if "%choice%"=="2" goto START_WEBUI
if "%choice%"=="3" goto START_DAEMON
if "%choice%"=="4" goto RUN_PIPELINE
if "%choice%"=="5" goto SHOW_CONFIG
if "%choice%"=="0" goto EXIT
echo.
echo [提示] 无效输入，请重新选择。
echo.
goto MENU

:START_BROWSER
echo.
echo 正在启动独立求职 Chrome 浏览器...
"%PYTHON_CMD%" -m jobagent browser
goto MENU

:START_WEBUI
echo.
echo 正在启动本地 WebUI (浏览器访问: http://127.0.0.1:8765)...
echo 按 Ctrl+C 可停止 WebUI 服务。
echo.
"%PYTHON_CMD%" -m jobagent ui
goto MENU

:START_DAEMON
echo.
echo 正在启动 HR 消息与求简历请求后台守护进程...
echo 按 Ctrl+C 可停止守护进程。
echo.
"%PYTHON_CMD%" tools/boss_chat_daemon.py
goto MENU

:RUN_PIPELINE
echo.
echo 正在运行自动化岗位发现、抓取与匹配分析...
"%PYTHON_CMD%" -m jobagent run
echo [完成] 运行结束。
echo.
goto MENU

:SHOW_CONFIG
echo.
echo 当前本地环境与数据目录信息:
"%PYTHON_CMD%" -c "from jobagent.config import load, get_default_data_dir; cfg=load(); print('数据存储目录:', get_default_data_dir()); print('数据库路径:', cfg.paths.db_path); print('个人画像路径:', cfg.paths.profile_path); print('Master简历路径:', cfg.paths.master_resume_path)"
echo.
goto MENU

:EXIT
echo 感谢使用 AI Job Hunter。再见！
timeout /t 2 >nul
exit /b 0
