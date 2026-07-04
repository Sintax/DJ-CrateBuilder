#!/bin/bash
# ============================================================================
# DJ-CrateBuilder — Debian package builder
#
# Usage: bash packaging/deb/build-deb.sh [debian-revision]
#
# Assembles dist/deb/dj-cratebuilder_<version>_all.deb with dpkg-deb. Runs on
# Linux (CI: .github/workflows/build-deb.yml). The package installs the app
# under /opt/dj-cratebuilder; its postinst builds a venv there and installs
# requirements.txt into it — the same pattern as install-linux.sh. Requires
# Pillow (python3-pil) for the icon.ico → PNG conversion.
# ============================================================================
set -e

REV="${1:-1}"
APP_VERSION="1.3"
PKG_VERSION="${APP_VERSION}-${REV}"
PKG_NAME="dj-cratebuilder"
SCRIPT_NAME="DJ-CrateBuilder_v1.3.py"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT_DIR="$ROOT/dist/deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PKG="$STAGE/pkg"
APP="/opt/dj-cratebuilder"

mkdir -p \
    "$PKG/DEBIAN" \
    "$PKG$APP" \
    "$PKG/usr/bin" \
    "$PKG/usr/share/applications" \
    "$PKG/usr/share/icons/hicolor/256x256/apps"

# ── Application payload ───────────────────────────────────────────────────
cp "$ROOT/$SCRIPT_NAME" "$PKG$APP/"
cp "$ROOT/requirements.txt" "$PKG$APP/"
cp -r "$ROOT/cratebuilder" "$PKG$APP/cratebuilder"
rm -rf "$PKG$APP/cratebuilder/__pycache__"

# ── Icon: icon.ico → 256×256 PNG (hicolor theme, named for the .desktop) ──
python3 - "$ROOT/icon.ico" \
    "$PKG/usr/share/icons/hicolor/256x256/apps/dj-cratebuilder.png" << 'PYEOF'
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
img = Image.open(src).convert("RGBA")
if img.size != (256, 256):
    img = img.resize((256, 256))
img.save(dst)
PYEOF

# ── Launcher ──────────────────────────────────────────────────────────────
cat > "$PKG/usr/bin/dj-cratebuilder" << EOF
#!/bin/sh
exec $APP/venv/bin/python $APP/$SCRIPT_NAME "\$@"
EOF
chmod 755 "$PKG/usr/bin/dj-cratebuilder"

# ── Desktop entry ─────────────────────────────────────────────────────────
cp "$HERE/dj-cratebuilder.desktop" "$PKG/usr/share/applications/"

# ── Control file + maintainer scripts ─────────────────────────────────────
INSTALLED_SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" << EOF
Package: $PKG_NAME
Version: $PKG_VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-tk, python3-venv, ffmpeg
Installed-Size: $INSTALLED_SIZE
Maintainer: Sintax <sintax@users.noreply.github.com>
Homepage: https://github.com/Sintax/DJ-CrateBuilder
Description: Batch download audio from YouTube and SoundCloud as MP3
 Desktop app for DJs: batch-downloads audio from YouTube and SoundCloud
 channels, playlists, and individual videos/tracks as MP3 files, organised
 into ~/Music/DJ-CrateBuilder/<Platform>/<Genre>/<Channel>/.
 .
 Python dependencies (yt-dlp, pystray, Pillow, ...) are installed into a
 private virtual environment under /opt/dj-cratebuilder at install time.
EOF

install -m 755 "$HERE/postinst" "$PKG/DEBIAN/postinst"
install -m 755 "$HERE/prerm"    "$PKG/DEBIAN/prerm"

mkdir -p "$OUT_DIR"
dpkg-deb --build --root-owner-group "$PKG" \
    "$OUT_DIR/${PKG_NAME}_${PKG_VERSION}_all.deb"
echo "Built: $OUT_DIR/${PKG_NAME}_${PKG_VERSION}_all.deb"
