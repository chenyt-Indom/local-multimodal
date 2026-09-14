# -*- coding: utf-8 -*-
"""文段式长期记忆库（类 WorkBuddy：把记忆组织成一份连续更新的文段文档）。

设计目标：不要再一条一条记，而是按“分区文段”整体维护 —— 例如：
    【工作背景】用户是……职业方向……
    【个人背景】简体中文交流……偏好分步骤……
    【当前关注】核心聚焦……
    【近期动态】- xxx; - yyy; …

- 支持的分区（标题）由对话自动创建 / 由 AI 用 remember 工具指定。
- AI 用 remember(section, content) 把整个文段改写或补充成最新一版（由模型自行合并旧文段+新信息）。
- 用户可在前端直接编辑/删除每个文段。
- build_context(query) 每次对话把整份文段精简注入 system prompt，供模型持续补充。
- 历史会话（transcript）保留为底层检索层：遇到“你还记得吗/我们之前”时用 search_memory 检索。
完全本地，数据不出机。
"""
import os
import re
import json
import time
import random
import hashlib
from . import config

DATA_DIR = config.data("data")
MEMORY_DOC_FILE = os.path.join(DATA_DIR, "memory_doc.json")
TRANSCRIPT_DIR = os.path.join(DATA_DIR, "transcript")

# 初始分区骨架（内容为空，出现相关信息时再填充 / 由 AI 创建）
# 分区按"信息类型"划分，而不是笼统一个"当前关注"——
# 混在一起写会让后写的要点覆盖先写的（文段是整体替换的）。
DEFAULT_SECTIONS = ["身份信息", "工作背景", "偏好习惯", "重要约定", "当前项目", "近期动态"]

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fresh_id(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def _next_id():
    return _fresh_id(str(_now()) + str(random.randrange(1 << 30)))


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
#  分区文段文档
# =====================================================================
def _load_doc() -> dict:
    doc = _read_json(MEMORY_DOC_FILE, None)
    if doc is None:
        sections = [{"id": _next_id(), "title": t, "content": "",
                     "created_at": _now(), "updated_at": _now()} for t in DEFAULT_SECTIONS]
        doc = {"sections": sections, "updated_at": _now()}
        _save_doc(doc)
    elif isinstance(doc, list):  # 兼容旧格式
        doc = {"sections": doc, "updated_at": _now()}
    doc.setdefault("sections", [])

    # 轻量迁移：补齐缺失的标准分区（老数据只有"工作背景/个人背景/当前关注/近期动态"）。
    # 只**追加**缺失的分区，绝不改动或删除用户已有的分区与内容。
    titles = {(s.get("title") or "") for s in doc["sections"]}
    missing = [t for t in DEFAULT_SECTIONS if t not in titles]
    if missing:
        for t in missing:
            doc["sections"].append({"id": _next_id(), "title": t, "content": "",
                                    "created_at": _now(), "updated_at": _now()})
        _save_doc(doc)
    return doc


def _save_doc(doc):
    _write_json(MEMORY_DOC_FILE, doc)


def _find_index(sections, xid=None, title=None):
    for i, s in enumerate(sections):
        if xid and s.get("id") == xid:
            return i
        if title and (s.get("title") or "") == title:
            return i
    return -1


def get_sections() -> list:
    return _load_doc().get("sections", [])


def list_all() -> list:
    """兼容旧接口名：返回文段列表。"""
    return get_sections()


def upsert(title: str, content: str) -> dict:
    """按标题新增或更新一个文段（内容整体替换为最新版）。"""
    doc = _load_doc()
    sections = doc["sections"]
    t = (title or "").strip() or "通用"
    now = _now()
    cleared = (content or "").strip()
    i = _find_index(sections, title=t)
    if i >= 0:
        sections[i]["content"] = cleared
        sections[i]["updated_at"] = now
        sec = sections[i]
    else:
        sec = {"id": _next_id(), "title": t, "content": cleared,
               "created_at": now, "updated_at": now}
        sections.append(sec)
    doc["updated_at"] = now
    _save_doc(doc)
    return sec


def add(title: str, content: str) -> dict:
    """兼容接口：新增/更新文段。"""
    return upsert(title, content)


def update(xid: str, title: str = None, content: str = None):
    doc = _load_doc()
    i = _find_index(doc["sections"], xid=xid)
    if i < 0:
        return None
    sec = doc["sections"][i]
    if title:
        sec["title"] = title
    if content is not None:
        sec["content"] = (content or "").strip()
    sec["updated_at"] = _now()
    _save_doc(doc)
    return sec


def remove(xid: str) -> bool:
    doc = _load_doc()
    nf = [s for s in doc["sections"] if s.get("id") != xid]
    if len(nf) == len(doc["sections"]):
        return False
    doc["sections"] = nf
    doc["updated_at"] = _now()
    _save_doc(doc)
    return True


def delete(xid: str) -> bool:
    return remove(xid)


def clear():
    _save_doc({"sections": [], "updated_at": _now()})


# =====================================================================
#  AI 主动写入（remember 工具）
# =====================================================================
def remember(section: str, content: str) -> dict:
    """工具接口：AI 把某个分区的文段整体更新为最新版（模型负责合并旧内容+新信息）。"""
    return upsert(section, content)


# =====================================================================
#  注入上下文 & 检索
# =====================================================================
def build_context(query: str, top_k: int = 5, cap: int = 4000) -> str:
    """拼装要注入 system prompt 的记忆：整份非空文段，精简、不膨胀（cap 字符上限）。

    上限从 3000 提到 4000：分区变细之后（身份/工作/偏好/约定/项目/动态），
    3000 字符很容易在最后一个分区处被截断，导致"记了却没注入给模型"。
    """
    sections = [s for s in get_sections() if (s.get("content") or "").strip()]
    if not sections:
        return ""
    lines = ["【用户长期记忆（文段式）。这是你**已经知道**的用户信息，回答时直接用，"
             "不要再问一遍；有新信息用 remember 工具更新对应文段（合并旧内容），只记稳定的。】"]
    total = 0
    for s in sections:
        block = f"【{s.get('title', '')}】\n{s.get('content', '').strip()}"
        if total + len(block) > cap:
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(lines)


def search_all(query: str, top_k: int = 5) -> list:
    """按关键词检索相关文段（供 search_memory 工具用）。"""
    q = _tokenize(query or "")
    scored = []
    for s in get_sections():
        body = s.get("content", "") or ""
        if not body.strip():
            continue
        toks = _tokenize(body)
        inter = set(q) & set(toks) if q else set(toks)
        score = (len(inter) / max(len(q), 1)) if q else 0.0
        if q and score <= 0:
            continue
        scored.append((score, {"id": s.get("id"), "level": "记忆",
                               "section": s.get("title"),
                               "content": f"【{s.get('title', '')}】\n{body.strip()}",
                               "source": "auto"}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:top_k]]


def search_memories(query: str, top_k: int = 5) -> list:
    # 兼容旧名：语义上即检索文段
    return search_all(query, top_k)


# =====================================================================
#  底层 · 历史会话（transcript）
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
            line = json.dumps({"role": msg["role"], "content": str(content)[:2000],
                               "ts": _now()}, ensure_ascii=False)
            f.write(line + "\n")
        f.write(json.dumps({"role": "sep", "ts": _now()}, ensure_ascii=False) + "\n")


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
        path = os.path.join(TRANSCRIPT_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
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
            toks = _tokenize(content)
            inter = set(q) & set(toks)
            if inter:
                buffer.append(content)
            else:
                buffer = []
            if len(buffer) >= 2:
                seg = " ".join(buffer[-4:])
                hits.append({"level": "历史会话", "content": seg[:500], "source": "transcript"})
                buffer = []
                break
        if len(hits) >= limit:
            break
    return hits