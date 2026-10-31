#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/test.sh
bash scripts/build-app.sh
