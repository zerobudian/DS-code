#!/usr/bin/env bash
set -euo pipefail
# Stop only an explicitly selected DS Code process. Closing its window also
# triggers graceful Sidecar cleanup; never kill arbitrary port owners.
if [[ $# -ne 1 || ! "$1" =~ ^[0-9]+$ || "$1" -le 1 ]]; then
  echo '用法: ./stop.sh <DS Code Electron PID>；也可以关闭 DS Code 窗口。' >&2
  exit 2
fi
DSCODE_COMMAND="$(ps -p "$1" -o args=)"
DSCODE_ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ "$DSCODE_COMMAND" != *"$DSCODE_ROOT/node_modules/electron/dist/"* ]]; then
  echo '所选 PID 不是当前 DS Code 安装的 Electron 进程。' >&2
  exit 1
fi
kill -TERM "$1"
