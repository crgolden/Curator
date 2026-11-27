[string[]]$Command = @($args)
if (-not $Command.Count) {
    Write-Host 'Use-TestDatabase.ps1 was given no command to run'
    exit 1
}
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([string]::IsNullOrWhiteSpace($env:CURATOR_TEST_DATABASE_URL)) {
    $envFile = Join-Path $repo '.env'
    $databaseLine = if (Test-Path -LiteralPath $envFile) { Select-String -LiteralPath $envFile -Pattern '^CURATOR_TEST_DATABASE_URL=' -Raw }
    if (-not $databaseLine) {
        Write-Host 'CURATOR_TEST_DATABASE_URL is neither in the environment nor in Curator/.env'
        exit 1
    }
    $env:CURATOR_TEST_DATABASE_URL = ($databaseLine -split '=', 2)[1].Trim()
}
Set-Location -LiteralPath $repo
$global:LASTEXITCODE = $null
& $Command[0] @($Command | Select-Object -Skip 1)
if ($null -eq $global:LASTEXITCODE) { exit 1 }
exit $global:LASTEXITCODE
