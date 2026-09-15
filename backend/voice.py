# -*- coding: utf-8 -*-
"""本地语音输入：唤醒词「小千小千」+ 离线流式识别 + 静音自动发送。

工作流程
--------
1. 程序启动后自动进入**待机监听**（state=listening），持续做流式识别
2. 识别文本中命中唤醒词（「小千小千」或「小千」）→ 进入**收音**（state=awake），
   同时**把应用窗口弹到最前**（见 set_wake_hook），唤醒词之后的内容作为指令开头
3. 收音期间持续累积文本并实时推给前端（partial 事件）
4. 检测到**连续静音超过 silence_sec 秒**且已有内容 → 发出 final 事件
   （auto=True），前端据此自动发送；随后回到待机监听
   · 唤醒后一直没说话 → 同样在 silence_sec 后结束，但**内容不足就不发**
     （不会凭空发一条空消息）

全程离线：麦克风由 sounddevice 采集，识别由 sherpa-onnx 本地模型完成。
"""
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Callable

# —— 音频参数 ——
SAMPLE_RATE = 16000          # sherpa-onnx 流式模型固定 16kHz 单声道
BLOCK_MS = 100               # 每次读取 100ms
BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000

# —— 行为参数 ——
WAKE_WORDS = ("小千小千", "小千")   # 命中任意一个即唤醒（双呼更稳，单呼更灵敏）
SILENCE_SEC = 2.0                   # 唤醒后静音多久自动发送
SILENCE_RMS = 0.010                 # 低于此 RMS 视为静音（调低=更不容易被当成静音，
                                    # 说话声小的时候不会被过早切断）
MIN_CHARS = 2                       # 至少要识别出这么多字才允许自动发送

# —— 静音门控（性能关键）——
# 常驻监听最容易踩的坑：每 100ms 就跑一次 ASR 解码，持续吃满 CPU 并持有 GIL，
# 把同时运行的 Web 服务饿死（实测健康检查从 20ms 恶化到 21 秒）。
# 所以待机时**只在检测到人声后才启动识别**，安静时就只算个音量，开销可忽略。
#
# 但门控本身也会带来"唤醒慢 / 少字"：门槛太高会把说话声的开头判成静音，
# 模型拿到的音频从半个字开始，既要多花时间才能认出唤醒词，还容易认错。
# 下面两个值都是**实测调过**的：门槛下调 + 前置音频加长。
GATE_RMS = 0.005                    # 音量超过它才算"有人在说话"（原 0.01 偏高，
                                    # 正常说话起音常低于它，导致开头被吃掉）
PRE_ROLL_BLOCKS = 10                # 检测到人声前多留 1.0 秒音频（原 0.4 秒，
                                    # 不够流式模型"热机"，是唤醒慢的一个直接原因）
IDLE_RESET_SEC = 3.0                # 安静这么久就重置识别流，避免上下文无限增长

# —— 唤醒词容错表 ——
# 语音识别把「小千」写成别的同音字太常见了（晓谦/小签/小迁…）。
# 只认精确的「小千」会导致"喊了没反应"，所以这里做同音归一化再匹配 ——
# 相当于把唤醒的置信度门槛放低，换取更高的激活率。
_XIAO_VARIANTS = "小晓筱肖萧校孝笑消宵霄"      # 「xiao」这一类听感相近的字
_QIAN_VARIANTS = "千谦签迁倩浅纤铅嵌仟乾黔阡茜"  # 「qian」这一类

EventFn = Callable[[dict], None]

# 唤醒时触发的钩子（由 run.py 注册，用来把窗口弹到最前）
_wake_hook: Callable[[], None] | None = None


def set_wake_hook(fn: Callable[[], None] | None) -> None:
    """注册"被唤醒时"的回调（把窗口带到前台）。"""
    global _wake_hook
    _wake_hook = fn


def _fire_wake_hook() -> None:
    fn = _wake_hook
    if fn is None:
        return
    try:
        # 放到子线程里做：置前窗口可能阻塞几百毫秒，不能拖慢音频回调
        threading.Thread(target=fn, daemon=True, name="wake-focus").start()
    except Exception:
        pass


# --------------------------------------------------------------------------
# 唤醒词匹配（宽松）
# --------------------------------------------------------------------------
_WAKE_MAP = {}
for _c in _XIAO_VARIANTS:
    _WAKE_MAP[ord(_c)] = "小"
for _c in _QIAN_VARIANTS:
    _WAKE_MAP[ord(_c)] = "千"


def normalize_wake(text: str) -> str:
    """把易混的同音字归一化成「小」「千」，并去掉空白与标点。

    归一化是**逐字替换**的，所以归一化后的下标与原串一一对应 ——
    这样才能用它的位置在原文里截出"唤醒词之后的内容"。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        if ch.isspace() or ch in "，。、！？,.!?；;：:～~-—":
            continue
        out.append(_WAKE_MAP.get(ord(ch), ch))
    return "".join(out)


def match_wake(text: str) -> tuple:
    """检测唤醒词，返回 (是否命中, 唤醒词之后的内容)。

    比精确匹配宽松得多，为的是提高激活率：
      1. 双呼「小千小千」→ 命中；
      2. 单呼「小千」→ 也命中（灵敏度优先，宁可多唤一次也别喊不动）；
      3. 同音字（晓谦/小签/小迁…）归一化后照样能命中。
    误唤醒的代价只是弹个窗口，没喊动的代价是"以为坏了" —— 所以偏向灵敏。
    """
    if not text:
        return False, ""
    norm = normalize_wake(text)
    if not norm:
        return False, ""
    # 先看双呼：必须**整体跳过 4 个字**，否则「小千小千，今天天气」
    # 会把第二个"小千"当成正文留在指令里。
    idx = norm.find("小千小千")
    if idx >= 0:
        return True, _tail_after(text, idx + 4)
    # 再看单呼
    idx = norm.find("小千")
    if idx >= 0:
        return True, _tail_after(text, idx + 2)
    return False, ""


def _tail_after(text: str, skip_chars: int) -> str:
    """取原文中"第 skip_chars 个有效字符"之后的内容。"""
    seen = 0
    for i, ch in enumerate(text):
        if ch.isspace() or ch in "，。、！？,.!?；;：:～~-—":
            continue
        seen += 1
        if seen >= skip_chars:
            return text[i + 1:].strip("，。、！？,.!?　 ")
    return ""


def strip_wake_prefix(text: str) -> str:
    """去掉句子**开头**的唤醒词，只留真正的指令。

    ⚠️ 为什么需要它：唤醒之后，流式模型给出的仍然是"整段"识别结果，
    开头照样带着「小千小千」。原先直接把整段当指令发出去，
    用户看到的输入前面就永远挂着唤醒词 —— 这也正是"识别内容乱七八糟"的一部分。
    """
    s = (text or "").strip()
    for _ in range(3):                     # 最多剥三层，覆盖「小千小千」连呼
        norm = normalize_wake(s)
        idx = norm.find("小千")
        if idx < 0 or idx > 2:             # 只在开头附近才剥，避免误伤正文
            break
        hit, rest = match_wake(s)
        if not hit or rest == s:
            break
        s = rest.strip()
    return s


# --------------------------------------------------------------------------
# 模型定位
# --------------------------------------------------------------------------
def _model_candidates() -> list[str]:
    """ASR 模型目录候选：优先打包资源，其次本机模型目录。"""
    from . import config
    return [
        config.res("asr_model"),
        r"D:\local-multimodal-models\sherpa-asr",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "asr_model"),
    ]


def _model_rank(path: str) -> int:
    """模型目录的偏好打分（越大越优先）。

    为什么要有这个：同一个目录下可能同时躺着好几个模型。
    原来的写法是"谁先被 glob 到就用谁"，结果精度很差的 14M 小模型
    只要排在前面就会一直被选中 —— 识别出来的字当然乱七八糟。
    这里把已知更好的模型显式排在前面。
    """
    name = os.path.basename(path).lower()
    if "bilingual-zh-en" in name:
        return 100          # 中英双语 zipformer，明显比 14M 准，仍是流式
    if "zipformer" in name and "14m" not in name:
        return 80
    if "paraformer" in name or "sense-voice" in name:
        return 60           # 非流式，精度高但不适合边听边出
    if "14m" in name:
        return 10           # 最小最快，但精度最差
    return 50


def find_model_dir() -> str | None:
    """找到可用的 ASR 模型目录（需含 tokens.txt 与 onnx 权重）。

    多个候选时**按精度优先**选，而不是"谁先被扫到用谁"。
    """
    found = []
    for root in _model_candidates():
        if not root or not os.path.isdir(root):
            continue
        # 允许模型直接放在该目录，或放在其下一级子目录
        for cand in [root] + [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]:
            if os.path.exists(os.path.join(cand, "tokens.txt")) and glob.glob(
                    os.path.join(cand, "*.onnx")):
                found.append(cand)
    if not found:
        return None
    found.sort(key=_model_rank, reverse=True)
    return found[0]


def _pick(model_dir: str, *keywords: str) -> str | None:
    """在模型目录里按关键词挑文件（兼容 int8 / 不同 epoch 命名）。"""
    for kw in keywords:
        hits = sorted(glob.glob(os.path.join(model_dir, f"*{kw}*.onnx")))
        # 优先非 int8（精度更高），没有再退回 int8
        plain = [h for h in hits if "int8" not in os.path.basename(h)]
        if plain:
            return plain[0]
        if hits:
            return hits[0]
    return None


class VoiceListener:
    """麦克风监听 + 唤醒词检测 + 流式识别（单实例，常驻后台线程）。"""

    def __init__(self, on_event: EventFn | None = None):
        self._on_event = on_event or (lambda _e: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"              # idle | listening | awake
        self.error: str | None = None
        self._recognizer = None
        self._stream = None
        self._awake = False
        self._text = ""                  # 唤醒后累积的文本
        self._silence_start: float | None = None
        self._last_partial = ""
        self._preroll: list = []         # 门控前的一小段音频（避免吃掉第一个字）
        self._idle_since: float | None = None

    # ---------------- 对外接口 ----------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        model_dir = find_model_dir()
        return {
            "running": self.running,
            "state": self.state,
            "model_ready": model_dir is not None,
            "model_dir": model_dir,
            "wake_words": list(WAKE_WORDS),
            "text": self._text,
            "error": self.error,
        }

    def start(self) -> dict:
        """开始监听（幂等）。"""
        if self.running:
            return {"ok": True, "state": self.state, "already": True}

        if find_model_dir() is None:
            self.error = "未找到语音识别模型（asr_model 目录）"
            return {"ok": False, "error": self.error}

        self.error = None
        self._stop.clear()
        self._awake = False
        self._text = ""
        self._preroll = []
        self._idle_since = None
        self._silence_start = None
        self._last_partial = ""
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="voice-listener")
        self._thread.start()
        return {"ok": True, "state": "listening"}

    def stop(self) -> dict:
        """停止监听。"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3)
        self._thread = None
        self.state = "idle"
        self._emit({"type": "state", "state": "idle"})
        return {"ok": True, "state": "idle"}

    # ---------------- 内部实现 ----------------
    def _emit(self, event: dict) -> None:
        try:
            self._on_event(event)
        except Exception:
            pass

    def _build_recognizer(self) -> bool:
        if self._recognizer is not None:
            return True
        model_dir = find_model_dir()
        if not model_dir:
            return False
        try:
            import sherpa_onnx
        except ImportError as exc:
            self.error = f"未安装 sherpa-onnx：{exc}"
            return False

        encoder = _pick(model_dir, "encoder")
        decoder = _pick(model_dir, "decoder")
        joiner = _pick(model_dir, "joiner")
        tokens = os.path.join(model_dir, "tokens.txt")
        if not (encoder and decoder and joiner):
            self.error = "模型文件不完整（缺少 encoder/decoder/joiner）"
            return False

        try:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=tokens,
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                num_threads=4,                     # 2 → 4：解码更快，唤醒更跟手
                                                   # （本机 CPU 核心多，不差这两个线程）
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
                enable_endpoint_detection=False,   # 自行做静音判定
            )
        except Exception as exc:
            self.error = f"模型加载失败：{exc}"
            return False
        return True

    @staticmethod
    def _rms(samples) -> float:
        try:
            import numpy as np
            if samples.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(np.square(samples))))
        except Exception:
            return 0.0

    @staticmethod
    def _match_wake(text: str) -> tuple:
        """检测唤醒词（宽松匹配，见模块级 match_wake）。"""
        return match_wake(text)

    def _reset_stream(self) -> None:
        if self._recognizer is not None:
            self._stream = self._recognizer.create_stream()

    def _loop(self) -> None:
        """音频采集 + 识别主循环。"""
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            self.error = f"缺少音频库（sounddevice/numpy）：{exc}"
            self.state = "idle"
            self._emit({"type": "error", "message": self.error})
            return

        if not self._build_recognizer():
            self.state = "idle"
            self._emit({"type": "error", "message": self.error or "模型加载失败"})
            return

        self._reset_stream()
        self.state = "listening"
        self._emit({"type": "state", "state": "listening"})

        def callback(indata, _frames, _time_info, status):   # noqa: ANN001
            if status:
                pass
            buf = indata[:, 0].copy()          # 取第一声道
            self._feed(buf)

        try:
            with sd.InputStream(channels=1, samplerate=SAMPLE_RATE,
                                blocksize=BLOCK_SIZE, dtype="float32",
                                callback=callback):
                while not self._stop.is_set():
                    time.sleep(0.05)
        except Exception as exc:
            self.error = f"麦克风打开失败：{exc}"
            self._emit({"type": "error", "message": self.error})
        finally:
            self.state = "idle"
            self._emit({"type": "state", "state": "idle"})

    def _feed(self, samples) -> None:
        """把一块音频喂给识别器，并驱动状态机（含静音门控）。"""
        if self._stream is None:
            return

        rms = self._rms(samples)
        loud = rms >= GATE_RMS

        # —— 待机 + 安静：**不跑识别**，只维护一小段前置音频 ——
        # 这是让"常驻监听"不吃满 CPU 的关键：绝大多数时间都是静音，
        # 那些时刻完全不需要跑 ASR（之前没做门控，把 Web 服务都拖慢了）。
        if not self._awake and not loud:
            self._preroll.append(samples)
            if len(self._preroll) > PRE_ROLL_BLOCKS:
                self._preroll.pop(0)
            now = time.time()
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since > IDLE_RESET_SEC:
                # 安静够久就重置识别流，防止上下文越积越长、解码越来越慢
                self._reset_stream()
                self._idle_since = now
            return

        # —— 有人在说话（或已唤醒）：把前置音频一起补喂进去 ——
        # 不补的话，第一个字往往落在门控之前，会被吃掉
        if self._preroll:
            for buf in self._preroll:
                try:
                    self._stream.accept_waveform(SAMPLE_RATE, buf)
                except Exception:
                    pass
            self._preroll.clear()
        self._idle_since = None

        try:
            rec = self._recognizer
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while rec.is_ready(self._stream):
                rec.decode_stream(self._stream)
            text = (rec.get_result(self._stream) or "").strip()
        except Exception:
            return

        quiet = rms < SILENCE_RMS

        # —— 尚未唤醒：只在文本里找唤醒词 ——
        if not self._awake:
            hit, rest = self._match_wake(text)
            if hit:
                self._awake = True
                self.state = "awake"
                self._text = rest
                self._silence_start = time.time() if not rest else None
                self._last_partial = ""
                self._emit({"type": "wake", "state": "awake"})
                # 把窗口弹到最前（用户喊唤醒词时多半没看着窗口）
                _fire_wake_hook()
                if rest:
                    self._emit({"type": "partial", "text": rest})
            return

        # —— 已唤醒：累积文本 + 静音计时 ——
        if text:
            # 模型给的是整段结果，开头还带着唤醒词，必须先剥掉再当指令
            cmd = strip_wake_prefix(text)
            self._text = cmd
            if cmd != self._last_partial:
                self._last_partial = cmd
                self._emit({"type": "partial", "text": cmd})

        if quiet:
            if self._silence_start is None:
                self._silence_start = time.time()
            elif time.time() - self._silence_start >= SILENCE_SEC:
                final_text = (self._text or "").strip()
                if len(final_text) >= MIN_CHARS:
                    self._emit({"type": "final", "text": final_text,
                                "auto": True})
                # 内容不足就什么也不发（不会凭空发一条空消息）
                self._finish_round()
        else:
            self._silence_start = None

    def _finish_round(self) -> None:
        """一轮结束，回到待机监听。"""
        self._awake = False
        self._text = ""
        self._silence_start = None
        self._last_partial = ""
        self._preroll = []
        self._idle_since = None
        self._reset_stream()
        self.state = "listening"
        self._emit({"type": "state", "state": "listening"})


# —— 全局单例（供 FastAPI 路由使用）——
_listener: VoiceListener | None = None


def get_listener(on_event: EventFn | None = None) -> VoiceListener:
    global _listener
    if _listener is None:
        _listener = VoiceListener(on_event)
    elif on_event is not None:
        _listener._on_event = on_event
    return _listener
