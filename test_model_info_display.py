# -*- coding: utf-8 -*-
"""验证「界面显示的模型版本与参数大小」是真的**从本机现读**的。

用户要求（2026-09-26 原话）：
    「前端显示的模型版本以及参数大小需要读取并换成最新的」

背景：界面原来只说"模型就绪 / 模型未下载"，**连自己在用哪个模型、多大参数都看不到** ——
这台机器已经换过好几次模型（sd-turbo→SDXL、qwen2.5-coder→qwen3-coder），
换完用户无从确认到底生效了没有；而且左下角那句和设置里的 tooltip 里
**还留着写死的旧模型名**（"Qwen 智能体"、"qwen2.5-coder"）——
换模型之后这些文案就是错的。

这个脚本守五件事：
  A. /api/health 带出真实模型信息（名 / 参数量 / 量化档 / 体积），且**与 Ollama 报的一致**；
  B. 专用代码模型单独一段；没下载时如实标记（界面不能显示成"就绪"）；
  C. Ollama 离线/报错时不崩、字段齐全（界面不能因为查不到就白屏）；
  D. 前端确实在用这几段（元素 + 渲染函数 + 样式都在）；
  E. ★ 前端**不留写死的模型名**（换模型后不该有陈旧文案 —— 这条就是防这次的复发）。

跑法（需要 Ollama 在跑；不占 8000 端口）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_model_info_display.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["MM_DATA_DIR"] = tempfile.mkdtemp(prefix="mm_modelinfo_")
sys.path.insert(0, ROOT)

from backend import ollama_client as OC      # noqa: E402
from backend import config as C              # noqa: E402

PASS, FAIL = 0, []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (label, ("  " + str(extra)) if extra else ""))
    else:
        FAIL.append(label)
        print("  [FAIL] %s  %s" % (label, extra))


HTML = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

cli = OC.OllamaClient()

# ============================================================
print("=" * 66)
print("① /api/health 必须带出**真实**的模型信息（名 / 参数量 / 量化 / 体积）")
print("=" * 66)
h = cli.health()
check("接口在线（离线时本节的断言没意义，请先启动 Ollama）", h.get("online") is True)
check("默认模型已就绪", h.get("model_ready") is True, h.get("model"))
mi = h.get("model_info")
check("model_info 存在（不是 None）", isinstance(mi, dict), type(mi).__name__)
if isinstance(mi, dict):
    check("带模型名", bool(mi.get("name")), mi.get("name"))
    check("★ 带参数量（8.8B / 30.5B 这种）", bool(mi.get("parameter_size")), mi.get("parameter_size"))
    check("★ 带量化档（Q4_K_M 这种）", bool(mi.get("quantization_level")), mi.get("quantization_level"))
    check("★ 带体积（GB）", bool(mi.get("size_text")) and "GB" in mi.get("size_text", ""),
          mi.get("size_text"))
    check("带架构家族（MoE / dense 一眼可辨）", bool(mi.get("family")), mi.get("family"))

    # ★ 关键：必须与 Ollama 现报的一致 —— 否则就是"写死的假信息"
    live = {m.get("name"): m for m in cli.list_models()}
    src = live.get(mi["name"]) or {}
    det = src.get("details") or {}
    check("★ 与 Ollama 现报的参数量一致（不写死）",
          mi.get("parameter_size") == det.get("parameter_size"),
          "%s vs %s" % (mi.get("parameter_size"), det.get("parameter_size")))
    check("★ 与 Ollama 现报的量化档一致（不写死）",
          mi.get("quantization_level") == det.get("quantization_level"),
          "%s vs %s" % (mi.get("quantization_level"), det.get("quantization_level")))
    check("体积跟 /api/tags 的 size 对得上（允许四舍五入）",
          abs(int(mi.get("size_bytes") or 0) - int(src.get("size") or 0)) < 10 ** 8,
          "%s vs %.2fGB" % (mi.get("size_text"), (src.get("size") or 0) / 1e9))

# ============================================================
print()
print("=" * 66)
print("② 专用代码模型：配了就要单独报，没下载不许显示成就绪")
print("=" * 66)
cfg = C.load_config()
want = str(cfg.get("code_model") or "").strip()
check("配置里确实配了 code_model", bool(want), want)
check("health 把它带出来了", h.get("code_model") == want, h.get("code_model"))
if want:
    installed = set(h.get("installed") or [])
    real_ready = want in installed
    check("★ 就绪标记与「本机真的装了没」一致",
          bool(h.get("code_model_ready")) == real_ready,
          "code_model_ready=%s，本机已装=%s" % (h.get("code_model_ready"), sorted(installed)))
    if real_ready:
        cmi = h.get("code_model_info") or {}
        check("装了 → 也要给出它的参数量与体积",
              bool(cmi.get("parameter_size")) and bool(cmi.get("size_text")),
              "%s %s" % (cmi.get("parameter_size"), cmi.get("size_text")))
    else:
        check("没装 → code_model_info 必须是 None（前端据此显示「未下载」）",
              h.get("code_model_info") is None)

# ============================================================
print()
print("=" * 66)
print("③ Ollama 连不上时必须不崩、字段齐全（界面不能白屏）")
print("=" * 66)


class _Dead(OC.OllamaClient):
    def _req(self, method, path, **kwargs):
        raise OC.OllamaError("模拟：Ollama 没开")


try:
    d = _Dead().health()
    check("离线时 health() 不抛异常，正常返回字典", isinstance(d, dict))
    check("online=False", d.get("online") is False)
    check("model_ready=False", d.get("model_ready") is False)
    check("model 字段仍在（界面要显示名字）", bool(d.get("model")), d.get("model"))
    check("model_info 为 None 而不是报错", d.get("model_info") is None)
    check("installed 是空列表而不是 KeyError", d.get("installed") == [])
except Exception as e:
    check("离线时 health() 不抛异常，正常返回字典", False, repr(e))

# ============================================================
print()
print("=" * 66)
print("④ 前端接线：元素 / 渲染 / 样式都在（改了后端忘了前端等于没做）")
print("=" * 66)
check("index.html 有 #modelInfo 这一行", 'id="modelInfo"' in HTML)
check("index.html 有 #brandSub（左上角那句品牌语）", 'id="brandSub"' in HTML)
check("app.js 有格式化函数（名·参数量·量化·体积）", "function fmtModel" in JS)
check("app.js 渲染时读的是 model_info（不是写死的字符串）", "d.model_info" in JS)
check("app.js 渲染时读的是 code_model_info / code_model_ready",
      "d.code_model_info" in JS and "d.code_model_ready" in JS)
check("app.js 在 refreshHealth 里填这行（不是定义完忘了调）",
      "refreshHealth" in JS and "$(\"#modelInfo\")" in JS)
check("左上角品牌语也按实际模型推导", "$(\"#brandSub\")" in JS)
check("style.css 有 .model-info 样式（否则挤成一团看不清）", ".model-info" in CSS)

# ============================================================
print()
print("=" * 66)
print("⑤ ★ 前端不许留写死的模型名（这条就是这次问题的根因）")
print("=" * 66)
import re                                    # noqa: E402

# ⚠️ 判据必须**先去掉注释**：注释里为了说明"原来写死了什么"会引用旧文案，
#    那不是用户可见的文字（第一次写这个测试时就被自己的注释绊了一下）。
HTML_VISIBLE = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
JS_VISIBLE = "\n".join(l for l in JS.splitlines() if not l.strip().startswith("//"))

_hard = []
for fn, txt in (("index.html", HTML_VISIBLE), ("app.js", JS_VISIBLE)):
    for i, line in enumerate(txt.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if re.search(r"qwen[\w.\-]*", line, re.I):
            # HTML 里若还出现模型名 → 直接算问题（用户可见文案）
            if fn == "index.html":
                _hard.append("%s:%d %s" % (fn, i, stripped[:70]))
check("★ index.html 里没有任何写死的模型名（换模型不会说错话）",
      not _hard, "；".join(_hard[:3]))
check("index.html 不再出现 'Qwen 智能体' 这种写死品牌（注释不算）",
      "Qwen 智能体" not in HTML_VISIBLE)
check("tooltip 里不再写死旧代码模型（qwen2.5-coder）",
      "qwen2.5-coder" not in HTML_VISIBLE and "qwen2.5-coder" not in JS_VISIBLE)
check("兜底文案本身不含模型名（认不出来时不乱说）",
      "本地智能体 · 数据不出机" in HTML)

print()
print("=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("  ! " + f)
sys.exit(1 if FAIL else 0)
