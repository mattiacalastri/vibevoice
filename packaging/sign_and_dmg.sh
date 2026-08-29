#!/bin/bash
# SPDX-License-Identifier: MIT
# Sign VibeVoice.app (Developer ID, hardened runtime), package a DMG, and
# notarize if a notarytool keychain profile exists. Run from repo root AFTER
# packaging/build_release.sh:  bash packaging/sign_and_dmg.sh
set -euo pipefail
cd "$(dirname "$0")/.."

APP="packaging/dist/VibeVoice.app"
DMG="packaging/dist/VibeVoice.dmg"
IDENTITY="Developer ID Application: MATTIA CALASTRI (54582KN4KW)"
NOTARY_PROFILE="${NOTARY_PROFILE:-polpo-notary}"
test -d "$APP" || { echo "missing $APP — run packaging/build_release.sh first"; exit 1; }

ENTITLEMENTS="$(mktemp -t vibevoice-entitlements).plist"
# Hardened runtime blocks exactly what a py2app+MLX app needs, so re-allow:
#   audio-input                       — mic capture (TCC prompt still applies)
#   allow-unsigned-executable-memory  — MLX Metal JIT + ctypes trampolines
#   disable-library-validation        — python .so files are signed by us, not Apple
cat > "$ENTITLEMENTS" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.device.audio-input</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
EOF

echo "── sign: nested Mach-O first, then the bundle ──"
find "$APP" \( -name "*.so" -o -name "*.dylib" -o -name "*.metallib" \) -print0 |
  xargs -0 -n 50 codesign --force --timestamp --options runtime --sign "$IDENTITY" 2>/dev/null
# Frameworks + embedded executables (python binary inside Resources, if any)
find "$APP/Contents" -type f -perm +111 ! -name "*.py*" ! -name "*.so" ! -name "*.dylib" -print0 |
  xargs -0 -n 20 codesign --force --timestamp --options runtime --sign "$IDENTITY" 2>/dev/null || true
# Python.framework's main dylib is named "Python" (no extension) and shipped
# mode 644 — both finds above skip it, and the notary rejects the DMG for that
# one file (submission a3e90245). Sign the versioned framework explicitly.
for fw in "$APP"/Contents/Frameworks/*.framework; do
  [ -d "$fw" ] || continue
  ver="$(ls "$fw/Versions" 2>/dev/null | grep -v Current | head -1)"
  [ -n "$ver" ] && codesign --force --timestamp --options runtime --sign "$IDENTITY" "$fw/Versions/$ver"
done
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP"

echo "── verify ──"
codesign --verify --strict --deep "$APP" && echo "codesign: OK"
spctl -a -vv "$APP" 2>&1 | tail -2 || echo "(spctl rejection is EXPECTED before notarization)"

echo "── dmg ──"
rm -f "$DMG"
STAGE="$(mktemp -d -t vibevoice-dmg)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "VibeVoice" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"
du -sh "$DMG"

echo "── notarize (best-effort) ──"
if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  xcrun stapler staple "$DMG"
  echo "notarized + stapled"
else
  cat <<EOF
notarytool profile '$NOTARY_PROFILE' not found — SKIPPING notarization.
One-time human step (needs the Apple ID app-specific password):
  xcrun notarytool store-credentials $NOTARY_PROFILE \\
    --apple-id <apple-id-email> --team-id 54582KN4KW
then re-run: bash packaging/sign_and_dmg.sh
EOF
fi
echo "Done: $DMG"
