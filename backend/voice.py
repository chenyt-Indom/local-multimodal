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
MIN_CHARS = 2                       # 至少要识别出这么多字才允许自动发送

# —— 兜底时限（**必须有**）——
# 实测踩坑：手机/桌面环境底噪常常贴着静音阈值，于是"安静 2 秒"这个条件**永远不成立**，
# 一轮对话就永远结束不了 —— 状态卡在 awake，下次喊「小千小千」时唤醒分支根本不会执行，
# 表现就是"第一声能用，之后怎么喊都没反应"。这两个上限保证无论如何都能回到待机。
MAX_UTTERANCE_SEC = 12.0            # 一轮最长收音时间，到了就按已识别的内容发出去
# ⚠️⚠️ 唤醒后"**还没说出任何内容**"时的等待窗口。
#     这个值必须**明显大于** SILENCE_SEC，而且在这段时间里**绝不能**用"安静 2 秒"
#     去结束一轮 —— 实测（2026-09-19 用户报）：喊完「小千小千」停下来等反应是
#     最自然的动作，停顿 2.5 秒这一轮就被判成结束；用户紧接着说的指令因为
#     不含唤醒词而被整个丢掉。现象就是**提示条绿一下、然后输入框一直是空的**。
AWAIT_CMD_SEC = 10.0                # 唤醒后一直没开口 → 回待机（不发空消息）
                                    # ⚠️ 必须 < MAX_UTTERANCE_SEC：后者是绝对硬顶

# —— 静音门控：**跟着环境底噪走**（性能关键）——
# 常驻监听最容易踩的坑：每 100ms 就跑一次 ASR 解码，持续吃满 CPU 并持有 GIL，
# 把同时运行的 Web 服务饿死（实测健康检查从 20ms 恶化到 21 秒）。
# 所以待机时**只在检测到人声后才启动识别**，安静时就只算个音量，开销可忽略。
#
# ⚠️⚠️ 但**绝对阈值会直接失效**：本机实测（默认麦克风是摄像头上的麦）底噪 RMS
# 稳定在 **0.007~0.009**，而原来的 GATE_RMS=0.005 —— 门控**几乎一直开着**：
#   · ASR 一直在跑 → 上下文越积越长 → 越用越慢，还会把噪声认成字；
#   · 底噪又贴着 SILENCE_RMS=0.010 → "安静"永远不成立 → 一轮永远结束不了
#     → **下次喊唤醒词没反应**（就是用户报的"下次喊小千小千就没反应了"）。
# ⇒ 阈值必须**按环境自适应**：先估一个底噪，再按倍数定"说话"和"安静"。
NOISE_INIT = 0.006                  # 底噪初值（启动后 0.5 秒内会用真实音频覆盖它）
NOISE_DOWN = 0.05                   # 更安静了 → 缓慢跟随
# ⚠️ 底噪估计的**下限**（2026-09-19 实测踩到）：合成音频/录音里的"数字静音"
#    会把估计一路拖到 0.002，于是说话阈值掉到 SPEECH_MIN=0.008 —— 而本机
#    （摄像头上的麦）环境底噪就有 0.007~0.009，**噪声于是被判成"有人在说话"**：
#    噪声被喂进识别流，甚至被当成指令发出去（现象：唤醒后一直没人说话，
#    却冒出一个「小千」来）。真实麦克风不可能录到 0.000 的静音。
NOISE_FLOOR = 0.005
NOISE_UP_FAST = 0.08                # 确认是**稳态背景噪声** → 快速跟上
NOISE_UP = 0.002                    # 像说话 → **极慢**上升（否则说话声会被当成底噪）
NOISE_CAP = 0.024                   # 底噪最高只认到这个值：避免它涨上去把说话阈值也带飞
NOISE_DIP_WINDOW = 30               # "最近 3 秒里有没有明显低谷"，用来区分"说话"和"环境噪声"
NOISE_DIP_QUIET = 0.5               # 低于底噪的这个比例才算一次"低谷"
QUIET_VS_SPEECH = 0.30              # 比"最近说话电平"低这么多 → 判定说话人停下来了
SPEECH_K = 2.5                      # "有人在说话" = 底噪 × 2.5
SPEECH_MIN, SPEECH_MAX = 0.008, 0.12
SILENCE_K = 1.5                     # "这会儿安静" = 底噪 × 1.5
SILENCE_MIN, SILENCE_MAX = 0.003, 0.05
PRE_ROLL_BLOCKS = 10                # 检测到人声前多留 1.0 秒音频（流式模型要"热机"）
IDLE_RESET_SEC = 3.0                # 安静这么久就重置识别流，避免上下文无限增长

# —— 音频缓冲 ——
# ⚠️ ASR 解码**绝不能放在音频回调里**：回调一慢，PortAudio 就丢数据
# （输入溢出），丢掉的正是说话声的开头或中间 —— 识别出来就是缺字、串字。
# 正确做法：回调只做"复制 + 入队"（微秒级），解码交给工作线程慢慢做。
AUDIO_Q_MAX = 100                   # 最多缓冲 10 秒音频，再多就说明真跟不上了

# —— 唤醒词容错表 ——
# 语音识别把「小千」写成别的同音字太常见了（晓谦/小签/小迁…）。
# 只认精确的「小千」会导致"喊了没反应"，所以这里做同音归一化再匹配 ——
# 相当于把唤醒的置信度门槛放低，换取更高的激活率。
_XIAO_VARIANTS = "小晓筱肖萧校孝笑消宵霄"      # 「xiao」这一类听感相近的字
_QIAN_VARIANTS = "千谦签迁倩浅纤铅嵌仟乾黔阡茜"  # 「qian」这一类

# 名字里带这些词的多半**不是真麦克风**：系统映射器、环路回采（立体声混音）等。
# 拿它们当输入时，录到的是系统播放的声音（甚至什么都没有），自然"喊不动"。
_FAKE_MIC_HINTS = ("声音映射器", "sound mapper", "立体声混音", "stereo mix",
                   "主声音捕获", "what u hear", "loopback", "输出")

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


def is_bare_wake(text: str) -> bool:
    """整段**只有唤醒词**（用户又喊了一遍、还没说内容）。

    用来刷新"等待指令"的窗口。⚠️ 判据必须这么严：
    环境噪声会被模型认成乱七八糟的字，偶尔正好含「小千」两个字 ——
    若拿"剥掉唤醒词之后为空"当判据，窗口会被无限刷新、**永远回不到待机**
    （实测：唤醒后一直没人说话，几十秒过去了状态还挂在 awake）。
    """
    norm = normalize_wake(text or "")
    if not norm or len(norm) > 8:
        return False
    while norm.startswith("小千"):
        norm = norm[2:]
    return not norm


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
    """模型偏好打分（越大越优先）—— 按**实测中文口语字错率**定，不是按模型大小。

    实测（系统 TTS 合成 6 句中文口语 → 16k → 100ms 分块，和 App 里一模一样）：

      | 模型 | 字错率 | 连字病态 |
      |---|---|---|
      | zipformer-**zh-14M**（中文专用，21MB） | **3.8%** | 0 处 |
      | zipformer-**bilingual-zh-en**（181MB） | **17.0%** | 1 处 |

    双语模型的具体毛病：把「小千小千，今天天气怎么样」认成
    「小千小千**小千**今天**天天天**气怎么样」，「现在几点了」认成「现在几点几点了」
    —— 正是用户报的"说小千小千出来一堆重复字"。

    ⚠️ 早期版本把双语模型排在最前，理由是"它更大、更准" —— 那是拿**长句朗读**
    测的结论，对"唤醒词 + 短口令"这种场景**正好是反的**。
    ⇒ 中文专用流式模型排第一，双语退居兜底（留它是因为它还能认英文）。
    """
    name = os.path.basename(path).lower()
    if "paraformer" in name or "sense-voice" in name:
        return 50           # 非流式：精度高，但不能边听边出
    if "bilingual" in name:
        return 60           # 兜底：中英都能认，但短中文句子容易多字
    if "zipformer" in name or "-zh" in name:
        return 100          # 中文流式（含 14M）：实测最准
    return 70


def _configured_model() -> str:
    """配置里指定的 ASR 模型（目录名或路径；空 = 自动挑）。"""
    try:
        from . import config
        return str((config.load_config() or {}).get("voice_asr_model") or "")
    except Exception:
        return ""


def find_model_dir() -> str | None:
    """找到可用的 ASR 模型目录（需含 tokens.txt 与 onnx 权重）。

    多个候选时**按实测精度优先**选，而不是"谁先被扫到用谁"；
    配置里点名了就用配置的那个（名字或路径都行）。
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
    want = _configured_model()
    if want:
        low = want.lower()
        for f in found:
            if os.path.basename(f).lower() == low or f.lower() == low:
                return f
        for f in found:                     # 再按"包含匹配"
            if low and low in f.lower():
                return f
        # 配的名字找不到 → 别静默换一个，报错更清楚（在 status 里能看到）
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


# --------------------------------------------------------------------------
# 麦克风选择
# --------------------------------------------------------------------------
def list_input_devices() -> list:
    """所有能录音的设备（同一个物理设备在 MME / DirectSound / WASAPI / WDM-KS
    下会各出现一次 —— 这是正常的，多出来的那几条是不同驱动路径）。"""
    try:
        import sounddevice as sd
    except Exception:
        return []
    out = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            try:
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception:
                api = ""
            out.append({"index": i, "name": d.get("name") or "",
                        "hostapi": api,
                        "channels": d.get("max_input_channels"),
                        "default_rate": int(d.get("default_samplerate") or 0)})
    except Exception:
        return []
    return out


def default_input_index():
    """系统默认输入设备的索引（拿不到就返回 None）。"""
    try:
        import sounddevice as sd
        idx = sd.default.device[0]
        return int(idx) if idx is not None and int(idx) >= 0 else None
    except Exception:
        return None


def _looks_fake_mic(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _FAKE_MIC_HINTS)


def resolve_device(spec) -> tuple:
    """把配置里的设备（名字 / 序号 / 空）解析成 sounddevice 的设备号。

    返回 (index | None, 说明文字)。
    · 空 → 系统默认；但**系统默认是个假麦克风**（映射器/立体声混音）时，
      自动换成一个看着像真麦克风的设备 —— 那种设备录到的是系统声音，
      "喊不动"往往就是它造成的（实测本机默认给的是摄像头上的麦，勉强能用；
      换成"立体声混音"就彻底没声音）。
    · 名字 → 按"包含匹配"找（前端下拉框给的就是名字）；
    · 数字 → 直接当索引（越界就退回默认）。
    """
    devs = list_input_devices()
    if not devs:
        return None, "（拿不到设备列表）"
    spec = str(spec or "").strip()
    if spec:
        if spec.isdigit():
            i = int(spec)
            for d in devs:
                if d["index"] == i:
                    return i, d["name"]
        low = spec.lower()
        for d in devs:                       # 先精确、再包含
            if (d["name"] or "").lower() == low:
                return d["index"], d["name"]
        for d in devs:
            if low and low in (d["name"] or "").lower():
                return d["index"], d["name"]
        return None, "（配置里的麦克风没找到：%s，已退回系统默认）" % spec
    idx = default_input_index()
    name = ""
    for d in devs:
        if d["index"] == idx:
            name = d["name"]
            break
    if idx is not None and not _looks_fake_mic(name):
        return idx, name
    # 默认是个假麦克风（或拿不到默认）→ 挑第一个像真麦克风的
    for d in devs:
        if not _looks_fake_mic(d["name"]):
            return d["index"], d["name"]
    return idx, name or "（系统默认）"


class VoiceListener:
    """麦克风监听 + 唤醒词检测 + 流式识别（单实例，常驻后台线程）。"""

    def __init__(self, on_event: EventFn | None = None):
        self._on_event = on_event or (lambda _e: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # ⚠️ 起停必须上锁：`start()` 里 `if self.running` 和 `thread.start()` 之间
        # 隔着一次磁盘扫描（find_model_dir），那是**毫秒级**的窗口 ——
        # 自动启动（run.py）和界面上点 🎤 撞在一起时，两个调用都通过检查，
        # 于是就**开了两个监听线程、两个 InputStream、往同一个识别流喂两遍音频**
        # → 输出里就是重复的字（"小小千小千千"），而且两边抢状态，
        # 唤醒时灵时不灵。实测用户报的"重复字 + 时好时坏"正是这个形态。
        self._lock = threading.Lock()
        self._token = None               # 本次监听的"身份"，用来丢弃过期回调
        # 时钟做成可替换的：`_feed` 里所有"过了多久"的判断都走它，
        # 这样测试可以喂一段音频就推进 0.1 秒，不必真的等 12 秒。
        self._now = time.time
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
        self._awake_since: float | None = None
        # 这一轮**从唤醒那一刻**算起的起点。它**永不被刷新**，
        # 用来给一轮对话封顶（见 _feed 里的硬顶）。
        self._round_start: float | None = None
        self._q = None                   # 音频队列（回调入队、工作线程出队）
        self._noise = NOISE_INIT         # 环境底噪（自适应门控的基准）
        self._dips = []                  # 最近几块里"有没有明显低谷"（区分说话与稳态噪声）
        self._speech_ref = 0.0           # 最近的说话电平（慢衰减）：判断"说话人停下了吗"
        self._level = 0.0                # 最近一块的电平（给界面画电平条）
        self._level_at = 0.0
        self._device = None
        self._device_name = ""
        # 是不是"用户主动关掉的" —— 看门狗靠它区分：
        #   · 用户点 🎤 关掉 → 不许自动重开（开关的主动权在用户手里）
        #   · 线程自己没了   → 自动重开（否则就是"喊半天没反应"）
        self.manual_stop = False
        self._xruns = 0                  # PortAudio 报告"丢数据"的次数
        self._dropped = 0                # 我们自己的队列满、丢掉的块数

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
            # 是用户主动关的、还是它自己停的（看门狗据此决定要不要自动重开）
            "manual_stop": self.manual_stop,
            # —— 排障用：喊不动的时候先看这几个数 ——
            "device": self._device_name,
            "level": round(self._level, 5),          # 当前电平
            "noise": round(self._noise, 5),          # 估出来的环境底噪
            "speech_th": round(self._speech_th(), 5),  # 超过它才算"有人在说"
            "silence_th": round(self._silence_th(), 5),
            "xruns": self._xruns,                    # 音频丢帧次数（>0 说明回调被拖慢）
            "dropped": self._dropped,
        }

    def list_devices(self) -> dict:
        idx, name = resolve_device(self._configured_device())
        return {"ok": True, "devices": list_input_devices(),
                "current_index": idx, "current": name,
                "configured": self._configured_device()}

    def set_device(self, spec: str) -> dict:
        """换麦克风：写进配置并重启监听（配置为空 = 回到系统默认）。"""
        try:
            from . import config
            cfg = config.load_config() or {}
            cfg["voice_input_device"] = str(spec or "")
            config.save_config(cfg)
        except Exception as e:
            return {"ok": False, "error": "保存麦克风设置失败：%s" % e}
        was = self.running
        self.stop()
        if was or True:                  # 换完直接重新开始听，用户不用再点一次
            return {"ok": True, **self.start()}
        return {"ok": True}

    def _configured_device(self) -> str:
        try:
            from . import config
            return str((config.load_config() or {}).get("voice_input_device") or "")
        except Exception:
            return ""

    def _speech_th(self) -> float:
        """判定"有人在说话"的阈值 = 环境底噪 × 倍数（带上下限）。"""
        return max(SPEECH_MIN, min(self._noise * SPEECH_K, SPEECH_MAX))

    def _silence_th(self) -> float:
        """判定"这会儿安静"的阈值（比说话阈值低一档，中间留出迟滞区）。"""
        return max(SILENCE_MIN, min(self._noise * SILENCE_K, SILENCE_MAX))

    def _track_noise(self, rms: float) -> None:
        """跟着环境更新底噪估计 —— 三档速度，每档都有实测理由。

        ⚠️ 简单版（"安静就快点跟、说话就慢慢跟"）会**死锁**，实测踩过：
        说话里的停顿把底噪拖到 0.004，而房间底噪是 0.014 —— 于是
        "有人在说话"的阈值(0.011)反而低于房间底噪 → 每个块都被当成说话 →
        `quiet` 永远不成立 → 一轮结束不了 → **下次喊唤醒词没反应**。
        所以这里靠**"最近有没有低谷"**来区分"说话"和"稳态噪声"：
        说话一定有停顿（电平会掉下去），空调/风扇那种稳态噪声没有。
        """
        dip = rms < self._noise * NOISE_DIP_QUIET
        self._dips.append(dip)
        if len(self._dips) > NOISE_DIP_WINDOW:
            self._dips.pop(0)
        no_dip = len(self._dips) >= NOISE_DIP_WINDOW and not any(self._dips)

        if rms < self._noise:
            self._noise += (rms - self._noise) * NOISE_DOWN
        elif no_dip and self._noise < NOISE_CAP:
            # 连续 3 秒没有低谷 + 电平高于底噪 → 这是环境本身变吵了，让底噪跟上。
            # ⚠️ 抬升有上限 NOISE_CAP：万一误判（一直没停顿的长句），
            #    也不至于把说话阈值带到"喊破喉咙才认"的高度。
            target = min(rms, NOISE_CAP)
            self._noise += (target - self._noise) * NOISE_UP_FAST
        else:
            self._noise += (rms - self._noise) * NOISE_UP

        if self._noise < NOISE_FLOOR:
            self._noise = NOISE_FLOOR       # 见 NOISE_FLOOR 的注释：不许掉到地板以下

    def start(self) -> dict:
        """开始监听（幂等 + 并发安全）。"""
        with self._lock:                 # 见 __init__ 里的说明：这里必须原子
            if self.running:
                return {"ok": True, "state": self.state, "already": True}

            if find_model_dir() is None:
                self.error = "未找到语音识别模型（asr_model 目录）"
                return {"ok": False, "error": self.error}

            self.error = None
            self.manual_stop = False      # 这次是"要它听"，看门狗的标志复位
            self._stop.clear()
            self._awake = False
            self._text = ""
            self._preroll = []
            self._idle_since = None
            self._awake_since = None
            self._round_start = None
            self._silence_start = None
            self._last_partial = ""
            self._noise = NOISE_INIT
            self._dips = []
            self._speech_ref = 0.0
            self._xruns = self._dropped = 0
            try:
                import queue as _q
                self._q = _q.Queue(maxsize=AUDIO_Q_MAX)
            except Exception:
                self._q = None
            self._token = object()
            self._thread = threading.Thread(target=self._loop, args=(self._token,),
                                            daemon=True, name="voice-listener")
            self._thread.start()          # 在锁里 start：第二个调用只会看到 already
        return {"ok": True, "state": "listening"}

    def stop(self, manual: bool = True) -> dict:
        """停止监听。

        `manual=True`（默认）= 用户/接口主动关的 → **看门狗不许自动重开**；
        线程自己退出（`_loop` 走完 finally，不会调这里）时标志保持原样，
        于是看门狗会把它重新拉起来 —— 见 `needs_restart()`。
        """
        with self._lock:
            self._stop.set()
            t = self._thread
            self._token = None            # 作废：正在路上的音频回调会被丢弃
            if manual:
                self.manual_stop = True
        if t is not None and t.is_alive():
            t.join(timeout=3)
        with self._lock:
            self._thread = None
        self.state = "idle"
        self._emit({"type": "state", "state": "idle"})
        return {"ok": True, "state": "idle"}

    def needs_restart(self) -> bool:
        """该不该由看门狗把它重新拉起来？

        ⚠️ 为什么需要：用户报"喊不出来"时，实测遇到 `running:false, error:null`
        —— 监听线程已经退出，可是**一点线索都没有**（不是"打开麦克风失败"，
        也不是"没找到模型"），界面那边只会显示"语音已停止"。这种情况必须能自愈，
        否则用户就是"怎么喊都没反应"。

        只重建"意外停止"的：**用户自己点 🎤 关掉的不动**（开关的主动权在用户手里）。
        """
        return (not self.running) and (not self.manual_stop)

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

    def _loop(self, token=None) -> None:
        """音频采集（回调只入队）+ 识别主循环（工作线程慢慢做）。"""
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
        dev, dev_name = resolve_device(self._configured_device())
        self._device, self._device_name = dev, dev_name

        def callback(indata, _frames, _time_info, status):   # noqa: ANN001
            # ⚠️ 这里只做"复制 + 入队"，**一点解码都不做**。
            # 以前是在回调里直接跑 ASR —— 回调一慢 PortAudio 就丢数据，
            # 而丢掉的正是说话声，识别出来就是缺字/串字。
            if token is not None and token is not self._token:
                return                      # 上一轮遗留的回调，丢弃
            if status:
                self._xruns += 1            # 记下来：>0 就说明还有地方拖慢了音频线程
            q = self._q
            if q is None:
                return
            try:
                q.put_nowait(indata[:, 0].copy())
            except Exception:
                self._dropped += 1          # 队列满（真跟不上了），丢最旧的更糟，只能丢这一块

        self.state = "listening"
        try:
            with sd.InputStream(channels=1, samplerate=SAMPLE_RATE,
                                blocksize=BLOCK_SIZE, dtype="float32",
                                device=self._device, callback=callback):
                self._emit({"type": "state", "state": "listening",
                            "device": dev_name})
                # 先用 0.5 秒真实音频估一个环境底噪（阈值全靠它，估不准就喊不动）
                self._prime_noise()
                while not self._stop.is_set():
                    item = None
                    try:
                        item = self._q.get(timeout=0.1)
                    except Exception:
                        item = None
                    if item is None:
                        continue
                    if token is not None and token is not self._token:
                        return              # 这轮已经作废（被 stop 或重启过）
                    self._feed(item)
        except Exception as exc:
            self.error = f"麦克风打开失败：{exc}"
            self._emit({"type": "error", "message": self.error})
        finally:
            self.state = "idle"
            self._emit({"type": "state", "state": "idle"})

    def _prime_noise(self) -> None:
        """用开头几块真实音频估环境底噪（取中位数，避免被偶发噪声带偏）。"""
        vals = []
        t0 = time.time()
        while time.time() - t0 < 0.6 and len(vals) < 6:
            try:
                b = self._q.get(timeout=0.2)
            except Exception:
                break
            vals.append(self._rms(b))
        if vals:
            vals.sort()
            self._noise = max(SILENCE_MIN, vals[len(vals) // 2])
            self._emit({"type": "level", "rms": round(self._level, 5),
                        "noise": round(self._noise, 5),
                        "speech_th": round(self._speech_th(), 5)})

    def _round_expired(self, now: float) -> bool:
        """这一轮是不是该结束了？**必须在门控之前调用**（原因见 _feed 里的注释）。

        返回 True 表示已经结束这一轮，调用方应当直接 return。

        两条界线，职责不同：
          · `AWAIT_CMD_SEC`（10 秒，从"上次听到唤醒词"算起）—— 还没开口时的等待窗口，
            用户重喊唤醒词会把起点往后挪（给"喊一声→停一下→再喊一声"留余地）；
          · `MAX_UTTERANCE_SEC`（12 秒，从**唤醒那一刻**算起，**永不刷新**）—— 绝对硬顶。
            刷新类的判据再好也会被环境噪声偶尔误触发，没有这条就会永远挂在 awake。
        """
        if not self._awake:
            return False
        has_text = len((self._text or "").strip()) >= MIN_CHARS
        hard = bool(self._round_start
                    and now - self._round_start >= MAX_UTTERANCE_SEC)
        if not has_text:
            wait = bool(self._awake_since
                        and now - self._awake_since >= AWAIT_CMD_SEC)
            if hard or wait:
                self._finish_round(note="没听到内容 · 再喊一次「小千小千」")
                return True
            return False
        if hard:
            self._emit({"type": "final", "text": (self._text or "").strip(),
                        "auto": True})
            self._finish_round()
            return True
        return False

    def _feed(self, samples) -> None:
        """把一块音频喂给识别器，并驱动状态机（含自适应门控）。"""
        if self._stream is None:
            return

        rms = self._rms(samples)
        self._level = rms
        self._track_noise(rms)

        loud = rms >= self._speech_th()          # 有人在说话（阈值跟着底噪走）
        # "最近的说话电平"：只跟着说话块走、每块衰减 0.5%（约 2 秒记忆）
        self._speech_ref = (max(rms, self._speech_ref * 0.995) if loud
                            else self._speech_ref * 0.995)
        # "安静"有两条路，命中任一即可（单靠底噪会死锁，见 _track_noise 的说明）：
        #   ① 电平低于（跟着底噪走的）静音阈值；
        #   ② 电平比"刚才说话时"低一大截 —— 说话人明显停下来了。
        quiet = (rms < self._silence_th()) or \
                (self._speech_ref > 0 and rms < self._speech_ref * QUIET_VS_SPEECH)

        # 每 0.5 秒把电平推给界面（画电平条、让人一眼看出"麦克风到底有没有声音"）
        now = self._now()
        if now - self._level_at >= 0.5:
            self._level_at = now
            self._emit({"type": "level", "rms": round(rms, 5),
                        "noise": round(self._noise, 5),
                        "speech_th": round(self._speech_th(), 5),
                        "state": self.state})

        # ⚠️⚠️ 本轮该不该结束 —— **必须在门控之前判**。
        #    门控下面会直接 return（安静就不喂识别流），把超时/等待判定放在它后面，
        #    等于"用户一直不说话时永远不会超时"：实测唤醒后没人开口，状态在 awake
        #    上挂了 15 秒下不来、界面就一直停在"我在听"。
        if self._round_expired(now):
            return

        # —— 还没"开口"（待机中 / 已唤醒但还没说出内容）+ 安静：**不喂识别流** ——
        #    前半段是常驻监听的性能关键（见 GATE 那一段的注释）；
        #    后半段是"唤醒后还在等用户开口"：那段时间的音频全是环境噪声，
        #    喂进去只会被认成乱七八糟的字、甚至当成指令发出去（实测过）。
        #    两种情况都只维护一小段前置音频，等真有人声时一起补喂（保住第一个字）。
        if (not loud and not (self._awake
                              and len((self._text or "").strip()) >= MIN_CHARS)):
            self._preroll.append(samples)
            if len(self._preroll) > PRE_ROLL_BLOCKS:
                self._preroll.pop(0)
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

        # —— 尚未唤醒：只在文本里找唤醒词 ——
        if not self._awake:
            hit, rest = self._match_wake(text)
            if hit:
                self._awake = True
                self.state = "awake"
                self._awake_since = now
                self._round_start = now
                # ⚠️ 只有 1 个字的"尾巴"不算内容：唤醒那一刻解码器常常还没吐完
                #    （「小千小千」后面跟半个字），当内容的话输入框会闪一个残字，
                #    还会让"还没开口"的等待窗口提前失效。
                rest = rest if len(rest) >= MIN_CHARS else ""
                self._text = rest
                # ⚠️ **不要**在这儿给"还没内容"的情况启动静音计时：
                #    用户喊完唤醒词后停下来等反应是最自然的动作，一停就超过
                #    SILENCE_SEC=2 秒，这一轮会被立刻结束，紧接着说的指令
                #    因为不含唤醒词而被整个丢掉（详见 AWAIT_CMD_SEC 的注释）。
                #    "还没开口"的结束时机由下面的兜底一（AWAIT_CMD_SEC）负责。
                self._silence_start = None
                self._last_partial = ""
                self._emit({"type": "wake", "state": "awake",
                            "note": "我在听，请说…"})
                # 把窗口弹到最前（用户喊唤醒词时多半没看着窗口）
                _fire_wake_hook()
                if rest:
                    self._emit({"type": "partial", "text": rest})
            return

        # —— 已唤醒：累积文本 + 静音计时 ——
        if text:
            # 模型给的是整段结果，开头还带着唤醒词，必须先剥掉再当指令
            cmd = strip_wake_prefix(text)
            # ⚠️ **只有非空结果才更新**：流式结果是累积的、不会倒退，
            #    出现空结果说明这一块是噪声/误识别 —— 拿它去赋值会把用户
            #    已经说出来的内容清掉（输入框里的字"闪一下没了"）。
            if len(cmd) >= MIN_CHARS:
                if cmd != self._text:
                    self._text = cmd
                if cmd != self._last_partial:
                    self._last_partial = cmd
                    self._emit({"type": "partial", "text": cmd})
            elif (is_bare_wake(text) and self._awake_since
                  and now - self._awake_since >= 1.0):
                # 整段**只有唤醒词本身**（用户又说了一遍「小千小千」）→ 刷新等待窗口。
                # 不刷新的话，"喊一声 → 停一下 → 再喊一声确认"这种动作会撞上
                # AWAIT_CMD_SEC 被判超时，用户会以为"喊了没反应"。
                self._awake_since = now

        # ⚠️ "已经开口"的判据是"**至少 MIN_CHARS 个字**"，不能只看"非空"：
        #    唤醒那一刻解码器常把唤醒词的尾音多吐一个字（实测「小千小千」后面
        #    跟一个「小」），只看非空的话 has_text 立刻为真 → 静音 2 秒就把这一轮
        #    结束了 —— 用户完全没机会开口。这正是"绿一下、没下文"的成因之一。
        #
        #    （超时/等待窗口在 _feed 开头就用 _round_expired 判过了，
        #      那儿才是对的判位置 —— 见那一段的注释。）
        if len((self._text or "").strip()) < MIN_CHARS:
            return

        # —— 已经说出内容了：安静 SILENCE_SEC 秒就自动发送 ——
        if quiet:
            if self._silence_start is None:
                self._silence_start = now
            elif now - self._silence_start >= SILENCE_SEC:
                final_text = (self._text or "").strip()
                if len(final_text) >= MIN_CHARS:
                    self._emit({"type": "final", "text": final_text,
                                "auto": True})
                # 内容不足就什么也不发（不会凭空发一条空消息）
                self._finish_round()
        else:
            self._silence_start = None

    def _finish_round(self, note: str = "") -> None:
        """一轮结束，回到待机监听。

        注意：**底噪 `_noise` 不重置**（那是环境的属性，不是这一轮的属性），
        只清掉与"这一轮"有关的状态。
        `note` 会随 state 事件发给界面：用来解释"这一轮为什么结束了"
        （比如"没听到内容"），否则用户只看到提示条不再发绿，不知道发生了什么。
        """
        self._awake = False
        self._text = ""
        self._silence_start = None
        self._awake_since = None
        self._round_start = None
        self._last_partial = ""
        self._preroll = []
        self._idle_since = None
        self._dips = []
        self._speech_ref = 0.0
        self._reset_stream()
        self.state = "listening"
        ev = {"type": "state", "state": "listening"}
        if note:
            ev["note"] = note
        self._emit(ev)


# —— 全局单例（供 FastAPI 路由使用）——
_listener: VoiceListener | None = None


def get_listener(on_event: EventFn | None = None) -> VoiceListener:
    global _listener
    if _listener is None:
        _listener = VoiceListener(on_event)
    elif on_event is not None:
        _listener._on_event = on_event
    return _listener
