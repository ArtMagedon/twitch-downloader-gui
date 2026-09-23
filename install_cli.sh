#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$APP_DIR/bin/TwitchDownloaderCLI"
mkdir -p "$APP_DIR/bin"
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v unzip >/dev/null || { echo "unzip is required" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 - <<'PY' "$TMP/release.json"
import json,urllib.request,sys
url='https://api.github.com/repos/lay295/TwitchDownloader/releases/latest'
with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'twitch-downloader-gui'}),timeout=30) as r:
    data=json.load(r)
asset=next((a for a in data['assets'] if a['name'].endswith('Linux-x64.zip') and 'CLI' in a['name']),None)
if not asset: raise SystemExit('Linux x64 CLI asset not found')
json.dump({'tag':data['tag_name'],'url':asset['browser_download_url'],'name':asset['name']},open(sys.argv[1],'w'))
PY
URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$TMP/release.json")"
NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$TMP/release.json")"
echo "Downloading $NAME"
curl -L --fail --progress-bar "$URL" -o "$TMP/cli.zip"
unzip -qo "$TMP/cli.zip" -d "$TMP/out"
FOUND="$(find "$TMP/out" -type f -name 'TwitchDownloaderCLI' -print -quit)"
[ -n "$FOUND" ] || { echo "TwitchDownloaderCLI not found in archive" >&2; exit 1; }
cp "$FOUND" "$BIN"
chmod +x "$BIN"
echo "Installed $BIN"
