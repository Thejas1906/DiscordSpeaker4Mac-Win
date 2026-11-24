#!/usr/bin/env bash
# Build DiscordSpeaker.app and a DMG on macOS for the *current* CPU architecture.
# Run once on an Apple Silicon Mac and once on an Intel Mac (or CI runner) for the two artifacts: libopus and
# the Python runtime are architecture-specific. Output: dist/DiscordSpeaker-<version>-macos-<arch>.dmg
#
# Signing: set DS_SIGN_IDENTITY="Developer ID Application: Name (TEAMID)" for a Developer ID build.
# Without it the app is ad-hoc signed (runs locally; Gatekeeper warns on other Macs). Notarization:
#   xcrun notarytool submit dist/*.dmg --keychain-profile <profile> --wait && xcrun stapler staple dist/*.dmg
set -euo pipefail
cd "$(dirname "$0")/.."
ARCH=$(uname -m); VERSION=${DS_VERSION:-2.0.0}
PY=${PYTHON:-python3}
command -v librespot >/dev/null || { echo "Install librespot first: brew install librespot"; exit 1; }
export DS_LIBRESPOT=$(python3 -c "import os,shutil;print(os.path.realpath(shutil.which('librespot')))")

src/audio/platforms/macos/tap/build.sh
mkdir -p build
if [ ! -f build/DiscordSpeaker.icns ]; then
  I=build/DiscordSpeaker.iconset; rm -rf "$I"; mkdir -p "$I"
  for s in 16 32 64 128 256 512; do sips -z $s $s src/ui/assets/logo.png --out "$I/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) src/ui/assets/logo.png --out "$I/icon_${s}x${s}@2x.png" >/dev/null; done
  iconutil -c icns "$I" -o build/DiscordSpeaker.icns
fi
DS_VERSION=$VERSION $PY -m PyInstaller packaging/discordspeaker.spec --noconfirm --distpath dist --workpath build/pyi

APP=dist/DiscordSpeaker.app
IDENT=${DS_SIGN_IDENTITY:--}
if [ "$IDENT" = "-" ]; then
  # Ad-hoc: no hardened runtime (library validation would reject the bundled Python/Qt/libopus signatures).
  OPTS=()
else
  OPTS=(--options runtime --timestamp --entitlements packaging/macos/entitlements.plist)
fi
# Sign inside-out: helper + libraries first, then the bundle
find "$APP" -type f \( -name "*.dylib" -o -name "*.so" -o -name dsaudiotap -o -path "*/Python.framework/Versions/*/Python" \) \
  -exec codesign --force ${OPTS[@]+"${OPTS[@]}"} -s "$IDENT" {} \;
codesign --force --deep ${OPTS[@]+"${OPTS[@]}"} -s "$IDENT" "$APP"
codesign --verify --deep --strict "$APP"

DMG="dist/DiscordSpeaker-$VERSION-macos-$ARCH.dmg"
rm -f "$DMG"; STAGE=$(mktemp -d); cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
# hdiutil sometimes fails with "Resource busy" right after signing (seen on CI runners): retry.
for attempt in 1 2 3 4 5; do
  hdiutil create -volname "DiscordSpeaker" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null && break
  [ "$attempt" = 5 ] && { echo "hdiutil failed after 5 attempts"; exit 1; }
  echo "hdiutil busy, retrying ($attempt)..."; sleep $((attempt * 3))
done
echo "Built $DMG"
