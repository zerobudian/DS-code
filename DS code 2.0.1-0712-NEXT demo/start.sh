#!/bin/bash
# DScode V2 — 一键启动脚本
# 启动 Sidecar + Electron

SIDECAR_PORT=8765
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 正在启动 DScode V2..."
echo ""

# 1. 杀掉占用端口的旧进程
echo "[1/3] 清理旧进程..."
lsof -ti:$SIDECAR_PORT 2>/dev/null | xargs kill -9 2>/dev/null
sleep 0.5

# 2. 启动 Sidecar（后台）
echo "[2/3] 启动 Python Sidecar..."
cd "$DIR"
python3 sidecar/sidecar.py &
SIDECAR_PID=$!

# 3. 等待就绪
echo "[3/3] 等待 Sidecar 就绪..."
for i in $(seq 1 30); do
  sleep 0.3
  if curl -s http://127.0.0.1:$SIDECAR_PORT/api/health > /dev/null 2>&1; then
    echo "✅ Sidecar 已就绪（PID=$SIDECAR_PID）"
    echo ""
    echo "🎯 启动 Electron..."
    # 按下 Ctrl+C 时同时杀掉 sidecar
    trap "echo ''; echo '正在关闭...'; kill $SIDECAR_PID 2>/dev/null; exit" SIGINT SIGTERM
    npx electron . --dev
    break
  fi
done

# 如果循环结束还没就绪
if ! curl -s http://127.0.0.1:$SIDECAR_PORT/api/health > /dev/null 2>&1; then
  echo "❌ Sidecar 启动超时，请检查端口 $SIDECAR_PORT"
  kill $SIDECAR_PID 2>/dev/null
  exit 1
fi

# 关闭 Electron 后清理
echo "关闭中..."
kill $SIDECAR_PID 2>/dev/null
echo "👋 已退出"
