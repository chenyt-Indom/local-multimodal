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
import logging
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
                f.write("把领域资料放进这个文件夹即可被检索到，不需要重新导入。\n"
                        "支持任意格式（txt / md / pdf / docx / xlsx / pptx…），\n"
                        "也支持子文件夹 —— 按课程、按项目分门别类放，检索时照样找得到。\n"
                        "以 _ 开头的文件或文件夹会被忽略（说明和索引就放在这类名字下）。\n")
        except Exception:
            pass
    return KB_DIR


def _rel(path: str) -> str:
    """取相对知识库根的路径（子文件夹用 / 分隔，跨平台一致）。"""
    return os.path.relpath(path, KB_DIR).replace(os.sep, "/")


def _is_hidden(rel: str) -> bool:
    """任意一段以 _ 开头就跳过（说明文件、索引、用户想忽略的东西）。"""
    return any(p.startswith("_") for p in rel.split("/"))


def _safe_rel(name: str) -> str:
    """把名字收敛成**库内**的安全相对路径（支持子文件夹）。

    和生成文库同一套规则：挡 `..`、挡盘符、非法字符替换成 _。
    盘符要**明确拒绝**而不是悄悄剥掉 —— 否则 `D:/笔记.md` 会变成库里的
    `笔记.md`，用户以为写到了 D 盘（生成文库那边踩过这个坑）。
    """
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("文件名不能为空")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError("不要写盘符（如 D:）—— 只能填知识库目录内的相对路径")
    raw = raw.lstrip("/")
    parts = []
    for p in raw.split("/"):
        p = p.strip()
        if not p or p == ".":
            continue
        if p == "..":
            raise ValueError("路径里不允许出现 ..")
        parts.append(re.sub(r'[<>:"|?*\x00-\x1f]', "_", p))
    if not parts:
        raise ValueError("文件名不合法")
    rel = "/".join(parts)
    full = os.path.abspath(os.path.join(KB_DIR, rel))
    root = os.path.abspath(KB_DIR) + os.sep
    if not full.startswith(root):
        raise ValueError("路径越界（只能写在知识库目录里）")
    return rel


def _iter_docs():
    """递归遍历知识库，产出 (rel, 绝对路径)。

    ⚠️ 遍历只留这一处。原来是"列目录"和"建索引"各写一遍 os.listdir ——
    加子目录支持时最容易只改一处，结果列表里有、检索里没有（或反过来）。
    """
    _ensure_dir()
    for root, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for fn in files:
            if fn.startswith("_") or fn.endswith(".json"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, KB_DIR).replace(os.sep, "/")
            if _is_hidden(rel):
                continue
            yield rel, full


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_num(text: str) -> str:
    """把中文数字换成阿拉伯数字，**只用于排序**（不改变显示）。

    「第一章」→「第1章」。不这么做的话，按拼音排出来是
    二、九、六、七、三、十、四、五、一 —— 用户完全看不懂这个顺序。
    """
    def conv(m):
        t = m.group(0)
        if len(t) == 1:
            if t == "十":
                return "10"                 # 单字「十」是个坑：不特判就漏掉
            return str(_CN_DIGITS.get(t, t))
        if "十" in t:                       # 十一 / 二十 / 二十三
            parts = t.split("十")
            head = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
            tail = _CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return str(head * 10 + tail)
        return "".join(str(_CN_DIGITS.get(c, c)) for c in t)

    return re.sub(r"[零〇一二两三四五六七八九十]+", conv, text)


def _sort_key(rel: str):
    """目录排序键：中文数字转阿拉伯 + 所有数字零填充。

    零填充是必须的：排序是按字符串比的，"第10章" < "第2章"
    （'1' < '2'），必须变成 0010 vs 0002 才是人期待的顺序。
    """
    out = []
    for part in rel.split("/"):
        t = _cn_to_num(part)
        t = re.sub(r"\d+", lambda m: m.group(0).zfill(4), t)
        out.append(t)
    return "/".join(out)


def _doc_id(path):
    """doc_id = 相对路径去掉扩展名（支持子文件夹，如 `课程/讲义1`）。

    用相对路径而不是 basename：子文件夹里出现同名文件时
    （`第一章/讲义.pdf` 和 `第二章/讲义.pdf`），basename 会在索引里
    **互相覆盖**，最后只有一个能被检索到 —— 另一个等于白放。
    """
    return os.path.splitext(_rel(path))[0]


def find_doc_path(doc_id: str) -> str:
    """按 doc_id（不含扩展名）找到实际文件路径。

    ⚠️ 以前检索里写死了 `did + ".txt"`，找不到再试 `".md"` ——
    PDF / Word 文档两个都试不中，取回空文本，于是就"检索不到"。
    """
    want = str(doc_id or "").strip().replace("\\", "/").lstrip("/")
    stem = os.path.splitext(want)[0]          # 允许传带扩展名的名字
    for rel, full in _iter_docs():
        if os.path.splitext(rel)[0] == stem:
            return full
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
    for rel, path in _iter_docs():
        text, note = read_doc(path)
        ext = os.path.splitext(rel)[1].lower().lstrip(".")
        docs.append({
            "id": _doc_id(path),
            "rel": rel,                                   # 相对路径（含子文件夹）
            "folder": os.path.dirname(rel),               # "" = 根目录
            "filename": os.path.basename(rel),
            "ext": ext,
            "size": os.path.getsize(path),
            "chars": len(text),
            "indexed": bool(text.strip()),
            "note": note,
            "updated_at": os.path.getmtime(path),
        })
    # 能检索的排前面；再按"转数字后的路径"排 —— 同层内就是自然顺序
    # （第一章 < 第二章 < 第十章），前端直接照这个顺序分组显示
    docs.sort(key=lambda d: (not d["indexed"], _sort_key(d["rel"])))
    return docs


def save_document(name: str, content: str) -> dict:
    """保存一份**纯文本**知识文档（界面上粘贴文字用的）。"""
    _ensure_dir()
    # 名字为空才用默认名；名字**不合法**（含 ..、盘符）要直接报错 ——
    # 静默回退成 doc_<时间戳>.txt 的话，用户以为存下了、回头根本找不到。
    # 注意：字符串可能本来就是 str，不用考虑 None
    if not str(name or "").strip():
        rel = f"doc_{int(time.time())}.txt"
    else:
        rel = _safe_rel(name)
    if not rel.lower().endswith((".txt", ".md")):
        rel += ".txt"
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path) or KB_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"id": _doc_id(path), "rel": rel, "filename": os.path.basename(rel),
            "size": len(content), "updated_at": os.path.getmtime(path)}


def save_bytes(name: str, data: bytes) -> dict:
    """保存**任意格式**的文档（PDF / Word / Excel …）到知识库。

    必须按二进制原样落盘 —— 之前上传接口是
    `(await file.read()).decode("utf-8", errors="replace")`，
    这会把 PDF 这类二进制文件**解码坏掉**再存进去，存进去就是垃圾。
    原始字节存好，读取时交给 doc_extract 解析。
    """
    _ensure_dir()
    if not str(name or "").strip():
        rel = f"doc_{int(time.time())}.txt"
    else:
        rel = _safe_rel(name)          # 非法名直接抛错，不静默改名
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path) or KB_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    _text_cache.pop(os.path.abspath(path), None)   # 覆盖了旧文件要清缓存
    text, note = read_doc(path)
    return {"id": _doc_id(path), "rel": rel, "filename": os.path.basename(rel),
            "size": len(data),
            "chars": len(text), "indexed": bool(text.strip()), "note": note,
            "updated_at": os.path.getmtime(path)}


def delete_document(name: str) -> bool:
    """删除文档。name 可以是相对路径（含子文件夹），也可以只是文件名。

    只给文件名、而子文件夹里存在**多个同名文件**时**拒绝删除**并返回 False ——
    猜错就删掉别人一份资料，代价太大，宁可让调用方给全路径。
    """
    _ensure_dir()
    try:
        rel = _safe_rel(name)
    except ValueError:
        return False
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    if os.path.isfile(path):
        return _remove_file(path)
    # 兜底：只给了文件名时，在子文件夹里找；有歧义就不动
    base = os.path.basename(rel)
    hits = [full for r, full in _iter_docs() if os.path.basename(r) == base]
    if len(hits) == 1:
        return _remove_file(hits[0])
    if not hits:
        # **一个都没找到 → 文件本来就不在，算成功**。
        # 实测踩过：用户先在资源管理器里把文件删了，界面上还留着旧列表，
        # 再点「删除」→ 原来是 404「文档不存在」，用户看着莫名其妙
        # （"我就是要删它，它没了不正是我要的结果吗"）。
        # 删除是**幂等**的：目标状态"这个文件不存在"已经达到，就该算成功。
        return True
    # 多个同名文件：猜错就删掉别人一份资料，宁可让调用方给全路径
    return False


def _remove_file(path: str) -> bool:
    """删掉一个知识库文件；**文件本来就不在也算成功**。

    ⚠️ 这里必须容错 —— 实测踩过（日志里那条 FileNotFoundError 就是它）：
      ① 用户**先在资源管理器里把文件删了**，再回应用界面点「删除」→
         `os.remove` 抛 FileNotFoundError → 整个 /api/kb/delete 返回 500。
         用户看到"删除失败"，可文件明明已经没了，只会一头雾水。
      ② 本机还有一层"删除走回收站"的拦截，回收站环节失败也会抛别的异常。

    删除应当是**幂等**的：目标状态是"这个文件不存在"，它已经达到了，就该算成功。
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True            # 本来就不在了 = 用户的目的已达成
    except Exception:
        logging.getLogger("uvicorn.error").warning(
            "删除知识库文档失败：%s", path, exc_info=True)
        return False


# ---------- 检索（TF-IDF + 余弦）----------
def _build_index():
    """返回 {doc_id: {term: tf, 'total': n}} 及全局 idf。"""
    _ensure_dir()
    documents = {}
    raw_len = {}
    for rel, path in _iter_docs():
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
        rel = _rel(path)
        results.append({"doc_id": did, "score": round(total, 4),
                        "content": snippet, "filename": os.path.basename(rel),
                        "rel": rel, "folder": os.path.dirname(rel)})
    return results


def tree_text(max_items: int = 200, head_chars: int = 50) -> str:
    """给模型看的知识库目录树（按文件夹分组）。

    为什么要树形：用户是按课程 / 项目建子文件夹来组织资料的，
    平铺一串文件名会让模型**看不出结构**，也就想不到"去某个文件夹里找"。
    带上每篇开头几十个字，模型才好判断"这篇是不是我要的"。
    """
    _ensure_dir()
    docs = list_documents()
    if not docs:
        return "（知识库是空的）"
    groups: dict = {}
    for d in docs:
        groups.setdefault(d.get("folder") or "", []).append(d)
    lines = []
    shown = 0
    # 根目录排最后，子文件夹按名字排 —— 看起来就像资源管理器
    for folder in sorted(groups, key=lambda x: (x == "", x)):
        items = groups[folder]
        lines.append("【%s】%d 篇" % (folder or "根目录", len(items)))
        for d in items:
            if shown >= max_items:
                break
            full = os.path.join(KB_DIR, d["rel"].replace("/", os.sep))
            text, _note = read_doc(full)
            head = (text or "").strip().replace("\n", " ")[:head_chars]
            mark = "" if d.get("indexed") else "（读不出文字：%s）" % (d.get("note") or "格式不支持")
            lines.append("  · %s %s%s" % (d["filename"], mark, head))
            shown += 1
    if len(docs) > shown:
        lines.append("…… 还有 %d 篇" % (len(docs) - shown))
    return "\n".join(lines)


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
        # 显示完整相对路径（含文件夹），模型才能说清"出自哪一篇"，
        # 也能据此知道该去哪翻更多资料
        tag = r.get("rel") or r.get("filename")
        lines.append(f"[资料{i}]({tag})\n{r['content']}")
    lines.append("\n（以上是自动检索到的相关片段，通常够用。"
                 "若需要更多细节、或想先看看知识库里都有哪些资料，"
                 "可调用 search_knowledge 工具（list_all=true 列目录）；"
                 "需要时效性信息时也可同时调用 web_search。）")
    return "\n".join(lines)