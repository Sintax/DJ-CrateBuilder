@echo off
setlocal EnableDelayedExpansion
title DJ-CrateBuilder - Build Windows App + Updater
color 0B

echo.
echo  =====================================================
echo    DJ-CrateBuilder - Windows Build
echo    (app + updater.exe + FFmpeg, ready for Inno Setup)
echo  =====================================================
echo.

:: Run from the folder this script lives in (repo root).
cd /d "%~dp0"

:: -- Check PyInstaller -------------------------------------------------------
echo  [*] Checking for PyInstaller...
python -m PyInstaller --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] PyInstaller not found. Installing it now...
    python -m pip install pyinstaller --quiet
    if %errorlevel% neq 0 (
        echo  [X] Could not install PyInstaller. Run: pip install pyinstaller
        pause & exit /b 1
    )
)
echo  [+] PyInstaller OK.
echo.

:: -- Step 1: build the main app (onedir) -- Packaging_Guide Step 1 -----------
echo  [*] Building the main app (this takes a minute)...
python -m PyInstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir --icon "icon.ico" ^
  --collect-submodules cratebuilder ^
  --hidden-import pystray._win32 --hidden-import PIL.ImageDraw ^
  --hidden-import send2trash ^
  DJ-CrateBuilder_v1.3.py
if %errorlevel% neq 0 (
    echo  [X] Main app build failed.
    pause & exit /b 1
)
echo  [+] Main app built -> dist\DJ-CrateBuilder\
echo.

:: -- Step 1b: build the updater (onefile) -- Packaging_Guide Step 1b ---------
echo  [*] Building updater.exe...
python -m PyInstaller --noconfirm --clean --name "updater" --windowed --onefile ^
  --hidden-import cratebuilder.updater_core ^
  updater.py
if %errorlevel% neq 0 (
    echo  [X] updater.exe build failed.
    pause & exit /b 1
)
copy /Y "dist\updater.exe" "dist\DJ-CrateBuilder\" >nul
if %errorlevel% neq 0 (
    echo  [X] Could not copy updater.exe into dist\DJ-CrateBuilder\
    pause & exit /b 1
)
echo  [+] updater.exe built and placed next to the app.
echo.

:: -- Step 2: bundle FFmpeg -- Packaging_Guide Step 2 -------------------------
echo  [*] Locating FFmpeg (ffmpeg.exe + ffprobe.exe)...
set "FFMPEG_SRC="
for /f "tokens=*" %%p in ('where ffmpeg 2^>nul') do set "FFMPEG_SRC=%%p"
set "FFPROBE_SRC="
for /f "tokens=*" %%p in ('where ffprobe 2^>nul') do set "FFPROBE_SRC=%%p"

if defined FFMPEG_SRC if defined FFPROBE_SRC (
    copy /Y "!FFMPEG_SRC!"  "dist\DJ-CrateBuilder\" >nul
    copy /Y "!FFPROBE_SRC!" "dist\DJ-CrateBuilder\" >nul
    echo  [+] Copied FFmpeg from PATH into the app folder.
) else (
    echo  [!] FFmpeg not found on PATH. Copy ffmpeg.exe AND ffprobe.exe into
    echo      dist\DJ-CrateBuilder\ manually before building the installer.
    echo      ^(Download: https://www.gyan.dev/ffmpeg/builds/^)
)
echo.

:: -- Done --------------------------------------------------------------------
echo  =====================================================
echo    Build complete.
echo  =====================================================
echo.
echo  Output folder:  dist\DJ-CrateBuilder\
echo.
echo  Next steps:
echo    1. Smoke test:  dist\DJ-CrateBuilder\DJ-CrateBuilder.exe
echo    2. Build the installer in Inno Setup
echo       ^(Packaging_Guide.md  -^>  Step 4^).
echo    3. To publish a nightly update afterward, see
echo       Packaging_Guide.md  -^>  "Nightly build channel".
echo.
pause
endlocal
