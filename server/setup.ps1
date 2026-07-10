# One-time backend setup: creates the Python environment Memoria runs from.
# Run from the server/ directory:  powershell -ExecutionPolicy Bypass -File setup.ps1
#
# -SkipML installs only the core (no faces / semantic search) — several GB smaller.

param([switch]$SkipML)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "Installing core dependencies..." -ForegroundColor Cyan
& .\.venv\Scripts\pip.exe install -r requirements.txt

$mlOk = $true
if (-not $SkipML) {
    # Faces + semantic search are a bonus, not the core app. insightface compiles
    # native code and needs the MSVC "Desktop development with C++" build tools,
    # which most users won't have. So we install it non-fatally: if it fails, the
    # core app (timeline, albums, places, duplicates, HEIC/video thumbnails) still
    # works fully -- only face grouping and "beach sunset" search are unavailable.
    try {
        Write-Host "Installing torch (CPU build)..." -ForegroundColor Cyan
        & .\.venv\Scripts\pip.exe install torch --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) { throw "torch install failed" }

        Write-Host "Installing ML dependencies (faces + semantic search)..." -ForegroundColor Cyan
        Write-Host "  (insightface compiles native code - needs 'Desktop development with C++' build tools)" -ForegroundColor DarkGray
        & .\.venv\Scripts\pip.exe install -r requirements-ml.txt
        if ($LASTEXITCODE -ne 0) { throw "ML dependency install failed" }
    } catch {
        $mlOk = $false
        Write-Host ""
        Write-Host "  Face grouping / semantic search could not be installed:" -ForegroundColor Yellow
        Write-Host "    $($_.Exception.Message)" -ForegroundColor DarkGray
        Write-Host "  This usually means the C++ build tools are missing. The rest of" -ForegroundColor DarkGray
        Write-Host "  Memoria still works. To add these features later, install" -ForegroundColor DarkGray
        Write-Host "  'Desktop development with C++' from the Visual Studio Build Tools" -ForegroundColor DarkGray
        Write-Host "  and re-run this setup." -ForegroundColor DarkGray
        Write-Host ""
    }
}

# External command-line tools (ffmpeg + ExifTool) are native binaries, NOT pip
# packages, so we download them here into tools\ — that way a first-time user
# needs nothing but the installer and this setup (no manual winget/scoop). They
# require an internet connection; each step is idempotent (skips if already
# present) and non-fatal (a failure warns with the manual fallback, and the app
# still runs — ffmpeg-less video posters and ExifTool-less original edits just
# degrade). Downloads land on the user's own machine, so nothing is rehosted.
$ProgressPreference = "SilentlyContinue"   # IE-style progress bar makes big downloads crawl on PS 5.1
$tools = Join-Path $PSScriptRoot "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null

Write-Host ""
Write-Host "Setting up external tools (needs internet)..." -ForegroundColor Cyan

# --- ffmpeg + ffprobe (video posters and metadata) ---
# The app resolves ffmpeg as: env override -> tools\ffmpeg.exe -> PATH
# (memoria/tools.py). So we only need to download it if it is in NONE of those
# places. If the user already has ffmpeg installed on their PC (on PATH), we
# leave it alone and the app uses their copy.
$ffmpegOnPath = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)
if (Test-Path (Join-Path $tools "ffmpeg.exe")) {
    Write-Host "  ffmpeg already in tools\, skipping." -ForegroundColor DarkGray
} elseif ($ffmpegOnPath) {
    Write-Host "  ffmpeg already installed on this PC (on PATH), skipping download." -ForegroundColor DarkGray
} else {
    try {
        Write-Host "  Downloading ffmpeg..." -ForegroundColor Cyan
        $zip = Join-Path $env:TEMP "memoria-ffmpeg.zip"
        $out = Join-Path $env:TEMP "memoria-ffmpeg"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
        if (Test-Path $out) { Remove-Item -Recurse -Force $out }
        Expand-Archive -Path $zip -DestinationPath $out -Force
        $ff = Get-ChildItem -Path $out -Recurse -Filter "ffmpeg.exe"  | Select-Object -First 1
        $fp = Get-ChildItem -Path $out -Recurse -Filter "ffprobe.exe" | Select-Object -First 1
        Copy-Item $ff.FullName (Join-Path $tools "ffmpeg.exe")  -Force
        Copy-Item $fp.FullName (Join-Path $tools "ffprobe.exe") -Force
        Remove-Item -Recurse -Force $zip, $out
        Write-Host "  ffmpeg + ffprobe installed." -ForegroundColor Green
    } catch {
        Write-Host "  Could not download ffmpeg: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "  Video thumbnails will be skipped. Fallback: 'winget install Gyan.FFmpeg'." -ForegroundColor DarkGray
    }
}

# --- ExifTool (the opt-in 'edit the original file' metadata write) ---
# Normally ExifTool ships committed in tools\, so this just skips. The download
# is a safety net if that copy is ever missing. The Windows package lives on
# SourceForge; master.dl is the direct-file host that bypasses the interstitial.
if (Test-Path (Join-Path $tools "exiftool.exe")) {
    Write-Host "  ExifTool already in tools\, skipping." -ForegroundColor DarkGray
} else {
    try {
        Write-Host "  Downloading ExifTool..." -ForegroundColor Cyan
        $ver = (Invoke-WebRequest -UseBasicParsing -Uri "https://exiftool.org/ver.txt").Content.Trim()
        $zip = Join-Path $env:TEMP "memoria-exiftool.zip"
        $out = Join-Path $env:TEMP "memoria-exiftool"
        Invoke-WebRequest -UseBasicParsing -Uri "https://master.dl.sourceforge.net/project/exiftool/exiftool-${ver}_64.zip?viasf=1" -OutFile $zip
        if (Test-Path $out) { Remove-Item -Recurse -Force $out }
        Expand-Archive -Path $zip -DestinationPath $out -Force
        # The Windows package ships 'exiftool(-k).exe' (the -k pauses on double-
        # click); renamed to exiftool.exe it runs as a plain CLI. It needs the
        # sibling exiftool_files\ folder alongside it.
        $exe   = Get-ChildItem -Path $out -Recurse -Filter "exiftool(-k).exe" | Select-Object -First 1
        $files = Get-ChildItem -Path $out -Recurse -Directory -Filter "exiftool_files" | Select-Object -First 1
        Copy-Item $exe.FullName (Join-Path $tools "exiftool.exe") -Force
        $filesDst = Join-Path $tools "exiftool_files"
        if (Test-Path $filesDst) { Remove-Item -Recurse -Force $filesDst }
        Copy-Item $files.FullName $filesDst -Recurse -Force
        Remove-Item -Recurse -Force $zip, $out
        Write-Host "  ExifTool $ver installed." -ForegroundColor Green
    } catch {
        Write-Host "  Could not download ExifTool: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "  The 'edit the original file' toggle stays off; edits remain catalogue-only." -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($mlOk) {
    Write-Host "Backend ready (all features)." -ForegroundColor Green
} else {
    Write-Host "Backend ready (core features; face grouping / semantic search skipped)." -ForegroundColor Green
}
Write-Host "The Memoria app launches this automatically -- just open Memoria.exe."
