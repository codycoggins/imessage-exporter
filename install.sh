#!/bin/bash
set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

INSTALL_DIR="$HOME/.imessage-exporter"
BIN_PATH="/usr/local/bin/imessage-export"

echo ""
echo "  iMessage Exporter — Installer"
echo "  ─────────────────────────────"
echo ""

# --- Check macOS ---
[[ "$(uname)" == "Darwin" ]] || error "This tool only works on macOS."

# --- Check Python 3 ---
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    info "Found Python $PY_VERSION"
else
    error "Python 3 is required. Install it from https://python.org or via Homebrew."
fi

# --- Clone or update ---
if [ -d "$INSTALL_DIR" ]; then
    warn "Existing installation found. Updating..."
    git -C "$INSTALL_DIR" pull --quiet
    info "Updated to latest version"
else
    git clone --quiet https://github.com/vishoo7/imessage-exporter.git "$INSTALL_DIR"
    info "Downloaded to $INSTALL_DIR"
fi

# --- Create CLI command ---
echo ""
warn "Creating command at $BIN_PATH (requires sudo)"
sudo mkdir -p /usr/local/bin
sudo tee "$BIN_PATH" > /dev/null << 'WRAPPER'
#!/bin/bash
exec python3 "$HOME/.imessage-exporter/imessage_export.py" "$@"
WRAPPER
sudo chmod +x "$BIN_PATH"
info "Command 'imessage-export' installed"

# --- Full Disk Access reminder ---
echo ""
echo "  ─────────────────────────────"
echo ""
warn "Full Disk Access is required to read iMessage data."
echo "     System Settings → Privacy & Security → Full Disk Access"
echo "     Add your terminal app (Terminal, iTerm, Ghostty, etc.)"
echo ""
info "Done! Run 'imessage-export' to get started."
echo ""
