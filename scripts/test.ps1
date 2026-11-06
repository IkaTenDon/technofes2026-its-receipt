$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
try {
    $tempPath = Join-Path $projectRoot '.setup-tmp'
    New-Item -ItemType Directory -Force -Path $tempPath | Out-Null
    $env:TEMP = $tempPath
    $env:TMP = $tempPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first. See README.md.' }
    & uv sync --locked --offline --check --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'The uv environment is not ready. Run scripts\setup.ps1 first.' }
    & uv run --offline --no-sync --no-python-downloads python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
}
finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    Pop-Location
}
