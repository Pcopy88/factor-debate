$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = [IO.Path]::GetFullPath((Join-Path $projectRoot '.venv\Scripts\python.exe'))
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -eq $pythonPath -and
    $_.CommandLine -match '\-m\s+research\.(launcher|worker)(\s|$)'
}
foreach ($process in $targets) { Stop-Process -Id $process.ProcessId -Force }
Write-Host 'Local processes stopped. Committed checkpoints will resume on the next start.'
