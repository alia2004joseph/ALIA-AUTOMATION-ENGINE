param(
    [Parameter(Position = 0)]
    [string]$PdfPath
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($PdfPath)) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Filter = "PDF files (*.pdf)|*.pdf"
    $dialog.Title = "Select an engineering drawing PDF"
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        $dialog.Dispose()
        Write-Host "No PDF selected."
        exit 0
    }
    $PdfPath = $dialog.FileName
    $dialog.Dispose()
}

$pdf = Get-Item -LiteralPath $PdfPath
if ($pdf.Extension -ne ".pdf") {
    throw "Input must be a PDF file: $($pdf.FullName)"
}

$outputDir = Join-Path $pdf.DirectoryName "$($pdf.BaseName)-raster-analysis"
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$reportPath = Join-Path $outputDir "report.json"
$mainPath = Join-Path $PSScriptRoot "main.py"
$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $workspacePython = Join-Path $PSScriptRoot "..\..\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $workspacePython) {
        $pythonPath = $workspacePython
    } else {
        $pythonPath = (Get-Command python -ErrorAction Stop).Source
    }
}

& $pythonPath $mainPath --inspect-pdf $pdf.FullName --analyze-raster --output $reportPath
if ($LASTEXITCODE -ne 0) {
    throw "PDF analysis failed with exit code $LASTEXITCODE."
}

$overlays = @(
    Get-ChildItem -LiteralPath $outputDir -Filter "page-*-raster-overlay.png" |
        Sort-Object Name
)
if ($overlays.Count -eq 0) {
    throw "Analysis report saved to '$reportPath', but no raster overlay was produced. The PDF may not contain embedded raster images."
}

Write-Host "Analysis report: $reportPath"
Write-Host "Opening overlay: $($overlays[0].FullName)"
Invoke-Item -LiteralPath $overlays[0].FullName
if ($overlays.Count -gt 1) {
    Write-Host "Additional page overlays:"
    $overlays | Select-Object -Skip 1 | ForEach-Object { Write-Host "  $($_.FullName)" }
}
