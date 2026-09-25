@echo off
chcp 65001 >nul
setlocal
set "TELEGRAMNEWSAI_UPDATER_SELF=%~f0"
set "TELEGRAMNEWSAI_UPDATER_MODE=%~1"
title Обновление Unofficial TelegramNewsAI

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$self=$env:TELEGRAMNEWSAI_UPDATER_SELF; $raw=[IO.File]::ReadAllText($self,[Text.Encoding]::UTF8); $marker=':__POWERSHELL_PAYLOAD__'; $index=$raw.LastIndexOf($marker,[StringComparison]::Ordinal); if($index -lt 0){Write-Error 'Updater payload not found.'; exit 2}; $script=$raw.Substring($index+$marker.Length); & ([ScriptBlock]::Create($script))"
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

$repository = 'ccuriu/Unofficial-TelegramNewsAI'
$registryPath = 'HKCU:\Software\Unofficial TelegramNewsAI'
$shortcutName = 'Unofficial TelegramNewsAI.lnk'
$githubHeaders = @{
    'User-Agent' = 'Unofficial-TelegramNewsAI-Updater'
    'Accept' = 'application/vnd.github+json'
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}
catch {
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
        return Select-InstallationFolder 'Найдено несколько установок. Выберите ту, которую нужно обновить.'
    }

    return Select-InstallationFolder 'Установка автоматически не найдена. Выберите папку установленного Unofficial TelegramNewsAI.'
}

function Invoke-GitHubJson([string]$Url) {
    return Invoke-RestMethod -Uri $Url -Headers $githubHeaders -Method Get -TimeoutSec 30
}

function Resolve-LatestStableRelease {
    Write-Host 'Проверяю последнюю опубликованную Stable-версию...'
    $release = Invoke-GitHubJson -Url "https://api.github.com/repos/$repository/releases/latest"

    if (-not $release -or $release.draft -or $release.prerelease) {
        throw 'GitHub не вернул опубликованный Stable Release.'
    }

    $tag = [string]$release.tag_name
    if ([string]::IsNullOrWhiteSpace($tag)) {
        throw 'У последнего Stable Release отсутствует tag.'
    }

    $tagEncoded = [Uri]::EscapeDataString($tag)
    $commit = Invoke-GitHubJson -Url "https://api.github.com/repos/$repository/commits/$tagEncoded"
    $sha = [string]$commit.sha
    if ($sha -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Не удалось определить неизменяемый commit последнего Stable Release.'
    }

    return @{
        Tag = $tag
        Sha = $sha.ToLowerInvariant()
        Name = [string]$release.name
    }
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
        $leaf -like '*.session' -or
        $leaf -like '*.session-journal' -or
        $leaf -ieq 'selected_channels.json' -or
        $leaf -ieq 'settings_free.json' -or
        $leaf -ieq 'news.db' -or
        $leaf -like 'news.db-*' -or
        $normalized -match '(^|\\)(Дайджесты|logs|Резервные_копии|models|\.venv|__pycache__)(\\|$)'
    )
}

function Get-RawRepositoryUrl([string]$CommitSha, [string]$RelativePath) {
    Assert-RelativeProgramPath $RelativePath
    $normalized = $RelativePath.Replace('\', '/')
    $safePath = (($normalized.Split('/') | ForEach-Object {
        [Uri]::EscapeDataString($_)
    }) -join '/')
    return "https://raw.githubusercontent.com/$repository/$CommitSha/$safePath"
}

function Download-RepositoryFile(
    [string]$CommitSha,
    [string]$RelativePath,
    [string]$Destination
) {
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $url = Get-RawRepositoryUrl $CommitSha $RelativePath
    Invoke-WebRequest -Uri $url -Headers $githubHeaders -OutFile $Destination -UseBasicParsing -TimeoutSec 60

    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "GitHub не вернул обязательный файл: $RelativePath"
    }
}

function Assert-DirectUpdateManifest($Manifest) {
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
        if (-not $seen.Add([string]$name)) {
            throw "Повторяющийся файл в ProgramFiles: $name"
        }
    }

    foreach ($required in @(
        'update.ps1',
        'release_manifest.json',
        'telegram_collector_free.py',
        'Telegram_Digest.exe',
        'Telegram_Digest.exe.sha256'
    )) {
        if ($required -notin $programFiles) {
            throw "Release manifest не содержит обязательный файл: $required"
        }
    }

    return @{
        ProgramFiles = $programFiles
        ObsoleteFiles = $obsoleteFiles
    }
}

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

function Show-CompletionMessage([string]$Version) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $text = if ($Version) {
            "Обновление установлено.\r\nВерсия: $Version\r\nПрограмма в актуальном состоянии."
        }
        else {
            "Обновление установлено.\r\nПрограмма в актуальном состоянии."
        }
        [void][System.Windows.Forms.MessageBox]::Show(
            $text,
            'Unofficial TelegramNewsAI',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        )
    }
    catch {
    }
}

if ([string]$env:TELEGRAMNEWSAI_UPDATER_MODE -ieq '--self-test') {
    $probeSha = 'a' * 40
    $probe = Get-RawRepositoryUrl $probeSha 'nested\file.txt'
    $expected = "https://raw.githubusercontent.com/$repository/$probeSha/nested/file.txt"
    if ($probe -cne $expected) {
        throw "Standalone updater URL normalization failed: $probe"
    }
    if (-not (Test-ProtectedStateName 'nested\credentials.bin')) {
        throw 'Standalone updater protected-state check failed.'
    }
    if (Test-ProtectedStateName 'README.md') {
        throw 'Standalone updater marked a program file as protected state.'
    }
    Write-Host 'Standalone updater self-test: OK'
    exit 0
}

$tempRoot = $null
try {
    $target = Resolve-InstallationFolder
    $release = Resolve-LatestStableRelease

    Write-Host ''
    Write-Host "Установка: $target"
    Write-Host "Последний Stable: $($release.Tag)"
    Write-Host 'Скачиваю только программные файлы напрямую из публичного репозитория...'

    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'TelegramNewsAI-direct-update-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    $manifestPath = Join-Path $tempRoot 'release_manifest.json'
    Download-RepositoryFile $release.Sha 'release_manifest.json' $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $validated = Assert-DirectUpdateManifest $manifest

    foreach ($name in @($validated.ProgramFiles)) {
        if ($name -ieq 'release_manifest.json') {
            continue
        }
        $destination = Join-Path $tempRoot $name
        Download-RepositoryFile $release.Sha $name $destination
    }

    $updater = Join-Path $tempRoot 'update.ps1'
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
        throw 'Не удалось подготовить штатный updater.'
    }

    Write-Host ''
    Write-Host 'Файлы обновления подготовлены.'
    Write-Host 'Если программа сейчас работает, текущий запуск и Telegram-соединение не прерываются.'
    Write-Host 'Updater дождётся обычного завершения программы и продолжит автоматически.'
    Write-Host ''

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $updater -TargetPath $target -WaitForExit
    if ($LASTEXITCODE -ne 0) {
        throw "Штатный updater завершился с кодом $LASTEXITCODE."
    }

    $installedVersion = Get-AppVersion $target
    Write-Host ''
    Write-Host 'Готово.'
    if ($installedVersion) {
        Write-Host "Версия: $installedVersion"
    }
    Write-Host 'Программа в актуальном состоянии.'
    Write-Host 'Telegram-сессия, credentials, настройки, каналы, база и история сохранены.'

    Show-CompletionMessage $installedVersion
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
