$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "未找到 conda。请先安装 Miniconda/Anaconda，并在可使用 conda 的 PowerShell 中重试。"
}

$EnvironmentNames = (conda env list --json | ConvertFrom-Json).envs | ForEach-Object { Split-Path $_ -Leaf }
if ($EnvironmentNames -notcontains "futures-view-system") {
    conda create -n futures-view-system python=3.11 pip -y
    if ($LASTEXITCODE -ne 0) { throw "Conda 环境创建失败。" }
}

$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n futures-view-system python -m pip install -e .
conda run --no-capture-output -n futures-view-system python -m playwright install chromium

Write-Host ""
Write-Host "环境安装完成。首次使用元宝前请运行："
Write-Host "conda run --no-capture-output -n futures-view-system python -m futures_view.run --login-only"
