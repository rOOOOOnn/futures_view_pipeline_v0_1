param(
    [switch]$Search
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$KeyPurpose = if ($Search) { "Guangzhou web-search" } else { "classification" }
$SecureKey = Read-Host "Enter TokenHub $KeyPurpose API Key (input is hidden)" -AsSecureString
$Credential = New-Object System.Management.Automation.PSCredential("tokenhub", $SecureKey)
$PlainKey = $Credential.GetNetworkCredential().Password.Trim()
if (-not $PlainKey.StartsWith("sk-")) {
    throw "Invalid API Key format: the value must start with sk-."
}

$SecretsDirectory = Join-Path $PSScriptRoot "secrets"
$KeyFileName = if ($Search) { "tokenhub_search_api_key.txt" } else { "tokenhub_api_key.txt" }
$KeyFile = Join-Path $SecretsDirectory $KeyFileName
New-Item -ItemType Directory -Path $SecretsDirectory -Force | Out-Null
Set-Content -LiteralPath $KeyFile -Value $PlainKey -Encoding UTF8 -NoNewline
$PlainKey = $null
Write-Host "TokenHub API Key saved to the local git-ignored file: $KeyFile"
