# One-time backend setup: creates the Python environment Memoria runs from,
# fetches the external tools, and puts shortcuts on the Desktop / Start Menu.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Normally launched by setup.cmd in the folder above this one.
#
# The console shows a header and ONE progress bar -- nothing else. Every command's
# real output goes to setup-log.txt next to Memoria.exe, and is only shown if
# something fails. Same idea as the in-app installer (memoria/api.py `_run_pip`):
# quiet when it works, detailed when it doesn't.
#
#   -ShowOutput  stream the raw output instead of the bar (for debugging)
#   -SkipML      accepted for backward compatibility; a no-op (faces and semantic
#                search install on demand from the app's Settings)

param([switch]$SkipML, [switch]$ShowOutput)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$appRoot = Split-Path -Parent $PSScriptRoot          # the folder holding Memoria.exe
$log     = Join-Path $appRoot "setup-log.txt"
$tools   = Join-Path $PSScriptRoot "tools"
$script:Stage = "starting"

"Memoria setup - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Set-Content -Path $log -Encoding utf8

# ---------------------------------------------------------------- the bar ----
# No ANSI escape codes anywhere: VT processing isn't reliably enabled in the
# classic Windows console, and a fresh Windows 10 box is exactly where it isn't.
# Carriage-return redraw + Write-Host colours work on every Windows console.
# ASCII bar characters for the same reason -- a console font without box-drawing
# glyphs would render a row of question marks.

function Get-LineWidth {
    try { $w = $Host.UI.RawUI.WindowSize.Width - 1 } catch { $w = 79 }
    if ($w -lt 40) { return 40 }
    return $w
}

function Write-Bar {
    param([double]$Fraction, [string]$Label)
    if ($ShowOutput) { return }
    $f = [Math]::Max(0.0, [Math]::Min(1.0, $Fraction))
    $slots = 20
    $filled = [int][Math]::Round($slots * $f)
    $bar = ('#' * $filled) + ('.' * ($slots - $filled))
    $line = "  [$bar] {0,3}%  $Label" -f [int][Math]::Round($f * 100)
    $max = Get-LineWidth
    if ($line.Length -gt $max) { $line = $line.Substring(0, $max) } else { $line = $line.PadRight($max) }
    Write-Host "`r$line" -NoNewline -ForegroundColor Cyan
}

function Clear-Bar {
    if ($ShowOutput) { return }
    Write-Host "`r$(' ' * (Get-LineWidth))`r" -NoNewline
}

function Write-Note {
    # A message that has to survive on screen: wipe the bar, print, redraw.
    param([string]$Text, [string]$Colour = "DarkGray", [double]$Fraction, [string]$Label)
    Clear-Bar
    Write-Host "  $Text" -ForegroundColor $Colour
    Write-Bar $Fraction $Label
}

function Add-Log {
    param([string]$Text)
    Add-Content -Path $log -Value $Text -Encoding utf8
}

# ------------------------------------------------------- running a command ----
# pip can't tell us a percentage, so each stage owns a slice of the bar and
# creeps through it on an exponential curve: always moving, never overshooting
# its own end point. `Detail` optionally pulls the current package name out of
# the log tail so the line reads "Installing core packages - pillow-heif".

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Label,
        [double]$From,
        [double]$To,
        [double]$ExpectSec = 60,
        [scriptblock]$Detail
    )
    $script:Stage = $Label
    Add-Log ""
    Add-Log "===== $Label ====="
    Add-Log "> $FilePath $($Arguments -join ' ')"

    if ($ShowOutput) {
        Write-Host ">> $Label" -ForegroundColor Cyan
        & $FilePath @Arguments 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit code $LASTEXITCODE)" }
        return
    }

    # Start-Process needs two distinct files; they're merged into the log after.
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -NoNewWindow -PassThru `
                          -RedirectStandardOutput $out -RedirectStandardError $err
    # Touching .Handle caches the process handle. Without this, .ExitCode reads
    # back as $null once the process has exited and every step "fails" with an
    # empty exit code -- a long-standing Start-Process -PassThru quirk.
    $null = $proc.Handle

    while (-not $proc.HasExited) {
        $suffix = ""
        if ($Detail) {
            try {
                $tail = Get-Content $out -Tail 40 -ErrorAction SilentlyContinue
                $suffix = & $Detail $tail
            } catch { }
        }
        $elapsed = $sw.Elapsed.TotalSeconds
        Write-Bar ($From + ($To - $From) * (1 - [Math]::Exp(-$elapsed / $ExpectSec))) "$Label$suffix"
        Start-Sleep -Milliseconds 250
    }
    $proc.WaitForExit()

    foreach ($f in @($out, $err)) {
        Get-Content $f -ErrorAction SilentlyContinue | ForEach-Object { Add-Log $_ }
    }
    Remove-Item $out, $err -Force -ErrorAction SilentlyContinue
    Write-Bar $To $Label
    if ($proc.ExitCode -ne 0) { throw "$Label failed (exit code $($proc.ExitCode))" }
}

# Pulls the package pip is currently working on out of its output.
$pipDetail = {
    param($tail)
    if (-not $tail) { return "" }
    $line = $tail | Where-Object { $_ -match '^(Collecting|Downloading|Installing collected packages)' } |
            Select-Object -Last 1
    if (-not $line) { return "" }
    if ($line -match '^Installing collected packages') { return " - finishing up" }
    if ($line -match '^Collecting\s+([A-Za-z0-9_.\-]+)') { return " - $($Matches[1])" }
    if ($line -match '^Downloading\s+([A-Za-z0-9_.\-]+)')  { return " - $($Matches[1])" }
    return ""
}

# Downloads with a real percentage when the server reports a size (the ffmpeg
# archive is ~110 MB -- by far the longest wait, so it's worth the extra work).
function Get-FileWithProgress {
    param([string]$Uri, [string]$OutFile, [string]$Label, [double]$From, [double]$To)
    $script:Stage = $Label
    $total = 0
    try {
        $req = [System.Net.HttpWebRequest]::Create($Uri)
        $req.Method = "HEAD"
        $resp = $req.GetResponse()
        $total = [int64]$resp.ContentLength
        $resp.Close()
    } catch { $total = 0 }   # no size available: fall back to the time-based creep

    $wc = New-Object System.Net.WebClient
    $task = $wc.DownloadFileTaskAsync($Uri, $OutFile)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $task.IsCompleted) {
        if ($total -gt 0 -and (Test-Path $OutFile)) {
            $have = (Get-Item $OutFile).Length
            $mb = "{0:N0} of {1:N0} MB" -f ($have / 1MB), ($total / 1MB)
            Write-Bar ($From + ($To - $From) * ([double]$have / $total)) "$Label - $mb"
        } else {
            $e = $sw.Elapsed.TotalSeconds
            Write-Bar ($From + ($To - $From) * (1 - [Math]::Exp(-$e / 45))) $Label
        }
        Start-Sleep -Milliseconds 250
    }
    $wc.Dispose()
    if ($task.IsFaulted) { throw $task.Exception.GetBaseException() }
    Write-Bar $To $Label
}

# ------------------------------------------------------------------ setup ----

Write-Host ""
Write-Host "============================================================" -ForegroundColor DarkGray
Write-Host "   Memoria - one-time setup" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Setting up Memoria. This takes a few minutes." -ForegroundColor White
Write-Host "  Please don't close this window." -ForegroundColor Yellow
Write-Host ""

try {
    Write-Bar 0.0 "Preparing"

    # --- Python version ------------------------------------------------------
    # setup.cmd's `where python` only proves SOME python is on PATH, not which.
    # That gap is real: the README says "Python 3.12" but links to
    # python.org/downloads, which now serves 3.14 -- so a user following the
    # instructions exactly can end up on a version this was never tested against.
    # Warn rather than block: a newer Python usually works fine, and refusing to
    # install would strand people over a version that's probably OK. The import
    # check at the end of setup is what actually proves the environment works.
    $script:Stage = "Checking Python"
    $pyVer = ""
    try {
        $pyVer = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1 |
                  Select-Object -First 1).ToString().Trim()
    } catch { }
    Add-Log "Python on PATH reports version '$pyVer'."
    if ($pyVer -and ($pyVer -notin @("3.12", "3.13"))) {
        Write-Note -Text "You have Python $pyVer. Memoria is built and tested on Python 3.12." `
                   -Colour Yellow -Fraction 0.0 -Label "Checking Python"
        Write-Note -Text "Setup will carry on. If it fails, install 3.12 from python.org and run setup.cmd again." `
                   -Colour DarkGray -Fraction 0.0 -Label "Checking Python"
    }

    # --- Python environment -------------------------------------------------
    Invoke-Logged -FilePath "python" -Arguments @("-m", "venv", ".venv") `
        -Label "Creating Memoria's Python environment" -From 0.00 -To 0.08 -ExpectSec 20

    Invoke-Logged -FilePath ".\.venv\Scripts\python.exe" `
        -Arguments @("-m", "pip", "install", "--upgrade", "pip") `
        -Label "Updating the package installer" -From 0.08 -To 0.15 -ExpectSec 25

    Invoke-Logged -FilePath ".\.venv\Scripts\pip.exe" `
        -Arguments @("install", "-r", "requirements.txt") `
        -Label "Installing core packages" -From 0.15 -To 0.70 -ExpectSec 90 -Detail $pipDetail

    # Faces + semantic search (torch/insightface/CLIP, ~2 GB) are NOT installed
    # here. They install on demand the first time you turn on "Faces & semantic
    # search" in the app's Settings -- every one of those packages is a prebuilt
    # wheel, so Python stays the ONLY prerequisite and no C++ build tools are
    # ever needed. (-SkipML is accepted for backward compatibility; it's a no-op.)

    # --- Visual C++ runtime -------------------------------------------------
    # onnxruntime and opencv (pulled in by insightface, when the user turns on
    # faces + semantic search) are C++ and link against msvcp140.dll. Python's
    # own installer ships vcruntime140.dll app-local but NOT msvcp140.dll -- it's
    # a C program and doesn't need the C++ standard library. So a machine can
    # have a perfectly good Python and still fail to import onnxruntime with
    # "DLL load failed". Windows 11 ships the runtime; a clean Windows 10 often
    # doesn't, which is exactly where this bites.
    #
    # Redistributing vc_redist.x64.exe is expressly permitted by the Visual
    # Studio licence terms, so we fetch and install it rather than making the
    # user hunt for it. Non-fatal: the base app never needs it, only ML does.
    $script:Stage = "Checking the Visual C++ runtime"
    $needVC = -not (Test-Path (Join-Path $env:SystemRoot "System32\msvcp140.dll"))
    if (-not $needVC) {
        Add-Log "Visual C++ runtime already present (msvcp140.dll found)."
        Write-Bar 0.74 "Visual C++ runtime already installed"
    } else {
        Add-Log "msvcp140.dll missing - installing the Visual C++ runtime."
        try {
            $vc = Join-Path $env:TEMP "memoria-vc_redist.x64.exe"
            Get-FileWithProgress -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" `
                -OutFile $vc -Label "Downloading the Visual C++ runtime" -From 0.70 -To 0.73
            # This installer always elevates, so an unelevated setup.cmd gets a
            # UAC prompt here. Warn first -- an unexplained prompt mid-progress-bar
            # reads as malware.
            Write-Note -Text "Windows will ask for permission to install the Visual C++ runtime - please click Yes." `
                       -Colour Yellow -Fraction 0.73 -Label "Installing the Visual C++ runtime"
            Invoke-Logged -FilePath $vc -Arguments @("/install", "/quiet", "/norestart") `
                -Label "Installing the Visual C++ runtime" -From 0.73 -To 0.74 -ExpectSec 40
            Add-Log "Visual C++ runtime installed."
        } catch {
            Add-Log "Visual C++ runtime install failed: $($_.Exception.Message)"
            Write-Note -Text "Could not install the Visual C++ runtime - faces & semantic search may not start." `
                       -Colour Yellow -Fraction 0.74 -Label "Continuing without it"
            Write-Note -Text "Install it later from https://aka.ms/vs/17/release/vc_redist.x64.exe" `
                       -Colour DarkGray -Fraction 0.74 -Label "Continuing without it"
        }
    }

    # --- External tools -----------------------------------------------------
    # ffmpeg and ExifTool are native binaries, not pip packages, so we fetch them
    # here rather than making a first-time user install anything by hand. Both
    # steps are idempotent (skip if present) and non-fatal (the app still runs;
    # video posters and original-file edits just degrade).
    $ProgressPreference = "SilentlyContinue"   # PS 5.1's own bar makes big downloads crawl
    New-Item -ItemType Directory -Force -Path $tools | Out-Null

    # ffmpeg + ffprobe (video posters and metadata). The app resolves ffmpeg as
    # env override -> tools\ffmpeg.exe -> PATH (memoria/tools.py), so if the user
    # already has it installed we leave their copy alone.
    $script:Stage = "Setting up video support"
    if (Test-Path (Join-Path $tools "ffmpeg.exe")) {
        Add-Log "ffmpeg already in tools\, skipping."
        Write-Bar 0.92 "Video support already installed"
    } elseif (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Add-Log "ffmpeg found on PATH, skipping download."
        Write-Bar 0.92 "Using the ffmpeg already on this PC"
    } else {
        try {
            $zip = Join-Path $env:TEMP "memoria-ffmpeg.zip"
            $out = Join-Path $env:TEMP "memoria-ffmpeg"
            Get-FileWithProgress -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
                -OutFile $zip -Label "Downloading video support" -From 0.74 -To 0.88
            Write-Bar 0.89 "Unpacking video support"
            if (Test-Path $out) { Remove-Item -Recurse -Force $out }
            Expand-Archive -Path $zip -DestinationPath $out -Force
            $ff = Get-ChildItem -Path $out -Recurse -Filter "ffmpeg.exe"  | Select-Object -First 1
            $fp = Get-ChildItem -Path $out -Recurse -Filter "ffprobe.exe" | Select-Object -First 1
            Copy-Item $ff.FullName (Join-Path $tools "ffmpeg.exe")  -Force
            Copy-Item $fp.FullName (Join-Path $tools "ffprobe.exe") -Force
            Remove-Item -Recurse -Force $zip, $out
            Add-Log "ffmpeg + ffprobe installed."
            Write-Bar 0.92 "Video support installed"
        } catch {
            Add-Log "ffmpeg download failed: $($_.Exception.Message)"
            Write-Note -Text "Could not download video support - video thumbnails will be skipped." `
                -Colour Yellow -Fraction 0.92 -Label "Continuing"
        }
    }

    # ExifTool normally ships in tools\, so this just skips; the download is a
    # safety net if that copy is ever missing.
    $script:Stage = "Setting up the metadata tool"
    if (Test-Path (Join-Path $tools "exiftool.exe")) {
        Add-Log "ExifTool already in tools\, skipping."
        Write-Bar 0.96 "Metadata tool already installed"
    } else {
        try {
            $ver = (Invoke-WebRequest -UseBasicParsing -Uri "https://exiftool.org/ver.txt").Content.Trim()
            $zip = Join-Path $env:TEMP "memoria-exiftool.zip"
            $out = Join-Path $env:TEMP "memoria-exiftool"
            Get-FileWithProgress -Uri "https://master.dl.sourceforge.net/project/exiftool/exiftool-${ver}_64.zip?viasf=1" `
                -OutFile $zip -Label "Downloading the metadata tool" -From 0.92 -To 0.95
            if (Test-Path $out) { Remove-Item -Recurse -Force $out }
            Expand-Archive -Path $zip -DestinationPath $out -Force
            # The Windows package ships 'exiftool(-k).exe' (-k pauses on double-
            # click); renamed to exiftool.exe it runs as a plain CLI. It needs the
            # sibling exiftool_files\ folder alongside it.
            $exe   = Get-ChildItem -Path $out -Recurse -Filter "exiftool(-k).exe" | Select-Object -First 1
            $files = Get-ChildItem -Path $out -Recurse -Directory -Filter "exiftool_files" | Select-Object -First 1
            Copy-Item $exe.FullName (Join-Path $tools "exiftool.exe") -Force
            $filesDst = Join-Path $tools "exiftool_files"
            if (Test-Path $filesDst) { Remove-Item -Recurse -Force $filesDst }
            Copy-Item $files.FullName $filesDst -Recurse -Force
            Remove-Item -Recurse -Force $zip, $out
            Add-Log "ExifTool $ver installed."
            Write-Bar 0.96 "Metadata tool installed"
        } catch {
            Add-Log "ExifTool download failed: $($_.Exception.Message)"
            Write-Note -Text "Could not download the metadata tool - edits stay inside Memoria only." `
                -Colour Yellow -Fraction 0.96 -Label "Continuing"
        }
    }

    # --- Shortcuts ----------------------------------------------------------
    $script:Stage = "Creating shortcuts"
    Write-Bar 0.97 "Creating shortcuts"
    $exePath = Join-Path $appRoot "Memoria.exe"
    if (Test-Path $exePath) {
        try {
            $ws = New-Object -ComObject WScript.Shell
            $targets = @(
                [Environment]::GetFolderPath('Desktop'),
                (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
            )
            foreach ($dir in $targets) {
                $lnk = $ws.CreateShortcut((Join-Path $dir 'Memoria.lnk'))
                $lnk.TargetPath = $exePath
                $lnk.WorkingDirectory = $appRoot
                $lnk.Description = 'Memoria - your photos, on your machine'
                $lnk.Save()
            }
            Add-Log "Shortcuts created on the Desktop and in the Start Menu."
        } catch {
            Add-Log "Shortcut creation failed: $($_.Exception.Message)"
            Write-Note -Text "Could not create shortcuts - open Memoria.exe from this folder instead." `
                -Colour Yellow -Fraction 0.99 -Label "Finishing"
        }
    } else {
        Add-Log "Memoria.exe not found next to server\, skipping shortcuts."
    }

    # --- Does it actually run? ----------------------------------------------
    # Everything above can "succeed" and still leave an engine that can't start:
    # pip reports 0 while a half-written file sits on disk (an interrupted
    # install, a VM snapshot rollback, an antivirus quarantine), and the failure
    # only shows up at first launch -- in a console window that closes in three
    # seconds, so the user sees the app silently fall back to demo mode.
    #
    # Importing the app is the cheapest proof the environment is real. It costs
    # about a second and turns an invisible first-launch failure into a setup
    # failure with the traceback already in setup-log.txt. `memoria.api` does no
    # work at import time beyond building the router, so this needs no library
    # or data folder to exist yet.
    $script:Stage = "Checking the installation"
    try {
        Invoke-Logged -FilePath ".\.venv\Scripts\python.exe" `
            -Arguments @("-c", "import memoria.api") `
            -Label "Checking the installation" -From 0.99 -To 1.0 -ExpectSec 8
        Add-Log "Import check passed - the engine starts."
    } catch {
        Add-Log "Import check FAILED: $($_.Exception.Message)"
        Write-Note -Text "Memoria's engine installed but can't start - see the error below." `
            -Colour Yellow -Fraction 0.99 -Label "Checking the installation"
        Write-Note -Text "Usually a half-finished install: delete the 'server\.venv' folder, then run setup.cmd again." `
            -Colour DarkGray -Fraction 0.99 -Label "Checking the installation"
        throw
    }

    Write-Bar 1.0 "Done"
    Clear-Bar

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkGray
    Write-Host "   All set." -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Open Memoria from the Desktop shortcut, or double-click"
    Write-Host "  Memoria.exe in this folder."
    Write-Host ""
    Write-Host "  Keep this folder where it is - Memoria.exe and the 'server'" -ForegroundColor DarkGray
    Write-Host "  folder next to it must stay together for the app to work." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Faces and semantic search are optional and install later, from" -ForegroundColor DarkGray
    Write-Host "  the app's Settings." -ForegroundColor DarkGray
    Write-Host ""
    exit 0

} catch {
    Clear-Bar
    Add-Log ""
    Add-Log "FAILED at: $script:Stage"
    Add-Log $_.Exception.Message

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkGray
    Write-Host "   Setup did not finish." -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Stage:  $script:Stage" -ForegroundColor White
    Write-Host "  Reason: $($_.Exception.Message)" -ForegroundColor White
    Write-Host ""
    # Prefer the actual error lines: a plain tail is usually 15 lines of
    # "Requirement already satisfied" with the one line that matters scrolled off.
    try {
        $lines = Get-Content $log -ErrorAction SilentlyContinue
        $errors = $lines | Where-Object {
            $_ -match '^\s*(ERROR|error:|FATAL|Traceback)' -or $_ -match 'No matching distribution'
        } | Select-Object -Last 6
        if ($errors) {
            Write-Host "  What went wrong:" -ForegroundColor DarkGray
            $errors | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        } else {
            Write-Host "  Last few lines of the log:" -ForegroundColor DarkGray
            $lines | Select-Object -Last 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
    } catch { }
    Write-Host ""
    Write-Host "  Full log: $log" -ForegroundColor White
    Write-Host ""
    Write-Host "  The usual causes are no internet connection, or antivirus" -ForegroundColor DarkGray
    Write-Host "  blocking a download. Fix that and run the setup again -- it's" -ForegroundColor DarkGray
    Write-Host "  safe to re-run and skips whatever already worked." -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}
