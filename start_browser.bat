@echo off
chcp 65001 >nul
echo ========================================================
echo   AI Job Hunter - 自动化求职 Chrome 一键启动器
echo ========================================================
echo.

set "PORT=9222"
set "USER_DIR=%~dp0data\chrome-profile"

:: 探测常见 Chrome 安装路径
set "CHROME_EXE="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
)

if "%CHROME_EXE%"=="" (
    echo [错误] 未在默认路径找到 Google Chrome 浏览器！
    echo 请确认已安装 Chrome，或在 config.toml 中手动指定 exe 路径。
    pause
    exit /b 1
)

echo [1/2] 正在启动 Chrome 专属求职会话 (端口: %PORT%)...
echo 用户数据目录: %USER_DIR%
echo.

if not exist "%USER_DIR%" (
    mkdir "%USER_DIR%"
)

start "" "%CHROME_EXE%" --remote-debugging-port=%PORT% --user-data-dir="%USER_DIR%" --remote-allow-origins=* --no-first-run --no-default-browser-check "https://www.zhipin.com/web/geek/chat"

echo [2/2] 启动指令已发出！
echo 请在弹出的 Chrome 浏览器中登录你的求职账号。
echo 保持该浏览器窗口打开，然后即可运行自动化求职命令！
echo ========================================================
timeout /t 5 >nul
