#!/bin/bash
# night_launch.sh —— 无人值守夜测启动标准件（R22 配套）
#
# 吸收的真实事故（2026-09-10 NBS 夜测）：
#   launchd/cron 最小 PATH 下 python3=系统解释器，无 curl_cffi/requests，
#   插件静默降级 urllib，WARN 只在 stderr——整晚测的是降级通道。
# 本脚本的三道防线：钉死解释器 → 启动即验后端 → 抽验判型非 net_error。
#
# 用法（由 launchd/cron/手工调用均可）：
#   bash night_launch.sh \
#     --runner /path/to/runner.py \
#     --python /opt/anaconda3/bin/python3 \
#     --skill-dir /path/to/universal-scraper \
#     --targets targets.json --results results.json --log run.log \
#     --deadline "2026-09-12 09:00" \
#     [--runner-args "--with-diagnose --min-interval 8"] \
#     [--probe-url https://example.com/]
set -u

RUNNER="" ; PY_BIN="" ; SKILL_DIR="" ; TARGETS="" ; RESULTS="" ; LOG="" ; DEADLINE="" ; PROBE_URL=""
RUNNER_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runner) RUNNER="$2"; shift 2 ;;
    --python) PY_BIN="$2"; shift 2 ;;
    --skill-dir) SKILL_DIR="$2"; shift 2 ;;
    --targets) TARGETS="$2"; shift 2 ;;
    --results) RESULTS="$2"; shift 2 ;;
    --log) LOG="$2"; shift 2 ;;
    --deadline) DEADLINE="$2"; shift 2 ;;
    --probe-url) PROBE_URL="$2"; shift 2 ;;
    --runner-args) RUNNER_ARGS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
# 审查修复（M）：旗标缺值时 $2 是下一个旗标（如 "--log" 后直接 "--targets"）——
# 值会被错配到别的变量。解析完统一校验必填项
[ -z "$RUNNER" -o -z "$SKILL_DIR" -o -z "$RESULTS" -o -z "$LOG" ] && { echo "缺必填参数（--runner/--skill-dir/--results/--log）" >&2; exit 1; }

log() {
  # 审查修复（M）：log 目录不存在时曾静默丢弃全部日志——先 mkdir
  mkdir -p "$(dirname "$LOG.orchestrate.log")" 2>/dev/null
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG.orchestrate.log"
}

# ---- 防线1：钉死解释器（默认 anaconda；不存在则显式报错而不是静默降级） ----
if [ -z "$PY_BIN" ]; then
  for c in /opt/anaconda3/bin/python3 /opt/miniconda3/bin/python3 "$HOME/anaconda3/bin/python3"; do
    [ -x "$c" ] && PY_BIN="$c" && break
  done
fi
[ -z "$PY_BIN" ] && { log "❌ 未找到 anaconda python——拒绝用系统解释器启动（防静默降级）"; exit 1; }
"$PY_BIN" -c "import curl_cffi" 2>/dev/null || { log "❌ $PY_BIN 无 curl_cffi——环境不对，终止"; exit 1; }
log "解释器: $PY_BIN (curl_cffi OK)"

# ---- 防线2：目标站连通 ----
if [ -n "$PROBE_URL" ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$PROBE_URL" || echo ERR)
  log "目标站预检: HTTP $CODE"
  case "$CODE" in 200|301|302) ;; *) log "❌ 目标站不可达，终止"; exit 1 ;; esac
fi

# ---- 幂等：同结果文件的 runner 在更新中则不重复启动 ----
# 审查修复（H）：pgrep -f basename 曾误报——编辑器开着的同名脚本、grep 自身
# 都会命中。这里用"python + runner 名 + 结果文件名"的整条命令行正则（-f 全
# 命令行匹配）降低误报；不能用 -x（那要求整条命令行等于模式，含 .* 时恒不匹配）。
# 审查八轮（MEDIUM）：只匹配 runner 文件名 → 同一 runner 跑**不同 --results**
# 的第二个目标会被误判"已在运行"而静默 exit 0（该目标永不产出结果文件，
# cron/launchd 只看到退出码 0）。幂等键必须是"runner + 结果文件"。
if [ -n "$RESULTS" ] && pgrep -f "python.*$(basename "$RUNNER").*$(basename "$RESULTS")" > /dev/null 2>&1; then
  log "runner 已在运行（同一结果文件），跳过启动"
  exit 0
fi
if [ -z "$RESULTS" ] && pgrep -f "python.*$(basename "$RUNNER")" > /dev/null 2>&1; then
  log "runner 已在运行，跳过启动"
  exit 0
fi

# ---- 启动（审查修复 P0：`nohup "$CAFF"` 会把 "caffeinate -is" 整个当可执行
# 文件名 → rc=127，且错误被 /dev/null 吞掉，表象是"90 秒内退出"的误导日志。
# 改用数组组装命令行；子进程 stderr 并入编排日志保住降级 WARN） ----
# 旧结果归档而非删除（审查 P2：启动万一失败，别毁掉上一晚的数据）
[ -f "$RESULTS" ] && mv -f "$RESULTS" "$RESULTS.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null
CMD=("$PY_BIN" "$RUNNER")
[ -n "$TARGETS" ] && CMD+=(--targets "$TARGETS")
[ -n "$RESULTS" ] && CMD+=(--results "$RESULTS")
[ -n "$LOG" ] && CMD+=(--log "$LOG")
[ -n "$DEADLINE" ] && CMD+=(--deadline "$DEADLINE")
[ -n "$SKILL_DIR" ] && CMD+=(--skill-dir "$SKILL_DIR")
if [ -n "$RUNNER_ARGS" ]; then
  # shellcheck disable=SC2206
  CMD+=($RUNNER_ARGS)   # 约定：RUNNER_ARGS 不含带空格的词元、不含 glob
fi
if command -v caffeinate >/dev/null 2>&1; then
  nohup caffeinate -is "${CMD[@]}" >> "$LOG.orchestrate.log" 2>&1 &
else
  nohup "${CMD[@]}" >> "$LOG.orchestrate.log" 2>&1 &
fi
PID=$!
log "runner 启动 PID=$PID"
# 审查修复（L）：固定睡 90s 曾在 runner 秒退时白等——改轮询，死了立即进入
# 抽验分支给出诊断
for _w in $(seq 1 45); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 2
done

# ---- 防线3：90 秒抽验——进程活着、有产出证据、且后端没有降级 ----
if ! kill -0 "$PID" 2>/dev/null; then
  log "❌ runner 90 秒内退出——查 $LOG.orchestrate.log（启动 stderr 也在那里）"
  exit 1
fi
# 审查修复 P0-2：默认"无证据=不健康"——文件缺失/损坏/空 targets 都不算健康；
# runner 批量攒写时以 LOG 文件有内容作为放行的补充证据
HAS_EVIDENCE=0
[ -s "$LOG" ] && HAS_EVIDENCE=1
"$PY_BIN" - "$RESULTS" << 'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    recs = d.get("targets")
    if not isinstance(recs, dict) or not recs:
        print("NO-EVIDENCE: 结果文件存在但无 targets 记录"); sys.exit(2)
    verdicts = [r.get("verdict", "?") for r in recs.values()]
    bad = all(v in ("failed", "net_error", "runner_error") for v in verdicts)
    print(f"SAMPLE verdicts={verdicts[:5]} all_bad={bad}")
    sys.exit(2 if bad else 0)
except Exception as e:
    print(f"NO-EVIDENCE: 结果在 90s 时不可读: {e}"); sys.exit(3)
PYEOF
SAMPLE_RC=$?
if [ "$SAMPLE_RC" -eq 3 ] && [ "$HAS_EVIDENCE" -eq 1 ]; then
  log "⚠️ 结果未产出但 runner 日志有内容——按批量攒写放行（心跳继续观察）"
elif [ "$SAMPLE_RC" -ne 0 ]; then
  log "❌ 抽验失败（rc=$SAMPLE_RC）：疑似环境/降级/空转——止损终止，别让废数据跑一宿"
  # 审查修复 P1：只 kill PID 可能只杀掉 caffeinate 外壳、python 变孤儿继续跑——
  # 先杀子进程再杀本体，并验证确实死了
  # 审查修复（H）：pkill -P 只杀直接子进程——caffeinate 外壳下 python 再派生的
  # 子进程（浏览器桥等）会存活。递归收集进程树再逐个杀
  _tree="$$"
  _collect() {
    local _ppid="$1"
    for _cpid in $(pgrep -P "$_ppid" 2>/dev/null); do
      _tree="$_tree $_cpid"
      _collect "$_cpid"
    done
  }
  _collect "$PID"
  for _p in $_tree; do
    [ "$_p" = "$$" ] && continue  # 不杀自己
    pkill -TERM -P "$_p" 2>/dev/null
    kill -TERM "$_p" 2>/dev/null
  done
  sleep 2
  for _p in $_tree; do
    [ "$_p" = "$$" ] && continue
    if kill -0 "$_p" 2>/dev/null; then
      kill -9 "$_p" 2>/dev/null
    fi
  done
  log "⚠️ 需要强杀（进程树: $_tree）"
  exit 2
fi
log "✅ 启动健康，PID=$PID"
