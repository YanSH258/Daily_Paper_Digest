# Daily Paper Digest 启动脚本（Windows PowerShell）
# 用法：在仓库目录下执行
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# 可选参数：
#   -DataRoot "$HOME\dpd-data"   数据根目录（配置/数据库/日志，默认 ~\dpd-data）
#   -Port 8080                   服务端口
param(
    [string]$DataRoot = "$HOME\dpd-data",
    [int]$Port = 8080,
    [string]$BindHost = "127.0.0.1"
)
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"

# 中文/emoji 日志编码保护（避免 Windows 控制台 GBK 编码报错）
$env:PYTHONUTF8 = "1"
$env:DPD_DATA_ROOT = $DataRoot

# 首次运行：自动创建 venv 并安装依赖
if (-not (Test-Path $Python)) {
    Write-Host "[初始化] 创建虚拟环境并安装依赖（约 2-3 分钟）..."
    python -m venv $VenvDir
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r (Join-Path $RepoRoot "requirement.txt")
}

$Config = Join-Path $DataRoot "config\config.yaml"
$MainPy = Join-Path $RepoRoot "src\main.py"
$WebPy = Join-Path $RepoRoot "src\web_server.py"

# 首次运行：初始化配置并打开记事本填写 key
if (-not (Test-Path $Config)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot "config") | Out-Null
    & $Python $MainPy --init-config
    Write-Host ""
    Write-Host "[配置] 已生成：$Config"
    Write-Host "[配置] 请在记事本中至少填写 llm.<provider>.api_key 和 model，"
    Write-Host "[配置] 保存关闭后服务会自动继续启动。"
    Start-Process notepad $Config -Wait
}

Write-Host ""
Write-Host "启动文献工作台 http://${BindHost}:${Port} （停止：Ctrl+C）"
& $Python $WebPy --config $Config --host $BindHost --port $Port
