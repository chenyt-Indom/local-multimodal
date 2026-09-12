# -*- coding: utf-8 -*-
"""多会话管理：新建 / 列表 / 切换 / 删除 / 持久化。

存储布局（data/sessions/）：
    index.json     会话元数据 [{id, title, created, updated, count}]
    {sid}.json     该会话的完整消息 [{role, content, ts}]

设计目标：每个会话互相独立（各自的上下文与历史），程序重启后仍可恢复；
同时每次保存都会把用户消息写入 transcript，供「你还记得吗」类检索使用。
"""
import json
import os
import time
import uuid

from . import config

DIR = config.data("sessions")
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
    """原子写：先写临时文件再替换，避免中途崩溃损坏数据。"""
    os.makedirs(os.path.dirname(path) or DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _path(sid: str) -> str:
    # 只保留安全字符，避免路径穿越
    safe = "".join(c for c in (sid or "") if c.isalnum() or c in "-_")
    return os.path.join(DIR, f"{safe}.json")


def list_sessions() -> list:
    """按最近更新倒序列出会话。"""
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        return []
    return sorted(idx, key=lambda s: s.get("updated", ""), reverse=True)


def create(title: str | None = None) -> dict:
    """新建一个会话。"""
    sid = uuid.uuid4().hex[:12]
    item = {"id": sid, "title": title or "新对话",
            "created": _now(), "updated": _now(), "count": 0}
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        idx = []
    idx.append(item)
    _write(INDEX, idx)
    _write(_path(sid), [])
    return item


def get_messages(sid: str) -> list:
    """读取某个会话的完整消息。"""
    msgs = _read(_path(sid), [])
    return msgs if isinstance(msgs, list) else []


def save_messages(sid: str, messages: list) -> dict | None:
    """保存完整消息，并更新索引（时间、条数、首条用户消息作标题）。"""
    if not sid:
        return None
    _write(_path(sid), messages)

    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        idx = []
    hit = None
    for s in idx:
        if s.get("id") == sid:
            hit = s
            break
    if hit is None:
        hit = {"id": sid, "title": "新对话", "created": _now()}
        idx.append(hit)
    hit["updated"] = _now()
    hit["count"] = len(messages)
    if hit.get("title") in (None, "", "新对话"):
        for m in messages:
            if m.get("role") == "user" and m.get("content"):
                hit["title"] = str(m["content"]).strip().replace("\n", " ")[:24]
                break
    _write(INDEX, idx)
    return hit


def rename(sid: str, title: str) -> bool:
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        return False
    for s in idx:
        if s.get("id") == sid:
            s["title"] = (title or "").strip()[:40] or s.get("title")
            s["updated"] = _now()
            _write(INDEX, idx)
            return True
    return False


def delete(sid: str) -> bool:
    """删除会话（索引 + 消息文件）。"""
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        idx = []
    kept = [s for s in idx if s.get("id") != sid]
    _write(INDEX, kept)
    try:
        os.remove(_path(sid))
    except Exception:
        pass
    return True


def ensure_default() -> dict:
    """确保至少有一个会话（首次启动时）。"""
    items = list_sessions()
    if items:
        return items[0]
    return create("新对话")


# =====================================================================
#  容量控制与清理
#  - 单个会话过长 → 裁剪（只留最近若干条，更早的丢弃）
#  - 太久未用且内容很少的会话 → 直接删除
#  重要信息由长期记忆（memory_doc.json）承载，不依赖聊天记录堆积。
# =====================================================================
MAX_KEEP = 120          # 单会话超过这么多条就触发裁剪
KEEP_RECENT = 60        # 裁剪后保留最近多少条
SESSION_TTL_DAYS = 45   # 超过这么多天未更新、且内容很少的会话会被清理


def _parse_ts(s: str) -> float:
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return time.time()


def prune(sid: str, max_keep: int = MAX_KEEP, keep_recent: int = KEEP_RECENT) -> int:
    """裁剪过长会话，返回被丢弃的条数（0 表示无需裁剪）。"""
    msgs = get_messages(sid)
    if len(msgs) <= max_keep:
        return 0
    dropped = len(msgs) - keep_recent
    _write(_path(sid), msgs[-keep_recent:])

    idx = _read(INDEX, [])
    if isinstance(idx, list):
        for s in idx:
            if s.get("id") == sid:
                s["count"] = keep_recent
                s["trimmed"] = int(s.get("trimmed") or 0) + dropped
                s["updated"] = _now()
                break
        _write(INDEX, idx)
    return dropped


def cleanup_old(days: int = SESSION_TTL_DAYS, max_count: int = 4) -> int:
    """清理"太久未用 + 内容很少"的会话，返回清理数量。

    只清理真正"不必要"的（基本没聊过的僵尸会话），
    有实际内容的会话一律保留，避免误删用户数据。
    """
    now = time.time()
    idx = _read(INDEX, [])
    if not isinstance(idx, list):
        return 0
    kept, removed = [], 0
    for s in idx:
        age_days = (now - _parse_ts(s.get("updated", ""))) / 86400
        if age_days > days and (s.get("count") or 0) <= max_count:
            try:
                os.remove(_path(s["id"]))
            except Exception:
                pass
            removed += 1
        else:
            kept.append(s)
    if removed:
        _write(INDEX, kept)
    return removed


def stats() -> dict:
    """会话总体占用情况（供界面展示）。"""
    items = list_sessions()
    total = sum(int(s.get("count") or 0) for s in items)
    return {"sessions": len(items), "messages": total,
            "max_keep": MAX_KEEP, "ttl_days": SESSION_TTL_DAYS}
