@echo off
chcp 65001 >nul
title Build YouTube Channel Downloader EXE

echo ============================================
echo   Building Windows EXE
echo ============================================
echo.

pip install pyinstaller >nul 2>&1

pyinstaller --onefile --windowed --name "YouTubeChannelDownloader" ^
  --add-data "customtkinter;customtkinter" ^
  --hidden-import customtkinter ^
  --hidden-import yt_dlp ^
  --icon NONE ^
  app.py

echo.
if exist "dist\YouTubeChannelDownloader.exe" (
    echo SUCCESS: dist\YouTubeChannelDownloader.exe created!
    echo.
    echo NOTE: The EXE still needs FFmpeg installed on the target machine.
    echo       Users can install it via: winget install Gyan.FFmpeg
) else (
    echo BUILD FAILED. Check errors above.
)
echo.
pause
