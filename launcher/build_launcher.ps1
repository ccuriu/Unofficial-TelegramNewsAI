param(
    [string]$OutputPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'Telegram_Digest.exe')
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'Telegram_Digest.cs'
$icon = Join-Path $PSScriptRoot 'TelegramNewsAI.ico'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

foreach ($required in @($source, $icon, $compiler)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher build file not found: $required"
    }
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

& $compiler /nologo /target:winexe /optimize+ /platform:anycpu `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    "/win32icon:$icon" "/out:$resolvedOutput" $source
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed with exit code $LASTEXITCODE."
}

Write-Host "Launcher built: $resolvedOutput"

