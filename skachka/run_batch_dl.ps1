# run_batch_dl.ps1 — запуск batch_dl.py через .venv с автоустановкой зависимостей.
# Запуск:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\run_batch_dl.ps1 --check
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\run_batch_dl.ps1
# Все аргументы передаются в batch_dl.py без изменений.
$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    chcp 65001 | Out-Null
} catch { }

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$BatchDl = Join-Path $ScriptDir 'batch_dl.py'
$SetupDeps = Join-Path $ScriptDir 'setup_deps.ps1'
$ForwardArgs = @($args)

function Test-BatchDlReady {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    try {
        $probe = "import requests, anime_dl_core, yt_dlp; " +
                 "rv=tuple(map(int, requests.__version__.split('.')[:2])); " +
                 "av=tuple(map(int, anime_dl_core.__version__.split('.')[:3])); " +
                 "raise SystemExit(0 if rv >= (2,28) and av >= (0,5,1) else 1)"
        & $VenvPython -c $probe 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $BatchDl)) {
    Write-Host "[ERROR] Не найден batch_dl.py: $BatchDl" -ForegroundColor Red
    exit 2
}

if (-not (Test-BatchDlReady)) {
    if (-not (Test-Path -LiteralPath $SetupDeps)) {
        Write-Host "[ERROR] Не найден установщик: $SetupDeps" -ForegroundColor Red
        exit 2
    }
    Write-Host "==> Зависимости не готовы, запускаю setup_deps.ps1" -ForegroundColor Cyan
    & $SetupDeps
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Установка зависимостей завершилась с кодом $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    if (-not (Test-BatchDlReady)) {
        Write-Host "[ERROR] После установки .venv всё ещё не видит requests/anime_dl_core/yt_dlp" -ForegroundColor Red
        exit 2
    }
}

& $VenvPython $BatchDl @ForwardArgs
exit $LASTEXITCODE
