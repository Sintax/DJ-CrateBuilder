#!/bin/bash
# ============================================================================
# DJ-CrateBuilder v1.2 — Linux Installer
# ============================================================================

set -e

APP_NAME="DJ-CrateBuilder"
APP_VERSION="1.2"
INSTALL_DIR="$HOME/.local/share/DJ-CrateBuilder"
BIN_LINK="$HOME/.local/bin/dj-cratebuilder"
DESKTOP_DIR="$HOME/.local/share/applications"
SCRIPT_NAME="DJ-CrateBuilder_v1.2.py"

echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │   DJ-CrateBuilder v1.2 — Linux Setup    │"
echo "  └─────────────────────────────────────────┘"
echo ""

# ── Check Python ──────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  ✗ Python 3.10+ is required but not found."
    echo "    Install it with your package manager:"
    echo "      Ubuntu/Debian:  sudo apt install python3 python3-tk"
    echo "      Fedora:         sudo dnf install python3 python3-tkinter"
    echo "      Arch:           sudo pacman -S python tk"
    exit 1
fi
echo "  ✓ Python: $($PYTHON --version)"

# ── Check tkinter ─────────────────────────────────────────────────────────
if ! $PYTHON -c "import tkinter" &>/dev/null; then
    echo "  ✗ tkinter is not installed."
    echo "    Install it with your package manager:"
    echo "      Ubuntu/Debian:  sudo apt install python3-tk"
    echo "      Fedora:         sudo dnf install python3-tkinter"
    echo "      Arch:           sudo pacman -S tk"
    exit 1
fi
echo "  ✓ tkinter: available"

# ── Check/install FFmpeg ──────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
    echo "  ✗ FFmpeg is not installed."
    echo "    Install it with your package manager:"
    echo "      Ubuntu/Debian:  sudo apt install ffmpeg"
    echo "      Fedora:         sudo dnf install ffmpeg"
    echo "      Arch:           sudo pacman -S ffmpeg"
    exit 1
fi
echo "  ✓ FFmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ── Install yt-dlp ────────────────────────────────────────────────────────
if ! $PYTHON -c "import yt_dlp" &>/dev/null; then
    echo "  → Installing yt-dlp..."
    $PYTHON -m pip install --user yt-dlp -q
fi
echo "  ✓ yt-dlp: installed"

# ── Copy application ─────────────────────────────────────────────────────
echo ""
echo "  → Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# Find the script (same directory as this installer)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SCRIPT_DIR/$SCRIPT_NAME" ]; then
    echo "  ✗ Cannot find $SCRIPT_NAME in $SCRIPT_DIR"
    echo "    Place this installer in the same folder as the .py file."
    exit 1
fi

cp "$SCRIPT_DIR/$SCRIPT_NAME" "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

# ── Create launcher script ────────────────────────────────────────────────
mkdir -p "$(dirname "$BIN_LINK")"
cat > "$BIN_LINK" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
$PYTHON "$INSTALL_DIR/$SCRIPT_NAME" "\$@"
EOF
chmod +x "$BIN_LINK"
echo "  ✓ Command: dj-cratebuilder"

# ── Create .desktop entry ────────────────────────────────────────────────
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/dj-cratebuilder.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DJ-CrateBuilder
Comment=Batch download audio from YouTube and SoundCloud as MP3
Exec=$BIN_LINK
Terminal=false
Categories=AudioVideo;Audio;Music;
Keywords=youtube;soundcloud;mp3;download;dj;music;
EOF
chmod +x "$DESKTOP_DIR/dj-cratebuilder.desktop"
echo "  ✓ Desktop entry: created"

# ── Verify PATH ───────────────────────────────────────────────────────────
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo "  ⚠ ~/.local/bin is not in your PATH."
    echo "    Add this line to your ~/.bashrc or ~/.zshrc:"
    echo ""
    echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │   ✓ Installation complete!              │"
echo "  │                                         │"
echo "  │   Launch from terminal:                 │"
echo "  │     dj-cratebuilder                     │"
echo "  │                                         │"
echo "  │   Or find it in your app launcher.      │"
echo "  └─────────────────────────────────────────┘"
echo ""
