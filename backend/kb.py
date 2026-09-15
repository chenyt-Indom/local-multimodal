# -*- coding: utf-8 -*-
"""本地知识库 + RAG 轻量检索。

- 用户可把领域知识文档保存到本地知识库（data/kb/*.txt 或 .md）
- 采用轻量 TF-IDF（词频 + 逆文档频率）向量化 + 余弦检索，
  无需额外下载大模型，完全本地、离线、占用小。
- 前端通过 RAG 开关控制是否启用检索增强。
"""
import os
import re
import json
import math
import time
from . import config

KB_DIR = config.data("data", "kb")
INDEX_FILE = os.path.join(KB_DIR, "_index.json")

STOP_WORDS = set("的 了 在 是 和 与 我 你 他 她 它 们 一 有 也 都 要 把 让 这 那 就 吗 呢 啊 吧 很 会 可以 请 并 及 或 上 下 中 的 关于 什么是 如何 怎么".split())


def _tokenize(text):
    """轻量分词：英文/数字按字词，中文按 2-gram 切分。
    中文 2-gram 可让检索匹配短语而无需额外分词库，完全本地零依赖。"""
    tokens = []
    for ch in re.findall(r"[\u4e00-\u9fa5_a-zA-Z0-9]+", text.lower()):
        # 纯中文段：2-gram
        if re.fullmatch(r"[\u4e00-\u9fa5]+", ch):
            for i in range(len(ch) - 1):
                tokens.append(ch[i:i + 2])
            # 保留单字符边界，避免 2 字词内部信息丢失
            if len(ch) == 1:
                tokens.append(ch)
        else:
            tokens.append(ch)
    return [w for w in tokens if w not in STOP_WORDS and w.strip()]


def _ensure_dir():
    os.makedirs(KB_DIR, exist_ok=True)


def ensure_dir() -> str:
    """确保知识库目录存在并放一份说明，返回路径（启动时调用）。

    用户直接把 .txt / .md 拷进这个目录就行，**不需要任何导入操作** ——
    检索时 `_build_index` 会按目录实际内容重建索引，放进去就能被查到。
    """
    _ensure_dir()
    readme = os.path.join(KB_DIR, "_说明.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write("把领域资料（.txt / .md）放进这个文件夹即可被检索到，"
                        "不需要重新导入。\n以 _ 开头的文件会被忽略。\n")
        except Exception:
            pass
    return KB_DIR


def _doc_id(path):
    return os.path.basename(path).split(".")[0]


def find_doc_path(doc_id: str) -> str:
    """按 doc_id（不含扩展名）找到实际文件路径。

    ⚠️ 以前检索里写死了 `did + ".txt"`，找不到再试 `".md"` ——
    PDF / Word 文档两个都试不中，取回空文本，于是就"检索不到"。
    """
    _ensure_dir()
    for fn in os.listdir(KB_DIR):
        if fn.startswith("_") or fn.endswith(".json"):
            continue
        if os.path.splitext(fn)[0] == doc_id:
            return os.path.join(KB_DIR, fn)
    return ""


# 文档内容缓存：{文件名: (mtime, 文本)}
# 解析 PDF/docx 比读 txt 贵得多，而检索每次都要遍历全部文档 ——
# 没有缓存的话每问一句都要把所有 PDF 重解析一遍，慢到不可用。
_text_cache: dict = {}


def read_doc(path: str) -> tuple:
    """读取文档正文，返回 (文本, 说明)。

    ⚠️ 以前这里是 `open(path, encoding="utf-8").read()` ——
    遇到 PDF / Word 会直接抛 UnicodeDecodeError，
    连累 list_documents 整个崩掉，用户放进去的文档"检索不到"。
    现在统一走 doc_extract，支持 txt/md/pdf/docx/xlsx/pptx。
    """
    from . import doc_extract
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0
    key = os.path.abspath(path)
    hit = _text_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    text, note = doc_extract.extract_text(path)
    _text_cache[key] = (mtime, text, note)
    return text, note


def _read(path):
    """兼容旧调用：只要文本。"""
    return read_doc(path)[0]


# ---------- 文档管理 ----------
def list_documents():
    """列出知识库文档。

    读不出文字的（例如旧版 .doc/.wps）也会列出来，但带上 note 说明原因 ——
    让用户知道"文件在，是格式读不了"，而不是"我明明放进去了怎么没有"。
    """
    _ensure_dir()
    docs = []
    for fn in os.listdir(KB_DIR):
        if fn.startswith("_") or fn.endswith(".json"):
            continue
        path = os.path.join(KB_DIR, fn)
        if not os.path.isfile(path):
            continue
        text, note = read_doc(path)
        ext = os.path.splitext(fn)[1].lower().lstrip(".")
        docs.append({
            "id": _doc_id(path),
            "filename": fn,
            "ext": ext,
            "size": os.path.getsize(path),
            "chars": len(text),
            "indexed": bool(text.strip()),
            "note": note,
            "updated_at": os.path.getmtime(path),
        })
    # 能检索的排前面
    docs.sort(key=lambda d: (not d["indexed"], d["filename"]))
    return docs


def save_document(name: str, content: str) -> dict:
    """保存一份**纯文本**知识文档（界面上粘贴文字用的）。"""
    _ensure_dir()
    safe = os.path.basename(name).replace("..", "").strip()
    if not safe:
        safe = f"doc_{int(time.time())}.txt"
    if not safe.endswith((".txt", ".md")):
        safe += ".txt"
    path = os.path.join(KB_DIR, safe)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"id": _doc_id(path), "filename": safe, "size": len(content),
            "updated_at": os.path.getmtime(path)}


def save_bytes(name: str, data: bytes) -> dict:
    """保存**任意格式**的文档（PDF / Word / Excel …）到知识库。

    必须按二进制原样落盘 —— 之前上传接口是
    `(await file.read()).decode("utf-8", errors="replace")`，
    这会把 PDF 这类二进制文件**解码坏掉**再存进去，存进去就是垃圾。
    原始字节存好，读取时交给 doc_extract 解析。
    """
    _ensure_dir()
    safe = os.path.basename(name or "").replace("..", "").strip()
    if not safe:
        safe = f"doc_{int(time.time())}.txt"
    path = os.path.join(KB_DIR, safe)
    with open(path, "wb") as f:
        f.write(data)
    _text_cache.pop(os.path.abspath(path), None)   # 覆盖了旧文件要清缓存
    text, note = read_doc(path)
    return {"id": _doc_id(path), "filename": safe, "size": len(data),
            "chars": len(text), "indexed": bool(text.strip()), "note": note,
            "updated_at": os.path.getmtime(path)}


def delete_document(name: str) -> bool:
    safe = os.path.basename(name)
    path = os.path.join(KB_DIR, safe)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


# ---------- 检索（TF-IDF + 余弦）----------
def _build_index():
    """返回 {doc_id: {term: tf, 'total': n}} 及全局 idf。"""
    _ensure_dir()
    documents = {}
    raw_len = {}
    for fn in os.listdir(KB_DIR):
        if fn.startswith("_") or fn.endswith(".json"):
            continue
        path = os.path.join(KB_DIR, fn)
        tokens = _tokenize(_read(path))
        if not tokens:
            continue
        did = _doc_id(path)
        documents[did] = {}
        for t in tokens:
            documents[did][t] = documents[did].get(t, 0) + 1
        raw_len[did] = len(tokens)
    # idf
    df = {}
    for did in documents:
        for t in set(documents[did]):
            df[t] = df.get(t, 0) + 1
    n = max(len(documents), 1)
    idf = {t: math.log((n + 1) / (freq + 1)) + 1 for t, freq in df.items()}
    return documents, idf, raw_len


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[t] * b.get(t, 0) for t in a)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search(query: str, top_k: int = 6):
    """检索知识库，返回相关文档片段。TF-IDF 计算 + 中文子串匹配兜底。"""
    documents, idf, raw_len = _build_index()
    if not documents:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    q_tfidf = {}
    for t in set(q_tokens):
        q_tfidf[t] = q_tfidf.get(t, 0) + idf.get(t, 1.0)
    scored = []
    for did, tf in documents.items():
        doc_tfidf = {t: (freq / max(raw_len[did], 1)) * idf.get(t, 1.0) for t, freq in tf.items()}
        s = _cosine(q_tfidf, doc_tfidf)
        # 中文子串匹配兜底：查询关键词若出现在正文任何位置都给予基础分，
        # 弥补 TF-IDF 切词对中文短语召回不足的问题
        raw = _read(find_doc_path(did))
        raw = raw or ""
        substr_hits = sum(1 for t in q_tokens if t in raw)
        bonus = substr_hits / max(len(q_tokens), 1) * 1.5
        total = s + bonus
        if total > 0:
            scored.append((total, did, s, substr_hits))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for total, did, s, hits in scored[:top_k]:
        path = find_doc_path(did)
        text = _read(path)
        # 抽取命中片段
        snippet = text[:800]
        if hits > 0 and text:
            positions = []
            for t in q_tokens:
                idx = text.find(t)
                if idx >= 0:
                    positions.append(idx)
            if positions:
                anchor = min(positions)
                snippet = text[max(0, anchor - 60): anchor + 400]
        results.append({"doc_id": did, "score": round(total, 4),
                        "content": snippet, "filename": did})
    return results


def build_rag_context(query: str, top_k: int = 4) -> str:
    """把检索结果拼成可注入 system 的 RAG 上下文。

    只在**真的命中**时才有内容（无关提问的分数是 0，`search` 会直接返回空），
    所以这相当于"自动检索"：相关就自动带上，不相关一点都不占上下文。

    同时在末尾告诉模型还能用 search_knowledge 工具继续深挖 ——
    自动检索只给最相关的几段，需要更多细节或想先看目录时由模型自己决定。
    """
    results = search(query, top_k)
    if not results:
        return ""
    lines = ["【知识库资料】（来自用户导入的领域文档，**优先于联网结果采信**；"
             "回答时注明出自哪一篇）"]
    for i, r in enumerate(results, 1):
        lines.append(f"[资料{i}]({r['filename']})\n{r['content']}")
    lines.append("\n（以上是自动检索到的相关片段，通常够用。"
                 "若需要更多细节、或想先看看知识库里都有哪些资料，"
                 "可调用 search_knowledge 工具（list_all=true 列目录）；"
                 "需要时效性信息时也可同时调用 web_search。）")
    return "\n".join(lines)