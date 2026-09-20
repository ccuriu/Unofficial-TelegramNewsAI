param(
    [string]$OutputPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'Telegram_Digest.exe')
)

$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'Telegram_Digest.cs'
$iconBuilder = Join-Path $PSScriptRoot 'build_icon.ps1'
$iconSource = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\icon.png'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

foreach ($required in @($source, $iconBuilder, $iconSource, $compiler)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher build file not found: $required"
    }
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput

if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$iconPath = Join-Path $env:TEMP (
    "Unofficial-TelegramNewsAI-" +
    [guid]::NewGuid().ToString('N') +
    ".ico"
)

try {
    & $iconBuilder -SourcePng $iconSource -OutputPath $iconPath

    if (-not (Test-Path -LiteralPath $iconPath)) {
        throw "Launcher icon was not created: $iconPath"
    }

    & $compiler /nologo /target:winexe /optimize+ /platform:anycpu `
        /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
        "/win32icon:$iconPath" "/out:$resolvedOutput" $source

    if ($LASTEXITCODE -ne 0) {
        throw "Launcher compilation failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item -LiteralPath $iconPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Launcher built with custom icon: $resolvedOutput"
