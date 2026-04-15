#!/bin/bash
# ============================================================================
# DJ-CrateBuilder v1.2 — Linux Uninstaller
# ============================================================================

INSTALL_DIR="$HOME/.local/share/DJ-CrateBuilder"
BIN_LINK="$HOME/.local/bin/dj-cratebuilder"
DESKTOP_FILE="$HOME/.local/share/applications/dj-cratebuilder.desktop"
CONFIG_FILE="$HOME/.dj_cratebuilder_config.json"

echo ""
echo "  DJ-CrateBuilder — Uninstall"
echo ""

read -p "  Remove DJ-CrateBuilder? [y/N] " confirm
if [[ "$confirm" != [yY] ]]; then
    echo "  Cancelled."
    exit 0
fi

[ -d "$INSTALL_DIR" ]   && rm -rf "$INSTALL_DIR"   && echo "  ✓ Removed $INSTALL_DIR"
[ -f "$BIN_LINK" ]      && rm -f "$BIN_LINK"       && echo "  ✓ Removed $BIN_LINK"
[ -f "$DESKTOP_FILE" ]  && rm -f "$DESKTOP_FILE"   && echo "  ✓ Removed desktop entry"

echo ""
read -p "  Also remove config file? [y/N] " rmconfig
if [[ "$rmconfig" == [yY] ]]; then
    [ -f "$CONFIG_FILE" ] && rm -f "$CONFIG_FILE" && echo "  ✓ Removed $CONFIG_FILE"
fi

echo ""
echo "  ✓ Uninstall complete."
echo "    Note: Downloaded MP3s in ~/Music/DJ-CrateBuilder/ were not removed."
echo ""
