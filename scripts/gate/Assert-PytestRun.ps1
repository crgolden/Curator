param([Parameter(Mandatory)][string]$LogPath)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repo
$global:LASTEXITCODE = $null
python -m pytest --cov=src/curator --cov-report=xml:coverage.xml --cov-report=term -q *>&1 | Tee-Object -FilePath $LogPath
$pytestExit = $global:LASTEXITCODE
$summary = Select-String -LiteralPath $LogPath -Pattern '(\d+) passed' | Select-Object -Last 1
$skipped = Select-String -LiteralPath $LogPath -Pattern '(\d+) skipped' | Select-Object -Last 1
$passed = if ($summary) { [int]$summary.Matches[0].Groups[1].Value } else { 0 }
$skippedCount = if ($skipped) { [int]$skipped.Matches[0].Groups[1].Value } else { 0 }
Write-Host "exit $pytestExit, passed $passed, skipped $skippedCount"
if ($null -eq $pytestExit -or $pytestExit -ne 0 -or $passed -eq 0 -or $skippedCount -gt 0) { exit 1 }
exit 0
