param(
    [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first. See README.md.' }
    $syncArgs = @('sync', '--locked')
    if ($PythonExe) { $syncArgs += @('--python', $PythonExe) }
    & uv @syncArgs
    if ($LASTEXITCODE -ne 0) { throw 'uv sync failed. Python 3.12 is required. See README.md.' }
    & uv run --offline --no-sync --no-python-downloads python -B -c "import sys, tkinter; assert sys.version_info[:2] == (3,12), 'Python 3.12 required'; root=tkinter.Tk(); root.withdraw(); root.update(); root.destroy(); print(sys.version)"
    if ($LASTEXITCODE -ne 0) { throw 'Tkinter could not start. Use Python 3.12 with Tcl/Tk, or pass -PythonExe. See README.md.' }
    Write-Host 'Setup complete. Printer dependencies are included. Run scripts\start.ps1.'
}
finally { Pop-Location }
