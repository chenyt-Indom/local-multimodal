# -*- coding: utf-8 -*-
"""「思考过程重复（打转）」修复验证。

用户反馈：思考过程里同一段话反复写 ——
    「可能的题目：… 或者：… 可能需要换一个例子：… 例如：…」
本脚本验证三件事：

  ① 打转检测函数的判据（用户截图那种要抓到，正常思考**不能**误报）
  ② **采样参数真的进到 Ollama 请求体里了**（这正是本 bug 的根因：
     之前只传了 temperature / num_ctx / num_predict，`repeat_last_n` 用的是
     Ollama 默认的 64 —— 而打转是**段落级**的，64 个 token 的窗口盖不住）
  ③ Ollama **接受**这些参数（传了不认识的字段会 400，那就是把整个对话搞挂）

跑法（⚠️ 必须用带依赖的解释器）：
    %LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe test_loop.py
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_loop_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import config as C          # noqa: E402
from backend import main as M            # noqa: E402
from backend import ollama_client as OC  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


def main() -> int:
    print("=" * 64)
    print("思考打转（复读）修复验证")
    print("=" * 64)

    # ---------------- ① 检测判据 ----------------
    print("\n① 打转检测：该抓的抓到、正常的不能误报")
    shot = ('可能的题目：\n- "什么字人们最不愿意看到？答案：死字。"\n\n或者：\n'
            '- "什么字人们最不愿意看到？答案：考字。"\n\n可能需要换一个例子：\n\n'
            '例如：\n- "什么字人们最不愿意看到？答案：死字。"')
    hit = M.find_looping_piece(shot)
    check("用户截图那种打转 → 命中", bool(hit), repr(hit[:24]))

    normal = ("用户说继续，我需要出一道新的脑筋急转弯。之前那题是问什么字最不愿意看到，"
              "答案老。现在换一个方向：和日常物品有关，比如水、镜子这类。")
    check("正常思考 → 不误报", M.find_looping_piece(normal) == "")

    short = "好的。然后呢？所以呢？那么呢？开始吧。这样呢？那样呢？"
    check("短词反复（正常语言）→ 不误报", M.find_looping_piece(short) == "")

    check("空文本不报错", M.find_looping_piece("") == "")
    check("刚好两遍不算（阈值 3）",
          M.find_looping_piece("这是一句比较长的话哦。这是一句比较长的话哦。") == "",
          "两遍属于正常复述")
    check("三遍才算打转",
          bool(M.find_looping_piece("这是一句比较长的话哦。" * 3)))

    # ⚠️ 假警报：英文侧全是**标识符碎片** —— 正则把 `search_knowledge` 拆成
    #    `search` / `knowledge`，模型正常讨论几次工具名就"重复 3 次"了。
    #    实测（2026-09-21）日志里 8 条打转警告全是同一个词 `search`，属误报。
    eng = ("I should call search_knowledge here. Actually search_knowledge returns the docs. "
           "Then search_knowledge again for the second query.")
    check("讨论工具名（search_knowledge 出现 3 次）→ 不误报",
          M.find_looping_piece(eng) == "", repr(M.find_looping_piece(eng)))
    check("纯英文短词反复 → 不误报",
          M.find_looping_piece("search search search knowledge knowledge knowledge") == "")
    check("特别长的英文串反复（≥16）→ 仍然算",
          bool(M.find_looping_piece("abcdefghijklmnopqrstuvwx " * 3)),
          "留个安全阀，别把英文场景整个废掉")
    check("中文段落复读**没有**被这层过滤误伤",
          bool(M.find_looping_piece("这段话在思考里原封不动地重复了三遍哦。" * 3)))
    # 实机第二次误报：9 字常用短语在不同句子里出现 3 次，是正常表达
    common = ("用户导入的领域文档可能有几份。我先看看用户导入的领域文档里都有什么，"
              "再决定要不要引用用户导入的领域文档。")
    check("9 字常用短语重复 3 次 → 不误报（实机抓到的假警报）",
          M.find_looping_piece(common) == "", repr(M.find_looping_piece(common)))
    check("⚠️ 但真正那段 30~60 字的复读**仍然要抓得到**",
          bool(M.find_looping_piece(shot)), repr(M.find_looping_piece(shot)[:20]))

    # ---------------- ② 采样参数进没进请求体 ----------------
    print("\n② 采样参数必须真的进到 Ollama 请求里（这是本 bug 的根因）")
    cfg = C.load_config()
    check("config 里有 repeat_penalty 默认值",
          isinstance(cfg.get("repeat_penalty"), (int, float)),
          "repeat_penalty=%s" % cfg.get("repeat_penalty"))
    check("config 里有 repeat_last_n 默认值",
          isinstance(cfg.get("repeat_last_n"), int),
          "repeat_last_n=%s" % cfg.get("repeat_last_n"))
    check("repeat_last_n 明显大于 Ollama 默认的 64（打转是段落级的，64 盖不住）",
          int(cfg.get("repeat_last_n") or 0) >= 256,
          "repeat_last_n=%s" % cfg.get("repeat_last_n"))
    check("repeat_penalty 在安全区间（>1.0 且 ≤1.3，太高会让模型结巴）",
          1.0 < float(cfg.get("repeat_penalty") or 0) <= 1.3,
          "repeat_penalty=%s" % cfg.get("repeat_penalty"))
    check("config 里有 top_p 默认值（界面「🎛 采样」第二条滑条）",
          0.5 < float(cfg.get("top_p") or 0) <= 1.0, "top_p=%s" % cfg.get("top_p"))

    cli = OC.OllamaClient()
    captured = {}

    def fake_req(method, path, **kwargs):
        captured["path"] = path
        captured["json"] = kwargs.get("json")

        class R:
            status_code = 200
            text = ""

            def json(self):
                return {"message": {"content": "ok"}, "done": True}
        return R()

    cli._req = fake_req            # 只换掉"发请求"这一步，payload 构造走真实代码
    cli.chat([{"role": "user", "content": "hi"}], model="qwen3-vl:8b",
             stream=False, params=cfg)
    opts = (captured.get("json") or {}).get("options") or {}
    check("请求体里有 options", bool(opts), str(sorted(opts.keys())))
    check("repeat_penalty 传进去了", opts.get("repeat_penalty") == cfg.get("repeat_penalty"),
          str(opts.get("repeat_penalty")))
    check("repeat_last_n 传进去了", opts.get("repeat_last_n") == cfg.get("repeat_last_n"),
          str(opts.get("repeat_last_n")))
    check("top_p 传进去了（顶栏「🎛 采样」第二条滑条就是它）",
          opts.get("top_p") == cfg.get("top_p"), str(opts.get("top_p")))
    check("原有的 temperature / num_ctx / num_predict 没被改坏",
          opts.get("temperature") == cfg.get("temperature")
          and opts.get("num_ctx") == cfg.get("num_ctx")
          and opts.get("num_predict") == cfg.get("max_tokens"),
          "temp=%s ctx=%s predict=%s" % (opts.get("temperature"),
                                         opts.get("num_ctx"), opts.get("num_predict")))

    # 界面上拖滑条 = 改 config。这里验证"改了 config，下一次请求就跟着变" ——
    # 后端每个 /api/chat 都重新 load_config，所以**拖完立刻生效，不用重启**。
    captured.clear()
    cli.chat([{"role": "user", "content": "hi"}], model="qwen3-vl:8b", stream=False,
             params=dict(cfg, temperature=1.35, top_p=0.62))
    opts1b = (captured.get("json") or {}).get("options") or {}
    check("**改了 config，请求体立刻跟着变**（拖动即生效，不用重启）",
          abs(float(opts1b.get("temperature") or 0) - 1.35) < 1e-6
          and abs(float(opts1b.get("top_p") or 0) - 0.62) < 1e-6,
          "temp=%s top_p=%s" % (opts1b.get("temperature"), opts1b.get("top_p")))

    # 没给值时**不能**塞 None 进去（会覆盖掉 Ollama 自己的默认）
    captured.clear()
    cli.chat([{"role": "user", "content": "hi"}], model="qwen3-vl:8b", stream=False,
             params={"temperature": 0.7, "max_tokens": 64, "num_ctx": 8192,
                     "repeat_penalty": None, "repeat_last_n": None,
                     "presence_penalty": 0})
    opts2 = (captured.get("json") or {}).get("options") or {}
    check("值为 None/0 的采样项**不会**被塞进请求（否则会覆盖 Ollama 默认）",
          opts2.get("repeat_penalty") is None and opts2.get("repeat_last_n") is None
          and opts2.get("presence_penalty") is None, str(sorted(opts2.keys())))

    # ---------------- ③ Ollama 得接受这些参数 ----------------
    print("\n③ Ollama 真的接受这些参数（不认识就会 400，那等于把对话搞挂）")
    # 直接发一个最小请求（不经 client）—— 断言的只是"参数被接受"。
    # ⚠️ 别断言"正文非空"：这是思考型模型，配额小的时候会全部花在思考上
    #    （项目里踩过：num_predict=300 时 content 一个字都没有）。
    import requests
    payload = {"model": C.load_config()["default_model"],
               "messages": [{"role": "user", "content": "只回答一个数字：1+1=?"}],
               "stream": False,
               "options": {"temperature": 0.7, "num_ctx": 24576, "num_predict": 64,
                           "repeat_penalty": 1.15, "repeat_last_n": 512,
                           "presence_penalty": 0.5}}
    try:
        resp = requests.post(C.load_config()["ollama_url"] + "/api/chat",
                             json=payload, timeout=600)
        check("Ollama 接受 repeat_penalty / repeat_last_n / presence_penalty",
              resp.status_code == 200,
              "HTTP %s  %s" % (resp.status_code, resp.text[:80]))
    except Exception as e:
        check("Ollama 接受 repeat_penalty / repeat_last_n / presence_penalty", False,
              "%s: %s" % (type(e).__name__, e))

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 64)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
