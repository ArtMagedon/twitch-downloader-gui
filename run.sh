#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(dirname "$SCRIPT_PATH")"

cd "$APP_DIR"
exec python3 "$APP_DIR/twitch_downloader_gui.py" "$@"
