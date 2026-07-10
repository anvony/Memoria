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

# Faces + semantic search (torch/insightface/CLIP, ~2 GB) are NOT installed here.
# They install on demand the first time you turn on "Faces & semantic search" in
# the app's Settings -- every one of those packages is a prebuilt wheel, so
# Python stays the ONLY prerequisite and no C++ build tools are ever needed.
# (-SkipML is accepted for backward compatibility but is now a no-op.)

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
Write-Host "Backend ready." -ForegroundColor Green
Write-Host "Faces & semantic search install on demand from the app's Settings."
Write-Host "The Memoria app launches this automatically -- just open Memoria.exe."
