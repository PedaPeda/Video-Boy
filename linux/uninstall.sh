#!/bin/sh
# Removes a Video Boy installation made with install.sh (use --system for /usr/local).
if [ "${1:-}" = "--system" ]; then BIN=/usr/local/bin; SHARE=/usr/local/share
else BIN=$HOME/.local/bin; SHARE=${XDG_DATA_HOME:-$HOME/.local/share}; fi
rm -f "$BIN/video-boy" "$BIN/video-boy-cli" "$SHARE/applications/video-boy.desktop" \
      "$SHARE/icons/hicolor/scalable/apps/video-boy.svg" "$SHARE/icons/hicolor/256x256/apps/video-boy.png"
rm -rf "$SHARE/video-boy"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$SHARE/applications" 2>/dev/null || true
echo "Video Boy removed. (Browser profile data, if any: ${XDG_CONFIG_HOME:-$HOME/.config}/video-boy)"
