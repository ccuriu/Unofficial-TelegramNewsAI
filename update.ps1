param(
    [Parameter(Position = 0)]
    [string]$TargetPath,
    [switch]$AllowDowngrade
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$sourceDir = [IO.Path]::GetFullPath(
    (Split-Path -Parent $MyInvocation.MyCommand.Path)
).TrimEnd('\')
$manifestPath = Join-Path $sourceDir 'release_manifest.psd1'
$shortcutName = 'Unofficial TelegramNewsAI.lnk'

function Get-AppVersion([string]$Root) {
    $collector = Join-Path $Root 'telegram_collector_free.py'
    if (-not (Test-Path -LiteralPath $collector -PathType Leaf)) {
        return $null
    }
    $match = Select-String -LiteralPath $collector -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $match) {
        return $null
    }
    return $match.Matches[0].Groups[1].Value
}

function Get-NumericVersion([string]$Value) {
    if ($Value -notmatch '^\s*(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?') {
        throw "Не удалось распознать числовую часть версии: $Value"
    }
    $revision = if ($Matches[4]) { [int]$Matches[4] } else { 0 }
    return [Version]::new(
        [int]$Matches[1],
        [int]$Matches[2],
        [int]$Matches[3],
        $revision
    )
}

function Test-InstallationFolder([string]$Path) {
    if (-not $Path) { return $false }
    try {
        $full = [IO.Path]::GetFullPath($Path)
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

function Resolve-ShortcutCandidates {
    $shell = New-Object -ComObject WScript.Shell
    $desktops = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory')
    ) | Where-Object { $_ } | Select-Object -Unique

    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $result = @()

    foreach ($desktop in $desktops) {
        $shortcutPath = Join-Path $desktop $shortcutName
        if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
            continue
        }
        try {
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $target = $shortcut.TargetPath
            if (-not $target) { continue }
            $candidate = Split-Path -Parent $target
            if (
                (Test-InstallationFolder $candidate) -and
                $seen.Add([IO.Path]::GetFullPath($candidate))
            ) {
                $result += [IO.Path]::GetFullPath($candidate)
            }
        }
        catch {
            continue
        }
    }
    return @($result)
}

function Assert-SafeTarget([string]$Path) {
    if (-not $Path) {
        throw 'Папка установки не указана.'
    }

    $full = [IO.Path]::GetFullPath(
        [Environment]::ExpandEnvironmentVariables($Path)
    )
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -ieq $root.TrimEnd('\')) {
        throw 'Корень диска нельзя использовать как папку установки.'
    }

    $dangerous = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory'),
        [Environment]::GetFolderPath('MyDocuments'),
        $env:USERPROFILE,
        (Join-Path $env:USERPROFILE 'Downloads')
    ) | Where-Object { $_ }

    foreach ($item in $dangerous) {
        if ($full.TrimEnd('\') -ieq ([IO.Path]::GetFullPath($item)).TrimEnd('\')) {
            throw "Нельзя обновлять прямо в системную пользовательскую папку: $full"
        }
    }

    if ($full.TrimEnd('\') -ieq $sourceDir.TrimEnd('\')) {
        throw 'Папка нового release ZIP и существующая установка должны быть разными.'
    }

    if (-not (Test-InstallationFolder $full)) {
        throw "Папка не похожа на установленный Unofficial TelegramNewsAI: $full"
    }

    return $full.TrimEnd('\')
}

function Assert-RelativeProgramPath([string]$Name) {
    if (
        [string]::IsNullOrWhiteSpace($Name) -or
        [IO.Path]::IsPathRooted($Name) -or
        $Name -match '(^|[\\/])\.\.([\\/]|$)'
    ) {
        throw "Небезопасный путь в release manifest: $Name"
    }
}

function Test-ProtectedStateName([string]$Name) {
    $normalized = $Name -replace '/', '\'
    $leaf = [IO.Path]::GetFileName($normalized)
    return (
        $leaf -ieq 'credentials.bin' -or
        $leaf -like 'credentials.unreadable-*.bin' -or
        $leaf -ieq 'telegram_session.session' -or
        $leaf -ieq 'telegram_session.session-journal' -or
        $leaf -ieq 'selected_channels.json' -or
        $leaf -ieq 'settings_free.json' -or
        $leaf -ieq 'news.db' -or
        $leaf -like 'news.db-*' -or
        $normalized -match '(^|\\)(Дайджесты|logs|Резервные_копии|\.venv)(\\|$)'
    )
}

function Assert-ProgramManifest($Manifest) {
    $programFiles = @($Manifest.ProgramFiles)
    $obsoleteFiles = @($Manifest.ObsoleteFiles)

    if (-not $programFiles.Count) {
        throw 'Release manifest не содержит ProgramFiles.'
    }

    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($name in @($programFiles + $obsoleteFiles)) {
        Assert-RelativeProgramPath $name
        if (Test-ProtectedStateName $name) {
            throw "Release manifest пытается управлять пользовательским файлом: $name"
        }
    }
    foreach ($name in $programFiles) {
        if (-not $seen.Add($name)) {
            throw "Повторяющийся файл в ProgramFiles: $name"
        }
        $source = Join-Path $sourceDir $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "В release отсутствует обязательный файл: $name"
        }
    }

    return @{
        ProgramFiles = $programFiles
        ObsoleteFiles = $obsoleteFiles
    }
}

function Assert-LauncherChecksum {
    $launcher = Join-Path $sourceDir 'Telegram_Digest.exe'
    $checksum = Join-Path $sourceDir 'Telegram_Digest.exe.sha256'
    if (
        -not (Test-Path -LiteralPath $launcher -PathType Leaf) -or
        -not (Test-Path -LiteralPath $checksum -PathType Leaf)
    ) {
        return
    }

    $expected = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split '\s+')[0]
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $launcher).Hash
    if (-not $expected -or $actual -ine $expected) {
        throw 'Контрольная сумма Telegram_Digest.exe в новом release не совпадает.'
    }
}

function Test-ApplicationRunning([string]$Target) {
    $targetPrefix = $Target.TrimEnd('\') + '\'
    $launcherPath = Join-Path $Target 'Telegram_Digest.exe'

    foreach ($process in @(Get-Process -Name 'Telegram_Digest' -ErrorAction SilentlyContinue)) {
        try {
            if (-not $process.Path -or $process.Path -ieq $launcherPath) {
                return $true
            }
        }
        catch {
            return $true
        }
    }

    try {
        $pythonProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -in @('python.exe', 'pythonw.exe', 'py.exe')
        })
    }
    catch {
        throw 'Не удалось безопасно проверить запущенные Python-процессы. Закройте Unofficial TelegramNewsAI и повторите обновление.'
    }

    foreach ($process in $pythonProcesses) {
        $exe = [string]$process.ExecutablePath
        $command = [string]$process.CommandLine
        if (
            ($exe -and $exe.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
            (
                $command -and
                $command.IndexOf($Target, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $command.IndexOf('telegram_collector_free.py', [StringComparison]::OrdinalIgnoreCase) -ge 0
            )
        ) {
            return $true
        }
    }
    return $false
}

function Get-StateSnapshot([string]$Target) {
    $snapshot = @{}
    $files = @(Get-ChildItem -LiteralPath $Target -File -Force -ErrorAction Stop | Where-Object {
        $_.Name -ieq 'credentials.bin' -or
        $_.Name -like 'credentials.unreadable-*.bin' -or
        $_.Name -ieq 'telegram_session.session' -or
        $_.Name -ieq 'telegram_session.session-journal' -or
        $_.Name -ieq 'selected_channels.json' -or
        $_.Name -ieq 'settings_free.json' -or
        $_.Name -ieq 'news.db' -or
        $_.Name -like 'news.db-*'
    })

    foreach ($file in $files) {
        $snapshot[$file.Name] = @{
            Length = [int64]$file.Length
            Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
        }
    }
    return $snapshot
}

function Assert-StateUnchanged($Before, $After) {
    $beforeNames = @($Before.Keys | Sort-Object)
    $afterNames = @($After.Keys | Sort-Object)
    $difference = @(Compare-Object -ReferenceObject $beforeNames -DifferenceObject $afterNames)
    if ($difference.Count) {
        throw 'Набор критических пользовательских файлов изменился во время обновления.'
    }

    foreach ($name in $beforeNames) {
        if (
            $Before[$name].Length -ne $After[$name].Length -or
            $Before[$name].Sha256 -ine $After[$name].Sha256
        ) {
            throw "Критический пользовательский файл изменился во время обновления: $name"
        }
    }
}

function Copy-ManagedFile([string]$SourceRoot, [string]$DestinationRoot, [string]$Name) {
    $source = Join-Path $SourceRoot $Name
    $destination = Join-Path $DestinationRoot $Name
    $parent = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Write-Host 'ОШИБКА: release_manifest.psd1 отсутствует рядом с UPDATE.bat.'
    exit 1
}

try {
    $manifest = Import-PowerShellDataFile -LiteralPath $manifestPath
    $validated = Assert-ProgramManifest $manifest
    $programFiles = @($validated.ProgramFiles)
    $obsoleteFiles = @($validated.ObsoleteFiles)
    Assert-LauncherChecksum

    if (-not $TargetPath) {
        $candidates = @(Resolve-ShortcutCandidates)
        if ($candidates.Count -eq 1) {
            $TargetPath = $candidates[0]
        }
        elseif ($candidates.Count -gt 1) {
            Write-Host 'Найдено несколько установок по ярлыкам:'
            $candidates | ForEach-Object { Write-Host "  $_" }
            throw 'Укажите нужную папку явно: UPDATE.bat "D:\путь\к\Unofficial TelegramNewsAI"'
        }
        else {
            throw 'Не удалось определить существующую установку по ярлыку. Укажите папку явно: UPDATE.bat "D:\путь\к\Unofficial TelegramNewsAI"'
        }
    }

    $target = Assert-SafeTarget $TargetPath
    $installedVersion = Get-AppVersion $target
    $newVersion = Get-AppVersion $sourceDir
    if (-not $installedVersion) {
        throw 'Не удалось определить версию существующей установки.'
    }
    if (-not $newVersion) {
        throw 'Не удалось определить версию нового release.'
    }

    $installedNumeric = Get-NumericVersion $installedVersion
    $newNumeric = Get-NumericVersion $newVersion
    if (-not $AllowDowngrade -and $newNumeric -lt $installedNumeric) {
        throw "Обнаружена попытка установки более старой версии: $installedVersion → $newVersion"
    }

    Write-Host ''
    Write-Host "Установлено: $installedVersion"
    Write-Host "Новая версия: $newVersion"
    Write-Host "Папка: $target"
    Write-Host ''

    if (Test-ApplicationRunning $target) {
        throw 'Закройте Unofficial TelegramNewsAI и повторите обновление.'
    }

    $stateBefore = Get-StateSnapshot $target

    $backupRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'TelegramNewsAI-update-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

    $managedFiles = @(@($programFiles + $obsoleteFiles) | Select-Object -Unique)
    $existedBefore = @{}
    $backupReady = $false
    $copyStarted = $false

    try {
        foreach ($name in $managedFiles) {
            $targetFile = Join-Path $target $name
            $exists = Test-Path -LiteralPath $targetFile -PathType Leaf
            $existedBefore[$name] = $exists
            if ($exists) {
                $backupFile = Join-Path $backupRoot $name
                $backupParent = Split-Path -Parent $backupFile
                if (-not (Test-Path -LiteralPath $backupParent)) {
                    New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
                }
                Copy-Item -LiteralPath $targetFile -Destination $backupFile -Force
            }
        }
        $backupReady = $true

        foreach ($name in $programFiles) {
            $copyStarted = $true
            Copy-ManagedFile $sourceDir $target $name
        }

        foreach ($name in $obsoleteFiles) {
            $obsolete = Join-Path $target $name
            if (Test-Path -LiteralPath $obsolete -PathType Leaf) {
                Remove-Item -LiteralPath $obsolete -Force
            }
        }

        $stateAfterCopy = Get-StateSnapshot $target
        Assert-StateUnchanged $stateBefore $stateAfterCopy

        Write-Host 'Обновляю существующее окружение Python и проверяю зависимости...'
        $installer = Join-Path $target 'install.ps1'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -NoShortcut
        if ($LASTEXITCODE -ne 0) {
            throw "Установка зависимостей завершилась ошибкой (код $LASTEXITCODE)."
        }

        Write-Host 'Проверяю launcher...'
        $launcher = Join-Path $target 'Telegram_Digest.exe'
        $selfTest = Start-Process -FilePath $launcher -ArgumentList '--self-test' -Wait -PassThru
        if ($selfTest.ExitCode -ne 0) {
            throw "Launcher --self-test завершился ошибкой (код $($selfTest.ExitCode))."
        }

        $stateAfter = Get-StateSnapshot $target
        Assert-StateUnchanged $stateBefore $stateAfter

        Write-Host ''
        Write-Host 'Обновление завершено.'
        Write-Host "$installedVersion → $newVersion"
        if ($stateBefore.ContainsKey('telegram_session.session')) {
            Write-Host 'Telegram-сессия сохранена.'
        }
        else {
            Write-Host 'Telegram-сессия до обновления отсутствовала; updater её не создавал.'
        }
        if ($stateBefore.ContainsKey('credentials.bin')) {
            Write-Host 'Учётные данные сохранены.'
        }
        else {
            Write-Host 'Файл учётных данных до обновления отсутствовал; updater его не создавал.'
        }
        Write-Host 'Каналы и настройки сохранены.'
        Write-Host 'База и история сохранены.'
        Write-Host 'Повторная авторизация не требуется, если Telegram-сессия была рабочей до обновления.'
    }
    catch {
        $failure = $_
        if ($backupReady -and $copyStarted) {
            Write-Host ''
            Write-Host 'Обновление не завершено. Восстанавливаю предыдущие программные файлы...'
            foreach ($name in $managedFiles) {
                $backupFile = Join-Path $backupRoot $name
                $targetFile = Join-Path $target $name
                if (Test-Path -LiteralPath $backupFile -PathType Leaf) {
                    $parent = Split-Path -Parent $targetFile
                    if (-not (Test-Path -LiteralPath $parent)) {
                        New-Item -ItemType Directory -Path $parent -Force | Out-Null
                    }
                    Copy-Item -LiteralPath $backupFile -Destination $targetFile -Force
                }
                elseif (-not $existedBefore[$name] -and (Test-Path -LiteralPath $targetFile -PathType Leaf)) {
                    Remove-Item -LiteralPath $targetFile -Force
                }
            }
            Write-Host 'Предыдущие программные файлы восстановлены.'
        }
        throw $failure
    }
    finally {
        if (Test-Path -LiteralPath $backupRoot) {
            Remove-Item -LiteralPath $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
catch {
    Write-Host ''
    Write-Host ('ОШИБКА: ' + $_.Exception.Message)
    exit 1
}

exit 0
