#!/usr/bin/env bash
# ============================================================
# 弹出「调试专用」Chrome（独立配置目录，不影响日常使用的 Chrome）
# 用法: bash "${SKILL_DIR}/scripts/open-debug-chrome.sh" [要打开的网址]
# 用户在弹窗里登录一次 → 爬虫配置 cdp=http://127.0.0.1:9222 复用登录态
# ============================================================
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || CHROME="$(command -v google-chrome-stable || command -v google-chrome || true)"
if [ -z "$CHROME" ]; then
  echo "❌ 未找到 Chrome，请先安装：https://www.google.com/chrome/"
  exit 1
fi
PROFILE="$HOME/.universal-scraper/cdp_profile"
mkdir -p "$PROFILE"
chmod 700 "$PROFILE" 2>/dev/null  # 审查修复（M）：CDP profile 含登录态 Cookie，收紧目录权限
START_URL="${1:-about:blank}"
# 审查修复（L）：START_URL 只放行 http/https 与 about:blank（防参数注入怪协议）
case "$START_URL" in
  http://*|https://*|about:blank) ;;
  *) echo "⚠️ 起始 URL 非法（仅 http/https/about:blank），已回退 about:blank"; START_URL="about:blank" ;;
esac

# 审查修复（M）：端口 9222 有监听 ≠ 是我们的调试 Chrome——验证 /json/version
# 返回体含 Browser 字段再复用，否则提示端口被其他程序占用
if curl -s -m 2 http://127.0.0.1:9222/json/version 2>/dev/null | grep -q '"Browser"'; then
  echo "✅ 调试端口 9222 已在运行，直接复用。"
elif curl -s -m 1 http://127.0.0.1:9222 >/dev/null 2>&1; then
  echo "❌ 端口 9222 已被其他程序占用（响应不是 Chrome DevTools 协议）"; exit 1
else
  open -na "$CHROME" --args --remote-debugging-port=9222 --user-data-dir="$PROFILE" \
       --no-first-run --no-default-browser-check "$START_URL"
  ok=0
  for _ in $(seq 1 15); do
    if curl -s -m 1 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] && echo "🚀 调试 Chrome 已启动（端口 9222）" \
               || { echo "❌ 9222 未能启动（Chrome 未装好或被拦截）"; exit 1; }
fi
echo "请在弹出的 Chrome 窗口里完成【登录/验证】，完成后回来告诉向导「好了」。"
exit 0
