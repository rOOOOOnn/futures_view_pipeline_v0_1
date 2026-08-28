$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "Conda was not found. Install Miniconda/Anaconda and retry in a Conda-enabled PowerShell."
}

$EnvironmentNames = (conda env list --json | ConvertFrom-Json).envs | ForEach-Object { Split-Path $_ -Leaf }
if ($EnvironmentNames -notcontains "futures-view-system") {
    conda create -n futures-view-system python=3.11 pip -y
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the Conda environment." }
}

$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n futures-view-system python -m pip install -e .
conda run --no-capture-output -n futures-view-system python -m playwright install chromium

Write-Host ""
Write-Host "Environment setup completed. Configure the TokenHub API Key, then run:"
Write-Host ".\run_all.ps1"
