# DJ-CrateBuilder v1.2

A desktop application for batch-downloading audio from YouTube and SoundCloud as MP3 files, organized by platform, genre, and channel — like a digital record crate for DJs and music collectors.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey) ![License](https://img.shields.io/badge/License-Personal_Use-orange)

---

## Features

- **Batch Queue** — Add multiple URLs (channels, playlists, single videos) and process them in sequence
- **Auto-Organization** — Downloads are sorted into folders by platform, genre, and channel name
- **MP3 Conversion** — Converts all audio to MP3 at your chosen bitrate (128 / 192 / 256 / 320 kbps)
- **Skip Existing** — Detects previously downloaded files by log history and/or folder scan, doubling as a resume function for interrupted batches
- **Time Limiter** — Automatically skip tracks longer than a set duration to filter out mixes, podcasts, and full albums
- **Browser Cookie Authentication** — Authenticate with a YouTube account for faster downloads and fewer restrictions (supports Firefox, Chrome, Edge, Brave, and cookie file export)
- **Throttle Controls** — Random delays between downloads with Auto presets or Manual min/max to avoid rate limiting
- **User-Agent Rotation** — Randomized browser fingerprints per session
- **Geo-Bypass** — Attempt to bypass geographic IP restrictions
- **Download Log** — Timestamped record of every download, skip, and error with a built-in color-coded log viewer
- **URL History** — The URL field remembers your last 6 inputs
- **Channel Auto-Detection** — Bare channel URLs (youtube.com/@Name) automatically resolve to the full video list
- **Dark Themed UI** — Purpose-built dark interface using tkinter

---

## Screenshots

*Coming soon*

---

## Requirements

- **Python 3.10+**
- **yt-dlp** — `pip install yt-dlp`
- **FFmpeg** — must be on PATH or in the same directory as the script
- **tkinter** — included with standard Python installations on Windows

---

## Installation

### Run from Source

```bash
git clone https://github.com/Sintax/DJ-CrateBuilder.git
cd DJ-CrateBuilder
pip install yt-dlp
python DJ-CrateBuilder_v1.2.py
```

### Windows Installer

Download the latest installer from the [Releases](https://github.com/Sintax/DJ-CrateBuilder/releases) page. The installer bundles Python, yt-dlp, and FFmpeg — no additional setup required.

---

## Usage

1. **Select a platform** — YouTube or SoundCloud
2. **Paste a URL** — Single video, playlist, or entire channel
3. **Choose a genre** — Select from existing genres or create a new one (optional)
4. **Add to Batch** — Queue multiple URLs, or download a single URL directly
5. **Press Download MP3's** — The batch processes sequentially with real-time progress

### Folder Structure

```
~/Music/DJ-CrateBuilder/
├── YouTube/
│   ├── Drum & Bass/
│   │   ├── ChannelName -(Complete Catalog)-/
│   │   │   ├── Track Title.mp3
│   │   │   └── ...
│   │   └── Single Track.mp3
│   ├── House/
│   └── _No Genre/
└── SoundCloud/
    └── ...
```

---

## Browser Cookie Authentication

For faster downloads and fewer "login required" errors, you can authenticate with a YouTube account.

**Recommended setup:** Create a dedicated/throwaway Google account for this purpose — do not use your personal account.

### Method 1 — Browser Profile (Firefox recommended)

1. Create a separate browser profile
2. Log into the throwaway YouTube account in that profile
3. In DJ-CrateBuilder Settings → Download Behavior → Use Browser Cookies
4. Select your browser and enter the profile name

### Method 2 — Cookie File

1. Install the "Get cookies.txt LOCALLY" browser extension
2. Navigate to youtube.com while logged into the throwaway account
3. Export cookies to a `.txt` file
4. In DJ-CrateBuilder Settings → Download Behavior → Use Browser Cookies
5. Select "Cookie File" method and browse to the exported file

> **Note:** Chrome 127+ blocks cookie extraction via DPAPI encryption. Use Firefox or the cookie file method instead.

---

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Time Limiter | 8 min | Skip tracks exceeding this duration |
| MP3 Bitrate | 192 kbps | Output quality (128 / 192 / 256 / 320) |
| Skip Existing | In Logs ~ In Folder | Prevent re-downloading completed files |
| Geo-Bypass | Off | Bypass geographic restrictions |
| Rotate User-Agent | On | Randomize browser fingerprint per session |
| Throttle Requests | On / Light | Random delay between downloads |
| Browser Cookies | Off | Authenticate with a YouTube account |

All settings auto-save and persist between sessions.

---

## Building from Source

### Create Windows Executable

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir DJ-CrateBuilder_v1.2.py
```

Copy `ffmpeg.exe` and `ffprobe.exe` into `dist\DJ-CrateBuilder\`.

### Create Installer

Use [Inno Setup 6](https://jrsoftware.org/isinfo.php) with the included `DJ-CrateBuilder_Installer.iss` file. Generate a unique GUID for the `AppId` field before compiling.

See `Packaging_Guide.md` for detailed instructions.

---

## File Locations

| File | Path |
|------|------|
| Config | `~/.dj_cratebuilder_config.json` |
| Log | `~/Music/DJ-CrateBuilder/DJ-CrateBuilder.log` |
| Downloads | `~/Music/DJ-CrateBuilder/YouTube/` or `.../SoundCloud/` |

---

## FAQ

See the built-in FAQ in the app's About tab for answers to common questions about bitrate, skip logic, throttle presets, folder organization, and more.

---

## Known Limitations

- **Chrome 127+** blocks cookie extraction due to DPAPI encryption — use Firefox or export a cookie file
- **Age-restricted videos** require age verification on the throwaway account, or the app falls back to anonymous download (which bypasses age gates via YouTube's embedded player)
- **YouTube rate limiting** may occur during large batch downloads — enable Throttle Requests with Moderate or Aggressive presets for 200+ file batches
- **VPN users** may encounter "login required" errors from YouTube — enabling Browser Cookies typically resolves this

---

## Tech Stack

- **Python 3** with tkinter (GUI)
- **yt-dlp** (download engine)
- **FFmpeg** (audio conversion)
- **PyInstaller** (packaging)
- **Inno Setup** (Windows installer)

---

## Disclaimer

This tool is intended for downloading audio that you have the right to access. Respect copyright laws and the terms of service of the platforms you use. The developers are not responsible for misuse of this software.

---

## Contributing

This project is in active development. Bug reports, feature requests, and pull requests are welcome.

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| 1.2 | 2026-03 | Browser cookie auth, cookie file support, age-gate retry, format diagnostics, _No Genre folder, URL history, genre confirmation, renamed from YouTube DJ-CrateBuilder |
| 1.1 | 2026-03 | Queue rewrite (Text widget), batch system, throttle presets, geo-bypass, UA rotation, log viewer, Settings tab overhaul |
| 1.0 | 2026-03 | Initial release — single/batch download, genre folders, skip-existing, time limiter, dark UI |
