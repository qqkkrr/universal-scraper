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
  # 用 open -na 启动独立实例（比直接跑二进制可靠，不会弹 Safari）
  open -na "$CHROME" --args --remote-debugging-port=9222 --user-data-dir="$PROFILE" \
       --no-first-run --no-default-browser-check "https://login.taobao.com"
  # 等端口就绪（最多 15 秒）
  for i in $(seq 1 15); do
    if curl -s -m 1 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
      echo "🚀 调试 Chrome 已启动（端口 9222）"
      break
    fi
    sleep 1
  done
  if ! curl -s -m 1 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo "❌ 9222 未能启动。可能原因：Chrome 未安装/被防火墙拦截。"
  fi
fi
echo ""
echo "请在弹出的 Chrome 窗口里【扫码登录淘宝】（只需一次，之后自动复用）。"
echo "登录完成后，回到 Codex 告诉我「已登录」，即可开始抓取。"
echo ""
exit 0
