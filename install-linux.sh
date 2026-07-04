#!/bin/bash
# ============================================================================
# DJ-CrateBuilder v1.3 — Linux Installer
#
# Self-bootstrapping. Download this one file and run it with:
#
#     bash install-linux.sh
#
# It installs any missing system packages (asking for your own password),
# downloads the app from GitHub if the files aren't already next to this
# script, builds an isolated virtual environment, and creates a launcher +
# menu entry. No git required, and you never need `chmod` or `sudo` on the
# script itself.
# ============================================================================

set -e

APP_NAME="DJ-CrateBuilder"
APP_VERSION="1.3"
INSTALL_DIR="$HOME/.local/share/DJ-CrateBuilder"
BIN_LINK="$HOME/.local/bin/dj-cratebuilder"
DESKTOP_DIR="$HOME/.local/share/applications"
SCRIPT_NAME="DJ-CrateBuilder_v1.3.py"
REPO_TARBALL="https://github.com/Sintax/DJ-CrateBuilder/archive/refs/heads/main.tar.gz"

echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │   DJ-CrateBuilder v1.3 — Linux Setup    │"
echo "  └─────────────────────────────────────────┘"
echo ""

# ── Package-manager abstraction ───────────────────────────────────────────
# Detect apt / dnf / pacman once, then map a generic need (tkinter, venv, ...)
# onto the right package name so the rest of the script can just call
# ensure_pkg without caring which distro it is running on.
PKG=""
for mgr in apt-get dnf pacman; do
    if command -v "$mgr" &>/dev/null; then PKG="$mgr"; break; fi
done

pkg_name() {
    # $1 = generic need; echoes the distro-specific package name(s), empty = skip
    case "$PKG:$1" in
        apt-get:python)  echo "python3" ;;
        apt-get:tkinter) echo "python3-tk" ;;
        apt-get:venv)    echo "python3-venv python3-full" ;;
        apt-get:ffmpeg)  echo "ffmpeg" ;;
        apt-get:curl)    echo "curl" ;;
        dnf:python)      echo "python3" ;;
        dnf:tkinter)     echo "python3-tkinter" ;;
        dnf:venv)        echo "" ;;    # bundled with python3
        dnf:ffmpeg)      echo "ffmpeg" ;;
        dnf:curl)        echo "curl" ;;
        pacman:python)   echo "python" ;;
        pacman:tkinter)  echo "tk" ;;
        pacman:venv)     echo "" ;;    # bundled with python
        pacman:ffmpeg)   echo "ffmpeg" ;;
        pacman:curl)     echo "curl" ;;
        *)               echo "" ;;
    esac
}

pkg_install() {
    # $@ = package names; installs them with the detected manager via sudo
    case "$PKG" in
        apt-get) sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
        dnf)     sudo dnf install -y "$@" ;;
        pacman)  sudo pacman -S --noconfirm "$@" ;;
    esac
}

ASKED_SUDO=""
ensure_pkg() {
    # ensure_pkg <generic-need> <human label> — installs it if missing
    local need="$1" label="$2" names
    names="$(pkg_name "$need")"
    if [ -z "$names" ]; then return 0; fi
    if [ -z "$PKG" ]; then
        echo "  ✗ $label is missing and no supported package manager was found."
        echo "    Install it manually, then re-run this script."
        exit 1
    fi
    if [ -z "$ASKED_SUDO" ]; then
        echo ""
        echo "  → Some system packages are missing. Installing them now —"
        echo "    you'll be asked for your password (your own system sudo prompt)."
        echo ""
        ASKED_SUDO="1"
    fi
    echo "  → Installing $label ($names)..."
    pkg_install $names
}

# ── Ensure Python 3.10+ ───────────────────────────────────────────────────
find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
                PYTHON="$cmd"; return 0
            fi
        fi
    done
    return 1
}

PYTHON=""
if ! find_python; then
    ensure_pkg python "Python 3.10+"
    if ! find_python; then
        echo "  ✗ Python 3.10+ still not found after install. Please install it manually."
        exit 1
    fi
fi
echo "  ✓ Python: $($PYTHON --version)"

# ── Ensure tkinter ────────────────────────────────────────────────────────
if ! "$PYTHON" -c "import tkinter" &>/dev/null; then
    ensure_pkg tkinter "tkinter (Python GUI toolkit)"
    if ! "$PYTHON" -c "import tkinter" &>/dev/null; then
        echo "  ✗ tkinter still not available after install. Please install it manually."
        exit 1
    fi
fi
echo "  ✓ tkinter: available"

# ── Ensure FFmpeg ─────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
    ensure_pkg ffmpeg "FFmpeg"
    if ! command -v ffmpeg &>/dev/null; then
        echo "  ✗ FFmpeg still not found after install. Please install it manually."
        exit 1
    fi
fi
echo "  ✓ FFmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ── Locate the application source (use local files, else download) ─────────
# If the app files are sitting next to this script (a repo checkout) use them;
# otherwise this is a lone downloaded install-linux.sh, so fetch the app from
# GitHub as a tarball — no git needed.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR=""
TMP_DIR=""
cleanup() { [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT

if [ -f "$SCRIPT_DIR/$SCRIPT_NAME" ] && [ -d "$SCRIPT_DIR/cratebuilder" ]; then
    SRC_DIR="$SCRIPT_DIR"
    echo "  ✓ Using application files found next to this installer"
else
    echo ""
    echo "  → App files aren't here — downloading DJ-CrateBuilder from GitHub..."
    DL=""
    for d in curl wget; do
        if command -v "$d" &>/dev/null; then DL="$d"; break; fi
    done
    if [ -z "$DL" ]; then
        ensure_pkg curl "curl (downloader)"
        if command -v curl &>/dev/null; then DL="curl"; fi
    fi
    if [ -z "$DL" ]; then
        echo "  ✗ Need curl or wget to download the app, and neither is available."
        exit 1
    fi

    TMP_DIR="$(mktemp -d)"
    if [ "$DL" = "curl" ]; then
        curl -fL "$REPO_TARBALL" -o "$TMP_DIR/src.tar.gz"
    else
        wget -O "$TMP_DIR/src.tar.gz" "$REPO_TARBALL"
    fi
    tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"
    SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'DJ-CrateBuilder-*' | head -1)"
    if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/$SCRIPT_NAME" ]; then
        echo "  ✗ Download completed but the app files weren't where expected."
        exit 1
    fi
    echo "  ✓ Downloaded application source"
fi

# ── Create an isolated virtual environment ────────────────────────────────
# Modern Debian/Ubuntu/Mint mark the system Python as "externally managed"
# (PEP 668), so pip refuses to install into it — even with --user. A dedicated
# venv sidesteps that cleanly and keeps the app's deps isolated from the OS.
VENV_DIR="$INSTALL_DIR/venv"
mkdir -p "$INSTALL_DIR"
echo ""
echo "  → Creating virtual environment"
if ! "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
    ensure_pkg venv "the Python venv module"
    if ! "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
        echo "  ✗ Could not create a virtual environment even after installing venv."
        exit 1
    fi
fi
VENV_PY="$VENV_DIR/bin/python"
echo "  ✓ Virtual environment: $VENV_DIR"

# ── Install Python dependencies into the venv ─────────────────────────────
# yt-dlp is the download engine; pystray + Pillow drive the system-tray icon.
echo "  → Installing Python dependencies (yt-dlp, pystray, Pillow)..."
"$VENV_PY" -m pip install --upgrade pip -q
if [ -f "$SRC_DIR/requirements.txt" ]; then
    "$VENV_PY" -m pip install -r "$SRC_DIR/requirements.txt" -q
else
    "$VENV_PY" -m pip install yt-dlp "pystray>=0.19" "Pillow>=10.0" send2trash "mutagen>=1.45" -q
fi
echo "  ✓ Python dependencies installed"

# ── Copy application ─────────────────────────────────────────────────────
echo ""
echo "  → Installing to $INSTALL_DIR"

cp "$SRC_DIR/$SCRIPT_NAME" "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

# Copy the cratebuilder/ package (util, sidecar, db, startup, tray)
rm -rf "$INSTALL_DIR/cratebuilder"
cp -r "$SRC_DIR/cratebuilder" "$INSTALL_DIR/cratebuilder"
echo "  ✓ cratebuilder/ package installed"

# Copy the app icon if present (used by the .desktop entry)
if [ -f "$SRC_DIR/icon.ico" ]; then
    cp "$SRC_DIR/icon.ico" "$INSTALL_DIR/icon.ico"
fi

# ── Create launcher script ────────────────────────────────────────────────
mkdir -p "$(dirname "$BIN_LINK")"
cat > "$BIN_LINK" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec "$VENV_DIR/bin/python" "$INSTALL_DIR/$SCRIPT_NAME" "\$@"
EOF
chmod +x "$BIN_LINK"
echo "  ✓ Command: dj-cratebuilder"

# ── Create .desktop entry ────────────────────────────────────────────────
mkdir -p "$DESKTOP_DIR"
ICON_LINE=""
[ -f "$INSTALL_DIR/icon.ico" ] && ICON_LINE="Icon=$INSTALL_DIR/icon.ico"

cat > "$DESKTOP_DIR/dj-cratebuilder.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DJ-CrateBuilder
Comment=Batch download audio from YouTube and SoundCloud as MP3
Exec=$BIN_LINK
$ICON_LINE
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

# Keep the window open so a novice sees the result (skip if piped in).
if [ -t 0 ]; then
    echo "  Press Enter to close."
    read -r _ || true
fi
