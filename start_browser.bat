@echo off
chcp 65001 >nul
echo ========================================================
echo   AI Job Hunter - 自动化求职 Chrome 独立启动器
echo ========================================================
echo.

:: 优先调用 Python 核心引擎的受托管启动逻辑（自动分配安全端口与独立数据目录）
where python >nul 2>&1
if %errorlevel% equ 0 (
    echo [1/2] 正在调用 Python 智能探针检测环境并拉起独立 Chrome...
    python -m jobagent browser
    if %errorlevel% equ 0 (
        timeout /t 5 >nul
        exit /b 0
    )
)

echo [提示] 未检测到 python 命令或执行异常，回退到原生批处理启动逻辑...
set "PORT=9222"
if not defined LOCALAPPDATA (
    set "USER_DIR=%~dp0data\chrome-profile"
) else (
    set "USER_DIR=%LOCALAPPDATA%\AIJobHunter\chrome-profile"
)

:: 探测常见 Chrome 安装路径
set "CHROME_EXE="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
)

if "%CHROME_EXE%"=="" (
    echo [错误] 未在系统常见路径找到 Google Chrome 浏览器！
    echo 请确认已安装 Chrome，或在 config.toml 的 [browser] 段中指定 exe 路径。
    pause
    exit /b 1
)

echo [1/2] 正在启动独立 Chrome 专属求职会话 (端口: %PORT%)...
echo 用户数据目录: %USER_DIR%
echo.

if not exist "%USER_DIR%" (
    mkdir "%USER_DIR%"
)

start "" "%CHROME_EXE%" --remote-debugging-port=%PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=http://127.0.0.1:%PORT%,http://localhost:%PORT% --user-data-dir="%USER_DIR%" --no-first-run --no-default-browser-check "https://www.zhipin.com/web/geek/chat"

echo [2/2] 启动指令已发出！
echo 请在弹出的 Chrome 浏览器中登录你的 BOSS 直聘账号。
echo 保持该浏览器窗口打开，随后即可运行自动化求职与会话监听！
echo ========================================================
timeout /t 5 >nul
