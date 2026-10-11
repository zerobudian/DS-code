#!/usr/bin/env bash
set -euo pipefail
DSCODE_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$DSCODE_ROOT"
if [[ -n "${HTTP_PROXY:-}" ]]; then
  export ELECTRON_GET_USE_PROXY=1
  export GLOBAL_AGENT_HTTP_PROXY="$HTTP_PROXY"
  export GLOBAL_AGENT_HTTPS_PROXY="${HTTPS_PROXY:-$HTTP_PROXY}"
fi
if [[ ! -f node_modules/electron/path.txt ]]; then
  npm ci
fi
# Electron owns Sidecar startup, readiness and cleanup.
exec npm start -- "$@"
