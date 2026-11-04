#!/bin/zsh
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "警告：当前不是 Apple Silicon arm64，仍可尝试安装，但本套安装包按 M 芯片 Mac 验证。"
fi

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "未找到 python3。请安装 Python 3.11 或更高版本后重新运行。"
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

PYTHON_OK="$($PYTHON -c 'import sys; print(int(sys.version_info >= (3, 11)))')"
if [[ "$PYTHON_OK" != "1" ]]; then
  echo "需要 Python 3.11 或更高版本。当前版本：$($PYTHON --version)"
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$APP_DIR/.venv"
fi

"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
# Bridge 通过脚本目录直接运行，不需要把 extension/skills 作为 Python 包安装。
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements-mac.txt"
chmod +x "$APP_DIR/start.command" "$APP_DIR/install.command" "$APP_DIR/stop.command"

echo "安装完成。"
echo "下一步："
echo "1. 在 Chrome 打开 chrome://extensions/，加载未打包扩展："
echo "   $APP_DIR/app/vendor/xiaohongshu-skills/extension"
echo "2. 在 Chrome 登录小红书。"
echo "3. 双击 start.command 启动软件。"
read -r "?按回车关闭此窗口："
