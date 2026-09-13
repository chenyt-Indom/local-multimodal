# -*- coding: utf-8 -*-
"""本地图片库：把搜到的图 / 生成的图保存下来，随时调用与删除。

存储布局（data/image_library/）：
    index.json      元数据 [{id, name, source, created, size, w, h, origin}]
    {id}.png        图片本体

设计要点：
- 元数据与图片分离，列表接口无需读图，响应快
- 原子写索引，避免中途崩溃损坏
- 图片本体保留原始格式（png/jpg/webp 统一存为原字节，扩展名记录在 meta）
"""
import base64
import io
import json
import os
import time
import uuid

from . import config

DIR = config.data("image_library")
INDEX = os.path.join(DIR, "index.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_id(iid: str) -> str:
    return "".join(c for c in (iid or "") if c.isalnum() or c in "-_")


def _img_path(iid: str, ext: str = "png") -> str:
    return os.path.join(DIR, f"{_safe_id(iid)}.{ext}")


def _find_file(iid: str) -> str | None:
    """按 id 找实际图片文件（扩展名不固定）。"""
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = _img_path(iid, ext)
        if os.path.isfile(p):
            return p
    return None


def list_images() -> list:
    """按入库时间倒序列出图片。"""
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        return []
    return sorted(idx, key=lambda x: x.get("created", ""), reverse=True)


def save_image(data, name: str = "", source: str = "",
               origin: str = "web") -> dict:
    """把图片存入图库。

    参数:
        data    base64 字符串（可带 data: 前缀）/ bytes / 本地文件路径
        name    显示名称，留空则自动生成
        source  来源说明（如某网页标题或"AI 生成"）
        origin  来源类型：web（搜到的）/ gen（生成的）/ edit（微改的）/ upload（用户上传）

    返回元数据字典；失败返回 {"ok": False, "error": ...}
    """
    raw = None
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        if os.path.isfile(data):
            with open(data, "rb") as f:
                raw = f.read()
        else:
            s = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
            try:
                raw = base64.b64decode(s, validate=False)
            except Exception as e:
                return {"ok": False, "error": f"base64 解码失败：{e}"}
    if not raw:
        return {"ok": False, "error": "没有可保存的图片数据"}

    # 识别真实格式（不信任扩展名）
    ext = "png"
    if raw[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    elif raw[:3] == b"GIF":
        ext = "gif"

    iid = uuid.uuid4().hex[:12]
    # 必须先确保目录存在：config.data() 只负责拼路径，不会创建目录
    os.makedirs(DIR, exist_ok=True)
    with open(_img_path(iid, ext), "wb") as f:
        f.write(raw)

    # 尺寸（拿不到就算了，不影响保存）
    w = h = 0
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
    except Exception:
        pass

    meta = {"id": iid, "name": (name or "").strip()[:40] or f"图片 {iid[:6]}",
            "source": (source or "").strip()[:200], "origin": origin,
            "created": _now(), "size": len(raw), "w": w, "h": h, "ext": ext}

    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        idx = []
    idx.append(meta)
    _write(INDEX, idx)
    return meta


def get_b64(iid: str) -> str | None:
    """读取图片为 base64（供前端展示）。"""
    p = _find_file(iid)
    if not p:
        return None
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_path(iid: str) -> str | None:
    return _find_file(iid)


def rename(iid: str, name: str) -> bool:
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        return False
    for m in idx:
        if m.get("id") == iid:
            m["name"] = (name or "").strip()[:40] or m.get("name")
            _write(INDEX, idx)
            return True
    return False


def delete(iid: str) -> bool:
    """删除图片（文件 + 元数据）。"""
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        idx = []
    kept = [m for m in idx if m.get("id") != iid]
    _write(INDEX, kept)
    p = _find_file(iid)
    if p:
        try:
            os.remove(p)
        except Exception:
            pass
    return True


def stats() -> dict:
    items = list_images()
    total = sum(int(m.get("size") or 0) for m in items)
    return {"count": len(items), "total_bytes": total}
