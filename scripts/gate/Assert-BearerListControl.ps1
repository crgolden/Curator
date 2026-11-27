param([Parameter(Mandatory)][string]$LogPath)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repo
$authzPath = Join-Path $repo 'tests\test_authz.py'
$authzOriginal = [IO.File]::ReadAllText($authzPath)
$entryPattern = '(?m)^[ \t]*consoles_routes\.list_consoles,\r?\n'
if ($authzOriginal -notmatch $entryPattern) {
    Write-Host 'the list_consoles entry is absent from _BEARER_REQUIRED_HANDLERS'
    exit 1
}
$controlLog = ''
try {
    [IO.File]::WriteAllText($authzPath, ($authzOriginal -replace $entryPattern, ''))
    $controlLog = (python -m pytest tests/test_authz.py -q -k listed_in_bearer_required_handlers 2>&1 | Out-String)
}
finally {
    [IO.File]::WriteAllText($authzPath, $authzOriginal)
}
if ([IO.File]::ReadAllText($authzPath) -ne $authzOriginal) {
    Write-Host 'the control could not restore tests/test_authz.py'
    exit 1
}
[IO.File]::WriteAllText($LogPath, $controlLog)
if ($controlLog -notmatch 'GET /consoles') {
    Write-Host "the completeness test did not fail naming GET /consoles; its output is in $LogPath"
    exit 1
}
Write-Host 'the completeness test failed naming GET /consoles'
exit 0
