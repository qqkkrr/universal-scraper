#!/bin/bash
# ============================================================
# 启动「淘宝/天猫抓取专用」调试 Chrome（独立配置，不影响正在用的 Chrome）
# 用法：双击本文件 → 在弹出的 Chrome 里扫码登录淘宝一次 → 之后工具自动复用
# ============================================================
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/.codex/taobao_cdp_profile"
mkdir -p "$PROFILE"
# 先确认 9222 没被占用
if curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "✅ 调试端口 9222 已在运行，直接使用。"
else
  "$CHROME" --remote-debugging-port=9222 --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check "https://login.taobao.com" >/dev/null 2>&1 &
  sleep 3
  echo "🚀 调试 Chrome 已启动（端口 9222）"
fi
echo ""
echo "请在弹出的 Chrome 窗口里【扫码登录淘宝】（只需一次，之后自动复用）。"
echo "登录完成后，回到 Codex 告诉我「已登录」，即可开始抓取。"
echo ""
open "http://127.0.0.1:9222/json/version" 2>/dev/null
sleep 2
exit 0
