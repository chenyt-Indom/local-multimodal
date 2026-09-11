# -*- coding: utf-8 -*-
"""本地语音输入：唤醒词「西派西派」+ 离线流式识别 + 静音自动发送。

工作流程
--------
1. 用户点开麦克风 → 进入**待机监听**（state=listening），持续做流式识别
2. 识别文本中命中唤醒词（默认「西派」）→ 进入**收音**（state=awake），
   唤醒词之后的内容作为指令开头
3. 收音期间持续累积文本并实时推给前端（partial 事件）
4. 检测到**连续静音超过 silence_sec 秒**且已有内容 → 发出 final 事件
   （auto=True），前端据此自动发送；随后回到待机监听

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
WAKE_WORDS = ("西派西派", "西派")   # 命中任意一个即唤醒
SILENCE_SEC = 2.0                   # 唤醒后静音多久自动发送
SILENCE_RMS = 0.012                 # 低于此 RMS 视为静音（可调）
MIN_CHARS = 2                       # 至少要识别出这么多字才允许自动发送

EventFn = Callable[[dict], None]


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


def find_model_dir() -> str | None:
    """找到可用的 ASR 模型目录（需含 tokens.txt 与 onnx 权重）。"""
    for root in _model_candidates():
        if not root or not os.path.isdir(root):
            continue
        # 允许模型直接放在该目录，或放在其下一级子目录
        for cand in [root] + [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]:
            if os.path.exists(os.path.join(cand, "tokens.txt")) and glob.glob(
                    os.path.join(cand, "*.onnx")):
                return cand
    return None


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
                num_threads=2,
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

    def _match_wake(self, text: str) -> tuple[bool, str]:
        """检测唤醒词，返回 (是否命中, 唤醒词之后的内容)。"""
        for w in WAKE_WORDS:
            idx = text.find(w)
            if idx >= 0:
                return True, text[idx + len(w):].strip()
        return False, ""

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
        """把一块音频喂给识别器，并驱动状态机。"""
        if self._stream is None:
            return
        try:
            rec = self._recognizer
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while rec.is_ready(self._stream):
                rec.decode_stream(self._stream)
            text = (rec.get_result(self._stream) or "").strip()
        except Exception:
            return

        rms = self._rms(samples)
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
                if rest:
                    self._emit({"type": "partial", "text": rest})
            return

        # —— 已唤醒：累积文本 + 静音计时 ——
        if text:
            self._text = text
            if text != self._last_partial:
                self._last_partial = text
                self._emit({"type": "partial", "text": text})

        if quiet:
            if self._silence_start is None:
                self._silence_start = time.time()
            elif time.time() - self._silence_start >= SILENCE_SEC:
                final_text = (self._text or "").strip()
                if len(final_text) >= MIN_CHARS:
                    self._emit({"type": "final", "text": final_text,
                                "auto": True})
                self._finish_round()
        else:
            self._silence_start = None

    def _finish_round(self) -> None:
        """一轮结束，回到待机监听。"""
        self._awake = False
        self._text = ""
        self._silence_start = None
        self._last_partial = ""
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
