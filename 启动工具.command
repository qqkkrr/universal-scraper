#!/bin/bash
# 万能爬虫工具 · 一键启动（macOS 双击运行）
cd "$(dirname "$0")"
PY=""
# 1) 优先用自带 lxml 的 Python
for cand in "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import lxml" >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 未找到带 lxml 的 Python。请先双击「安装依赖.command」安装依赖，然后重试。"
  read -n 1 -s -r -p "按任意键退出..."
  exit 1
fi
echo "🕷️ 正在启动万能爬虫工具（Python: $PY）..."
"$PY" -m universal_scraper.cli webui
