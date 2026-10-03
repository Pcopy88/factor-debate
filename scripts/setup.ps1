$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 is required'"
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 first.' }
    python -m venv .venv
}
$requirementsPath = if (Test-Path -LiteralPath 'requirements.lock.txt') { 'requirements.lock.txt' } else { 'requirements.txt' }
& '.\.venv\Scripts\python.exe' -m pip install -r $requirementsPath
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
if (-not (Test-Path -LiteralPath '.tools\npm\package\bin\npm-cli.js')) {
    & '.\.venv\Scripts\python.exe' scripts/bootstrap_npm.py
    if ($LASTEXITCODE -ne 0) { throw 'npm bootstrap failed.' }
}
Get-Command node -ErrorAction Stop | Out-Null
Push-Location frontend
try {
    node ../.tools/npm/package/bin/npm-cli.js ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependencies failed.' }
    node ../.tools/npm/package/bin/npm-cli.js run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Host 'Setup complete. Run the start shortcut in the project folder.'
