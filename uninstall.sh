#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }

INSTALL_DIR="$HOME/.imessage-exporter"
BIN_PATH="/usr/local/bin/imessage-export"

echo ""
echo "  iMessage Exporter — Uninstaller"
echo "  ────────────────────────────────"
echo ""

if [ -f "$BIN_PATH" ]; then
    warn "Removing command (requires sudo)"
    sudo rm -f "$BIN_PATH"
    info "Removed $BIN_PATH"
fi

if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    info "Removed $INSTALL_DIR"
fi

echo ""
info "iMessage Exporter has been uninstalled."
echo ""
