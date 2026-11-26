#!/bin/sh
# Installs Video Boy for the current user (no root needed):
#   ~/.local/bin/video-boy, ~/.local/bin/video-boy-cli
#   ~/.local/share/video-boy/            the app and videoboy.py
#   application menu entry and icon
# Run with --system (as root) to install into /usr/local instead.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" = "--system" ]; then
    PREFIX=/usr/local
    BIN=$PREFIX/bin
    SHARE=$PREFIX/share
else
    BIN=$HOME/.local/bin
    SHARE=${XDG_DATA_HOME:-$HOME/.local/share}
fi

# Files come from the release archive (app/) or from a repository checkout (../web, ..).
if [ -f "$HERE/app/index.html" ]; then
    APP_HTML=$HERE/app/index.html; CLI_PY=$HERE/app/videoboy.py
else
    APP_HTML=$HERE/../web/index.html; CLI_PY=$HERE/../videoboy.py
fi
[ -f "$APP_HTML" ] || { echo "index.html not found" >&2; exit 1; }

mkdir -p "$BIN" "$SHARE/video-boy" "$SHARE/applications" \
         "$SHARE/icons/hicolor/scalable/apps" "$SHARE/icons/hicolor/256x256/apps"
install -m 644 "$APP_HTML" "$SHARE/video-boy/index.html"
[ -f "$CLI_PY" ] && install -m 644 "$CLI_PY" "$SHARE/video-boy/videoboy.py"
install -m 755 "$HERE/video-boy" "$BIN/video-boy"
install -m 755 "$HERE/video-boy-cli" "$BIN/video-boy-cli"
install -m 644 "$HERE/video-boy.svg" "$SHARE/icons/hicolor/scalable/apps/video-boy.svg"
install -m 644 "$HERE/video-boy.png" "$SHARE/icons/hicolor/256x256/apps/video-boy.png"
sed "s|^Exec=video-boy|Exec=$BIN/video-boy|" "$HERE/video-boy.desktop" \
    > "$SHARE/applications/video-boy.desktop"
chmod 644 "$SHARE/applications/video-boy.desktop"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$SHARE/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true

echo "Video Boy installed."
echo "  Start it from your application menu or run: video-boy"
echo "  Command-line converter: video-boy-cli movie.mp4   (needs python3-numpy and ffmpeg)"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  Note: $BIN is not in your PATH yet (log out and back in, or add it)." ;; esac
