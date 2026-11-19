# setup_deps.ps1 — установка всего, что нужно batch_dl.py, без ручных действий.
# Запуск:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\setup_deps.ps1
# Проверка без изменений:
#   ... -File .\skachka\setup_deps.ps1 -CheckOnly
param(
    [switch]$CheckOnly,
    [switch]$Force,
    [switch]$SkipFfmpeg,
    [switch]$SkipVcRedist,
    [switch]$SkipPythonPackages,
    [switch]$NoElevate,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    chcp 65001 | Out-Null
} catch { }

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvDir = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $ScriptDir 'requirements-batch_dl.txt'
$LogDir = Join-Path $ScriptDir 'logs'
$LogFile = Join-Path $LogDir 'setup_deps.log'
$script:TranscriptStarted = $false

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
try {
    Start-Transcript -Path $LogFile -Append | Out-Null
    $script:TranscriptStarted = $true
} catch { }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) {
    Write-Host "  [OK] $Message" -ForegroundColor Green
}
function Write-Warn([string]$Message) {
    Write-Host "  [WARN] $Message" -ForegroundColor Yellow
}
function Write-Err([string]$Message) {
    Write-Host "  [ERROR] $Message" -ForegroundColor Red
}
function Test-Admin {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}
function Test-Executable([string]$Path, [string[]]$Arguments = @('-version')) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        & $Path @Arguments 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}
function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Команда завершилась с кодом ${code}: $FilePath $($Arguments -join ' ')"
    }
}
function Download-File([string]$Url, [string]$OutFile, [int]$Attempts = 4) {
    $dir = Split-Path -Parent $OutFile
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $params = @{ Uri = $Url; OutFile = $OutFile; TimeoutSec = 180 }
            if ($PSVersionTable.PSVersion.Major -lt 6) { $params['UseBasicParsing'] = $true }
            Invoke-WebRequest @params
            Unblock-File -LiteralPath $OutFile -ErrorAction SilentlyContinue
            return
        } catch {
            if ($i -eq $Attempts) { throw }
            Write-Warn "Скачивание не удалось ($i/$Attempts): $($_.Exception.Message); повтор…"
            Start-Sleep -Seconds (2 * $i)
        }
    }
}

Write-Host "=== batch_dl: установка зависимостей ===" -ForegroundColor White
Write-Host "Проект : $ProjectRoot"
Write-Host "Скрипт : $ScriptDir"
Write-Host "Режим  : $(if ($CheckOnly) { 'только проверка' } else { 'установка' })"

function Get-PythonLauncher {
    $candidates = New-Object System.Collections.ArrayList
    if ($env:BATCH_DL_PYTHON) {
        [void]$candidates.Add([pscustomobject]@{ File = $env:BATCH_DL_PYTHON; Args = @() })
    }
    if (Test-Path -LiteralPath $VenvPython) {
        [void]$candidates.Add([pscustomobject]@{ File = $VenvPython; Args = @() })
    }
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) { [void]$candidates.Add([pscustomobject]@{ File = $pyCmd.Source; Args = @('-3') }) }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { [void]$candidates.Add([pscustomobject]@{ File = $pythonCmd.Source; Args = @() }) }
    $pythonCmd3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($pythonCmd3) { [void]$candidates.Add([pscustomobject]@{ File = $pythonCmd3.Source; Args = @() }) }
    if ($env:LOCALAPPDATA) {
        foreach ($version in @('Python313', 'Python312', 'Python311', 'Python310')) {
            $p = Join-Path $env:LOCALAPPDATA (Join-Path 'Programs\Python' (Join-Path $version 'python.exe'))
            if (Test-Path -LiteralPath $p) { [void]$candidates.Add([pscustomobject]@{ File = $p; Args = @() }) }
        }
    }

    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate.File @($candidate.Args + @('-c', 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2])')) 2>$null
            $code = $LASTEXITCODE
            if ($code -eq 0 -and $version) {
                $parts = @($version.Trim() -split '\s+')
                $major = [int]$parts[0]; $minor = [int]$parts[1]
                if ($major -ge 3 -and $minor -ge 9) {
                    $micro = 0
                    if ($parts.Count -ge 3) { $micro = [int]$parts[2] }
                    return [pscustomobject]@{
                        File = $candidate.File
                        Args = $candidate.Args
                        Version = "$major.$minor.$micro"
                    }
                }
            }
        } catch { }
    }
    return $null
}

function Install-Python {
    Write-Step "Python не найден — устанавливаю Python 3.12"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            & $winget.Source install --id Python.Python.3.12 -e --source winget `
                --accept-source-agreements --accept-package-agreements --silent --disable-interactivity
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Python установлен через winget"
                return
            }
        } catch {
            Write-Warn "winget не смог установить Python: $($_.Exception.Message)"
        }
        try {
            & $winget.Source install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
        } catch { }
    }
    $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    Download-File 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' $installer
    $installArgs = @('/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1',
                     'Include_test=0', 'Include_pip=1', 'Include_doc=0')
    $proc = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "Установщик Python завершился с кодом $($proc.ExitCode)"
    }
    Write-Ok "Python установлен"
}

function Get-VenvPython {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $null }
    if (Test-Executable $VenvPython @('-c', 'import sys')) { return $VenvPython }
    return $null
}

function New-ProjectVenv([pscustomobject]$Python) {
    if ((Test-Path -LiteralPath $VenvDir) -and $Force) {
        Write-Warn "Удаляю старый .venv (-Force)"
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
    Write-Step "Создаю виртуальное окружение .venv"
    $venvArgs = @()
    $venvArgs += $Python.Args
    $venvArgs += @('-m', 'venv', $VenvDir)
    & $Python.File @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Не удалось создать .venv. Проверьте, что установлен модуль venv/ensurepip."
    }
    Write-Ok ".venv создан: $VenvDir"
    return $VenvPython
}

function Test-PythonPackages([string]$PythonPath) {
    if (-not $PythonPath) { return $false }
    try {
        $probe = "import requests, anime_dl_core, yt_dlp; " +
                 "rv=tuple(map(int, requests.__version__.split('.')[:2])); " +
                 "av=tuple(map(int, anime_dl_core.__version__.split('.')[:3])); " +
                 "raise SystemExit(0 if rv >= (2,28) and av >= (0,5,1) else 1)"
        & $PythonPath -c $probe 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Install-PythonPackages([string]$PythonPath) {
    Write-Step "Устанавливаю Python-зависимости"
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                 '--prefer-binary', '--timeout', '60', '--retries', '5')
    if (Test-Path -LiteralPath $Requirements) {
        $pipArgs += @('-r', $Requirements)
    } else {
        $pipArgs += @('requests>=2.31,<3', 'anime-dl-core==0.5.1', 'yt-dlp>=2024.1.1')
    }
    & $PythonPath @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "pip не смог установить с --prefer-binary, повторяю без него"
        $pipArgs = $pipArgs | Where-Object { $_ -ne '--prefer-binary' }
        & $PythonPath @pipArgs
        if ($LASTEXITCODE -ne 0) {
            throw "pip завершился с кодом $LASTEXITCODE"
        }
    }
    if (-not (Test-PythonPackages $PythonPath)) {
        throw "После pip импорт requests/anime_dl_core/yt_dlp всё ещё не работает"
    }
    Write-Ok "Python-зависимости установлены"
}

function Get-FfmpegPath {
    $candidates = New-Object System.Collections.ArrayList
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { [void]$candidates.Add($cmd.Source) }
    if ($env:LOCALAPPDATA) {
        [void]$candidates.Add((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe'))
        [void]$candidates.Add((Join-Path $env:LOCALAPPDATA 'ffmpeg\bin\ffmpeg.exe'))
        [void]$candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\ffmpeg\bin\ffmpeg.exe'))
        $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        if (Test-Path -LiteralPath $packages) {
            Get-ChildItem -LiteralPath $packages -Filter 'Gyan.FFmpeg*' -Directory -ErrorAction SilentlyContinue |
                ForEach-Object {
                    Get-ChildItem -LiteralPath $_.FullName -Recurse -Filter 'ffmpeg.exe' -File -ErrorAction SilentlyContinue |
                        ForEach-Object { [void]$candidates.Add($_.FullName) }
                }
        }
    }
    [void]$candidates.Add('C:\ffmpeg\bin\ffmpeg.exe')
    if ($env:ProgramFiles) {
        [void]$candidates.Add((Join-Path $env:ProgramFiles 'ffmpeg\bin\ffmpeg.exe'))
    }
    [void]$candidates.Add((Join-Path $ProjectRoot 'tools\ffmpeg\bin\ffmpeg.exe'))
    [void]$candidates.Add((Join-Path $ScriptDir 'tools\ffmpeg\bin\ffmpeg.exe'))
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Executable $candidate @('-version'))) {
            return $candidate
        }
    }
    return $null
}

function Add-ToUserPath([string]$Directory) {
    if (-not $Directory) { return }
    try {
        $current = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not $current) { $current = '' }
        $parts = @($current -split ';' | Where-Object { $_ -and $_.Trim() })
        if ($parts -notcontains $Directory) {
            $newPath = (($parts + $Directory) -join ';')
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Write-Ok "Добавлено в PATH пользователя: $Directory"
        }
    } catch {
        Write-Warn "Не удалось обновить PATH пользователя: $($_.Exception.Message)"
    }
    $envParts = @($env:Path -split ';' | Where-Object { $_ -and $_.Trim() })
    if ($envParts -notcontains $Directory) {
        $env:Path = "$env:Path;$Directory"
    }
}

function Install-Ffmpeg {
    Write-Step "ffmpeg не найден — устанавливаю"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "  пробую winget (Gyan.FFmpeg)…"
        try {
            & $winget.Source install --id Gyan.FFmpeg -e --source winget `
                --accept-source-agreements --accept-package-agreements --silent --disable-interactivity
            if ($LASTEXITCODE -ne 0) {
                & $winget.Source install --id Gyan.FFmpeg -e --source winget `
                    --accept-source-agreements --accept-package-agreements --silent
            }
        } catch {
            Write-Warn "winget: $($_.Exception.Message)"
        }
        $found = Get-FfmpegPath
        if ($found) { return $found }
    }
    $choco = Get-Command choco -ErrorAction SilentlyContinue
    if ($choco) {
        Write-Host "  пробую Chocolatey…"
        try {
            & $choco.Source install ffmpeg -y --no-progress
            $found = Get-FfmpegPath
            if ($found) { return $found }
        } catch {
            Write-Warn "choco: $($_.Exception.Message)"
        }
    }
    Write-Host "  скачиваю официальную сборку gyan.dev…"
    $zip = Join-Path $env:TEMP 'ffmpeg-release-essentials.zip'
    Download-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zip
    $dest = Join-Path $env:LOCALAPPDATA 'ffmpeg'
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
    $foundExe = Get-ChildItem -LiteralPath $dest -Recurse -Filter 'ffmpeg.exe' -File -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.Directory.FullName 'ffprobe.exe') } |
        Select-Object -First 1
    if (-not $foundExe) { throw "В архиве ffmpeg не найден ffmpeg.exe" }
    Add-ToUserPath $foundExe.Directory.FullName
    return $foundExe.FullName
}

function Test-VcRedist {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )
    foreach ($key in $keys) {
        if (Test-Path -LiteralPath $key) {
            try {
                $item = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
                if ($item.Installed -eq 1) { return $true }
            } catch { }
        }
    }
    return $false
}

function Install-VcRedist {
    Write-Step "Visual C++ Redistributable x64"
    if (Test-VcRedist) {
        Write-Ok "уже установлен"
        return
    }
    $exe = Join-Path $env:TEMP 'vc_redist.x64.exe'
    Download-File 'https://aka.ms/vs/17/release/vc_redist.x64.exe' $exe
    $isAdmin = Test-Admin
    $arguments = @('/install', '/quiet', '/norestart')
    try {
        if ($isAdmin) {
            $proc = Start-Process -FilePath $exe -ArgumentList $arguments -Wait -PassThru
        } elseif ($NoElevate -or $NonInteractive) {
            Write-Warn "нет прав администратора; установите $exe вручную или запустите скрипт от администратора"
            return
        } else {
            Write-Host "  требуется UAC-подтверждение…"
            $proc = Start-Process -FilePath $exe -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        }
        if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010) {
            if ($proc.ExitCode -eq 3010) { Write-Warn "требуется перезагрузка Windows" }
            Write-Ok "Visual C++ Redistributable установлен"
        } else {
            Write-Warn "установщик VC++ вернул код $($proc.ExitCode)"
        }
    } catch {
        Write-Warn "не удалось установить VC++: $($_.Exception.Message)"
    }
}

function Enable-LongPaths {
    Write-Step "Длинные пути Windows"
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    try {
        $value = Get-ItemProperty -LiteralPath $key -Name 'LongPathsEnabled' -ErrorAction SilentlyContinue
        if ($value -and $value.LongPathsEnabled -eq 1) {
            Write-Ok "LongPathsEnabled=1"
            return
        }
        if (Test-Admin) {
            Set-ItemProperty -LiteralPath $key -Name 'LongPathsEnabled' -Value 1 -Type DWord
            Write-Ok "LongPathsEnabled=1 (применится к новым процессам)"
        } else {
            Write-Warn "нет прав администратора; включите LongPathsEnabled вручную"
        }
    } catch {
        Write-Warn "не удалось проверить/включить длинные пути: $($_.Exception.Message)"
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git -and (Test-Admin)) {
        try { & $git.Source config --system core.longpaths true } catch { }
    }
}

function Stop-TranscriptSafe {
    if ($script:TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:TranscriptStarted = $false
    }
}

$exitCode = 0
try {
    $python = Get-PythonLauncher
    if (-not $python) {
        if ($CheckOnly) {
            Write-Err "Python 3.9+ не найден"
            Write-Host "Установите: winget install Python.Python.3.12"
            $exitCode = 1
            Stop-TranscriptSafe
            exit $exitCode
        }
        Install-Python
        $python = Get-PythonLauncher
    }
    if (-not $python) { throw "Python 3.9+ не найден после установки" }
    Write-Ok "Python $($python.Version): $($python.File)"

    $venvPython = Get-VenvPython
    if (-not $venvPython -and -not $SkipPythonPackages) {
        if ($CheckOnly) {
            Write-Warn ".venv не найден"
        } else {
            $venvPython = New-ProjectVenv $python
        }
    } elseif ($venvPython) {
        Write-Ok ".venv: $venvPython"
    }

    if (-not $SkipPythonPackages) {
        if (-not $venvPython) {
            Write-Err "Не удалось создать/найти .venv"
            $exitCode = 1
        } elseif ((Test-PythonPackages $venvPython) -and -not $Force) {
            Write-Ok "Python-пакеты уже установлены"
        } elseif ($CheckOnly) {
            Write-Warn "Python-пакеты не установлены"
            $exitCode = 1
        } else {
            Install-PythonPackages $venvPython
        }
    } elseif ($venvPython) {
        if (Test-PythonPackages $venvPython) { Write-Ok "Python-пакеты уже установлены" }
        else { Write-Warn "Python-пакеты не установлены (-SkipPythonPackages)" }
    }

    if (-not $SkipFfmpeg) {
        $ffmpeg = Get-FfmpegPath
        if (-not $ffmpeg) {
            if ($CheckOnly) {
                Write-Warn "ffmpeg не найден"
                $exitCode = 1
            } else {
                $ffmpeg = Install-Ffmpeg
            }
        }
        if ($ffmpeg) {
            $version = (& $ffmpeg -version 2>&1 | Select-Object -First 1)
            Write-Ok "ffmpeg: $ffmpeg"
            Write-Host "         $version"
            $ffprobe = Join-Path (Split-Path -Parent $ffmpeg) 'ffprobe.exe'
            if (Test-Path -LiteralPath $ffprobe) { Write-Ok "ffprobe: $ffprobe" }
            else { Write-Warn "ffprobe.exe не найден рядом с ffmpeg" }
            try {
                $x264 = (& $ffmpeg -hide_banner -encoders 2>&1 | Select-String -Pattern 'libx264' -SimpleMatch)
                if ($x264) { Write-Ok "кодек libx264 доступен (нужен пайплайну)" }
                else { Write-Warn "libx264 не найден в этой сборке ffmpeg" }
            } catch { }
        }
    } else {
        Write-Warn "установка ffmpeg пропущена (-SkipFfmpeg)"
    }

    if (-not $SkipVcRedist) {
        if ($CheckOnly) {
            if (Test-VcRedist) { Write-Ok "Visual C++ Redistributable x64 установлен" }
            else { Write-Warn "Visual C++ Redistributable x64 не найден" }
        } else {
            Install-VcRedist
        }
    }

    if (-not $CheckOnly) {
        Enable-LongPaths
    }

    Write-Host ""
    if ($exitCode -eq 0) {
        if ($CheckOnly) {
            Write-Host "=== ПРОВЕРКА ЗАВЕРШЕНА: ВСЁ В ПОРЯДКЕ ===" -ForegroundColor Green
            Write-Host "Установка не требуется."
        } else {
            Write-Host "=== ГОТОВО ===" -ForegroundColor Green
            Write-Host "Проверка:"
            Write-Host "  & `"$VenvPython`" `"$(Join-Path $ScriptDir 'batch_dl.py')`" --check"
            Write-Host "Запуск:"
            Write-Host "  & `"$VenvPython`" `"$(Join-Path $ScriptDir 'batch_dl.py')`""
        }
    } else {
        if ($CheckOnly) {
            Write-Host "=== ПРОВЕРКА ЗАВЕРШЕНА: ЕСТЬ ПРОБЛЕМЫ ===" -ForegroundColor Yellow
            Write-Host "Запустите установщик без -CheckOnly:"
            Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptDir 'setup_deps.ps1')`""
        } else {
            Write-Host "=== ЕСТЬ ПРОБЛЕМЫ ===" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Err $_.Exception.Message
    Write-Host $_.ScriptStackTrace
    $exitCode = 2
} finally {
    Stop-TranscriptSafe
}

exit $exitCode

