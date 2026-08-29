#!/bin/bash
# SPDX-License-Identifier: MIT
# Local Developer ID sign + DMG for VibeVoice, run BY MATTIA in his own Terminal.
# Fixes the "keychain keeps asking for the password" spam: unlock once + disable
# the idle auto-lock so codesign can hit the private key 260× without re-prompting.
# The partition-list (codesign: ACL) must already be set once (scar sess.9187).
#
#   cd ~/projects/💼-prodotti/vibevoice && bash packaging/sign_local.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

APP="packaging/dist/VibeVoice.app"
DMG="packaging/dist/VibeVoice.dmg"
IDENTITY="Developer ID Application: MATTIA CALASTRI (54582KN4KW)"
KC="$HOME/Library/Keychains/login.keychain-db"
test -d "$APP" || { echo "missing $APP — run packaging/build_release.sh first"; exit 1; }

echo "── unlock keychain (one password prompt) + kill idle auto-lock ──"
security unlock-keychain "$KC"          # prompts ONCE, securely
security set-keychain-settings "$KC"    # no -t/-l → no timeout, no lock-on-sleep

ENT="$(mktemp -t vv-ent).plist"
cat > "$ENT" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.device.audio-input</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
EOF

echo "── sign nested Mach-O (errors shown, don't stop on one) ──"
ok=0; fail=0
while IFS= read -r f; do
  if codesign --force --timestamp --options runtime --sign "$IDENTITY" "$f" 2>/tmp/vv_err.txt; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1)); echo "  FAIL $f"; head -1 /tmp/vv_err.txt
  fi
done < <(find "$APP" \( -name "*.so" -o -name "*.dylib" -o -name "*.metallib" \))
echo "  nested: ok=$ok fail=$fail"

echo "── sign embedded executables ──"
find "$APP/Contents" -type f -perm +111 ! -name "*.py*" ! -name "*.so" ! -name "*.dylib" -print0 |
  xargs -0 -n 20 codesign --force --timestamp --options runtime --sign "$IDENTITY" 2>/dev/null || true

echo "── sign the bundle (entitlements + hardened runtime) ──"
codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$IDENTITY" "$APP"

echo "── verify ──"
codesign --verify --strict --deep "$APP" && echo "  codesign: OK"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "Authority=Developer|flags"

echo "── dmg ──"
rm -f "$DMG"
STAGE="$(mktemp -d -t vv-dmg)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "VibeVoice" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"
echo "  DMG signed:"; du -sh "$DMG"

echo "── notarize (needs the one-time app-specific password profile) ──"
if xcrun notarytool history --keychain-profile polpo-notary >/dev/null 2>&1; then
  xcrun notarytool submit "$DMG" --keychain-profile polpo-notary --wait
  xcrun stapler staple "$APP" && xcrun stapler staple "$DMG" && echo "  notarized + stapled"
else
  echo "  notarytool profile 'polpo-notary' missing — skipping (see gesture #3)."
fi
echo "DONE → $DMG"
