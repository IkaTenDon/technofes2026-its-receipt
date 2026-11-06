param([switch]$TestMode, [string]$Config = 'config.toml')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first. See README.md.' }
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts\setup.ps1 first.' }
    & uv sync --locked --offline --check --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'The uv environment is not ready. Run scripts\setup.ps1 before reception.' }
    $appArgs = @('run', '--offline', '--no-sync', '--no-python-downloads', 'python', '-m', 'receipt_app', '--config', $Config)
    if ($TestMode) { $appArgs += '--test' }
    $appArgs += 'run'
    & uv @appArgs
    if ($LASTEXITCODE -ne 0) { throw 'The receipt app exited with an error. Read the message above.' }
}
finally { Pop-Location }
