#!/usr/bin/env bash
set -euo pipefail
DSCODE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$DSCODE_ROOT/stop.sh" "$@"
