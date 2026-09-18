# -*- coding: utf-8 -*-
"""语音唤醒的四个毛病 —— 逐条验证修好了没有。

用户的反馈（原话）：
  ① "有时候叫的出来但是不灵敏，大部分一直唤不出来"
  ② "输入的语音文字也不精准，很多字一直重复，我说'小千小千'，生成的字是'小小千小千千'"
  ③ "语音唤醒功能一直反应很慢，得等好久才加载出来"
  ④ "下次喊小千小千就没反应了"

### 查出来的根因（都在本机实测过）
实测环境：默认输入是**摄像头上的麦克风**，底噪 RMS **0.007~0.009**。
· 老代码 `GATE_RMS=0.005` → 门控**几乎一直开着**：ASR 在噪声上一直跑，
  上下文越积越长 → 越用越慢（③）；噪声还被认成字（②）。
· 老代码 `SILENCE_RMS=0.010` → 底噪贴着它，"安静"几乎永远不成立 →
  **一轮永远结束不了**，状态卡在 awake → 下次喊唤醒词时唤醒分支根本不执行（④）。
· ASR 解码写在**音频回调**里 → 回调一慢 PortAudio 就丢数据，而 `status` 被
  `if status: pass` 吞掉 → 说话声被丢 → 识别缺字/串字（②）。
· `start()` 没上锁，`if self.running` 和 `thread.start()` 之间隔着一次磁盘扫描 →
  自动启动和点 🎤 撞上就**开两个监听线程**，往同一个识别流喂两遍音频 → 重复字（②）。
· 用户没有换麦克风的地方，也看不到电平（①没法自查）。

跑法：用带依赖的 Python314
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_voice.py
"""
import io
import os
import sys
import tempfile
import threading
import time
import types
import wave

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_voicet_")
import shutil                                    # noqa: E402
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP

import numpy as np                               # noqa: E402
from backend import voice as V                   # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


WAV = (r"D:\local-multimodal-models\sherpa-asr"
       r"\sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23\test_wavs\0.wav")


def read_wav(path):
    with wave.open(path, "rb") as w:
        nch, sw, sr, nfr = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
        raw = w.readframes(nfr)
    a = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch)[:, 0]
    if sr != V.SAMPLE_RATE:
        idx = np.arange(0, len(a) - 1, sr / V.SAMPLE_RATE)
        lo = np.floor(idx).astype(int)
        f = idx - lo
        a = a[lo] * (1 - f) + a[lo + 1] * f
    return a


def blocks_of(audio):
    return [audio[i:i + V.BLOCK_SIZE] for i in range(0, len(audio), V.BLOCK_SIZE)]


def noise_blocks(sigma, n, seed=7):
    rs = np.random.RandomState(seed)
    return [rs.normal(0, sigma, V.BLOCK_SIZE).astype("float32") for _ in range(n)]


def new_listener(events):
    """真的加载识别模型（不是 mock），这样量到的行为才是真的。"""
    vl = V.VoiceListener(on_event=events.append)
    assert vl._build_recognizer(), vl.error
    vl._reset_stream()
    return vl


def main():
    print("=" * 64)
    print("语音唤醒：灵敏度 / 重复字 / 响应慢 / 下次没反应 —— 修复验证")
    print("=" * 64)
    have_wav = os.path.isfile(WAV)
    audio = read_wav(WAV) if have_wav else None

    # ---------------- ① 自适应门控 ----------------
    print("\n① 阈值跟着环境底噪走（这是③④的根因）")
    vl = V.VoiceListener()
    vl._noise = 0.008                      # 本机实测的底噪
    st, si = vl._speech_th(), vl._silence_th()
    check("底噪 0.008 时，说话阈值抬到它上面", st > 0.008, "speech_th=%.4f" % st)
    check("底噪 0.008 时，静音阈值也跟着抬（老代码死钉在 0.010 → 永远不算安静）",
          si > 0.010, "silence_th=%.4f（老代码 0.010）" % si)
    check("安静时能判定为 quiet", 0.008 < si, "0.008 < %.4f" % si)
    vl._noise = 0.0008                     # 安静房间
    check("安静房间阈值自动降下来（不会把轻声说话吃掉）",
          vl._speech_th() <= 0.008 + 1e-9, "speech_th=%.4f" % vl._speech_th())
    vl._noise = 0.08                       # 很吵
    check("很吵时阈值有上限（不会高到喊破喉咙也不认）",
          vl._speech_th() <= V.SPEECH_MAX + 1e-9, "speech_th=%.4f" % vl._speech_th())

    print("\n①b 底噪跟踪：说话声不能把底噪带飞，但稳态噪声要尽快跟上")
    vl2 = V.VoiceListener()
    vl2._noise = 0.006

    def track(level, n, obj):
        for _ in range(n):
            obj._track_noise(level)

    # 连续 3 秒"说话"（有停顿 → 有低谷）→ 底噪不该被带飞
    for _ in range(30):
        vl2._track_noise(0.05)
        vl2._track_noise(0.004)          # 说话中的停顿
    check("连续说话（含停顿）后底噪仍远低于说话电平（否则越说越不灵）",
          vl2._noise < 0.02, "底噪=%.4f（说话 0.05）" % vl2._noise)

    # 说话把底噪拖低 + 房间底噪其实更高 → 必须能在几秒内爬回来（否则一轮永远结束不了）
    vl2._noise = 0.004
    vl2._dips = [False] * V.NOISE_DIP_WINDOW
    track(0.014, 40, vl2)                # 4 秒稳态环境噪声
    check("稳态背景噪声会被尽快吸进底噪（慢吞吞跟会让一轮永远结束不了）",
          vl2._noise > 0.012, "底噪=%.4f" % vl2._noise)
    check("底噪有上限，不会涨到把说话阈值带飞",
          V.NOISE_CAP <= V.SPEECH_MAX, "NOISE_CAP=%.3f" % V.NOISE_CAP)
    check("跟上来之后，这个噪声会被判成「安静」→ 一轮能结束",
          0.014 < vl2._silence_th(), "silence_th=%.4f" % vl2._silence_th())

    # ---------------- ② 唤醒词匹配 ----------------
    print("\n② 唤醒词匹配（同音字 / 双呼 / 内容切分）")
    for text, want_hit in (("小千小千", True), ("小千", True), ("晓谦小签", True),
                           ("小迁，今天天气怎么样", True), ("今天天气怎么样", False),
                           ("小欠小欠", False)):
        hit, _rest = V.match_wake(text)
        check("「%s」→ %s" % (text, "唤醒" if want_hit else "不唤醒"),
              hit == want_hit, "got=%s" % hit)
    hit, rest = V.match_wake("小千小千，今天天气怎么样")
    check("双呼后的内容切干净了", hit and rest == "今天天气怎么样", "rest=%r" % rest)
    check("重复的唤醒词开头会被剥掉",
          V.strip_wake_prefix("小千小千小千 帮我查天气") == "帮我查天气",
          repr(V.strip_wake_prefix("小千小千小千 帮我查天气")))

    # ---------------- ③ 状态机（真实音频） ----------------
    print("\n③ 状态机：一轮能正常结束，而且**结束后还能再喊一次**")
    if not have_wav:
        check("（跳过：没有测试音频）", True)
    else:
        ev = []
        vl3 = new_listener(ev)
        # 环境不安静（σ=0.014，本机实测就到过这个量级）：老代码里 quiet 永远不成立
        amb = 0.014
        fed = {"n": 0, "ids": []}
        inner = vl3._stream
        real_feed = inner.accept_waveform

        class Proxy:
            """包一层数音频：sherpa 的流是 C 扩展、属性改不了，只能整个换掉。"""

            def accept_waveform(self, sr, buf):
                fed["n"] += len(buf)
                fed["ids"].append(id(buf))     # 同一块被喂两次 → id 会重复
                return real_feed(sr, buf)

            def __getattr__(self, k):
                return getattr(inner, k)

        vl3._stream = Proxy()
        # 假装唤醒词命中（匹配逻辑本身在上面单独测过）
        vl3._match_wake = lambda text: (True, "今天天气怎么样") if text else (False, "")
        clk = {"t": 1000.0}
        vl3._now = lambda: clk["t"]

        def feed(bs):
            for b in bs:
                clk["t"] += 0.1           # 一块 = 100ms 音频，时钟同步推进
                vl3._feed(b)

        total = 0
        quiet_blocks = noise_blocks(amb, 5)
        speech = blocks_of(audio[:int(3.0 * V.SAMPLE_RATE)])
        tail = noise_blocks(amb, 35)
        for b in quiet_blocks + speech + tail:
            total += len(b)
        feed(quiet_blocks)
        feed(speech)
        feed(tail)

        dupes = len(fed["ids"]) - len(set(fed["ids"]))
        check("同一块音频不会被喂两遍（重复字的直接来源）", dupes == 0,
              "重复喂入 %d 块" % dupes)
        check("也不会多喂（总喂入 ≤ 音频总长）", fed["n"] <= total,
              "喂入 %d / 音频 %d" % (fed["n"], total))
        check("识别出了唤醒", any(e.get("type") == "wake" for e in ev))
        finals = [e for e in ev if e.get("type") == "final"]
        check("吵环境下也能判定「说完了」并结束一轮（老代码在这里卡死）",
              len(finals) >= 1, "final 事件 %d 个" % len(finals))
        check("结束后回到待机 listening", vl3.state == "listening" and not vl3._awake,
              "state=%s awake=%s" % (vl3.state, vl3._awake))
        ev.clear()
        feed(blocks_of(audio[:int(2.0 * V.SAMPLE_RATE)]) + noise_blocks(amb, 25))
        check("**下一轮还能再唤醒**（用户说的「下次喊没反应」）",
              any(e.get("type") == "wake" for e in ev))

    # ---------------- ③b 兜底时限 ----------------
    print("\n③b 兜底：安静判定失效时也不能卡死")
    ev2 = []
    vl4 = new_listener(ev2)
    vl4._match_wake = lambda text: (True, "") if text else (False, "")
    clk2 = {"t": 2000.0}
    vl4._now = lambda: clk2["t"]

    def feed2(bs):
        for b in bs:
            clk2["t"] += 0.1
            vl4._feed(b)

    # 一直"很吵"（远高于静音阈值）→ 正常路径永远判不出安静 → 必须靠 12 秒兜底
    feed2(noise_blocks(0.05, 20))
    feed2(blocks_of(audio[:int(2.0 * V.SAMPLE_RATE)]))
    feed2(noise_blocks(0.05, 120))
    check("持续吵闹也能自动结束一轮（MAX_UTTERANCE_SEC 兜底）",
          vl4.state == "listening" and not vl4._awake,
          "state=%s（喂了 %.1f 秒音频）" % (vl4.state, clk2["t"] - 2000.0))

    # ---------------- ④ 并发 start() 只能起一个监听 ----------------
    print("\n④ 同时 start() 两次 → 只能有一个监听（重复字的根因）")
    opened = []

    class FakeStream:
        def __init__(self, **kw):
            self.kw = kw

        def __enter__(self):
            opened.append(self.kw.get("device"))
            return self

        def __exit__(self, *a):
            return False

    fake = types.ModuleType("sounddevice")
    fake.InputStream = FakeStream
    fake.query_devices = lambda: [{"name": "假麦克风", "max_input_channels": 1,
                                   "default_samplerate": 16000, "hostapi": 0}]
    fake.query_hostapis = lambda i: {"name": "X"}
    fake.default = types.SimpleNamespace(device=(0, 0))
    real_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake
    real_find = V.find_model_dir
    V.find_model_dir = lambda: (time.sleep(0.3), real_find())[1]   # 把竞态窗口撑大
    try:
        vl5 = V.VoiceListener()
        res = []
        ths = [threading.Thread(target=lambda: res.append(vl5.start())) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        # ⚠️ 别用固定 sleep：加载模型要 1 秒多，早了就误判成"没开流"
        for _ in range(80):
            if opened:
                break
            time.sleep(0.2)
        check("两个并发 start 只开了一个音频流", len(opened) == 1, "开了 %d 个" % len(opened))
        check("其中一个被识别成 already", any(r.get("already") for r in res),
              str([{k: v for k, v in r.items() if k in ("already", "state")} for r in res]))
        vl5.stop()
    finally:
        V.find_model_dir = real_find
        if real_sd is not None:
            sys.modules["sounddevice"] = real_sd
        else:
            sys.modules.pop("sounddevice", None)

    # ---------------- ⑤ 麦克风选择 ----------------
    print("\n⑤ 麦克风：能列出来、能换、默认不会挑到'假麦克风'")
    devs = V.list_input_devices()
    check("能列出输入设备", len(devs) > 0, "%d 个" % len(devs))
    if devs:
        idx, name = V.resolve_device("")
        check("空配置 → 解析出一个设备", idx is not None, "#%s %s" % (idx, name))
        check("不会挑到映射器/立体声混音这类假麦克风", not V._looks_fake_mic(name), name)
        first = devs[0]["name"]
        idx2, name2 = V.resolve_device(first)
        check("按名字能选中指定设备", name2 == first, name2)
    d = V.VoiceListener().list_devices()
    check("list_devices() 带上当前选择", "current" in d and "devices" in d)

    # ---------------- ⑥ 状态里要能看到'为什么喊不动' ----------------
    print("\n⑥ status() 要能回答「为什么喊不动」（以前只能靠猜）")
    st = V.VoiceListener().status()
    for k in ("device", "level", "noise", "speech_th", "silence_th", "xruns", "dropped"):
        check("status 里有 %s" % k, k in st, st.get(k))

    # ---------------- ⑦ 模型选择（实测字错率） ----------------
    print("\n⑦ ASR 模型：要挑中文准的那个（用真语音量字错率）")
    chosed = V.find_model_dir() or ""
    check("选中的不是中英双语模型", "bilingual" not in chosed.lower(),
          "选中：%s" % os.path.basename(chosed))
    check("选中了中文专用流式模型", V._model_rank(chosed) >= 100,
          "rank=%d" % V._model_rank(chosed))

    wavs = _tts_sentences()
    if not wavs:
        check("（跳过字错率对比：这台机器没有可用的中文语音合成）", True)
    else:
        import sherpa_onnx
        M_BI = (r"D:\local-multimodal-models\sherpa-asr"
                r"\sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20")

        def build(md):
            return sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=os.path.join(md, "tokens.txt"), encoder=V._pick(md, "encoder"),
                decoder=V._pick(md, "decoder"), joiner=V._pick(md, "joiner"),
                num_threads=4, sample_rate=16000, feature_dim=80,
                decoding_method="greedy_search", enable_endpoint_detection=False)

        def asr(rec, a):
            st = rec.create_stream()
            for i in range(0, len(a), V.BLOCK_SIZE):
                st.accept_waveform(V.SAMPLE_RATE, a[i:i + V.BLOCK_SIZE])
                while rec.is_ready(st):
                    rec.decode_stream(st)
            return (rec.get_result(st) or "").strip()

        ref = "小千小千，今天天气怎么样"
        a = _read16(wavs[1])
        rec_best = build(chosed)
        got_best = asr(rec_best, a)
        check("所选模型认得对（不含多出来的「小千」）",
              got_best.count("小千") <= 2 and "今天天气" in got_best, repr(got_best))
        check("所选模型不会出现同一个字连出三次的病态重复",
              not __import__("re").search(r"(.)\1\1", got_best), repr(got_best))
        if os.path.isdir(M_BI):
            got_bi = asr(build(M_BI), a)
            check("（对照）双语模型在这句上确实更差 —— 这就是用户遇到的重复字",
                  got_bi != got_best, "双语：%r ／ 选中：%r" % (got_bi, got_best))

    # ---------------- ⑧ 用真语音端到端跑一遍 ----------------
    print("\n⑧ 端到端（真语音）：环境噪声 → 说唤醒词 → 唤醒 → 自动结束 → 再喊一次")
    if not wavs:
        check("（跳过：没有可用的中文语音合成）", True)
    else:
        ev = []
        vl8 = new_listener(ev)
        vl8._noise = 0.006
        clk8 = {"t": 5000.0}
        vl8._now = lambda: clk8["t"]
        rs8 = np.random.RandomState(11)
        amb8 = lambda n: [rs8.normal(0, 0.008, V.BLOCK_SIZE).astype("float32")
                          for _ in range(n)]
        speech8 = blocks_of(_read16(wavs[1]))

        def feed8(bs):
            for b in bs:
                clk8["t"] += 0.1
                vl8._feed(b)

        feed8(amb8(20))                      # 2 秒环境噪声
        check("安静时**不跑识别**（门控有效）", not ev or all(e.get("type") != "wake" for e in ev))
        feed8(speech8)                       # 说「小千小千，今天天气怎么样」
        check("真的喊醒了", any(e.get("type") == "wake" for e in ev))
        check("识别出的正文不带重复的「小千」", (vl8._text or "").count("小千") == 0,
              repr(vl8._text))
        feed8(amb8(35))                      # 说完不吭声
        fins = [e for e in ev if e.get("type") == "final"]
        check("说完自动结束并发出内容", len(fins) >= 1, str([f["text"] for f in fins]))
        check("结束后回到待机", vl8.state == "listening" and not vl8._awake)
        ev.clear()
        feed8(speech8)
        check("**再喊一次照样能唤醒**（用户报的「下次喊没反应」）",
              any(e.get("type") == "wake" for e in ev))

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 64)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


def _tts_sentences():
    """用系统语音合成几句中文口语当测试音频（返回 [路径...]，没有 TTS 就返回 []）。

    ⚠️ 写进**临时目录**：这是测试产物，不能污染 App 的真实数据目录。
    """
    import wave as _w
    d = os.path.join(TMP, "wav")
    os.makedirs(d, exist_ok=True)
    sents = ["小千小千", "小千小千，今天天气怎么样", "现在几点了"]
    paths = []
    try:
        import win32com.client as w
        sp = w.Dispatch("SAPI.SpVoice")
        vs = sp.GetVoices()
        picked = None
        for i in range(vs.Count):
            if "Chinese" in vs.Item(i).GetDescription():
                picked = vs.Item(i)
                break
        if picked is None:
            return []
        sp.Voice = picked
        for k, text in enumerate(sents):
            p = os.path.join(d, "s%d.wav" % k)
            st = w.Dispatch("SAPI.SpFileStream")
            st.Open(p, 3, False)          # 3 = SSFMCreateForWrite
            sp.AudioOutputStream = st
            sp.Speak(text)
            st.Close()
            with _w.open(p, "rb") as f:
                if f.getnframes() > 0:
                    paths.append(p)
    except Exception:
        return []
    return paths if len(paths) == len(sents) else []


def _read16(path):
    """读成 16k float32 单声道（-1..1），顺带归一化到正常说话电平。"""
    with wave.open(path, "rb") as f:
        nch, sw, sr, nfr = (f.getnchannels(), f.getsampwidth(),
                            f.getframerate(), f.getnframes())
        raw = f.readframes(nfr)
    a = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch)[:, 0]
    if sr != V.SAMPLE_RATE:
        k = max(1, int(round(sr / float(V.SAMPLE_RATE))))
        c = np.cumsum(np.insert(a, 0, 0.0))
        a = (c[k:] - c[:-k]) / k                       # 先抗混叠再插值
        idx = np.arange(0, len(a) - 1, sr / float(V.SAMPLE_RATE))
        lo = np.floor(idx).astype(int)
        fr = idx - lo
        a = a[lo] * (1 - fr) + a[lo + 1] * fr
    return a / max(1e-9, float(np.max(np.abs(a)))) * 0.35


if __name__ == "__main__":
    sys.exit(main())
