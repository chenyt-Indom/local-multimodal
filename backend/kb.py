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

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "kb")
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


def _doc_id(path):
    return os.path.basename(path).split(".")[0]


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# ---------- 文档管理 ----------
def list_documents():
    _ensure_dir()
    docs = []
    for fn in os.listdir(KB_DIR):
        if fn.startswith("_") or fn.endswith(".json"):
            continue
        path = os.path.join(KB_DIR, fn)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            docs.append({
                "id": _doc_id(path),
                "filename": fn,
                "size": len(content),
                "chars": len(content),
                "updated_at": os.path.getmtime(path),
            })
    return docs


def save_document(name: str, content: str) -> dict:
    """保存一份知识文档。name 会做安全化处理（仅保留文件名，防路径穿越）。"""
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
        raw = _read(os.path.join(KB_DIR, did + ".txt"))
        if not os.path.exists(os.path.join(KB_DIR, did + ".txt")):
            raw = _read(os.path.join(KB_DIR, did + ".md"))
        raw = raw or ""
        substr_hits = sum(1 for t in q_tokens if t in raw)
        bonus = substr_hits / max(len(q_tokens), 1) * 1.5
        total = s + bonus
        if total > 0:
            scored.append((total, did, s, substr_hits))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for total, did, s, hits in scored[:top_k]:
        path = os.path.join(KB_DIR, did + ".txt")
        if not os.path.exists(path):
            path = os.path.join(KB_DIR, did + ".md")
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
    """把检索结果拼成可注入 system 的 RAG 上下文。"""
    results = search(query, top_k)
    if not results:
        return ""
    lines = ["【知识库资料】"]
    for i, r in enumerate(results, 1):
        lines.append(f"[资料{i}]({r['filename']})\n{r['content']}")
    return "\n".join(lines)