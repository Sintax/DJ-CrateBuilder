@echo off
setlocal EnableDelayedExpansion
title DJ-CrateBuilder — Windows Setup
color 0A

echo.
echo  =====================================================
echo    DJ-CrateBuilder v1.3 — Windows Setup
echo  =====================================================
echo.

:: ── Check for admin rights ────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] This installer needs to run as Administrator.
    echo      Right-click the .bat file and choose "Run as administrator"
    echo.
    pause
    exit /b 1
)

:: ── Check Python ──────────────────────────────────────────────────────────
echo  [*] Checking for Python 3.10+...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] Python not found.
    echo      Downloading Python 3.12 installer...
    echo.
    curl -L -o "%TEMP%\python_installer.exe" https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe
    if %errorlevel% neq 0 (
        echo  [X] Download failed. Please install Python manually from:
        echo      https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo  [*] Running Python installer...
    echo      IMPORTANT: Make sure "Add Python to PATH" is checked!
    echo.
    "%TEMP%\python_installer.exe" /passive InstallAllUsers=1 PrependPath=1 Include_pip=1
    if %errorlevel% neq 0 (
        echo  [X] Python installation failed. Please install manually.
        pause
        exit /b 1
    )
    echo  [+] Python installed successfully.
    set "PATH=%PATH%;C:\Program Files\Python312;C:\Program Files\Python312\Scripts"
) else (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    echo  [+] Python !PYVER! found.
)

:: ── Verify Python version is 3.10+ ───────────────────────────────────────
python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] Python 3.10 or higher is required.
    echo      Please update Python from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: ── Check tkinter ─────────────────────────────────────────────────────────
echo  [*] Checking for tkinter...
python -c "import tkinter" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] tkinter not found. It should be included with Python on Windows.
    echo      Try reinstalling Python and ensure "tcl/tk and IDLE" is checked.
    pause
    exit /b 1
) else (
    echo  [+] tkinter OK.
)

:: ── Upgrade pip ───────────────────────────────────────────────────────────
echo  [*] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo  [+] pip up to date.

:: ── Install yt-dlp ────────────────────────────────────────────────────────
echo  [*] Installing yt-dlp...
python -m pip install --upgrade yt-dlp --quiet
if %errorlevel% neq 0 (
    echo  [X] Failed to install yt-dlp. Check your internet connection.
    pause
    exit /b 1
)
echo  [+] yt-dlp installed.

:: ── Check FFmpeg ──────────────────────────────────────────────────────────
echo  [*] Checking for FFmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] FFmpeg not found on PATH.
    echo.
    echo      FFmpeg is required for MP3 conversion.
    echo      Options:
    echo.
    echo      A: winget install Gyan.FFmpeg  (then restart terminal)
    echo      B: Download from https://www.gyan.dev/ffmpeg/builds/
    echo         Extract ffmpeg.exe + ffprobe.exe to the app folder.
    echo.
    echo  [!] Continuing — but MP3 conversion will fail without FFmpeg.
    echo.
) else (
    echo  [+] FFmpeg found.
)

:: ── Locate the script ─────────────────────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_V13=%SCRIPT_DIR%.claude\worktrees\inspiring-jackson\DJ-CrateBuilder_v1.3.py"
set "SCRIPT_MAIN=%SCRIPT_DIR%DJ-CrateBuilder_v1.2.py"

if exist "%SCRIPT_V13%" (
    set "RUN_SCRIPT=%SCRIPT_V13%"
    echo  [+] Found DJ-CrateBuilder v1.3
) else if exist "%SCRIPT_MAIN%" (
    set "RUN_SCRIPT=%SCRIPT_MAIN%"
    echo  [+] Found DJ-CrateBuilder v1.2
) else (
    echo  [X] Could not find DJ-CrateBuilder .py file.
    echo      Make sure this batch file is in the DJ-CrateBuilder folder.
    pause
    exit /b 1
)

:: ── Create desktop shortcut ───────────────────────────────────────────────
echo  [*] Creating desktop shortcut...
for /f "tokens=*" %%p in ('where python 2^>nul') do (
    set "PYTHON_PATH=%%p"
    goto :found_python
)
:found_python
set "SHORTCUT=%USERPROFILE%\Desktop\DJ-CrateBuilder.lnk"
powershell -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%PYTHON_PATH%'; $s.Arguments='\"%RUN_SCRIPT%\"'; $s.WorkingDirectory='%SCRIPT_DIR%'; $s.Description='DJ-CrateBuilder'; $s.Save()" >nul 2>&1
if exist "%SHORTCUT%" (
    echo  [+] Desktop shortcut created.
) else (
    echo  [!] Shortcut could not be created ^(non-critical^).
)

:: ── Done ──────────────────────────────────────────────────────────────────
echo.
echo  =====================================================
echo    Setup complete!
echo  =====================================================
echo.
echo  To run DJ-CrateBuilder:
echo    python "%RUN_SCRIPT%"
echo.
echo  Or double-click the shortcut on your Desktop.
echo.
set /p LAUNCH="  Launch DJ-CrateBuilder now? [Y/N]: "
if /i "!LAUNCH!"=="Y" (
    echo  [*] Launching...
    start "" python "%RUN_SCRIPT%"
)
echo.
pause
endlocal
