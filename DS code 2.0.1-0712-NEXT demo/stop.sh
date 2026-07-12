#!/bin/bash
# DScode V2 — 一键停止
SIDECAR_PORT=8765
echo "🛑 正在停止 DScode..."
lsof -ti:$SIDECAR_PORT 2>/dev/null | xargs kill -9 2>/dev/null && echo "✅ Sidecar 已停止" || echo "ℹ️  Sidecar 未运行"
pkill -f "npx electron" 2>/dev/null && echo "✅ Electron 已关闭" || echo "ℹ️  Electron 未运行"
echo "👋 已退出"
