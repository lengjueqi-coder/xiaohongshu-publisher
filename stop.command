#!/bin/zsh
set -u

PORT="5818"
# Stop the optional recovery wrapper as well, so a deliberate stop stays stopped.
# Its plist remains available for the next Mac login.
AGENT_TARGET="gui/$(id -u)/com.xhsassistant.publisher"
if launchctl print "$AGENT_TARGET" >/dev/null 2>&1; then
  launchctl bootout "$AGENT_TARGET" >/dev/null 2>&1 || true
fi
if ! curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "端口 ${PORT} 没有运行小红书助手。"
  exit 0
fi

PIDS="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null || true)"
if [[ -z "$PIDS" ]]; then
  echo "没有找到监听端口 ${PORT} 的进程。"
  exit 0
fi

for pid in ${(f)PIDS}; do
  if [[ "$pid" == <-> ]]; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
done

sleep 1
if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "服务仍在运行，请关闭启动它的终端窗口后再试。"
  exit 1
fi
echo "小红书助手已停止。"
