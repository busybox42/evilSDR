#!/usr/bin/env bash
set -euo pipefail

APP_NAME=evilSDR
APPDIR=build/electron-appimage
RELEASE_DIR=electron-app/dist/linux-unpacked
ICON_SOURCE=assets/evilSDR.png

rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -r "$RELEASE_DIR"/* "$APPDIR"

# overwrite bundled resources with current repo tree
rm -rf "$APPDIR/resources/evilSDR"
mkdir -p "$APPDIR/resources/evilSDR"
rsync -a --delete \
  --exclude '.git' \
  --exclude 'build' \
  --exclude 'electron-app/node_modules' \
  --exclude 'electron-app/dist' \
  ./ "$APPDIR/resources/evilSDR/"

EVILSDR_ROOT="$APPDIR/resources/evilSDR"
BACKEND_DIR="$EVILSDR_ROOT/src/backend"
PYTHON_BIN=python3

rm -rf "$BACKEND_DIR/venv"
$PYTHON_BIN -m venv "$BACKEND_DIR/venv"
"$BACKEND_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$BACKEND_DIR/venv/bin/pip" install --no-cache-dir -r "$EVILSDR_ROOT/requirements.txt" >/dev/null

cat <<'APP' > "$APPDIR/AppRun"
#!/usr/bin/env bash
set -euo pipefail
here="$(dirname "$(readlink -f "$0")")"
BACKEND_RES="$here/resources/evilSDR/backend"
DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/evilSDR"
mkdir -p "$DATA_DIR/recordings"
cp -n "$BACKEND_RES/config.json" "$DATA_DIR/config.json" 2>/dev/null || true
cp -n "$BACKEND_RES/bookmarks.json" "$DATA_DIR/bookmarks.json" 2>/dev/null || true
cp -n "$BACKEND_RES/connections.json" "$DATA_DIR/connections.json" 2>/dev/null || true
cp -n "$BACKEND_RES/metadata_prefs.json" "$DATA_DIR/metadata_prefs.json" 2>/dev/null || true
export EVILSDR_CONFIG_FILE="$DATA_DIR/config.json"
export EVILSDR_BOOKMARKS_FILE="$DATA_DIR/bookmarks.json"
export EVILSDR_CONNECTIONS_FILE="$DATA_DIR/connections.json"
export EVILSDR_METADATA_PREFS_FILE="$DATA_DIR/metadata_prefs.json"
export EVILSDR_RECORDINGS_DIR="$DATA_DIR/recordings"
export PATH="$BACKEND_RES/venv/bin:$PATH"
exec "$here/evilsdr-electron" "$@"
APP
chmod +x "$APPDIR/AppRun"

mkdir -p "$APPDIR/usr/bin"
ln -sf "../AppRun" "$APPDIR/usr/bin/$APP_NAME"

cat <<'DESK' > "$APPDIR/$APP_NAME.desktop"
[Desktop Entry]
Name=evilSDR
Exec=evilSDR
Icon=evilSDR
Type=Application
Categories=Utility;
StartupNotify=false
DESK

cp "$ICON_SOURCE" "$APPDIR/$APP_NAME.png"
ln -sf "$APP_NAME.png" "$APPDIR/.DirIcon"

echo "Electron AppDir ready at $APPDIR. Build AppImage with appimagetool." 
