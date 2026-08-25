param(
    [string]$Date = "",
    [switch]$Headed,
    [switch]$Debug
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"
$Arguments = @("run", "--no-capture-output", "-n", "futures-view-system", "python", "-m", "futures_view.run")
if ($Date) { $Arguments += @("--date", $Date) }
if ($Headed) { $Arguments += "--headed" }
if ($Debug) { $Arguments += "--debug" }
conda @Arguments
