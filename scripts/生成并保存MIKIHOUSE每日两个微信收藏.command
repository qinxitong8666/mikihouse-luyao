#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
PYTHON="$PROJECT_DIR/.venv/bin/python"
ENTRYPOINT="$PROJECT_DIR/scripts/run_mikihouse_daily_production.py"
CONFIRMATION_PHRASE="CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE"

if [[ ! -x "$PYTHON" || ! -f "$ENTRYPOINT" ]]; then
  echo "启动失败：项目 Python 或正式生产入口不存在。"
  read "?按回车关闭。"
  exit 1
fi

echo "本操作将先重新抓取官网并生成当日报价，然后创建恰好两条微信收藏："
echo "1. PDF版"
echo "2. LOSSLESS_COMPACT文字版"
echo "不会发送聊天，也不会修改或删除已有收藏。"
echo "默认生产开关关闭；未得到明确授权时会在官网抓取前安全停止。"
echo ""
read "confirmation?请输入完整确认语句 ${CONFIRMATION_PHRASE}："

cd "$PROJECT_DIR"
if PYTHONPATH=src "$PYTHON" "$ENTRYPOINT" \
  --production-save \
  --confirm "$confirmation"; then
  open "$PROJECT_DIR/outputs/daily_quote/最新"
else
  status=$?
  echo "流程已安全停止；不会自动重试任何微信写入。"
  read "?按回车关闭。"
  exit "$status"
fi
