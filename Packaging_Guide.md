# DJ-CrateBuilder v1.2 — Packaging Guide

## PREREQUISITES

```bash
pip install pyinstaller yt-dlp
```

Download FFmpeg: https://www.gyan.dev/ffmpeg/builds/ (get ffmpeg-release-essentials.zip)
Download Inno Setup 6: https://jrsoftware.org/isinfo.php


## STEP 1 — BUILD EXE (run from the folder containing DJ-CrateBuilder_v1.2.py)

Without icon:
```bash
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir DJ-CrateBuilder_v1.2.py
```

With icon:
```bash
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir --icon "icon.ico" DJ-CrateBuilder_v1.2.py
```

Output lands in: dist\DJ-CrateBuilder\


## STEP 2 — COPY FFMPEG INTO BUILD FOLDER

```bash
copy "C:\path\to\ffmpeg.exe" "dist\DJ-CrateBuilder\"
copy "C:\path\to\ffprobe.exe" "dist\DJ-CrateBuilder\"
```


## STEP 3 — VERIFY BUILD

```bash
dist\DJ-CrateBuilder\DJ-CrateBuilder.exe
```

Test a short download to confirm yt-dlp + FFmpeg work.


## STEP 4 — CREATE INSTALLER

1. Open Inno Setup Compiler
2. Open the appropriate .iss file:
   - Windows:  DJ-CrateBuilder_Installer_Windows.iss  (installs to Program Files, requires admin)
   - Linux/Wine:  DJ-CrateBuilder_Installer_Linux.iss  (installs to user folder, no admin)
3. Generate a GUID at https://www.guidgenerator.com/ and paste it into the AppId line
4. Update the Source path under [Files] to your dist\DJ-CrateBuilder\ folder
5. Ctrl+F9 to compile

Installer output:
  Windows:  Output\DJ-CrateBuilder_v1.2_Setup_Windows.exe
  Linux:    Output\DJ-CrateBuilder_v1.2_Setup_Linux.exe


## FILE LOCATIONS (for reference)

Config: %USERPROFILE%\.dj_cratebuilder_config.json
Log:    (install directory)\DJ-CrateBuilder.log
Downloads: %USERPROFILE%\Music\DJ-CrateBuilder\
