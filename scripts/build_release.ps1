param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd('\')
$versionMatch = Select-String -LiteralPath (Join-Path $root 'telegram_collector_free.py') -Pattern '^APP_VERSION\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw 'Could not determine APP_VERSION.' }
$versionRaw = $versionMatch.Matches[0].Groups[1].Value
$channel = if ($versionRaw -match '\s+Testing$') { 'Testing' } else { 'Stable' }
$version = $versionRaw -replace '\s+(Stable|Testing)$', '' -replace '[^0-9A-Za-z._-]', '_'
$packageName = "Unofficial-Telegram-News-Digest-$version-$channel-Windows"
$package = [IO.Path]::GetFullPath((Join-Path $output $packageName))
$zip = [IO.Path]::GetFullPath((Join-Path $output ($packageName + '.zip')))
$zipChecksum = [IO.Path]::GetFullPath((Join-Path $output ($packageName + '.sha256.txt')))
$outputPrefix = $output + [IO.Path]::DirectorySeparatorChar

foreach ($target in @($package, $zip, $zipChecksum)) {
    if (-not $target.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe release output path: $target"
    }
}

if (Test-Path -LiteralPath $package) { Remove-Item -LiteralPath $package -Recurse -Force }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
if (Test-Path -LiteralPath $zipChecksum) { Remove-Item -LiteralPath $zipChecksum -Force }
New-Item -ItemType Directory -Path $package -Force | Out-Null

$copied = @(
    'INSTALL.bat', 'install.ps1', 'README.md', 'LICENSE', 'SECURITY.md',
    'requirements.txt', 'run_python.bat', 'start_free.bat',
    'telegram_collector_free.py'
)
foreach ($name in $copied) {
    $source = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $source)) { throw "Required release file not found: $name" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $package $name) -Force
}

# Launcher в release всегда собирается из актуального исходника.
# Так пакет не зависит от старого prebuilt EXE в корне репозитория.
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
$forbiddenPattern = '(^|[\\/])(credentials(?:\.unreadable-[^\\/]*)?\.bin|[^\\/]*\.session(?:-journal)?|news\.db(?:-[^\\/]*)?|settings_free\.json|selected_channels\.json|collector\.lock)$|(^|[\\/])(Дайджесты|logs|Резервные_копии|models|\.venv|__pycache__)([\\/]|$)'
$forbidden = @(Get-ChildItem -LiteralPath $package -Recurse -Force | Where-Object {
    $_.FullName.Substring($package.Length).TrimStart('\') -match $forbiddenPattern
})
if ($forbidden.Count) { throw 'A local or sensitive file entered the release package.' }

Compress-Archive -LiteralPath $package -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash
[IO.File]::WriteAllText(
    $zipChecksum,
    "$hash  $([IO.Path]::GetFileName($zip))`r`n",
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Release candidate: $zip"
Write-Host "SHA-256: $hash"
