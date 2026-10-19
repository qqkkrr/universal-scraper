# -*- coding: utf-8 -*-
"""前端 JS 语法回归：webui/index.html 内嵌 <script> 必须可解析。"""
import subprocess, re
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "webui" / "index.html"

def test_index_js_syntax():
    s = HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", s)
    assert m, "未找到内嵌 <script>"
    code = m.group(1)
    r = subprocess.run(["node", "-e", f"new Function({code!r}); console.log('OK')"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:300]}"
    assert "OK" in r.stdout

def test_index_contains_key_ui():
    s = HTML.read_text(encoding="utf-8")
    for key in ("a_guide", "diag_wrap", "a_examples", "第一次用？"):
        assert key in s, f"缺少 UI 元素/文案: {key}"


def test_stop_button_wiring_uses_data_attr():
    """停止按钮必须用 data 属性传递 job id；禁止回归到引号拼接坏模式（点击会变成字面串）。"""
    s = HTML.read_text(encoding="utf-8")
    assert 'data-stop-job' in s, "缺少 data-stop-job 绑定"
    assert "stopJob('+esc(d.id)+')" not in s, "存在引号拼接坏模式：onclick 会收到字面串而不是 job id"


def test_index_contains_novice_flow_ui():
    """小白一步完成主路径：直达按钮、三步向导、数据预览、文件可操作。"""
    s = HTML.read_text(encoding="utf-8")
    for key in ("a_btn_direct", "startAutoDirect", "wizardBuild", "w_url",
                "loadPreview", "copyPath", "revealPath", "data-path",
                "手把手三步", "你抓到的数据", "打开所在文件夹"):
        assert key in s, f"缺少小白流程 UI/函数: {key}"


def test_start_buttons_recover_after_finish():
    """任务结束后启动按钮必须恢复：小白不刷新页面就能改描述再跑。
    恢复必须按 jobId 精准进行（并发任务不互相提前解锁）。"""
    s = HTML.read_text(encoding="utf-8")
    assert "function resetStartButtons" in s
    assert s.count("resetStartButtons(jobId);") >= 2, "pollJob 完成/出错两条路径都要恢复按钮"
    assert "jobBtns.set(d.job, btn)" in s, "startJob 需登记 jobId→按钮用于精准恢复"


def test_polljob_error_task_not_short_circuited():
    """任务级失败（status=error 且带 error 字段）必须走 renderResult 完整失败 UX，
    只有请求级失败（无 status 键）才短路为一行红字。"""
    s = HTML.read_text(encoding="utf-8")
    assert "d.error && d.status===undefined" in s, \
        "pollJob 不得把任务失败当传输错误短路（会丢解决方案卡/AI 诊断/重跑按钮）"


def test_auto_buttons_mutex_and_diagnose_debounce():
    s = HTML.read_text(encoding="utf-8")
    assert "lockAutoButtons" in s and s.count("lockAutoButtons(true);") >= 2, \
        "直达/看计划两个入口在运行与生成计划期间必须互斥"
    assert "autoDiagnosed" in s, "自动 AI 诊断需要按 jobId 去重"
    assert "history-box" in s, "历史回看卡需防堆叠"


def test_solution_action_uses_api_function_not_string():
    """回归：解决方案卡「打开调试 Chrome」曾因参数名 api 遮蔽全局 api() 请求函数，
    点击报 "api is not a function"。node 行为级验证：必须把路径当参数传给 api() 并成功。"""
    s = HTML.read_text(encoding="utf-8")
    m1 = re.search(r"async function runSolutionAction[\s\S]*?\n}", s)
    m2 = re.search(r"function runSolutionActionFrom[\s\S]*?\n}", s)
    assert m1 and m2, "未找到解决方案动作函数"
    js = (
        "const calls=[];"
        "globalThis.api=async(path,opts)=>{calls.push(path);return {ok:true,message:'started'};};"
        "globalThis.alert=()=>{};"
        + m1.group(0) + m2.group(0) +
        "const btn={getAttribute:(k)=>k==='data-sol-api'?'/api/chrome/start'"
        ":(k==='data-sol-url'?'https://x.example/':'')};"
        "(async()=>{"
        "  runSolutionActionFrom(btn);"
        "  await new Promise(r=>setTimeout(r,50));"
        "  if(calls.length!==1||calls[0]!=='/api/chrome/start'){"
        "    console.error('BAD',JSON.stringify(calls));process.exit(1);}"
        "  console.log('OK');"
        "})();"
    )
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"行为回归: {r.stderr[:300]}"
    assert "OK" in r.stdout
