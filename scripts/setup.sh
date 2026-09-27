#!/usr/bin/env bash
# ============================================================
# 万能爬虫技能 · 一键环境安装（幂等，可重复跑）
# 用法: bash "${SKILL_DIR}/scripts/setup.sh"
# ============================================================
# 审查修复（H）：`set -e` 曾缺——cd/npm 任一步失败后脚本继续跑，把"成功安装"
# 的假象打满全屏；符号链接解析改用 cd -P 物理路径（通过链接调用也能定位技能根）
set -euo pipefail
SKILL_DIR="$(cd -P "$(dirname "$0")/.." && pwd)"
cd "$SKILL_DIR"

say() { printf "\n\033[1;36m%s\033[0m\n" "$*"; }

say "📦 [1/4] 安装 Python 依赖…"
if ! python3 -m pip install -q -r "$SKILL_DIR/requirements.txt" 2>/dev/null; then
  echo "  常规安装失败，改用 --user 方式重试…"
  python3 -m pip install -q --user -r "$SKILL_DIR/requirements.txt"
fi
# 可选增强（失败不打扰）：ddddocr 验证码识别 / rapidocr 扫描件兜底（cli research OCR 链）
python3 -m pip install -q ddddocr 2>/dev/null || echo "  （可选）验证码识别库 ddddocr 未装，需要时再补"
python3 -m pip install -q rapidocr_onnxruntime 2>/dev/null || echo "  （可选）扫描件 OCR 库 rapidocr 未装——research 批量遇扫描件时建议补装"

say "🟢 [2/4] 检查 Node 运行时…"
if ! command -v node >/dev/null 2>&1; then
  echo "  ❌ 未找到 Node.js（浏览器方案需要它）。"
  echo "     安装: brew install node   或   https://nodejs.org 下载安装包"
  echo "     （只抓普通网页可以不装 Node，先用 HTTP 直抓。）"
else
  echo "  Node $(node -v) ✓"
fi

if command -v node >/dev/null 2>&1; then
  say "🌐 [3/4] 安装浏览器引擎（首次约 1-2 分钟，之后秒级）…"
  # 审查修复（H）：package.json 曾被无条件覆盖——若用户在技能目录手工初始化过
  # npm 项目（有 dependencies/scripts），会被单行占位 JSON 毁掉。只在缺失时创建
  if [ ! -f package.json ]; then
    echo '{"name":"universal-scraper-skill","private":true}' > package.json
  elif ! node -e "JSON.parse(require('fs').readFileSync('package.json'))" 2>/dev/null; then
    echo "  ⚠️ package.json 已损坏（非合法 JSON），跳过 npm 安装以免覆盖"
    echo "  ⚠️ 请手工修复 package.json 后重跑 setup"
  else
    npm install --no-fund --no-audit --silent playwright patchright || echo "  ⚠️ npm 安装失败，浏览器方案暂不可用"
    # 审查修复（H）：npx 交互确认曾挂死无人值守脚本——CI 环境加 --yes
    npx --yes --no-install playwright install chromium >/dev/null 2>&1 \
      || npx --yes playwright install chromium >/dev/null 2>&1 \
      || echo "  （Chromium 下载跳过/失败——有本机 Chrome 即可，走 CDP 方案）"
  fi
fi

say "🩺 [4/4] 体检结果"
python3 "$SKILL_DIR/scripts/doctor.py"
