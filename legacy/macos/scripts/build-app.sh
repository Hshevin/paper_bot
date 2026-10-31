#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
PAPERHARBOR_APP="$PWD/dist/PaperHarbor.app"
if /bin/ps -axo comm= | /usr/bin/grep -Fxq "$PAPERHARBOR_APP/Contents/MacOS/PaperHarbor"; then
    echo '请先退出 dist 中的论文港应用，再重新打包。' >&2
    exit 1
fi
swift build -c release
PAPERHARBOR_BIN="$(swift build -c release --show-bin-path)"
mkdir -p "$PAPERHARBOR_APP/Contents/MacOS" "$PAPERHARBOR_APP/Contents/Library/LaunchAgents" "$PAPERHARBOR_APP/Contents/Resources"
cp "$PAPERHARBOR_BIN/PaperHarbor" "$PAPERHARBOR_APP/Contents/MacOS/PaperHarbor"
cp "$PAPERHARBOR_BIN/PaperHarborAgent" "$PAPERHARBOR_APP/Contents/MacOS/PaperHarborAgent"
cp Resources/Info.plist "$PAPERHARBOR_APP/Contents/Info.plist"
cp Resources/app.paperharbor.agent.plist "$PAPERHARBOR_APP/Contents/Library/LaunchAgents/"
/usr/bin/plutil -lint "$PAPERHARBOR_APP/Contents/Info.plist" "$PAPERHARBOR_APP/Contents/Library/LaunchAgents/app.paperharbor.agent.plist"
# Finder can add metadata after a previous launch; remove only signing-incompatible
# attributes from this generated bundle, preserving other filesystem metadata.
/usr/bin/xattr -dr com.apple.FinderInfo "$PAPERHARBOR_APP" 2>/dev/null || true
/usr/bin/xattr -dr com.apple.ResourceFork "$PAPERHARBOR_APP" 2>/dev/null || true
# Local development signing. Distribution requires a Developer ID and notarization.
/usr/bin/codesign --force --sign - --identifier app.paperharbor.agent "$PAPERHARBOR_APP/Contents/MacOS/PaperHarborAgent"
/usr/bin/codesign --force --sign - "$PAPERHARBOR_APP"
/usr/bin/codesign --verify --deep --strict "$PAPERHARBOR_APP"
printf 'Built: %s\n' "$PAPERHARBOR_APP"
