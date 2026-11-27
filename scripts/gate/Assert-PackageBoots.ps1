param([Parameter(Mandatory)][string]$PackagePath)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repo
if (Test-Path -LiteralPath $PackagePath) { Remove-Item -LiteralPath $PackagePath -Recurse -Force }
New-Item -ItemType Directory -Force -Path $PackagePath | Out-Null
foreach ($item in 'src', 'db', 'pyproject.toml', 'poetry.lock', 'app.py') {
    Copy-Item -LiteralPath (Join-Path $repo $item) -Destination $PackagePath -Recurse
}
$workflow = [IO.File]::ReadAllText((Join-Path $repo '.github\workflows\main_crgolden-curator.yml'))
$exports = [regex]::Matches($workflow, "(?m)^[ \t]*export (\w+)='([^']*)'")
if ($exports.Count -eq 0) {
    Write-Host 'no export lines found in the workflow boot-check step'
    exit 1
}
$bootKey = python -c "from curator.persistence.crypto import TokenCrypto; print(TokenCrypto.generate_key().decode())"
$boot = [Diagnostics.ProcessStartInfo]::new((Get-Command python).Source)
$boot.ArgumentList.Add('-c')
$boot.ArgumentList.Add("import app; print('Package boots OK:', app.app)")
$boot.WorkingDirectory = $PackagePath
$boot.UseShellExecute = $false
$boot.Environment.Clear()
foreach ($name in 'SystemRoot', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA') {
    $boot.Environment[$name] = [Environment]::GetEnvironmentVariable($name)
}
foreach ($export in $exports) { $boot.Environment[$export.Groups[1].Value] = $export.Groups[2].Value }
$boot.Environment['CURATOR_TOKEN_KEY'] = $bootKey
$bootProcess = [Diagnostics.Process]::Start($boot)
$bootProcess.WaitForExit()
if ($bootProcess.ExitCode -ne 0) {
    Write-Host "exit $($bootProcess.ExitCode) with $($exports.Count) exported settings"
    exit 1
}
Write-Host "booted with $($exports.Count) exported settings"
exit 0
