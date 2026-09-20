param(
    [string]$SourcePng = (Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\icon.png'),
    [string]$OutputPath = (Join-Path $PSScriptRoot 'app.ico')
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing

$sourcePath = [IO.Path]::GetFullPath($SourcePng)
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Icon source PNG not found: $sourcePath"
}

$outputDirectory = Split-Path -Parent $resolvedOutput
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$images = New-Object System.Collections.Generic.List[byte[]]

$source = [System.Drawing.Image]::FromFile($sourcePath)
try {
    foreach ($size in $sizes) {
        $bitmap = New-Object System.Drawing.Bitmap(
            $size,
            $size,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )

        try {
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.Clear([System.Drawing.Color]::Transparent)
                $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
                $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.DrawImage(
                    $source,
                    (New-Object System.Drawing.Rectangle(0, 0, $size, $size))
                )
            }
            finally {
                $graphics.Dispose()
            }

            $stream = New-Object IO.MemoryStream
            try {
                $bitmap.Save(
                    $stream,
                    [System.Drawing.Imaging.ImageFormat]::Png
                )
                $images.Add($stream.ToArray())
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $bitmap.Dispose()
        }
    }
}
finally {
    $source.Dispose()
}

$file = [IO.File]::Open(
    $resolvedOutput,
    [IO.FileMode]::Create,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
$writer = New-Object IO.BinaryWriter($file)

try {
    # ICONDIR
    $writer.Write([UInt16]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]$sizes.Count)

    $offset = 6 + (16 * $sizes.Count)

    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $size = $sizes[$i]
        $data = $images[$i]

        $dimension = if ($size -eq 256) { 0 } else { $size }

        $writer.Write([Byte]$dimension)
        $writer.Write([Byte]$dimension)
        $writer.Write([Byte]0)
        $writer.Write([Byte]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]32)
        $writer.Write([UInt32]$data.Length)
        $writer.Write([UInt32]$offset)

        $offset += $data.Length
    }

    foreach ($data in $images) {
        $writer.Write($data)
    }
}
finally {
    $writer.Dispose()
    $file.Dispose()
}

if (-not (Test-Path -LiteralPath $resolvedOutput)) {
    throw "ICO file was not created: $resolvedOutput"
}

Write-Host "Icon built: $resolvedOutput"
