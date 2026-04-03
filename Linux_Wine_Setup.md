# DJ-CrateBuilder v1.2 — Linux Setup (via Wine)

## Install Wine

```bash
# Ubuntu/Debian
sudo apt install wine

# Fedora
sudo dnf install wine

# Arch
sudo pacman -S wine
```

## Install FFmpeg (required)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Arch
sudo pacman -S ffmpeg
```

## Run the Installer

```bash
wine DJ-CrateBuilder_v1.2_Setup.exe
```

Follow the installer prompts. Default install location:
`~/.wine/drive_c/users/USERNAME/Program Files/DJ-CrateBuilder/`

## Launch the App

```bash
wine ~/.wine/drive_c/users/$USER/Program\ Files/DJ-CrateBuilder/DJ-CrateBuilder.exe
```

## Create a Quick Launcher (optional)

```bash
echo '#!/bin/bash
wine "$HOME/.wine/drive_c/users/'$USER'/Program Files/DJ-CrateBuilder/DJ-CrateBuilder.exe"' > ~/.local/bin/dj-cratebuilder

chmod +x ~/.local/bin/dj-cratebuilder
```

Then launch anytime with: `dj-cratebuilder`

## Make FFmpeg Visible to Wine

Wine apps can't see Linux-native FFmpeg. Symlink it into the install folder:

```bash
APPDIR="$HOME/.wine/drive_c/users/$USER/Program Files/DJ-CrateBuilder"
ln -sf "$(which ffmpeg)" "$APPDIR/ffmpeg.exe"
ln -sf "$(which ffprobe)" "$APPDIR/ffprobe.exe"
```

If symlinks don't work, copy the binaries directly:

```bash
cp "$(which ffmpeg)" "$APPDIR/ffmpeg.exe"
cp "$(which ffprobe)" "$APPDIR/ffprobe.exe"
```

## Uninstall

```bash
wine uninstaller
```

Select DJ-CrateBuilder from the list, or delete manually:

```bash
rm -rf "$HOME/.wine/drive_c/users/$USER/Program Files/DJ-CrateBuilder"
```

## Troubleshooting

**App won't launch:** Make sure Wine is configured for Windows 10:
```bash
winecfg
```
Set Windows version to "Windows 10" in the Applications tab.

**No audio downloads:** FFmpeg must be accessible. Verify the symlink/copy step above.

**Display issues:** Install Wine's GUI dependencies:
```bash
# Ubuntu/Debian
sudo apt install wine64 wine32 winetricks
winetricks corefonts
```
