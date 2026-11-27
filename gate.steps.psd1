@{
    Schema      = 1
    Repo        = 'Curator'
    Workflow    = '.github/workflows/main_crgolden-curator.yml'
    Sonar       = @{ ProjectKey = 'crgolden_Curator' }
    Environment = @{ MYPYPATH = 'src'; PYTHONPATH = 'src' }
    Probe       = @{
        Kind  = 'pytest'
        Roots = @{ 'tests/' = 'pytest' }
        Wrap  = @{ 'tests/' = @('pwsh', '-NoProfile', '-File', 'scripts/gate/Use-TestDatabase.ps1') }
    }
    Steps       = @(
        @{ Name = 'Install Poetry'; Tool = 'poetry'; Inputs = @('*') }
        @{ Name = 'Verify poetry.lock matches pyproject.toml'; Tier = 'Fast'; Run = @('poetry', 'check', '--lock'); Needs = @('Install Poetry'); Inputs = @('pyproject.toml', 'poetry.lock') }
        @{ Name = 'Install dependencies'; Delivery = $true }
        @{ Name = 'Ruff lint'; Tier = 'Fast'; Run = @('python', '-m', 'ruff', 'check', 'src', 'tests', 'app.py', 'dev_server.py'); Inputs = @('*.py', 'pyproject.toml') }
        @{ Name = 'Ruff format check'; Tier = 'Fast'; Run = @('python', '-m', 'ruff', 'format', '--check', 'src', 'tests', 'app.py', 'dev_server.py'); Inputs = @('*.py', 'pyproject.toml') }
        @{ Name = 'Mypy'; Tier = 'Fast'; Run = @('python', '-m', 'mypy', 'src', 'tests', 'app.py', 'dev_server.py'); Inputs = @('*.py', 'pyproject.toml', 'poetry.lock') }
        @{
            Name   = 'Bearer-list control'
            Inputs = @('*')
            Run    = @('pwsh', '-NoProfile', '-File', 'scripts/gate/Assert-BearerListControl.ps1', '-LogPath', '{output}\bearer-control.txt')
        }
        @{
            Name   = 'Run tests with coverage'
            Inputs = @('*')
            Run    = @(
                'pwsh', '-NoProfile', '-File', 'scripts/gate/Use-TestDatabase.ps1',
                'pwsh', '-NoProfile', '-File', 'scripts/gate/Assert-PytestRun.ps1', '-LogPath', '{output}\pytest.txt'
            )
        }
        @{
            Name   = 'SonarCloud analysis'
            Sonar  = 'scan'
            Inputs = @('*')
            Run    = @(
                'sonar-scanner', '-Dsonar.projectKey={sonarProject}', '-Dsonar.organization=crgolden', '-Dsonar.host.url=https://sonarcloud.io',
                '-Dsonar.sources=src', '-Dsonar.tests=tests', '-Dsonar.python.coverage.reportPaths=coverage.xml', '-Dsonar.python.version=3.15',
                '-Dsonar.exclusions=**/__pycache__/**,**/*.pyc,.venv/**', '-Dsonar.qualitygate.wait=true', '-Dsonar.scanner.skipJreProvisioning=true',
                '-Dsonar.scm.use.blame.algorithm=GIT_NATIVE_BLAME', '-Dsonar.branch.name={sonarBranch}'
            )
        }
        @{ Name = 'Fail on open Sonar issues'; Sonar = 'issues'; Needs = @('SonarCloud analysis'); Inputs = @('*') }
        @{ Name = 'Assemble deployment package'; Delivery = $true }
        @{
            Name   = 'Verify deployment package boots'
            Inputs = @('*')
            Run    = @('pwsh', '-NoProfile', '-File', 'scripts/gate/Assert-PackageBoots.ps1', '-PackagePath', '{output}\publish')
        }
        @{ Name = 'Run database migrations against the test database'; Delivery = $true }
        @{ Name = 'Run database migrations against production'; Delivery = $true }
        @{ Name = 'Verify secrets at rest are readable'; Delivery = $true }
        @{ Name = 'Set the App Service Python runtime'; Delivery = $true }
    )
}
