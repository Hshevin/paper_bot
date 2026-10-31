#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
PAPERHARBOR_DEST="$HOME/Applications/PaperHarbor.app"
if /bin/ps -axo comm= | /usr/bin/grep -Fq '/PaperHarbor.app/Contents/MacOS/PaperHarbor'; then
    echo '请先退出论文港，再安装。' >&2
    exit 1
fi
if /bin/launchctl print "gui/$(id -u)/app.paperharbor.agent" >/dev/null 2>&1; then
    echo '请先在论文港设置中停用后台更新，再安装。安装后可重新启用。' >&2
    exit 1
fi
if [ ! -d dist/PaperHarbor.app ]; then
    echo '请先运行 bash scripts/build-app.sh。' >&2
    exit 1
fi
mkdir -p "$HOME/Applications"
/usr/bin/ditto --norsrc --noextattr dist/PaperHarbor.app "$PAPERHARBOR_DEST"
/usr/bin/codesign --verify --deep --strict "$PAPERHARBOR_DEST"
printf 'Installed: %s\n' "$PAPERHARBOR_DEST"
