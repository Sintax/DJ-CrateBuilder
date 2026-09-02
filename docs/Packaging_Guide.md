# DJ-CrateBuilder v1.3 — Packaging Guide

Supported install flows:

| Platform | Method | Target user |
|----------|--------|-------------|
| Windows | PyInstaller + Inno Setup | End users on Windows |
| Debian / Ubuntu / Linux Mint | `.deb` package (CI-built) | **Recommended Linux path** |
| Other Linux (Fedora / Arch / …) | Bash installer (`install-linux.sh`) | Distros `dpkg` can't serve |

Both platforms poll for updates: Windows polls `update.json` on the
`nightly` branch, Linux polls `update-linux.json` on the `linux-v2.0` release.

---

# WINDOWS — PyInstaller + Inno Setup

## Prerequisites

```bash
pip install pyinstaller -r requirements.txt
```

(`requirements.txt` pulls in yt-dlp, pystray, and Pillow — the last two power the system-tray icon.)

- FFmpeg: https://www.gyan.dev/ffmpeg/builds/ (get `ffmpeg-release-essentials.zip`)
- Inno Setup 6: https://jrsoftware.org/isinfo.php

> **Shortcut (recommended):** run **`python scripts/release.py --build-only`** from the
> repo root. It does Steps 1, 1b, and 2 below in one go — builds the app, builds
> `updater.exe` and drops it into the app folder, and copies FFmpeg from your
> PATH. Then skip straight to Step 3 (smoke test) / Step 4 (installer). The
> manual steps below are the reference for what that command automates.
>
> (`scripts/release.py` is the single script that handles both fresh-installer builds
> and nightly publishing — see "Nightly build channel" below. Run with `--help`
> for the full flag reference and examples.)

## Step 1 — Build the EXE

From the folder containing `DJ-CrateBuilder_v2.0.py`:

```bash
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir --icon "icon.ico" ^
  --collect-submodules cratebuilder ^
  --hidden-import pystray._win32 --hidden-import PIL.ImageDraw ^
  --hidden-import send2trash ^
  DJ-CrateBuilder_v2.0.py
```

Output: `dist\DJ-CrateBuilder\`

> The `--collect-submodules cratebuilder` flag bundles the local `cratebuilder/`
> package (util, sidecar, db, startup, tray). The `--hidden-import` flags pull
> in pystray's Windows backend and Pillow's drawing module (imported lazily for
> the tray icon) and send2trash (used by Folders Cleanup to move files to the
> Recycle Bin) — PyInstaller can't detect these automatically.
> (`^` is the Windows line-continuation character — keep it as one command.)

## Step 1b — Build the updater (`updater.exe`)

The in-app auto-updater hands off to a tiny separate process so it can replace
the main app's files while they're unlocked. Build it once and drop it into the
same `dist\DJ-CrateBuilder\` folder so it ships beside the app:

```bash
pyinstaller --noconfirm --clean --name "updater" --windowed --onefile ^
  --hidden-import cratebuilder.updater_core ^
  updater.py

copy "dist\updater.exe"  "dist\DJ-CrateBuilder\"
```

> `--windowed` keeps a console window from flashing during the swap. The updater
> writes a log to `%LOCALAPPDATA%\DJ-CrateBuilder\update\update.log`, so failures
> are still diagnosable without a console. `updater.exe` **must** sit next to
> `DJ-CrateBuilder.exe`; if it's missing, the app falls back to running
> `updater.py` with Python (source/dev only) and won't self-update when frozen.

## Step 2 — Bundle FFmpeg

```bash
copy "C:\path\to\ffmpeg.exe"  "dist\DJ-CrateBuilder\"
copy "C:\path\to\ffprobe.exe" "dist\DJ-CrateBuilder\"
```

> Placement matters: the packaged app locates FFmpeg by looking next to its own
> executable (`bundled_ffmpeg_dir()` in the source sets yt-dlp's `ffmpeg_location`
> to that folder). So `ffmpeg.exe`/`ffprobe.exe` **must** sit in
> `dist\DJ-CrateBuilder\` alongside `DJ-CrateBuilder.exe`. No PATH entry is
> required — the installer deliberately makes no per-user PATH changes.

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

Output: `releases\Build_Output\DJ-CrateBuilder_v2.0_Setup_Windows.exe`

`OutputDir` in the `.iss` is relative to the script's own folder (`docs\`), so
`..\releases\Build_Output` lands at the repo root.

The installer attached to the GitHub release carries the build number as well —
`DJ-CrateBuilder_v2.0.<N>_Setup_Windows.exe`, where `<N>` is `APP_BUILD`. That
suffix is added when the release is cut, not by the `.iss` script, so a build
from source uses the plain name above.

---

# NIGHTLY BUILD CHANNEL (in-app updates)

The app ships a pinned display version (`1.3`) plus an incrementing
`APP_BUILD` integer, shown together in the About tab as `v1.3.<build>`. Small
fixes go out as **nightly builds** that bump only the build number — no new
installer, no version jump, and **`main` / the tagged v1.3 release are never
touched.**

How it works:

- A `nightly` branch holds a single `update.json` manifest (build number +
  download URL + SHA-256). The app fetches it from
  `raw.githubusercontent.com/.../nightly/update.json`.
- The build payload is a zip of `dist\DJ-CrateBuilder\` attached as an asset to
  a reused `nightly` GitHub pre-release. **Never commit the zip into git** — it
  permanently bloats history. Release assets don't count against repo size.
- The app checks on launch (throttled to once / 6 h) and via the About tab's
  **Check for updates** button. When a newer build exists it shows an antivirus
  note **before** downloading, then downloads, verifies the SHA-256, and hands
  off to `updater.exe`, which closes the app, swaps the files, and relaunches.

### One-time setup — create the `nightly` branch

```bash
python scripts/release.py --init
```

This creates the orphan `nightly` branch (holding only `update.json`) using git
plumbing — it **never switches your branch or touches your working tree** — and
pushes it. Do this once. (Already done for v1.3; you only need it on a fresh
clone or if the branch is ever deleted.)

### Publishing a nightly build — one command

From the repo root, with the GitHub CLI `gh` authenticated, run:

```bash
python scripts/release.py
```

It will prompt for one line of release notes, then do **everything**:

1. Auto-increment `APP_BUILD` in `DJ-CrateBuilder_v2.0.py` (so the `.exe` reports
   the new build — no manual edit).
2. Build the app + `updater.exe` + bundle FFmpeg (Steps 1, 1b, 2).
3. Work out the **smallest payload**: it hashes every file in the build and zips
   only the files that changed since the last full build — typically just
   `DJ-CrateBuilder.exe`, a few MB instead of 150 MB+. FFmpeg and the CPython
   runtime are never re-downloaded.
4. SHA-256 the zip, upload it to the reused `nightly` pre-release, and push
   `update.json` to the `nightly` branch via git plumbing (your checkout and
   `main` are never touched).

Useful flags:

| Flag | What it does |
|------|--------------|
| `--notes "..."` | Provide notes instead of being prompted |
| `--full` | Force a full payload (minus FFmpeg) and reset the delta baseline |
| `--build N` | Override the auto-incremented build number |
| `--dry-run` | Build + zip locally; don't upload or publish |
| `--no-build` | Publish from an existing `dist/` (skip PyInstaller) |
| `--build-only` | Just build `dist/` for a fresh installer (no publish) |
| `--keep` | Keep `build/`, `dist/`, and the zip after publishing (they're deleted by default) |

After a **successful** nightly publish the script deletes `build/`, `dist/`, and
the `DJ-CrateBuilder-<ver>.zip` automatically, so the repo folder stays clean.
This is safe: the delta baseline is stored in `.nightly_release_state.json`
(file hashes), not in `dist/`, and the zip already lives on the GitHub release.
Pass `--keep` if you want to inspect the build. (`--build-only` never deletes
`dist/` — you need it for Inno Setup.)

**How the delta stays correct:** the updater is an additive overlay — it copies
the zip's files over the install and leaves everything else alone. Deltas are
diffed against a **fixed baseline** (the last `--full` build, tracked locally in
`.nightly_release_state.json`), so one delta zip always carries the complete
current version of every file changed since that baseline. A user who skipped
several nightlies still ends up on the exact current build. Run `--full`
occasionally (e.g. when you ship a new dependency) to refresh the baseline.

After publishing, commit the `APP_BUILD` bump so the source matches the shipped
build.

> **Unsigned-binary note:** the app and `updater.exe` are not Authenticode-signed,
> so Windows SmartScreen / Defender may warn or quarantine on first run and
> during updates. This is a known false positive — see the README's
> "Windows SmartScreen & antivirus" section. Submitting each build to
> https://www.microsoft.com/wdsi/filesubmission reduces Defender false positives.

---

# LINUX — `.deb` package (Debian / Ubuntu / Linux Mint)

There is no PyInstaller step on Linux and no Wine. The app runs on the distro's
own Python 3; the package ships the source and provisions a private virtualenv
at install time.

## What the package contains

```
/opt/dj-cratebuilder/
├── DJ-CrateBuilder_v2.0.py
├── cratebuilder/                     # the package the .py imports
├── requirements.txt
└── venv/                             # created by postinst, not shipped
/usr/bin/dj-cratebuilder              # shim: exec venv/bin/python <app>
/usr/share/applications/dj-cratebuilder.desktop
/usr/share/icons/hicolor/256x256/apps/dj-cratebuilder.png
```

System dependencies are declared in the control file and resolved by `apt`:
`python3 (>= 3.10)`, `python3-venv`, `python3-gi`, `python3-gi-cairo`,
`gir1.2-gtk-3.0`, `gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0`, `ffmpeg`. The
Python dependencies (`yt-dlp`, `pywebview`, `fastapi`, `uvicorn`, `Pillow`,
`send2trash`, `mutagen`, ...) go into `/opt/dj-cratebuilder/venv` — PEP 668
forbids installing them into the system Python, so the venv is not optional.
The venv is created with `--system-site-packages` so pywebview's GTK backend
can see the system-installed PyGObject (pip cannot sanely build it).

Package version mirrors the app's own version: `build-deb.sh` greps `APP_BUILD`
out of the source, so the `.deb` filename, the About screen, and
`update-linux.json` all report the same `2.0.<build>`.

## Publishing a release (the normal path)

The `.deb` is built and published by CI, **manual trigger only**:

```bash
gh workflow run build-deb.yml
```

Or: GitHub → Actions → **build-deb** → *Run workflow*.

[`.github/workflows/build-deb.yml`](../.github/workflows/build-deb.yml) runs on
`ubuntu-latest` and:

1. Builds the package via `bash packaging/deb/build-deb.sh`
2. **Smoke-tests it** — installs the `.deb` on the runner, asserts
   `/usr/bin/dj-cratebuilder` exists, imports every venv dependency, and
   byte-compiles the app
3. Uploads the `.deb` to the `linux-v2.0` GitHub Release (creating it on first
   run), replacing any previous `.deb` asset so exactly one is ever attached
4. Generates `update-linux.json` (build number, download URL, SHA-256,
   filename) and uploads it to the same release with `--clobber`
5. Retitles the release to `DJ-CrateBuilder v2.0 (Build N) — Linux package (.deb)`

This channel is **completely separate from the Windows nightly channel** — it
never touches `scripts/release.py`, the `nightly` branch, or `update.json`.

> **Bump `APP_BUILD` first.** The workflow reads it from the source on `main`;
> it does not increment anything itself. Run the Windows nightly (which owns
> the bump) or land the bump commit before dispatching the workflow, or the
> `.deb` will ship under a build number that's already published.

## Building locally (optional)

Requires a Linux machine or WSL with `dpkg-deb` and `python3-pil`:

```bash
sudo apt-get install -y python3-pil
bash packaging/deb/build-deb.sh          # → dist/deb/dj-cratebuilder_2.0.N_all.deb
bash packaging/deb/build-deb.sh 99       # override the build number
```

Sources live in [`packaging/deb/`](../packaging/deb/): `build-deb.sh`,
`postinst` (creates the venv, refreshes desktop/icon caches), `prerm` (removes
the venv and `__pycache__` so `dpkg` can clean out `/opt`), and
`dj-cratebuilder.desktop`.

## What users do

Download the `.deb` from the
[`linux-v2.0` release](https://github.com/Sintax/DJ-CrateBuilder/releases/tag/linux-v2.0)
and double-click it, or:

```bash
sudo apt install ./dj-cratebuilder_2.0.*_all.deb
```

`apt` pulls in `ffmpeg` and the GTK/WebKit stack automatically; `postinst` then
builds the venv (this is the slow part — it's `pip install` over the network).
The app appears in the menu under Sound & Video and as the `dj-cratebuilder`
command.

### Updates

The Linux build polls `update-linux.json` on the `linux-v2.0` release, the same
way Windows polls `update.json`, and notifies when a newer build is published.
Installing it is manual: the v2.0 web app doesn't port the old pkexec/apt
self-install flow — users download the new `.deb` from the release page and
install it over the old one.

### Uninstall

```bash
sudo apt remove dj-cratebuilder       # keeps config
sudo apt purge  dj-cratebuilder       # also removes package config
```

`prerm` deletes the venv it created. Config (`~/.cratebuilder/`)
and downloaded MP3s in `~/Music/DJ-CrateBuilder/` are left alone either way.

---

# LINUX — script installer (non-Debian distros)

For Fedora, Arch, openSUSE, and anything else `dpkg` can't serve, ship the repo
contents plus [`install-linux.sh`](../install-linux.sh). It does the same job
without a package manager: verifies Python 3.10+, the GTK/WebKit stack
(PyGObject + WebKit2GTK — pywebview's Linux backend), and `ffmpeg`, builds a
`--system-site-packages` venv and pip-installs `requirements.txt` into it,
copies `web_window.py`/`web_server.py`/`web/`/`cratebuilder/` (plus the
monolith source the service parses for version data) into
`~/.local/share/DJ-CrateBuilder/`, creates the `dj-cratebuilder` launcher in
`~/.local/bin/`, and writes a `.desktop` entry. Missing dependencies are
installed via the detected `apt`/`dnf`/`pacman`.

The script refuses to run if `cratebuilder/` is missing — the app will not
launch without it.

Uninstall with [`uninstall-linux.sh`](../uninstall-linux.sh): removes the
install dir, launcher, and `.desktop` entry, asks before deleting config, and
leaves downloaded MP3s alone.

> This path gets **no in-app updates** — the updater only knows the `.deb` and
> the Windows nightly. Users re-run `install-linux.sh` to upgrade.

---

# FILE LOCATIONS

| File | Path |
|------|------|
| Config | `~/.cratebuilder/config.json` |
| Activity log | `<install dir>/activity.log` |
| Debug log | `<install dir>/debug.log` *(new in v1.3)* |
| Downloads | `~/Music/DJ-CrateBuilder/` |

The **debug log** is new — it captures yt-dlp options, cookie config, and full error tracebacks. Users can view it from the Settings tab → Debug Log section. It's the primary tool for diagnosing the cookie-authentication formatting errors this release targets.

---

# RELEASE CHECKLIST

- [ ] `APP_VERSION = "2.0"` in `DJ-CrateBuilder_v2.0.py`
- [ ] `pytest -q` passes (`requirements-dev.txt` installed)
- [ ] (Nightly) `python scripts/release.py` run — it auto-bumps `APP_BUILD`, builds,
      publishes the delta, and pushes `update.json`. About-tab "Check for
      updates" sees and installs it on a test machine. Commit the bump after.
- [ ] (Fresh installer) `python scripts/release.py --build-only`, smoke-test, then build
      the Windows installer in Inno Setup
- [ ] (Linux) `APP_BUILD` bump landed on `main`, then `gh workflow run build-deb.yml`
      — CI smoke-test green, `.deb` + `update-linux.json` on the `linux-v2.0` release
- [ ] Installed the published `.deb` on a Linux Mint VM; venv built, app launches
- [ ] About screen "Check for updates" sees the new build on Linux and points at the release page
- [ ] `sudo apt remove dj-cratebuilder` cleans up `/opt` and leaves MP3s intact
- [ ] Debug log viewer opens and displays data after a download
- [ ] Watch List startup auto-scan refreshes new-track counts on launch
- [ ] Tray icon appears when "Minimize to system tray" is enabled
