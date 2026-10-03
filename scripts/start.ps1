param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup first.' }
$online = $false
try { $online = (Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2).status -eq 'ok' } catch {}
if (-not $online) {
    New-Item -ItemType Directory -Path runtime -Force | Out-Null
    $process = Start-Process -FilePath $pythonPath -ArgumentList '-m','research.launcher' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput 'runtime\server.log' -RedirectStandardError 'runtime\server-error.log'
    Set-Content -LiteralPath 'runtime\server.pid' -Value $process.Id
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 1000
        try { $online = (Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2).status -eq 'ok' } catch {}
        if ($online) { break }
        if ($process.HasExited) { throw 'Startup failed. Read runtime/server-error.log.' }
    }
}
if (-not $online) { throw 'Startup is still pending. Read runtime/server-error.log.' }
$health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
if (-not $health.worker_running) {
    Start-Process -FilePath $pythonPath -ArgumentList '-m','research.worker' -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
}
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
