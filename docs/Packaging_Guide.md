# DJ-CrateBuilder v1.3 — Packaging Guide

Two supported install flows:

| Platform     | Method                         | Target user         |
|--------------|--------------------------------|---------------------|
| Windows      | PyInstaller + Inno Setup       | End users on Windows |
| Linux (native) | Bash installer (`install-linux.sh`) | Linux Mint / Ubuntu / Fedora / Arch |
| Linux (Wine) | Inno Setup .exe run under Wine | Fallback only       |

The **native Linux path** is now the recommended flow on Linux — no Wine required.

---

# WINDOWS — PyInstaller + Inno Setup

## Prerequisites

```bash
pip install pyinstaller yt-dlp
```

- FFmpeg: https://www.gyan.dev/ffmpeg/builds/ (get `ffmpeg-release-essentials.zip`)
- Inno Setup 6: https://jrsoftware.org/isinfo.php

## Step 1 — Build the EXE

From the folder containing `DJ-CrateBuilder_v1.3.py`:

```bash
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir --icon "icon.ico" DJ-CrateBuilder_v1.3.py
```

Output: `dist\DJ-CrateBuilder\`

## Step 2 — Bundle FFmpeg

```bash
copy "C:\path\to\ffmpeg.exe"  "dist\DJ-CrateBuilder\"
copy "C:\path\to\ffprobe.exe" "dist\DJ-CrateBuilder\"
```

## Step 3 — Smoke test

```bash
dist\DJ-CrateBuilder\DJ-CrateBuilder.exe
```

Run one short YouTube download to confirm yt-dlp + FFmpeg work.

## Step 4 — Build the Installer

1. Open Inno Setup Compiler
2. Open `docs\DJ-CrateBuilder_Installer_Windows.iss`
3. Generate a GUID at https://www.guidgenerator.com/ and paste into `AppId=`
4. Confirm the `[Files]` Source path points to your `dist\DJ-CrateBuilder\` folder
5. Ctrl+F9 to compile

Output: `Output\DJ-CrateBuilder_v1.3_Setup_Windows.exe`

---

# LINUX (NATIVE) — Recommended

This is the "just as easy as Windows" path you tested on the Linux Mint VM. No Wine, no exe, no PyInstaller.

## What you ship

A single archive (zip or tar.gz) containing three files:

```
DJ-CrateBuilder-v1.3-linux/
├── DJ-CrateBuilder_v1.3.py
├── install-linux.sh
└── uninstall-linux.sh
```

## Build the release archive

From the repo root:

```bash
mkdir -p release/DJ-CrateBuilder-v1.3-linux
cp DJ-CrateBuilder_v1.3.py    release/DJ-CrateBuilder-v1.3-linux/
cp install-linux.sh           release/DJ-CrateBuilder-v1.3-linux/
cp uninstall-linux.sh         release/DJ-CrateBuilder-v1.3-linux/
chmod +x release/DJ-CrateBuilder-v1.3-linux/*.sh

cd release
tar -czf DJ-CrateBuilder-v1.3-linux.tar.gz DJ-CrateBuilder-v1.3-linux/
# or:
zip -r DJ-CrateBuilder-v1.3-linux.zip DJ-CrateBuilder-v1.3-linux/
```

## What users do (Linux Mint / Ubuntu / Debian)

1. Download and extract the archive
2. Open a terminal in the extracted folder
3. Run:
   ```bash
   ./install-linux.sh
   ```

The script:
- Verifies Python 3.10+ and `tkinter`
- Verifies `ffmpeg` is on PATH
- Installs `yt-dlp` to user pip if missing
- Copies the `.py` into `~/.local/share/DJ-CrateBuilder/`
- Creates the `dj-cratebuilder` command in `~/.local/bin/`
- Creates a `.desktop` entry so it shows in the app menu

If dependencies are missing it prints the exact apt/dnf/pacman command to fix it.

### Linux Mint specifics

Mint 21/22 ships Python 3.10+ and tkinter by default. Only thing users typically need:

```bash
sudo apt install ffmpeg
```

## Uninstall

```bash
./uninstall-linux.sh
```

Removes the install dir, launcher, and `.desktop` entry. Asks before deleting config. Downloaded MP3s in `~/Music/DJ-CrateBuilder/` are left alone.

---

# LINUX (WINE) — Fallback only

If a user can't run Python natively, the Wine path still works. See `docs/Linux_Wine_Setup.md`.

Build the Wine-targeted installer:

1. Open `docs\DJ-CrateBuilder_Installer_Linux.iss` in Inno Setup Compiler
2. Same GUID/path steps as Windows
3. Ctrl+F9 → `Output\DJ-CrateBuilder_v1.3_Setup_Linux.exe`

User runs: `wine DJ-CrateBuilder_v1.3_Setup_Linux.exe`

---

# FILE LOCATIONS (v1.3)

| File | Path |
|------|------|
| Config | `~/.dj_cratebuilder_config.json` |
| Activity log | `<install dir>/activity.log` |
| Debug log | `<install dir>/debug.log` *(new in v1.3)* |
| Downloads | `~/Music/DJ-CrateBuilder/` |

The **debug log** is new — it captures yt-dlp options, cookie config, and full error tracebacks. Users can view it from the Settings tab → Debug Log section. It's the primary tool for diagnosing the cookie-authentication formatting errors this release targets.

---

# RELEASE CHECKLIST

- [ ] `APP_VERSION = "1.3"` in `DJ-CrateBuilder_v1.3.py`
- [ ] Windows installer built and smoke-tested
- [ ] Linux `.tar.gz` archive built
- [ ] Tested `install-linux.sh` on Linux Mint VM
- [ ] Tested `uninstall-linux.sh` (leaves MP3s intact)
- [ ] Debug log viewer opens and displays data after a download
