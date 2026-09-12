param([switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDir

function Find-Python {
    $commands = @(
        @{ File = 'py.exe'; Args = @('-3') },
        @{ File = 'python.exe'; Args = @() }
    )
    foreach ($candidate in $commands) {
        $command = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if ($command) {
            return @{ File = $command.Source; Args = $candidate.Args }
        }
    }

    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python*')
    )
    foreach ($root in $roots) {
        $found = Get-ChildItem -Path $root -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\WindowsApps\\' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) {
            return @{ File = $found.FullName; Args = @() }
        }
    }
    return $null
}

Write-Host 'Проверяю Python...'
$python = Find-Python
if (-not $python) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python 3 не найден. Установите Python 3.13 с сайта python.org и снова запустите INSTALL.bat.'
    }
    Write-Host 'Python не найден. Устанавливаю Python 3.13 через Windows Package Manager...'
    & $winget.Source install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось установить Python (код $LASTEXITCODE)."
    }
    $python = Find-Python
    if (-not $python) {
        $expected = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
        if (Test-Path -LiteralPath $expected) {
            $python = @{ File = $expected; Args = @() }
        }
    }
    if (-not $python) {
        throw 'Python установлен, но ещё не обнаружен. Перезапустите INSTALL.bat.'
    }
}

Write-Host 'Создаю изолированное окружение программы...'
$venvPython = Join-Path $appDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python.File @($python.Args) -m venv (Join-Path $appDir '.venv')
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

$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop -and -not $NoShortcut) {
    $shortcutPath = Join-Path $desktop 'TelegramNewsAI.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $appDir 'Telegram_Digest.exe'
    $shortcut.WorkingDirectory = $appDir
    $shortcut.IconLocation = (Join-Path $appDir 'Telegram_Digest.exe') + ',0'
    $shortcut.Description = 'TelegramNewsAI'
    $shortcut.Save()
    Write-Host 'Ярлык TelegramNewsAI создан на рабочем столе.'
}

Write-Host ''
Write-Host 'Готово. При первом запуске программа попросит ваши данные Telegram API.'
