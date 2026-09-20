param(
    [string]$OutputPath = (Join-Path $PSScriptRoot 'app.ico'),
    [string]$PreviewPng = ''
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing

function New-RoundedRectanglePath {
    param(
        [float]$X,
        [float]$Y,
        [float]$Width,
        [float]$Height,
        [float]$Radius
    )

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $diameter = [Math]::Max(1.0, $Radius * 2.0)

    $path.AddArc($X, $Y, $diameter, $diameter, 180, 90)
    $path.AddArc($X + $Width - $diameter, $Y, $diameter, $diameter, 270, 90)
    $path.AddArc(
        $X + $Width - $diameter,
        $Y + $Height - $diameter,
        $diameter,
        $diameter,
        0,
        90
    )
    $path.AddArc($X, $Y + $Height - $diameter, $diameter, $diameter, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-MasterIcon {
    $bitmap = New-Object System.Drawing.Bitmap(
        512,
        512,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    )

    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality

        # Dark rounded-square tile.
        $background = New-RoundedRectanglePath 18 18 476 476 94
        try {
            $bgBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
                (New-Object System.Drawing.PointF(30, 30)),
                (New-Object System.Drawing.PointF(480, 480)),
                ([System.Drawing.Color]::FromArgb(255, 4, 22, 50)),
                ([System.Drawing.Color]::FromArgb(255, 4, 47, 102))
            )
            try {
                $graphics.FillPath($bgBrush, $background)
            }
            finally {
                $bgBrush.Dispose()
            }

            $borderPen = New-Object System.Drawing.Pen(
                ([System.Drawing.Color]::FromArgb(210, 27, 111, 224)),
                4
            )
            try {
                $graphics.DrawPath($borderPen, $background)
            }
            finally {
                $borderPen.Dispose()
            }
        }
        finally {
            $background.Dispose()
        }

        # Network/radar ring.
        $ringPen = New-Object System.Drawing.Pen(
            ([System.Drawing.Color]::FromArgb(150, 18, 105, 220)),
            7
        )
        $cyanPen = New-Object System.Drawing.Pen(
            ([System.Drawing.Color]::FromArgb(255, 72, 235, 255)),
            16
        )
        $innerPen = New-Object System.Drawing.Pen(
            ([System.Drawing.Color]::FromArgb(235, 168, 250, 255)),
            7
        )
        try {
            $graphics.DrawEllipse($ringPen, 99, 79, 314, 314)
            $graphics.DrawEllipse($cyanPen, 143, 123, 226, 226)
            $graphics.DrawEllipse($innerPen, 174, 154, 164, 164)
        }
        finally {
            $ringPen.Dispose()
            $cyanPen.Dispose()
            $innerPen.Dispose()
        }

        # Lens / eye.
        $irisBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
            (New-Object System.Drawing.PointF(190, 170)),
            (New-Object System.Drawing.PointF(325, 305)),
            ([System.Drawing.Color]::FromArgb(255, 83, 241, 255)),
            ([System.Drawing.Color]::FromArgb(255, 18, 92, 231))
        )
        try {
            $graphics.FillEllipse($irisBrush, 190, 170, 132, 132)
        }
        finally {
            $irisBrush.Dispose()
        }

        $pupilBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(255, 4, 22, 48)
        )
        $highlightBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::White
        )
        try {
            $graphics.FillEllipse($pupilBrush, 226, 206, 60, 60)
            $graphics.FillEllipse($highlightBrush, 272, 192, 20, 20)
        }
        finally {
            $pupilBrush.Dispose()
            $highlightBrush.Dispose()
        }

        # Radial nodes: readable even in small Windows icon sizes.
        $nodePen = New-Object System.Drawing.Pen(
            ([System.Drawing.Color]::FromArgb(245, 72, 235, 255)),
            7
        )
        $nodeBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(255, 72, 235, 255)
        )
        try {
            $graphics.DrawLine($nodePen, 256, 72, 256, 101)
            $graphics.DrawLine($nodePen, 137, 112, 158, 133)
            $graphics.DrawLine($nodePen, 375, 112, 354, 133)
            $graphics.DrawLine($nodePen, 83, 236, 113, 236)
            $graphics.DrawLine($nodePen, 429, 236, 399, 236)

            foreach ($point in @(
                @(256, 63),
                @(130, 105),
                @(382, 105),
                @(74, 236),
                @(438, 236)
            )) {
                $graphics.FillEllipse($nodeBrush, $point[0] - 9, $point[1] - 9, 18, 18)
            }
        }
        finally {
            $nodePen.Dispose()
            $nodeBrush.Dispose()
        }

        # Compact digest card in front of the lens.
        $shadow = New-RoundedRectanglePath 132 333 264 94 24
        $card = New-RoundedRectanglePath 112 309 288 105 25
        try {
            $shadowBrush = New-Object System.Drawing.SolidBrush(
                [System.Drawing.Color]::FromArgb(210, 14, 137, 221)
            )
            $cardBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
                (New-Object System.Drawing.PointF(112, 309)),
                (New-Object System.Drawing.PointF(112, 414)),
                ([System.Drawing.Color]::FromArgb(255, 255, 255, 255)),
                ([System.Drawing.Color]::FromArgb(255, 210, 238, 255))
            )
            try {
                $graphics.FillPath($shadowBrush, $shadow)
                $graphics.FillPath($cardBrush, $card)
            }
            finally {
                $shadowBrush.Dispose()
                $cardBrush.Dispose()
            }
        }
        finally {
            $shadow.Dispose()
            $card.Dispose()
        }

        # Thumbnail and three bold article lines.
        $thumb = New-RoundedRectanglePath 136 332 69 60 12
        try {
            $thumbBrush = New-Object System.Drawing.SolidBrush(
                [System.Drawing.Color]::FromArgb(255, 19, 87, 190)
            )
            $graphics.FillPath($thumbBrush, $thumb)
            $thumbBrush.Dispose()
        }
        finally {
            $thumb.Dispose()
        }

        $accentBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(255, 91, 209, 255)
        )
        $lineBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(255, 18, 83, 176)
        )
        try {
            $graphics.FillEllipse($accentBrush, 183, 342, 8, 8)
            $mountain = @(
                (New-Object System.Drawing.PointF(141, 384)),
                (New-Object System.Drawing.PointF(160, 360)),
                (New-Object System.Drawing.PointF(176, 377)),
                (New-Object System.Drawing.PointF(188, 364)),
                (New-Object System.Drawing.PointF(201, 384))
            )
            $graphics.FillPolygon($accentBrush, $mountain)

            $graphics.FillRectangle($lineBrush, 226, 337, 137, 13)
            $graphics.FillRectangle($lineBrush, 226, 361, 123, 13)
            $graphics.FillRectangle($lineBrush, 226, 385, 91, 13)
        }
        finally {
            $accentBrush.Dispose()
            $lineBrush.Dispose()
        }

        return $bitmap
    }
    finally {
        $graphics.Dispose()
    }
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$images = New-Object System.Collections.Generic.List[byte[]]
$master = New-MasterIcon

try {
    if ($PreviewPng) {
        $previewPath = [IO.Path]::GetFullPath($PreviewPng)
        $previewDirectory = Split-Path -Parent $previewPath
        if (-not (Test-Path -LiteralPath $previewDirectory)) {
            New-Item -ItemType Directory -Path $previewDirectory -Force | Out-Null
        }
        $master.Save($previewPath, [System.Drawing.Imaging.ImageFormat]::Png)
    }

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
                    $master,
                    (New-Object System.Drawing.Rectangle(0, 0, $size, $size))
                )
            }
            finally {
                $graphics.Dispose()
            }

            $stream = New-Object IO.MemoryStream
            try {
                $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
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
    $master.Dispose()
}

$file = [IO.File]::Open(
    $resolvedOutput,
    [IO.FileMode]::Create,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
$writer = New-Object IO.BinaryWriter($file)

try {
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
