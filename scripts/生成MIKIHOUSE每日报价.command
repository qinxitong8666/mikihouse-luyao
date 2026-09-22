#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
PYTHON="$PROJECT_DIR/.venv/bin/python"
ENTRYPOINT="$PROJECT_DIR/scripts/generate_daily_quote.py"

if [[ ! -x "$PYTHON" ]]; then
  echo "启动失败：找不到项目 Python：$PYTHON"
  read "?按回车关闭。"
  exit 1
fi

if [[ ! -f "$ENTRYPOINT" ]]; then
  echo "启动失败：找不到每日报价入口：$ENTRYPOINT"
  read "?按回车关闭。"
  exit 1
fi

cd "$PROJECT_DIR"
if PYTHONPATH=src "$PYTHON" "$ENTRYPOINT"; then
  open "$PROJECT_DIR/outputs/daily_quote/最新"
else
  status=$?
  echo "生成失败并已安全停止；未覆盖上一次成功报价。"
  read "?按回车关闭。"
  exit "$status"
fi
