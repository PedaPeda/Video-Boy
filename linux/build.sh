#!/bin/sh
# Builds the Linux packages into linux/dist/:
#   video-boy_<version>_all.deb      Debian, Ubuntu, Linux Mint, Pop!_OS, ...
#   video-boy-<version>-linux.tar.gz any distribution (./install.sh)
# Needs dpkg-deb for the .deb (the tar.gz is built anyway).
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^VERSION="\(.*\)"/\1/p' video-boy)
DIST=dist
rm -rf "$DIST" && mkdir -p "$DIST"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}

# --- tar.gz -------------------------------------------------------------------
T=$DIST/video-boy-$VERSION
mkdir -p "$T/app"
cp video-boy video-boy-cli video-boy.desktop video-boy.svg video-boy.png install.sh uninstall.sh "$T/"
cp ../web/index.html ../videoboy.py "$T/app/"
cat > "$T/README.txt" <<TXT
Video Boy $VERSION for Linux

Install for your user:   ./install.sh
Install system-wide:     sudo ./install.sh --system
Remove:                  ./uninstall.sh   (or sudo ./uninstall.sh --system)
Run without installing:  ./video-boy

The app opens in its own window using a Chromium-based browser (Chrome, Chromium,
Brave, Edge, Vivaldi) or Firefox. Everything runs locally; nothing is uploaded.
Command-line converter: video-boy-cli (needs python3, python3-numpy and ffmpeg).
https://github.com/PedaPeda/Video-Boy
TXT
# run-in-place support: the launcher looks for ../web or ../share; give it app/ directly
sed -i 's|"\$here/../web"; do|"$here/../web" "$here/app"; do|' "$T/video-boy"
sed -i 's|"\$(dirname "\$(readlink -f "\$0")")/.."; do|"$(dirname "$(readlink -f "$0")")/.." "$(dirname "$(readlink -f "$0")")/app"; do|' "$T/video-boy-cli"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH" \
    -C "$DIST" -czf "$DIST/video-boy-$VERSION-linux.tar.gz" "video-boy-$VERSION"
rm -rf "$T"

# --- .deb ---------------------------------------------------------------------
if command -v dpkg-deb >/dev/null 2>&1; then
    R=$DIST/deb
    mkdir -p "$R/DEBIAN" "$R/usr/bin" "$R/usr/share/video-boy" "$R/usr/share/applications" \
             "$R/usr/share/icons/hicolor/scalable/apps" "$R/usr/share/icons/hicolor/256x256/apps" \
             "$R/usr/share/doc/video-boy"
    install -m 755 video-boy video-boy-cli "$R/usr/bin/"
    install -m 644 ../web/index.html ../videoboy.py "$R/usr/share/video-boy/"
    install -m 644 video-boy.desktop "$R/usr/share/applications/"
    install -m 644 video-boy.svg "$R/usr/share/icons/hicolor/scalable/apps/"
    install -m 644 video-boy.png "$R/usr/share/icons/hicolor/256x256/apps/"
    cat > "$R/usr/share/doc/video-boy/copyright" <<TXT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Video Boy
Source: https://github.com/PedaPeda/Video-Boy
TXT
    SIZE=$(du -sk "$R/usr" | cut -f1)
    cat > "$R/DEBIAN/control" <<TXT
Package: video-boy
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Maintainer: PedaPeda <https://github.com/PedaPeda>
Installed-Size: $SIZE
Depends: python3
Recommends: chromium | chromium-browser | google-chrome-stable | brave-browser | microsoft-edge-stable | firefox | firefox-esr, xdg-utils
Suggests: python3-numpy, ffmpeg
Homepage: https://github.com/PedaPeda/Video-Boy
Description: turn videos into ROMs for Nintendo handhelds
 Video Boy converts any video into a ROM that plays the clip with picture
 and sound on the Game Boy, Game Boy Color, Game Boy Advance / Micro and
 Nintendo DS / 3DS (via a flash cart). The converter opens in its own
 window and runs entirely offline; nothing is uploaded.
 .
 The package also contains the command-line converter video-boy-cli
 (needs python3-numpy and ffmpeg).
TXT
    cat > "$R/DEBIAN/postinst" <<'TXT'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
TXT
    cp "$R/DEBIAN/postinst" "$R/DEBIAN/postrm"
    chmod 755 "$R/DEBIAN/postinst" "$R/DEBIAN/postrm"
    find "$R" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
    dpkg-deb --root-owner-group -Zxz --build "$R" "$DIST/video-boy_${VERSION}_all.deb" >/dev/null
    rm -rf "$R"
fi
ls -l "$DIST"
