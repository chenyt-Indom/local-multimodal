# -*- coding: utf-8 -*-
"""视频读取能力。

将视频的若干关键帧抽出来（间隔采样 + 均匀采样），把帧作为 base64 图片
送入 Qwen-VL 理解，从而让多模态模型“读取视频内容”。
依赖 OpenCV（cv2），完全本地。
"""
import os
import base64
import io
import time

import cv2

MAX_FRAMES = 8        # 最多送多少帧进模型（越多越准但越慢、越吃上下文）
SAMPLE_INTERVAL = 2.0  # 秒，超过一定时长按间隔抽帧


def extract_frames(path: str, max_frames: int = MAX_FRAMES,
                   interval: float = SAMPLE_INTERVAL,
                   max_side: int = 768):
    """抽取视频关键帧，返回 list[base64] 及元信息。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"视频不存在: {path}"}
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return {"ok": False, "error": "无法打开视频（可能格式不支持或缺少解码器）"}

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0.0

    frames_b64 = []
    picked = []
    # 策略1：按时间间隔抽帧
    step = max(int(fps * interval), 1)
    idx = 0
    while idx < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            break
        b64 = _frame_to_b64(frame, max_side)
        frames_b64.append(b64)
        picked.append(idx)
        idx += step
        if len(frames_b64) >= max_frames:
            break
    # 策略2：若抽得不够（视频很短），从头均匀补帧
    if len(frames_b64) < min(max_frames, 4) and total > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        frames_b64 = []
        picked = []
        n = min(max_frames, max(3, total))
        for i in range(n):
            pos = int(i * total / n)
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ok, frame = cap.read()
            if ok:
                frames_b64.append(_frame_to_b64(frame, max_side))
                picked.append(pos)
    cap.release()
    cv2.destroyAllWindows()

    if not frames_b64:
        return {"ok": False, "error": "未能从视频中抽取到有效帧"}
    return {"ok": True, "frames": frames_b64, "count": len(frames_b64),
            "fps": fps, "frames_total": total, "duration": round(duration, 1),
            "picked": picked}


def _frame_to_b64(frame, max_side=768):
    h, w = frame.shape[:2]
    if max(h, w) > max_side:
        ratio = max_side / max(h, w)
        frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
    # 转 JPG 压缩
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def build_video_caption_prompt(frames: int, duration: float) -> str:
    """拼接视频说明指令，附在图片帧之后。"""
    return (f"这是一段视频（约{duration}秒，抽了{frames}个关键帧）。"
            f"请综合这些画面帧，详细描述视频的内容、人物、动作和情节。")