# -*- coding: utf-8 -*-
"""记忆库：**一份长期记忆（跨对话共享） + 每个对话一份短期记忆（互相独立）**。

为什么这么分
------------
- **长期记忆**放"无论聊什么都成立"的稳定信息：姓名、身份、偏好、约定。
  它**全局只有一份**，所以新开对话也不会"不认识你"。
- **短期记忆**放"这件事本身"的上下文要点：正在做的项目、这段讨论的结论。
  它**按对话隔离**——切到别的对话就是另一份，互不串味；而且**可以随时释放**。

存储（data/memory/）
    long_term.json    {"content": "...", "updated_at": 123}
    short_term.json   {"<session_id>": {"content": "...", "updated_at": 123}}

归档（transcript）仍在 data/transcript/，是"提取记忆"的原始素材；
释放归档前会先把它沉淀进记忆（见 main.py 的 _sweep_*），避免重要内容一起被删掉。
"""
import os
import re
import json
import time
from . import config

DATA_DIR = config.data("data")
MEM_DIR = os.path.join(DATA_DIR, "memory")
LONG_FILE = os.path.join(MEM_DIR, "long_term.json")
SHORT_FILE = os.path.join(MEM_DIR, "short_term.json")
TRANSCRIPT_DIR = os.path.join(DATA_DIR, "transcript")

# 老格式（一次性迁移用，迁移后改名保留，不删）
LEGACY_DOC_FILE = os.path.join(DATA_DIR, "memory_doc.json")        # 多分区版
LEGACY_CONV_FILE = os.path.join(MEM_DIR, "conversations.json")     # 早期"每对话一块"版
LEGACY_GLOBAL_FILE = os.path.join(MEM_DIR, "global.json")          # 早期"全局偏好"版

# 上限。
# 长期记忆放宽到 15000 字：它是跨对话共享的"用户档案 + 领域知识"，
# 用户明确要求能装下更多内容（原来 2000 字确实容易顶到）。
# 短期记忆仍保持精简 —— 它只服务当前这一件事，塞多了反而稀释重点。
LONG_CAP = 15000     # 长期记忆
SHORT_CAP = 1000     # 单个对话的短期记忆

_STOP_WORDS = set(
    "的 了 在 是 和 与 我 你 他 她 它 我们 你们 一 个 有 也 都 要 把 让 这 那 就 吗 呢 啊 吧 很 会 可以 请 帮 下 换 或 并 及 想 要 说 请".split())


# =====================================================================
#  基础读写
# =====================================================================
def _now():
    return time.time()


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 原子替换，避免写一半断电把记忆写坏


def _size(path) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _safe_sid(sid) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or ""))[:48]


def _norm_point(p: str) -> str:
    """归一化一条要点：去掉首尾空白与句末标点。

    否则「喜欢简洁的回答」和「喜欢简洁的回答。」会被当成两条重复记进去。
    """
    return (p or "").strip().strip("。.；;，,、！!？?　 -•*")


def _split_items(text: str) -> list:
    return [p.strip() for p in re.split(r"[；;\n]", text or "") if p.strip()]


def _trim(text: str, cap: int) -> str:
    """超上限时从**最早**的条目开始丢（新的更重要）。"""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    items = _split_items(text)
    while items and len("；".join(items)) > cap:
        items.pop(0)
    return "；".join(items)


def _tokenize(text):
    tokens = []
    for ch in re.findall(r"[\u4e00-\u9fa5_a-zA-Z0-9]+", (text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fa5]+", ch):
            for i in range(len(ch) - 1):
                tokens.append(ch[i:i + 2])
            if len(ch) == 1:
                tokens.append(ch)
        else:
            tokens.append(ch)
    return [w for w in tokens if w not in _STOP_WORDS and w.strip()]


# =====================================================================
#  长期记忆（全局唯一一份，跨对话共享）
# =====================================================================
def get_long() -> str:
    d = _read_json(LONG_FILE, {})
    return str((d or {}).get("content") or "") if isinstance(d, dict) else ""


def get_long_doc() -> dict:
    d = _read_json(LONG_FILE, {})
    if not isinstance(d, dict):
        d = {}
    return {"content": str(d.get("content") or ""),
            "updated_at": float(d.get("updated_at") or 0)}


def set_long(content: str) -> dict:
    content = _trim(content, LONG_CAP)
    doc = {"content": content, "updated_at": _now()}
    _write_json(LONG_FILE, doc)
    return doc


def merge_long(point: str) -> bool:
    """把一条要点并入长期记忆（追加 + 去重），返回是否真的写入了。"""
    point = _norm_point(point)
    if not point:
        return False
    cur = get_long()
    if _already(cur, point):
        return False
    new = (cur.rstrip("；;。 \n") + "；" + point) if cur.strip() else point
    set_long(new)
    return True


def _already(body: str, point: str) -> bool:
    """判断要点是否已在文段里（完全重复 / 被现有条目涵盖 / 涵盖现有条目）。"""
    if not body:
        return False
    if point in body:
        return True
    for e in _split_items(body):
        e = _norm_point(e)
        if not e:
            continue
        if point == e or point in e:
            return True
    return False


# =====================================================================
#  短期记忆（每个对话一份，互相独立）
# =====================================================================
def _load_shorts() -> dict:
    d = _read_json(SHORT_FILE, {})
    return d if isinstance(d, dict) else {}


def get_short_doc(sid: str) -> dict:
    sid = _safe_sid(sid)
    if not sid:
        return {"content": "", "updated_at": 0}
    it = _load_shorts().get(sid) or {}
    return {"content": str(it.get("content") or ""),
            "updated_at": float(it.get("updated_at") or 0)}


def get_short(sid: str) -> str:
    return get_short_doc(sid)["content"]


def set_short(sid: str, content: str) -> dict:
    sid = _safe_sid(sid)
    if not sid:
        return {"content": "", "updated_at": 0}
    content = _trim(content, SHORT_CAP)
    shorts = _load_shorts()
    if content:
        shorts[sid] = {"content": content, "updated_at": _now()}
    else:
        shorts.pop(sid, None)       # 清空就不占地方
    _write_json(SHORT_FILE, shorts)
    return {"content": content, "updated_at": _now()}


def merge_short(sid: str, point: str) -> bool:
    point = _norm_point(point)
    if not point or not _safe_sid(sid):
        return False
    cur = get_short(sid)
    if _already(cur, point):
        return False
    new = (cur.rstrip("；;。 \n") + "；" + point) if cur.strip() else point
    set_short(sid, new)
    return True


def delete_short(sid: str) -> bool:
    """对话被删除时调用，免得攒一堆孤儿短期记忆。"""
    sid = _safe_sid(sid)
    shorts = _load_shorts()
    if sid in shorts:
        shorts.pop(sid, None)
        _write_json(SHORT_FILE, shorts)
        return True
    return False


# =====================================================================
#  改写已有条目（目标的进展更新 / 作废旧信息）
# =====================================================================
# 为什么需要这一层：原来只有 merge_*（**只会追加**，遇到重复就整条丢掉）。
# 但"目标/计划"这类信息天生是要变化的 ——
#   3 月记下「打算考四级」，6 月用户说「四级过了」，12 月又说「准备考六级」。
# 只会追加的话，档案里会堆出互相矛盾的几句（"打算考四级"和"四级已过"并存），
# 模型读到就不知道该信哪句。所以必须支持**原地改**和**删除**。
def _grams(text: str, n: int = 2) -> set:
    """中文短句的 n 元字符组（去掉空白）。"""
    t = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _overlap(a: str, b: str) -> float:
    """两条要点的相似度（0~1）：互相包含 → 1.0，否则取「字符序列比例」和「2元组覆盖度」的较大值。

    为什么不能只用 SequenceMatcher（踩过）：
    模型改写措辞时字序会变，SequenceMatcher 会明显低估 ——
    实测「考网络工程师」与「已报名网络工程师考试，2027年3月开班，正在看教材」
    的序列比例只有 **0.40**，刚好卡在匹配阈值下方，导致改写失败、变成新增一条。
    换成 2 元组覆盖度后同一对比是 0.80，能正确认出"说的是同一件事"。
    """
    a, b = _norm_point(a), _norm_point(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a, b).ratio()
    ga, gb = _grams(a), _grams(b)
    # 分母取**较短**那一侧：问的是"较短的那句有多少能在另一句里找到"。
    # 取较长一侧会把长条目稀释掉（实测同一条对比 0.17 vs 0.80）。
    cov = len(ga & gb) / max(1, min(len(ga), len(gb)))
    return max(ratio, cov)


def _locate(body: str, hint: str):
    """在记忆正文里找与 hint 最像的那一条，返回 (下标, 条目列表, 相似度)。"""
    items = _split_items(body)
    best_i, best_s = -1, 0.0
    for i, e in enumerate(items):
        sc = _overlap(e, hint)
        if sc > best_s:
            best_i, best_s = i, sc
    return best_i, items, best_s


MATCH_THRESHOLD = 0.5           # 低于它就认为"不是同一条"，宁可不改也不乱改


def update_long(old_hint: str, new_point: str, threshold: float = MATCH_THRESHOLD,
                append_missing: bool = True) -> str:
    """把长期记忆里的某一条**改成新内容**。

    返回：replaced（改成功）/ added（没找到旧条目，改为追加）/ skipped（没变化或内容为空）
    append_missing=False 时，找不到旧条目就返回 "missing"（不追加）——
    调用方想"先在这里改，改不到再换别处"时用得上。
    """
    new_point = _norm_point(new_point)
    if not new_point:
        return "skipped"
    cur = get_long()
    if not cur.strip():
        return ("added" if merge_long(new_point) else "skipped") if append_missing else "missing"
    i, items, score = _locate(cur, old_hint)
    if i < 0 or score < threshold:
        if not append_missing:
            return "missing"
        return "added" if merge_long(new_point) else "skipped"
    if _norm_point(items[i]) == new_point:
        return "skipped"                     # 和原句一样，不用动
    items[i] = new_point
    set_long("；".join(items))
    return "replaced"


def update_short(sid: str, old_hint: str, new_point: str,
                 threshold: float = MATCH_THRESHOLD,
                 append_missing: bool = True) -> str:
    """短期记忆版的 update_long。"""
    sid = _safe_sid(sid)
    new_point = _norm_point(new_point)
    if not sid or not new_point:
        return "skipped"
    cur = get_short(sid)
    if not cur.strip():
        return ("added" if merge_short(sid, new_point) else "skipped") if append_missing else "missing"
    i, items, score = _locate(cur, old_hint)
    if i < 0 or score < threshold:
        if not append_missing:
            return "missing"
        return "added" if merge_short(sid, new_point) else "skipped"
    if _norm_point(items[i]) == new_point:
        return "skipped"
    items[i] = new_point
    set_short(sid, "；".join(items))
    return "replaced"


def drop_long(point: str, threshold: float = 0.5) -> bool:
    """删掉长期记忆里最像 point 的那一条（用户明确说"不做了/弄错了"时用）。"""
    cur = get_long()
    if not cur.strip():
        return False
    i, items, score = _locate(cur, point)
    if i < 0 or score < threshold:
        return False
    items.pop(i)
    set_long("；".join(items))
    return True


def drop_short(sid: str, point: str, threshold: float = 0.5) -> bool:
    sid = _safe_sid(sid)
    if not sid:
        return False
    cur = get_short(sid)
    if not cur.strip():
        return False
    i, items, score = _locate(cur, point)
    if i < 0 or score < threshold:
        return False
    items.pop(i)
    set_short(sid, "；".join(items))
    return True


# =====================================================================
#  注入上下文
# =====================================================================
def build_context(sid: str, query: str = "") -> str:
    """拼装注入 system prompt 的记忆：长期（全局）+ 短期（本对话）。"""
    lng = get_long().strip()
    sht = get_short(sid).strip()
    if not lng and not sht:
        return ""
    parts = ["【记忆】以下是已经知道的用户信息，回答时**直接用**，不要再问一遍。"
             "有新信息用 remember 工具补充（是**追加**，不用重写旧的）："]
    if lng:
        parts.append(f"【长期记忆 · 所有对话通用】\n{lng}")
    if sht:
        parts.append(f"【短期记忆 · 仅本次对话】\n{sht}")
    return "\n\n".join(parts)


# =====================================================================
#  检索（供 search_memory 工具）
# =====================================================================
def search_all(query: str, sid: str = "", top_k: int = 5) -> list:
    q = _tokenize(query or "")
    if not q:
        return []
    out = []
    for label, body in (("长期记忆", get_long()),
                        ("短期记忆", get_short(sid) if sid else "")):
        body = (body or "").strip()
        if not body:
            continue
        inter = set(q) & set(_tokenize(body))
        if inter:
            out.append((len(inter) / max(len(q), 1),
                        {"level": "记忆", "section": label,
                         "content": f"【{label}】\n{body}", "source": "auto"}))
    out.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in out[:top_k]]


def search_memories(query: str, top_k: int = 5) -> list:
    return search_all(query, "", top_k)


# =====================================================================
#  占用 / 释放
# =====================================================================
def usage() -> dict:
    shorts = _load_shorts()
    used = sum(1 for v in shorts.values() if str((v or {}).get("content") or "").strip())
    s_chars = sum(len(str((v or {}).get("content") or "")) for v in shorts.values())
    return {
        "long_bytes": _size(LONG_FILE), "long_chars": len(get_long()),
        "short_bytes": _size(SHORT_FILE), "short_chars": s_chars,
        "short_used": used,
        "long_cap": LONG_CAP, "short_cap": SHORT_CAP,
    }


def release(scope: str = "short", session_id: str = "") -> dict:
    """释放记忆占用。

    scope=short  : 清空所有对话的**短期记忆**（长期记忆保留）
    scope=session: 只清某个对话的短期记忆
    长期记忆**不参与释放** —— 那是最该保住的东西，只能由用户显式清空。
    """
    if scope == "session":
        sid = _safe_sid(session_id)
        n = len(get_short(sid))
        delete_short(sid)
        return {"freed_chars": n, "scope": "session"}
    shorts = _load_shorts()
    freed = sum(len(str((v or {}).get("content") or "")) for v in shorts.values())
    _write_json(SHORT_FILE, {})
    return {"freed_chars": freed, "scope": "short", "cleared": len(shorts)}


# =====================================================================
#  老数据迁移（一次性，只做加法，绝不删用户数据）
# =====================================================================
def migrate_legacy(active_sid: str = "") -> dict:
    """把历史版本的记忆文件搬进新结构。

    历史版本有三代：
      A. memory_doc.json    多分区（工作背景/个人背景/当前关注/近期动态…）
      B. memory/conversations.json  每对话一块
      C. memory/global.json         极简全局偏好
    统一搬到：稳定信息 → 长期记忆；对话相关 → 该对话的短期记忆。
    老文件一律**改名保留**（.migrated），不删 —— 迁移出错还能翻回来。
    """
    moved = {"long": 0, "short": 0}

    # ---- A. 多分区版 ----
    if os.path.exists(LEGACY_DOC_FILE):
        doc = _read_json(LEGACY_DOC_FILE, None)
        secs = doc.get("sections") if isinstance(doc, dict) else doc
        if not isinstance(secs, list):
            secs = []
        for s in secs:
            if not isinstance(s, dict):
                continue
            body = str(s.get("content") or "").strip()
            if not body:
                continue
            title = str(s.get("title") or "").strip()
            for piece in _split_items(body):
                # 身份/偏好/约定/项目 → 长期；"近期动态"这类时效性内容 → 当前对话短期
                target = "short" if any(k in title for k in ("近期", "动态", "当前关注")) else "long"
                if target == "long":
                    if merge_long(piece if not title else f"{title}：{piece}"):
                        moved["long"] += 1
                elif active_sid:
                    if merge_short(active_sid, piece):
                        moved["short"] += 1
        _rename(LEGACY_DOC_FILE)

    # ---- C. 早期"全局偏好" → 长期记忆 ----
    if os.path.exists(LEGACY_GLOBAL_FILE):
        g = _read_json(LEGACY_GLOBAL_FILE, {})
        body = str((g or {}).get("content") or "").strip() if isinstance(g, dict) else ""
        for piece in _split_items(body):
            if merge_long(piece):
                moved["long"] += 1
        _rename(LEGACY_GLOBAL_FILE)

    # ---- B. 早期"每对话一块" → 各自的短期记忆 ----
    if os.path.exists(LEGACY_CONV_FILE):
        convs = _read_json(LEGACY_CONV_FILE, {})
        if isinstance(convs, dict):
            for sid, it in convs.items():
                body = str((it or {}).get("content") or "").strip()
                if not body:
                    continue
                for piece in _split_items(body):
                    if merge_short(sid, piece):
                        moved["short"] += 1
        _rename(LEGACY_CONV_FILE)

    return {"migrated": bool(moved["long"] or moved["short"]), **moved}


def _rename(path: str) -> None:
    try:
        os.replace(path, path + ".migrated")
    except Exception:
        pass


# =====================================================================
#  历史会话归档（transcript）
# =====================================================================
def _transcript_path(session_id: str):
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "session")[:40]
    return os.path.join(TRANSCRIPT_DIR, f"{safe}.jsonl")


def save_transcript(session_id: str, messages: list):
    if not session_id:
        return
    path = _transcript_path(session_id)
    with open(path, "a", encoding="utf-8") as f:
        for msg in messages:
            if msg.get("role") not in ("user", "assistant"):
                continue
            content = msg.get("content") or ""
            if not content or len(str(content)) < 2:
                continue
            f.write(json.dumps({"role": msg["role"], "content": str(content)[:2000],
                                "ts": _now()}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "sep", "ts": _now()}, ensure_ascii=False) + "\n")


def read_transcript(session_id: str, limit: int = 200) -> list:
    path = _transcript_path(session_id)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("role") in ("user", "assistant") and (o.get("content") or "").strip():
                    out.append({"role": o["role"], "content": o["content"]})
    except Exception:
        return []
    return out[-limit:]


def transcript_stats() -> dict:
    files, size = 0, 0
    if os.path.isdir(TRANSCRIPT_DIR):
        for fn in os.listdir(TRANSCRIPT_DIR):
            if fn.endswith(".jsonl"):
                files += 1
                size += _size(os.path.join(TRANSCRIPT_DIR, fn))
    return {"files": files, "bytes": size}


def search_transcripts(query: str, limit: int = 3) -> list:
    q = _tokenize(query or "")
    if not q:
        return []
    hits = []
    if not os.path.isdir(TRANSCRIPT_DIR):
        return hits
    for fn in os.listdir(TRANSCRIPT_DIR):
        if not fn.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(TRANSCRIPT_DIR, fn), "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        buffer = []
        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("role") == "sep":
                buffer = []
                continue
            content = obj.get("content", "")
            if set(q) & set(_tokenize(content)):
                buffer.append(content)
            else:
                buffer = []
            if len(buffer) >= 2:
                hits.append({"level": "历史会话", "content": " ".join(buffer[-4:])[:500],
                             "source": "transcript"})
                buffer = []
                break
        if len(hits) >= limit:
            break
    return hits
