# 启动专用于自动求职的 Google Chrome 调试实例
# 端口：9222，独立配置目录，绝不影响日常 Chrome 与 Codex 的 Edge

$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$userDataDir = "C:\Users\ASUS\Documents\ChatGPT\自动找工作\data\chrome-profile"
$port = 9222
$targetUrl = "https://www.zhipin.com/web/geek/chat"

if (-not (Test-Path $chromePath)) {
    Write-Error "未找到 Google Chrome，请确认路径: $chromePath"
    exit 1
}

if (-not (Test-Path $userDataDir)) {
    New-Item -ItemType Directory -Path $userDataDir -Force | Out-Null
}

Write-Host "正在启动 Google Chrome (专属求职调试模式, 端口 $port)..." -ForegroundColor Green
Start-Process $chromePath -ArgumentList @(
    "--remote-debugging-port=$port",
    "--user-data-dir=$userDataDir",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    $targetUrl
)
