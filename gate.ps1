param([string]$Goal)

$ErrorActionPreference = 'Continue'
$gateCommon = Join-Path $PSScriptRoot '..\Tools\Gates\GateCommon.ps1'
if (-not (Test-Path -LiteralPath $gateCommon)) {
    Write-Host "GATE: FAILED (the Tools repository must be cloned beside this one: $gateCommon)"
    exit 1
}
. $gateCommon
$gateOutput = Join-Path ([IO.Path]::GetTempPath()) "crgolden-gates\$(Split-Path -Leaf $PSScriptRoot)"
New-Item -ItemType Directory -Force -Path $gateOutput | Out-Null

Register-GateSteps @('Schema test database configuration', 'poetry check --lock', 'ruff check', 'ruff format --check', 'mypy',
    'Bearer-list control', 'Run tests with coverage', 'SonarCloud analysis')
$repo = $PSScriptRoot
$pytestLog = Join-Path $gateOutput 'pytest.txt'
$sonarBranch = "branch-local-$($env:COMPUTERNAME.ToLowerInvariant())"
$pytestStep = 'Run tests with coverage (pytest -x --cov)'
$sonarStep = "SonarCloud analysis (sonar-scanner, branch $sonarBranch, quality gate waited)"
$env:TZ = 'UTC'
if ($env:TZ -ne 'UTC') { Write-Host 'GATE: FAILED (TZ pin)'; exit 1 }
Set-Location $repo
Initialize-GateState 'Curator' $repo
Invoke-CatalogSteps

$databaseLine = Select-String -Path (Join-Path $repo '.env') -Pattern '^CURATOR_TEST_DATABASE_URL=' -Raw
if (-not $databaseLine) { Stop-Gate 'Schema test database configuration' 'CURATOR_TEST_DATABASE_URL is not in Curator/.env' }
$env:CURATOR_TEST_DATABASE_URL = ($databaseLine -split '=', 2)[1].Trim()
Write-Row 'Schema test database configuration' 'PASS' 'CURATOR_TEST_DATABASE_URL loaded from Curator/.env'
$env:MYPYPATH = 'src'
$env:PYTHONPATH = 'src'

if (-not (Test-StepCarried 'poetry check --lock')) {
    $global:LASTEXITCODE = $null
    poetry check --lock
    $null = Test-Exit 'poetry check --lock'
}
if (-not (Test-StepCarried 'ruff check')) {
    $global:LASTEXITCODE = $null
    python -m ruff check src tests app.py dev_server.py
    $null = Test-Exit 'ruff check'
}
if (-not (Test-StepCarried 'ruff format --check')) {
    $global:LASTEXITCODE = $null
    python -m ruff format --check src tests app.py dev_server.py
    $null = Test-Exit 'ruff format --check'
}
if (-not (Test-StepCarried 'mypy')) {
    $global:LASTEXITCODE = $null
    python -m mypy src tests app.py dev_server.py
    $null = Test-Exit 'mypy'
}

$GateDelta = @('plant:tests/test_authz.py')
$bearerControl = 'Bearer-list control (must FAIL naming GET /consoles without its handler)'
if (-not (Test-StepCarried $bearerControl)) {
    $authzPath = Join-Path $repo 'tests\test_authz.py'
    $authzOriginal = [IO.File]::ReadAllText($authzPath)
    $entryPattern = '(?m)^[ \t]*consoles_routes\.list_consoles,\r?\n'
    if ($authzOriginal -notmatch $entryPattern) { Stop-Gate $bearerControl 'the list_consoles entry is absent from _BEARER_REQUIRED_HANDLERS' }
    $controlLog = ''
    try {
        [IO.File]::WriteAllText($authzPath, ($authzOriginal -replace $entryPattern, ''))
        $controlLog = (python -m pytest tests/test_authz.py -q -k listed_in_bearer_required_handlers 2>&1 | Out-String)
    }
    finally {
        [IO.File]::WriteAllText($authzPath, $authzOriginal)
    }
    if ([IO.File]::ReadAllText($authzPath) -ne $authzOriginal) { Write-Host 'GATE: FAILED (the control could not restore test_authz.py)'; exit 1 }
    if ($controlLog -notmatch 'GET /consoles') { Stop-Gate $bearerControl 'the completeness test did not fail naming GET /consoles' }
    Write-Row $bearerControl 'PASS' 'the completeness test failed naming GET /consoles'
}

if (-not (Test-StepCarried $pytestStep)) {
    $global:LASTEXITCODE = $null
    python -m pytest -x --cov=src/curator --cov-report=xml:coverage.xml --cov-report=term -q *>&1 | Tee-Object -FilePath $pytestLog
    $pytestExit = $global:LASTEXITCODE
    $summary = Select-String -Path $pytestLog -Pattern '(\d+) passed' | Select-Object -Last 1
    $skipped = Select-String -Path $pytestLog -Pattern '(\d+) skipped' | Select-Object -Last 1
    $passed = if ($summary) { [int]$summary.Matches[0].Groups[1].Value } else { 0 }
    $detail = "exit $pytestExit, passed $passed, skipped $(if ($skipped) { $skipped.Matches[0].Groups[1].Value } else { 0 })"
    if ($pytestExit -ne 0 -or $passed -eq 0 -or $skipped) { Stop-Gate $pytestStep $detail }
    Write-Row $pytestStep 'PASS' $detail
}

if (-not (Test-StepCarried $sonarStep)) {
    $env:JAVA_HOME = "$env:SystemDrive\sonar-scanner-8.0.1.6346-windows-x64\jre"
    $global:LASTEXITCODE = $null
    sonar-scanner -D"sonar.projectKey=crgolden_Curator" -D"sonar.organization=crgolden" -D"sonar.host.url=https://sonarcloud.io" -D"sonar.sources=src" -D"sonar.tests=tests" -D"sonar.python.coverage.reportPaths=coverage.xml" -D"sonar.python.version=3.10,3.11,3.12,3.13,3.14" -D"sonar.exclusions=**/__pycache__/**,**/*.pyc,.venv/**" -D"sonar.qualitygate.wait=true" -D"sonar.scanner.skipJreProvisioning=true" -D"sonar.branch.name=$sonarBranch"
    $null = Test-Exit $sonarStep
}

Write-Row 'Package / Migrate / Deploy' 'NOT RUN' 'delivery jobs, not checks'
Complete-Gate
