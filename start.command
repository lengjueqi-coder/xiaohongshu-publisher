#!/bin/zsh
set -u
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
APP_VERSION="2026.11.4"

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "小红书发布助手缺少 Python 3" message "请先运行安装教程中的 Python 安装步骤。"'
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$APP_DIR/install.command" || exit $?
fi

if response="$(curl -fsS --max-time 3 "http://127.0.0.1:5818/api/version" 2>/dev/null)"; then
  if [[ "$response" == *"\"version\": \"$APP_VERSION\""* || "$response" == *"\"version\":\"$APP_VERSION\""* ]]; then
    open "http://127.0.0.1:5818"
    exit 0
  fi
  echo "端口 5818 已被旧版本小红书助手占用。请先双击 stop.command，再重新运行 start.command。"
  exit 1
fi

if curl -fsS --max-time 3 "http://127.0.0.1:5818/api/status" >/dev/null 2>&1; then
  echo "端口 5818 已被旧版本或其他小红书助手服务占用。请先双击 stop.command，再重新运行 start.command。"
  exit 1
fi

(sleep 1; open "http://127.0.0.1:5818") &
exec "$APP_DIR/.venv/bin/python" app/server.py
