# ========================================================
#   AI Job Hunter - Windows 一键环境安装与初始化脚本
# ========================================================

$ErrorActionPreference = "Stop"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  正在为您搭建 AI Job Hunter 专属求职智能体运行环境...  " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 检查 Python 环境与版本 (必须 >= 3.10)
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "❌ 未检测到 Python，请先安装 Python 3.10 或更高版本 (https://www.python.org/downloads/)，并务必勾选 'Add Python to PATH'。"
    exit 1
}

$pyVersionMajor = & $pythonCmd.Source -c "import sys; print(sys.version_info.major)"
$pyVersionMinor = & $pythonCmd.Source -c "import sys; print(sys.version_info.minor)"
$pyVersionStr = "$pyVersionMajor.$pyVersionMinor"

if ([int]$pyVersionMajor -lt 3 -or ([int]$pyVersionMajor -eq 3 -and [int]$pyVersionMinor -lt 10)) {
    Write-Error "❌ 检测到的 Python 版本过低 ($pyVersionStr)！AI Job Hunter 严格依赖 Python 3.10+，请前往官网下载更新。"
    exit 1
}
Write-Host "✔ 检测到符合要求的 Python 版本: $pyVersionStr" -ForegroundColor Green

# 2. 检查 Google Chrome 浏览器（根据安全规范仅使用 Chrome，严禁调用 Edge）
Write-Host "`n正在检测 Google Chrome 浏览器..."
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
    Write-Warning "⚠️ 未在常见标准路径检测到 Google Chrome。AI Job Hunter 严格使用 Google Chrome 独立实例（严禁使用 Edge）。若已安装在自定义路径，请在 config.toml 的 [browser] exe 段中指定。"
}

# 3. 创建专属虚拟环境 .venv（避免污染系统 Python 环境）
Write-Host "`n正在检查并创建专属项目虚拟环境 (.venv)..." -ForegroundColor Yellow
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $scriptDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "正在创建虚拟环境: $venvDir"
    & $pythonCmd.Source -m venv $venvDir
    if (-not (Test-Path $venvPython)) {
        Write-Error "❌ 创建虚拟环境失败，请确认系统 Python 安装完整且支持 venv 模块。"
        exit 1
    }
    Write-Host "✔ 专属虚拟环境创建成功！" -ForegroundColor Green
} else {
    Write-Host "✔ 检测到现有专属虚拟环境: $venvPython" -ForegroundColor Green
}

# 4. 在虚拟环境中安装依赖
Write-Host "`n正在使用虚拟环境安装项目依赖 (requirements.txt)..." -ForegroundColor Yellow
$reqFile = Join-Path $scriptDir "requirements.txt"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "❌ 依赖安装失败，请检查网络连接或更换国内 pip 镜像源。"
    exit 1
}
Write-Host "✔ 依赖安装完成！" -ForegroundColor Green

# 5. 初始化用户独立数据存储空间 (%LOCALAPPDATA%\AIJobHunter)
Write-Host "`n正在初始化当前用户独立数据存储目录..." -ForegroundColor Yellow
& $venvPython -m jobagent init
if ($LASTEXITCODE -ne 0) {
    Write-Error "❌ 初始化数据目录失败，请检查本地 AppData 目录写权限。"
    exit 1
}
Write-Host "✔ 本地独立数据目录准备就绪！" -ForegroundColor Green

# 6. 运行全量单元测试与安全规则自检（测试失败则阻断安装完成）
Write-Host "`n正在执行本地单元测试与安全规则自检..." -ForegroundColor Yellow
& $venvPython -m unittest discover (Join-Path $scriptDir "tests")
if ($LASTEXITCODE -ne 0) {
    Write-Error "`n❌ 自动化单元测试未完全通过，系统未达到投产安全标准！请根据上方日志排查失败项。"
    exit 1
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host "  🎉 全部安装与环境自检通过！系统已可以独立安全运行！" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "下一步极速上手：" -ForegroundColor Cyan
Write-Host "  1. 双击运行根目录下的 【run_windows.bat】 启动控制台"
Write-Host "  2. 在菜单中按 [1] 拉起专属 Chrome 并在弹出的浏览器中登录求职账号"
Write-Host "  3. 按 [2] 打开 WebUI 面板，或按 [3] 启动后台消息监听守护"
