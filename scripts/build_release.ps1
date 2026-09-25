param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd('\')
$manifestPath = Join-Path $root 'release_manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'release_manifest.json not found.'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$programFiles = @($manifest.ProgramFiles)
if (-not $programFiles.Count) { throw 'Release manifest has no ProgramFiles.' }
if (@($programFiles | Select-Object -Unique).Count -ne $programFiles.Count) {
    throw 'Release manifest contains duplicate ProgramFiles.'
}

$standaloneUpdaterName = [string]$manifest.StandaloneUpdater
if (
    [string]::IsNullOrWhiteSpace($standaloneUpdaterName) -or
    [IO.Path]::IsPathRooted($standaloneUpdaterName) -or
    $standaloneUpdaterName -match '(^|[\\/])\.\.([\\/]|$)' -or
    [IO.Path]::GetFileName($standaloneUpdaterName) -ne $standaloneUpdaterName
) {
    throw 'Release manifest has an invalid StandaloneUpdater.'
}
$standaloneUpdaterSource = Join-Path $root $standaloneUpdaterName
if (-not (Test-Path -LiteralPath $standaloneUpdaterSource -PathType Leaf)) {
    throw "Standalone updater not found: $standaloneUpdaterName"
}

$versionMatch = Select-String -LiteralPath (Join-Path $root 'telegram_collector_free.py') -Pattern '^APP_VERSION\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw 'Could not determine APP_VERSION.' }
$versionRaw = $versionMatch.Matches[0].Groups[1].Value
$channel = if ($versionRaw -match '\s+Testing$') { 'Testing' } else { 'Stable' }
$version = $versionRaw -replace '\s+(Stable|Testing)$', '' -replace '[^0-9A-Za-z._-]', '_'
$packageName = "Unofficial-TelegramNewsAI-$version-$channel-Windows"
$package = [IO.Path]::GetFullPath((Join-Path $output $packageName))
$zip = [IO.Path]::GetFullPath((Join-Path $output ($packageName + '.zip')))
$zipChecksum = [IO.Path]::GetFullPath((Join-Path $output ($packageName + '.sha256.txt')))
$standaloneUpdater = [IO.Path]::GetFullPath((Join-Path $output $standaloneUpdaterName))
$outputPrefix = $output + [IO.Path]::DirectorySeparatorChar

foreach ($target in @($package, $zip, $zipChecksum, $standaloneUpdater)) {
    if (-not $target.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe release output path: $target"
    }
}

if (Test-Path -LiteralPath $package) { Remove-Item -LiteralPath $package -Recurse -Force }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
if (Test-Path -LiteralPath $zipChecksum) { Remove-Item -LiteralPath $zipChecksum -Force }
if (Test-Path -LiteralPath $standaloneUpdater) { Remove-Item -LiteralPath $standaloneUpdater -Force }
New-Item -ItemType Directory -Path $package -Force | Out-Null

$generated = @('Telegram_Digest.exe', 'Telegram_Digest.exe.sha256')
foreach ($name in $programFiles) {
    if ($name -in $generated) { continue }
    if ([IO.Path]::IsPathRooted($name) -or $name -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Unsafe release manifest path: $name"
    }
    $source = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required release file not found: $name"
    }
    $destination = Join-Path $package $name
    $destinationDirectory = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $destinationDirectory)) {
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

$launcher = Join-Path $package 'Telegram_Digest.exe'
& (Join-Path $root 'launcher\build_launcher.ps1') -OutputPath $launcher
if (-not (Test-Path -LiteralPath $launcher)) {
    throw 'Launcher was not built.'
}
$launcherHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $launcher).Hash
$checksumText = $launcherHash + '  Telegram_Digest.exe' + [Environment]::NewLine
[IO.File]::WriteAllText(
    (Join-Path $package 'Telegram_Digest.exe.sha256'),
    $checksumText,
    [Text.UTF8Encoding]::new($false)
)

$actualFiles = @(
    Get-ChildItem -LiteralPath $package -Recurse -File -Force |
    ForEach-Object {
        $_.FullName.Substring($package.Length).TrimStart('\') -replace '\\', '/'
    } |
    Sort-Object
)
$expectedFiles = @($programFiles | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object)
$difference = @(Compare-Object -ReferenceObject $expectedFiles -DifferenceObject $actualFiles)
if ($difference.Count -or $actualFiles.Count -ne $expectedFiles.Count) {
    $difference | Format-Table | Out-String | Write-Error
    throw 'Release package does not match release_manifest.json.'
}

$forbiddenPattern = '(^|[\\/])(credentials(?:\.unreadable-[^\\/]*)?\.bin|[^\\/]*\.session(?:-journal)?|news\.db(?:-[^\\/]*)?|settings_free\.json|selected_channels\.json|collector\.lock)$|(^|[\\/])(Дайджесты|logs|Резервные_копии|models|\.venv|__pycache__)([\\/]|$)'
$forbidden = @(Get-ChildItem -LiteralPath $package -Recurse -Force | Where-Object {
    $_.FullName.Substring($package.Length).TrimStart('\') -match $forbiddenPattern
})
if ($forbidden.Count) { throw 'A local or sensitive file entered the release package.' }

Compress-Archive -LiteralPath $package -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash
[IO.File]::WriteAllText(
    $zipChecksum,
    ("$hash  $([IO.Path]::GetFileName($zip))" + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)

Copy-Item -LiteralPath $standaloneUpdaterSource -Destination $standaloneUpdater -Force

Write-Host "Release candidate: $zip"
Write-Host "SHA-256: $hash"
Write-Host "Standalone updater: $standaloneUpdater"
