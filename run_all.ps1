param(
    [string]$Date = "",
    [ValidateSet("browser", "api")]
    [string]$LlmBackend = "api",
    [switch]$Headed,
    [switch]$Debug
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"
if ($LlmBackend -eq "api" -and -not $env:TENCENT_TOKENHUB_API_KEY) {
    $KeyFile = Join-Path $PSScriptRoot "secrets\tokenhub_api_key.txt"
    if (-not (Test-Path -LiteralPath $KeyFile)) {
        & (Join-Path $PSScriptRoot "save_api_key.ps1")
    }
    $env:TENCENT_TOKENHUB_API_KEY = (Get-Content -Raw -Encoding UTF8 -LiteralPath $KeyFile).Trim()
}
$Arguments = @("run", "--no-capture-output", "-n", "futures-view-system", "python", "-m", "futures_view.run")
$Arguments += @("--llm-backend", $LlmBackend)
if ($Date) { $Arguments += @("--date", $Date) }
if ($Headed) { $Arguments += "--headed" }
if ($Debug) { $Arguments += "--debug" }
conda @Arguments
if ($LlmBackend -eq "api") {
    Remove-Item Env:TENCENT_TOKENHUB_API_KEY -ErrorAction SilentlyContinue
}
