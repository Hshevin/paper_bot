#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .build
PAPERHARBOR_PORT_FILE="$(mktemp "$PWD/.build/mock-port.XXXXXX")"
python3 scripts/mock-api.py "$PAPERHARBOR_PORT_FILE" &
PAPERHARBOR_MOCK_PID=$!
trap 'kill "$PAPERHARBOR_MOCK_PID" 2>/dev/null || true; rm -f "$PAPERHARBOR_PORT_FILE"' EXIT
for attempt in {1..50}; do
    if [ -s "$PAPERHARBOR_PORT_FILE" ]; then break; fi
    sleep 0.1
done
PAPERHARBOR_TEST_API="http://127.0.0.1:$(cat "$PAPERHARBOR_PORT_FILE")/v1" swift run PaperCoreChecks
