param([switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDir
$minimumPython = [Version]'3.10'

function Get-PythonVersion($candidate) {
    try {
        $value = & $candidate.File @($candidate.Args) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $value) { return $null }
        return [Version]($value | Select-Object -Last 1)
    }
    catch {
        return $null
    }
}

function Find-Python {
    $commands = @(
        @{ File = 'py.exe'; Args = @('-3') },
        @{ File = 'python.exe'; Args = @() }
    )
    $candidates = @()
    foreach ($candidate in $commands) {
        $command = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += @{ File = $command.Source; Args = $candidate.Args }
        }
    }

    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python*')
    )
    foreach ($root in $roots) {
        $found = @(Get-ChildItem -Path $root -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\WindowsApps\\' } |
            Sort-Object FullName -Descending)
        foreach ($item in $found) {
            $candidates += @{ File = $item.FullName; Args = @() }
        }
    }

    $seen = @{}
    foreach ($candidate in $candidates) {
        $key = $candidate.File + '|' + ($candidate.Args -join ' ')
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $version = Get-PythonVersion $candidate
        if ($version -and $version -ge $minimumPython) {
            $candidate.Version = $version
            return $candidate
        }
    }
    return $null
}

Write-Host 'Проверяю Python...'
$venvDir = Join-Path $appDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$existingVenvVersion = $null
if (Test-Path -LiteralPath $venvPython) {
    $existingVenvVersion = Get-PythonVersion @{ File = $venvPython; Args = @() }
}

$python = $null
if ($existingVenvVersion -and $existingVenvVersion -ge $minimumPython) {
    Write-Host "Использую существующее окружение Python $existingVenvVersion."
}
else {
    $python = Find-Python
    if (-not $python) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw 'Совместимый Python 3.10 или новее не найден. Установите Python 3.13 с сайта python.org и снова запустите INSTALL.bat.'
        }
        Write-Host 'Совместимый Python 3.10 или новее не найден. Устанавливаю Python 3.13 через Windows Package Manager...'
        & $winget.Source install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось установить Python (код $LASTEXITCODE)."
        }
        $python = Find-Python
        if (-not $python) {
            $expected = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
            if (Test-Path -LiteralPath $expected) {
                $python = @{ File = $expected; Args = @(); Version = [Version]'3.13' }
            }
        }
        if (-not $python) {
            throw 'Python установлен, но ещё не обнаружен. Перезапустите INSTALL.bat.'
        }
    }
}

Write-Host 'Создаю изолированное окружение программы...'
$venvBackup = $null
if (Test-Path -LiteralPath $venvDir) {
    if (-not $existingVenvVersion -or $existingVenvVersion -lt $minimumPython) {
        $venvBackup = "$venvDir.unsupported-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Write-Host 'Существующее окружение использует неподдерживаемый Python; временно сохраняю его и создаю новое.'
        Move-Item -LiteralPath $venvDir -Destination $venvBackup
    }
}

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $python.File @($python.Args) -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
    }

    Write-Host 'Устанавливаю проверенные зависимости...'
    & $venvPython -m pip install --disable-pip-version-check --no-cache-dir --upgrade -r (Join-Path $appDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }

    Write-Host 'Проверяю файлы программы...'
    & $venvPython -m py_compile (Join-Path $appDir 'telegram_collector_free.py')
    if ($LASTEXITCODE -ne 0) { throw 'Проверка программы завершилась ошибкой.' }
    & $venvPython -c "import telethon; print('Telethon ' + telethon.__version__ + ' готов.')"
    if ($LASTEXITCODE -ne 0) { throw 'Telethon не запускается.' }
}
catch {
    if ($venvBackup) {
        if (Test-Path -LiteralPath $venvDir) { Remove-Item -LiteralPath $venvDir -Recurse -Force }
        Move-Item -LiteralPath $venvBackup -Destination $venvDir
    }
    throw
}

if ($venvBackup -and (Test-Path -LiteralPath $venvBackup)) {
    Remove-Item -LiteralPath $venvBackup -Recurse -Force
}

$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop -and -not $NoShortcut) {
    $shortcutPath = Join-Path $desktop 'Unofficial Telegram News Digest.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $appDir 'Telegram_Digest.exe'
    $shortcut.WorkingDirectory = $appDir
    $shortcut.IconLocation = (Join-Path $appDir 'Telegram_Digest.exe') + ',0'
    $shortcut.Description = 'Unofficial Telegram News Digest'
    $shortcut.Save()
    Write-Host 'Ярлык Unofficial Telegram News Digest создан на рабочем столе.'
}

Write-Host ''
Write-Host 'Готово. При первом запуске без сохранённой сессии программа попросит данные Telegram API.'
