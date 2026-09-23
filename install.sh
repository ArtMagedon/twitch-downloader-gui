#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$BIN_DIR" "$DESKTOP_DIR"
ln -sf "$APP_DIR/run.sh" "$BIN_DIR/twitch-downloader-gui"
cat > "$DESKTOP_DIR/twitch-downloader-gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Twitch Downloader
Name[ru]=Twitch Downloader
Comment=GUI for TwitchDownloaderCLI
Exec=$BIN_DIR/twitch-downloader-gui
Terminal=false
Categories=AudioVideo;Network;
StartupNotify=true
EOF
chmod +x "$DESKTOP_DIR/twitch-downloader-gui.desktop"
echo "Installed: $BIN_DIR/twitch-downloader-gui"
echo "Desktop entry: $DESKTOP_DIR/twitch-downloader-gui.desktop"
