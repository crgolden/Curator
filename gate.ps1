param([string]$Goal, [string]$Only)

if (-not $env:GATE_LIBRARY -or -not (Test-Path -LiteralPath $env:GATE_LIBRARY -PathType Leaf)) {
    Write-Host 'GATE: FAILED (GATE_LIBRARY names no gate library script; README.md, section Gate, says how to plug one in or run the steps without one)'
    exit 1
}
& (Join-Path (Split-Path -Parent $env:GATE_LIBRARY) 'Invoke-RepoGate.ps1') -RepoPath $PSScriptRoot -Only $Only
exit $LASTEXITCODE
