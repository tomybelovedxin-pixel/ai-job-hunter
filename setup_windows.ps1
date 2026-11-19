# ========================================================
#   AI Job Hunter - Windows 一键环境安装与初始化脚本
# ========================================================

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  正在为您搭建 AI Job Hunter 专属求职智能体运行环境...  " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 检查 Python 环境与版本
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "❌ 未检测到 Python，请先安装 Python 3.10 或更高版本 (https://www.python.org/downloads/)，并勾选 Add to PATH。"
    exit 1
}

$pyVersion = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "✔ 检测到 Python 版本: $pyVersion" -ForegroundColor Green

# 2. 检查 Google Chrome
Write-Host "正在检测 Google Chrome 浏览器..."
$chromeFound = $false
$chromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
foreach ($path in $chromePaths) {
    if (Test-Path $path) {
        $chromeFound = $true
        Write-Host "✔ 检测到 Google Chrome: $path" -ForegroundColor Green
        break
    }
}
if (-not $chromeFound) {
    Write-Warning "⚠️ 未在系统标准路径检测到 Google Chrome。如果已安装在自定义目录，稍后可在 config.toml 中指定 [browser] exe 路径。"
}

# 3. 安装依赖包
Write-Host "`n正在安装项目依赖 (requirements.txt)..." -ForegroundColor Yellow
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "❌ 依赖安装失败，请检查网络或 Python 镜像源。"
    exit 1
}
Write-Host "✔ 依赖安装成功！" -ForegroundColor Green

# 4. 初始化用户独立数据空间
Write-Host "`n正在初始化当前用户独立的数据存储目录..." -ForegroundColor Yellow
python -m jobagent init

# 5. 执行环境自检测试
Write-Host "`n正在执行本地单元测试与安全规则自检..." -ForegroundColor Yellow
python -m unittest discover tests
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n========================================================" -ForegroundColor Green
    Write-Host "  🎉 全部安装与环境自检通过！系统已可以独立运行！" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "下一步极速上手：" -ForegroundColor Cyan
    Write-Host "  1. 双击运行 【run_windows.bat】 或执行 【python -m jobagent browser】 拉起专属 Chrome"
    Write-Host "  2. 在弹出的 Chrome 中登录你的 BOSS 直聘账号"
    Write-Host "  3. 启动管理面板：【python -m jobagent ui】"
    Write-Host "  4. 启动后台守护：【python tools/boss_chat_daemon.py】"
} else {
    Write-Warning "⚠️ 部分单元测试未通过，请根据上方日志排查。"
}
