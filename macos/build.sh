#!/bin/bash
# Build Setlist.app with CLT only (no Xcode): swiftc + hand-rolled bundle.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build

echo "── compiling"
swiftc -O Sources/main.swift -o build/Setlist

if [ ! -f build/AppIcon.icns ]; then
  echo "── icon"
  swift gen_icon.swift build/icon1024.png
  ICONSET=build/AppIcon.iconset
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s build/icon1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z $d $d build/icon1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o build/AppIcon.icns
fi

echo "── bundling"
APP=build/Setlist.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/"
cp build/Setlist "$APP/Contents/MacOS/"
cp build/AppIcon.icns "$APP/Contents/Resources/"
codesign --force --sign - "$APP"

DEST=/Applications
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"
rm -rf "$DEST/Setlist.app"
cp -R "$APP" "$DEST/"
echo "── installed: $DEST/Setlist.app"
