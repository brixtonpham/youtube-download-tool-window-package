@echo off
chcp 65001 >nul
title YouTube Channel Downloader - Setup

echo ============================================
echo   YouTube Channel Downloader - Auto Setup
echo ============================================
echo.

:: ====== Find the correct Python ======
echo [1/4] Checking Python...
set "PY="

:: Try 'py' first (Windows Python Launcher - most reliable)
py --version >nul 2>&1
if not errorlevel 1 (
    set "PY=py"
    goto python_found
)

:: Try 'python3'
python3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY=python3"
    goto python_found
)

:: Try 'python' but verify it has pip (skip MSYS2/MinGW)
python --version >nul 2>&1
if errorlevel 1 goto no_python

python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo       Found Python but it has no pip (likely MSYS2/MinGW^).
    echo       Skipping...
    goto no_python
)
set "PY=python"
goto python_found

:no_python
echo       Python not found. Attempting auto-install...
echo.

:: Try winget
winget --version >nul 2>&1
if errorlevel 1 goto no_winget_python

echo       Installing Python via winget...
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto no_winget_python

echo       Python installed via winget!
echo       Please RESTART this script for PATH to take effect.
echo.
pause
exit /b 0

:no_winget_python
echo       Trying Microsoft Store...
start ms-windows-store://pdp/?productid=9NCVDN91XZQP
echo.
echo       Microsoft Store opened for Python 3.12.
echo       Please install Python, then re-run this script.
echo.
echo       OR download from: https://www.python.org/downloads/
echo       IMPORTANT: Check "Add Python to PATH" during installation!
echo.
pause
exit /b 1

:python_found
%PY% --version
echo       OK (using: %PY%^)
echo.

:: ====== Check and install FFmpeg ======
echo [2/4] Checking FFmpeg...
set "FFMPEG_DIR=%~dp0ffmpeg"

ffmpeg -version >nul 2>&1
if not errorlevel 1 (
    echo       OK (system PATH^)
    goto ffmpeg_done
)

if exist "%FFMPEG_DIR%\ffmpeg.exe" (
    set "PATH=%FFMPEG_DIR%;%PATH%"
    echo       OK (local folder^)
    goto ffmpeg_done
)

echo       FFmpeg not found. Attempting auto-install...

:: Try winget
winget --version >nul 2>&1
if errorlevel 1 goto ffmpeg_download

winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
if not errorlevel 1 (
    echo       FFmpeg installed via winget!
    goto ffmpeg_done
)

:ffmpeg_download
echo       Downloading FFmpeg via PowerShell (this may take a minute^)...
powershell -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference = 'SilentlyContinue'; " ^
    "$url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'; " ^
    "$zip = Join-Path $env:TEMP 'ffmpeg.zip'; " ^
    "$dest = Join-Path $env:TEMP 'ffmpeg_extract'; " ^
    "Write-Host '       Downloading...'; " ^
    "Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing; " ^
    "Write-Host '       Extracting...'; " ^
    "if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }; " ^
    "Expand-Archive -Path $zip -DestinationPath $dest -Force; " ^
    "$binDir = Get-ChildItem -Path $dest -Filter 'bin' -Recurse -Directory | Select-Object -First 1; " ^
    "$targetDir = '%FFMPEG_DIR%'; " ^
    "if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }; " ^
    "Copy-Item -Path (Join-Path $binDir.FullName '*') -Destination $targetDir -Force; " ^
    "Remove-Item $zip -Force; " ^
    "Remove-Item $dest -Recurse -Force; " ^
    "Write-Host '       Done!'; "

if exist "%FFMPEG_DIR%\ffmpeg.exe" (
    set "PATH=%FFMPEG_DIR%;%PATH%"
    echo       FFmpeg downloaded to %FFMPEG_DIR%
) else (
    echo.
    echo       ERROR: FFmpeg download failed.
    echo       Please download manually from: https://ffmpeg.org/download.html
    echo       Extract ffmpeg.exe to: %FFMPEG_DIR%\
    echo.
    pause
    exit /b 1
)

:ffmpeg_done
echo.

:: ====== Install Python packages ======
echo [3/4] Installing Python packages...
%PY% -m pip install --upgrade pip 2>nul
%PY% -m pip install --upgrade yt-dlp customtkinter
if errorlevel 1 (
    echo.
    echo       ERROR: Failed to install packages.
    echo       Try running manually: %PY% -m pip install yt-dlp customtkinter
    echo.
    pause
    exit /b 1
)

:: Verify packages actually installed
%PY% -c "import customtkinter; import yt_dlp; print('       Packages verified OK')"
if errorlevel 1 (
    echo.
    echo       ERROR: Packages installed but cannot be imported.
    echo       Try: %PY% -m pip install --force-reinstall yt-dlp customtkinter
    echo.
    pause
    exit /b 1
)
echo.

:: ====== Launch ======
echo [4/4] Launching YouTube Channel Downloader...
echo.
echo ============================================
echo   Setup complete! Starting app...
echo ============================================
echo.

%PY% "%~dp0app.py"
if errorlevel 1 (
    echo.
    echo App exited with error. Check above for details.
    pause
)
