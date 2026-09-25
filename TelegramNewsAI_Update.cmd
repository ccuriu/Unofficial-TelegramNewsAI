@echo off
chcp 65001 >nul
setlocal
set "TELEGRAMNEWSAI_UPDATER_SELF=%~f0"
set "TELEGRAMNEWSAI_UPDATER_RELEASE=%~1"
title Обновление Unofficial TelegramNewsAI

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$self=$env:TELEGRAMNEWSAI_UPDATER_SELF; $raw=[IO.File]::ReadAllText($self,[Text.Encoding]::UTF8); $marker=':__POWERSHELL_PAYLOAD__'; $index=$raw.IndexOf($marker,[StringComparison]::Ordinal); if($index -lt 0){Write-Error 'Updater payload not found.'; exit 2}; $script=$raw.Substring($index+$marker.Length); & ([ScriptBlock]::Create($script))"
set "code=%ERRORLEVEL%"
if not "%code%"=="0" (
  echo.
  echo Обновление не завершено. Текст ошибки указан выше.
  pause
)
exit /b %code%

:__POWERSHELL_PAYLOAD__

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$registryPath = 'HKCU:\Software\Unofficial TelegramNewsAI'
$shortcutName = 'Unofficial TelegramNewsAI.lnk'
$selfPath = [IO.Path]::GetFullPath($env:TELEGRAMNEWSAI_UPDATER_SELF)
$selfDir = Split-Path -Parent $selfPath
$requestedRelease = [Environment]::ExpandEnvironmentVariables(
    [string]$env:TELEGRAMNEWSAI_UPDATER_RELEASE
)

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Test-InstallationFolder([string]$Path) {
    if (-not $Path) { return $false }
    try {
        $full = [IO.Path]::GetFullPath(
            [Environment]::ExpandEnvironmentVariables($Path)
        )
    }
    catch {
        return $false
    }

    $collector = Join-Path $full 'telegram_collector_free.py'
    $launcher = Join-Path $full 'Telegram_Digest.exe'
    $batch = Join-Path $full 'start_free.bat'
    return (
        (Test-Path -LiteralPath $collector -PathType Leaf) -and
        (
            (Test-Path -LiteralPath $launcher -PathType Leaf) -or
            (Test-Path -LiteralPath $batch -PathType Leaf)
        )
    )
}

function Add-UniqueCandidate(
    [Collections.Generic.List[string]]$List,
    [Collections.Generic.HashSet[string]]$Seen,
    [string]$Path
) {
    if (-not (Test-InstallationFolder $Path)) { return }
    $full = [IO.Path]::GetFullPath(
        [Environment]::ExpandEnvironmentVariables($Path)
    ).TrimEnd('\')
    if ($Seen.Add($full)) {
        $List.Add($full)
    }
}

function Resolve-InstallationCandidates {
    $result = [Collections.Generic.List[string]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )

    try {
        $registered = (Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop).InstallPath
        Add-UniqueCandidate $result $seen ([string]$registered)
    }
    catch {
    }

    $shortcutRoots = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory'),
        [Environment]::GetFolderPath('Programs'),
        [Environment]::GetFolderPath('CommonPrograms')
    ) | Where-Object { $_ } | Select-Object -Unique

    $shell = New-Object -ComObject WScript.Shell
    foreach ($root in $shortcutRoots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            continue
        }

        $shortcutPaths = @()
        $direct = Join-Path $root $shortcutName
        if (Test-Path -LiteralPath $direct -PathType Leaf) {
            $shortcutPaths += $direct
        }

        if ($root -notin @(
            [Environment]::GetFolderPath('Desktop'),
            [Environment]::GetFolderPath('CommonDesktopDirectory')
        )) {
            $shortcutPaths += @(
                Get-ChildItem -LiteralPath $root -Filter $shortcutName -File -Recurse -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
            )
        }

        foreach ($shortcutPath in @($shortcutPaths | Select-Object -Unique)) {
            try {
                $shortcut = $shell.CreateShortcut($shortcutPath)
                if ($shortcut.TargetPath) {
                    Add-UniqueCandidate $result $seen (Split-Path -Parent $shortcut.TargetPath)
                }
            }
            catch {
            }
        }
    }

    return @($result)
}

function Select-InstallationFolder([string]$Description) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw 'Обновление отменено: папка установки не выбрана.'
    }
    if (-not (Test-InstallationFolder $dialog.SelectedPath)) {
        throw "Выбранная папка не похожа на установленный Unofficial TelegramNewsAI: $($dialog.SelectedPath)"
    }
    return [IO.Path]::GetFullPath($dialog.SelectedPath).TrimEnd('\')
}

function Resolve-InstallationFolder {
    $candidates = @(Resolve-InstallationCandidates)
    if ($candidates.Count -eq 1) {
        return $candidates[0]
    }

    if ($candidates.Count -gt 1) {
        Write-Host 'Найдено несколько установок:'
        $candidates | ForEach-Object { Write-Host "  $_" }
        return Select-InstallationFolder 'Выберите папку той установки Unofficial TelegramNewsAI, которую нужно обновить.'
    }

    return Select-InstallationFolder 'Установка автоматически не найдена. Выберите папку установленного Unofficial TelegramNewsAI.'
}

function Select-ReleaseZip {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Выберите новый release ZIP Unofficial TelegramNewsAI'
    $dialog.Filter = 'Unofficial TelegramNewsAI (*.zip)|Unofficial-TelegramNewsAI-*-Windows.zip|ZIP (*.zip)|*.zip'
    $dialog.CheckFileExists = $true
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw 'Обновление отменено: release ZIP не выбран.'
    }
    return [IO.Path]::GetFullPath($dialog.FileName)
}

function Resolve-ReleaseZip {
    if ($requestedRelease) {
        $candidate = [IO.Path]::GetFullPath($requestedRelease)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Указанный release ZIP не найден: $candidate"
        }
        if ([IO.Path]::GetExtension($candidate) -ine '.zip') {
            throw "Ожидался ZIP-файл release: $candidate"
        }
        return $candidate
    }

    $nearby = @(
        Get-ChildItem -LiteralPath $selfDir -Filter 'Unofficial-TelegramNewsAI-*-Windows.zip' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    )
    if ($nearby.Count -eq 1) {
        return $nearby[0].FullName
    }

    if ($nearby.Count -gt 1) {
        Write-Host 'Рядом с updater найдено несколько release ZIP. Выберите нужный файл.'
    }
    return Select-ReleaseZip
}

function Assert-ZipChecksumIfPresent([string]$ZipPath) {
    $checksumPath = [IO.Path]::ChangeExtension($ZipPath, $null) + '.sha256.txt'
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        Write-Host 'Файл SHA-256 рядом с ZIP не найден; целостность программных файлов дополнительно проверит штатный updater.'
        return
    }

    $expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
    $actual = Get-Sha256 $ZipPath
    if (-not $expected -or $expected -ine $actual) {
        throw 'SHA-256 release ZIP не совпадает. Обновление остановлено.'
    }
    Write-Host 'SHA-256 release ZIP: OK'
}

function Set-InstallRegistration([string]$Path) {
    try {
        if (-not (Test-Path -LiteralPath $registryPath)) {
            New-Item -Path $registryPath -Force | Out-Null
        }
        Set-ItemProperty -LiteralPath $registryPath -Name InstallPath -Value $Path -Type String
    }
    catch {
        Write-Host 'Предупреждение: не удалось сохранить путь установки в профиле Windows.'
    }
}

$tempRoot = $null
try {
    $target = Resolve-InstallationFolder
    $zipPath = Resolve-ReleaseZip

    Write-Host ''
    Write-Host "Установка: $target"
    Write-Host "Release ZIP: $zipPath"
    Write-Host ''

    Assert-ZipChecksumIfPresent $zipPath

    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'TelegramNewsAI-portable-update-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    Write-Host 'Распаковываю новый release во временную папку...'
    Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force

    $packageDirs = @(
        Get-ChildItem -LiteralPath $tempRoot -Directory -Force
    )
    if ($packageDirs.Count -ne 1) {
        throw "Некорректный release ZIP: ожидалась одна корневая папка, найдено $($packageDirs.Count)."
    }

    $package = $packageDirs[0].FullName
    $updater = Join-Path $package 'update.ps1'
    $manifest = Join-Path $package 'release_manifest.json'
    if (
        -not (Test-Path -LiteralPath $updater -PathType Leaf) -or
        -not (Test-Path -LiteralPath $manifest -PathType Leaf)
    ) {
        throw 'Некорректный release ZIP: отсутствует штатный updater или release manifest.'
    }

    Write-Host ''
    Write-Host 'Запускаю штатное безопасное обновление.'
    Write-Host 'Если программа сейчас работает, она НЕ будет принудительно закрыта.'
    Write-Host 'Updater дождётся, когда вы сами закончите текущий запуск, и продолжит автоматически.'
    Write-Host ''

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $updater -TargetPath $target -WaitForExit
    if ($LASTEXITCODE -ne 0) {
        throw "Штатный updater завершился с кодом $LASTEXITCODE."
    }

    Set-InstallRegistration $target
    Write-Host ''
    Write-Host 'Готово. Telegram-сессия, credentials, настройки, каналы, база и история сохранены.'
    Write-Host 'Повторная авторизация не требуется, если сессия была рабочей до обновления.'
}
catch {
    Write-Host ''
    Write-Host ('ОШИБКА: ' + $_.Exception.Message)
    exit 1
}
finally {
    if ($tempRoot -and (Test-Path -LiteralPath $tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit 0
