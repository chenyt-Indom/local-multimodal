# -*- coding: utf-8 -*-
"""图片取用：把「模型写的图片来源」变成磁盘上一个真实可用的图片文件。

为什么需要单独一层
------------------
模型给图片只会写它"知道"的东西：一个网址、一个文件名、图片库里看到的名字。
但排版库（python-pptx / python-docx）只认**本地文件**。中间这层转换如果写散在
各处，会出现"有的地方支持网址、有的地方不支持"，用户看到的就是"有时能配图，
有时配不上"。

支持的来源（五选一，自动识别）
------------------------------
1. 本地绝对路径
2. `http(s)://` 网址 —— 下载并**缓存**（网上搜到的图、用户贴的链接都走这条）
3. 图片库 id（12 位十六进制）
4. 图片库里的名称关键词（模糊匹配）
5. 相对文件名 —— 依次在 生成文库 / 当前工作区 / 图片库 / 用户存图 / 下载缓存 里找

两个必须记住的坑
----------------
⚠️ **URL 一定要缓存**：同一张图在一份文稿里可能被引用多次，重生成时更不该重下。
   按 URL 的 sha1 落盘，命中直接复用。
⚠️ **下载到的可能是 webp**，而 python-pptx / python-docx **都不认 webp**
   （会抛 `UnrecognizedImageError`，报错信息还很难懂）。统一转成 png 再交出去。
"""

import hashlib
import os
import re
import time
import urllib.parse

from . import config

# 图片库 id 形如 a1b2c3d4e5f6（12 位十六进制）
_ID_RE = re.compile(r"^[0-9a-f]{12}$", re.I)

# 这些格式排版库自己能吃，不用转
_OK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff")


def cache_dir() -> str:
    """下载缓存的落盘位置。"""
    d = config.data("data", "image_cache")
    os.makedirs(d, exist_ok=True)
    return d


def chat_dir() -> str:
    """用户在本轮对话里附的图，落盘到这里（供"按用户给的图配图"用）。"""
    d = config.data("data", "chat_images")
    os.makedirs(d, exist_ok=True)
    return d


def default_bases() -> list:
    """相对文件名去哪里找（顺序即优先级）。"""
    out = []
    try:
        from . import doclib
        out.append(doclib.LIB_DIR)
    except Exception:
        pass
    try:
        from . import workspace
        out.append(workspace.root())
    except Exception:
        pass
    try:
        from . import image_library
        out.append(image_library.DIR)
    except Exception:
        pass
    for extra in ("saved_images", "data/chat_images", "data/image_cache"):
        try:
            out.append(config.data(*extra.split("/")))
        except Exception:
            pass
    seen, uniq = set(), []
    for b in out:
        if b and b not in seen and os.path.isdir(b):
            seen.add(b)
            uniq.append(b)
    return uniq


# --------------------------------------------------------------------------
# 格式归一
# --------------------------------------------------------------------------
def ensure_insertable(path: str) -> str:
    """把排版库不认的格式（主要是 webp）转成 png。返回可插入的路径。

    转换结果缓存在下载目录里，同名 `.png`，不重复转。
    """
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in _OK_EXT:
        return path
    # 扩展名不可信（很多站点把 webp 命名成 .jpg），按真实格式再判一次
    try:
        from PIL import Image
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            if fmt in ("PNG", "JPEG", "JPG", "GIF", "BMP", "TIFF"):
                return path
            dst = os.path.splitext(path)[0] + ".png"
            if not os.path.exists(dst):
                im.convert("RGBA" if fmt == "WEBP" and im.mode == "RGBA"
                           else "RGB").save(dst, "PNG")
            return dst if os.path.exists(dst) else ""
    except Exception:
        return path if ext in _OK_EXT else ""


def size_of(path: str):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


# --------------------------------------------------------------------------
# 各来源
# --------------------------------------------------------------------------
def fetch_url(url: str, referer=None, max_bytes: int = 10 * 1024 * 1024) -> str:
    """下载一张网图到缓存目录，返回本地路径（失败返回空串）。"""
    url = str(url or "").strip().strip('"')
    if not url.lower().startswith(("http://", "https://")):
        return ""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    d = cache_dir()
    # 已经下过就直接用（不信任扩展名，按目录里实际存在的文件找）
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        p = os.path.join(d, key + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return ensure_insertable(p)
    try:
        from . import web_tools
        raw = web_tools.download_image(url, max_bytes=max_bytes, referer=referer)
    except Exception:
        raw = None
    if not raw:
        return ""
    ext = ".png"
    if raw[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = ".webp"
    elif raw[:3] == b"GIF":
        ext = ".gif"
    p = os.path.join(d, key + ext)
    try:
        with open(p, "wb") as f:
            f.write(raw)
    except Exception:
        return ""
    return ensure_insertable(p)


def from_id(iid: str) -> str:
    """图片库 id → 本地路径。"""
    try:
        from . import image_library
        p = image_library.get_path(iid)
        return ensure_insertable(p) if p else ""
    except Exception:
        return ""


def search_name(kw: str) -> str:
    """在图片库里按名称/来源模糊找一个（模型常用"那张猫的图"这种说法）。"""
    kw = str(kw or "").strip()
    if not kw:
        return ""
    try:
        from . import image_library
        items = image_library.list_images()
    except Exception:
        return ""
    exact = None
    for it in items:
        nm = str(it.get("name") or "")
        if nm == kw:
            exact = it
            break
        if exact is None and (kw in nm or nm in kw):
            exact = it
    if exact is None:
        for it in items:
            src = str(it.get("source") or "")
            if kw and (kw in src or src in kw):
                exact = it
                break
    if exact:
        p = None
        try:
            from . import image_library
            p = image_library.get_path(str(exact.get("id") or ""))
        except Exception:
            p = None
        return ensure_insertable(p) if p else ""
    return ""


def _search_bases(name: str, bases) -> str:
    name = str(name or "").strip().strip('"').replace("\\", os.sep)
    if not name:
        return ""
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    for base in (bases or []):
        cand = os.path.join(base, name.lstrip("/\\"))
        if os.path.isfile(cand):
            return ensure_insertable(cand)
    # 精确路径没命中 → 按文件名（不含扩展名）在各目录里模糊找一层
    if stem:
        for base in (bases or []):
            try:
                for fn in os.listdir(base):
                    if os.path.splitext(fn)[0].lower() == stem:
                        return ensure_insertable(os.path.join(base, fn))
            except Exception:
                continue
    return ""


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def resolve(src, bases=None) -> str:
    """把任意一种图片来源变成本地路径；找不到返回空串。"""
    s = str(src or "").strip().strip('"')
    if not s:
        return ""
    # 1) 网址
    if s.lower().startswith(("http://", "https://")):
        return fetch_url(s)
    # 2) data URL（用户附件直接贴过来的情形）
    if s.lower().startswith("data:image/"):
        return save_data_url(s) or ""
    # 3) 绝对路径
    if os.path.isabs(s) and os.path.isfile(s):
        return ensure_insertable(s)
    # 4) 图片库 id
    if _ID_RE.match(s):
        p = from_id(s)
        if p:
            return p
    # 5) 图片库里按名字找
    p = search_name(s)
    if p:
        return p
    # 6) 在常见目录里按文件名找
    return _search_bases(s, bases if bases is not None else default_bases())


_MEMO_FILE = "_search_memo.json"


def _memo_read() -> dict:
    import json
    try:
        with open(os.path.join(cache_dir(), _MEMO_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _memo_write(d: dict) -> None:
    import json
    try:
        if len(d) > 300:                 # 别无限长
            for k in list(d)[:len(d) - 300]:
                d.pop(k, None)
        with open(os.path.join(cache_dir(), _MEMO_FILE), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def search_one(query: str, want_wide: bool = False, exclude=None) -> str:
    """联网搜一张能用的图，返回本地路径（失败返回空串）。

    供「文档/PPT 自动配图」用。会多试几个候选：
    搜索结果里总有一部分因防盗链或链接失效下不下来。

    ⚠️ 结果按关键词**记忆**（存在缓存目录的小 json 里）—— 同一份文稿反复生成、
    或者同一主题做 Word + PPT 两次，不应该每次都重新联网搜。

    `exclude` 是**已经用过的图片路径集合**。同一份文稿多页配图时，同主题的
    不同关键词常常搜出同一批图（实测 Bing 对「校园植物 绿色封面」与
    「校园植物 观察笔记」返回的前三张**完全一样**）—— 不排除的话整份 PPT
    会反复出现同一张照片。给了 exclude 就顺着候选往后挑。
    """
    q = str(query or "").strip()
    if not q:
        return ""
    excl = {str(x) for x in (exclude or ())}
    memo = _memo_read()
    key = ("W|" if want_wide else "N|") + q
    hit = memo.get(key)
    if hit and os.path.exists(hit) and hit not in excl:
        return hit
    try:
        from . import web_tools
        results = web_tools.image_search(q, n=12)
    except Exception:
        return ""
    tried = 0
    for r in (results or []):
        if tried >= 6:
            break
        tried += 1
        # ⚠️ 优先用结果自带的 referer（各图库的自家 referer），拿不到才退回来源页。
        referer = r.get("referer") or r.get("source") or None
        for url in (r.get("url"), r.get("thumb")):
            if not url:
                continue
            p = fetch_url(url, referer=referer)
            if not p:
                continue
            w, h = size_of(p)
            if w < 320 or h < 200:        # 太小插进页面就是一团马赛克
                continue
            if want_wide and h and w / float(h) < 1.1:
                continue                  # 封面/整页图想要横向的
            if p in excl:
                continue                  # 这份文稿里已经用过这张了
            memo[key] = p
            _memo_write(memo)
            return p
    return ""


# 看着像「路径 / 网址 / 带扩展名 / 图库 id」的，就别再拿去当搜索词 ——
# 否则一个确实找不到的文件会被当作关键词联网搜一次，白等网络。
_NOT_QUERY = re.compile(
    r"[/\\]|^[a-zA-Z]:|\.(png|jpe?g|gif|bmp|webp|tiff?|svg)$", re.I)


def looks_like_query(v) -> bool:
    """判断一个字符串是"图片搜索词"还是"图片标识（路径/网址/扩展名/id）"。

    ⚠️ 为什么要这个：实测模型经常把**搜索词直接写进 `image` / `bg_image`**，
    而不是单独写在 `image_query` 里。只按"路径"处理的话，它会以为插了图、
    实际一片空白，而且**不报错**。
    """
    v = str(v or "").strip()
    if not v or len(v) > 80:
        return False
    if v.lower().startswith(("http://", "https://", "data:")):
        return False
    if _NOT_QUERY.search(v):
        return False
    if _ID_RE.match(v):
        return False
    return True


# 模型"想配图但没给有效来源"时常见的占位词，拿它们去搜等于白搜
_USELESS = {"image", "img", "images", "photo", "picture", "pic", "placeholder",
            "图片", "配图", "图", "示意图", "photo.jpg", "image.png"}


def as_query(v) -> str:
    """把"可能是路径 / 可能是搜索词"的一段文本，尽量变回一个能用的搜索词。

    ⚠️ 实测模型会写 `"image"`、`"images/植物.jpg"` 这类**编造的图片名** ——
    按路径找不到，按原样搜也搜不出东西。这里剥掉目录与扩展名再判断，
    明显是占位词的直接放弃。
    """
    t = str(v or "").strip().strip('"').replace("\\", "/")
    if not t or t.lower().startswith(("http://", "https://", "data:")):
        return ""
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    stem, dot, ext = t.rpartition(".")
    if dot and len(ext) <= 5 and ext.isalnum():
        t = stem
    t = t.strip(" _-")
    if len(t) < 2 or t.lower() in _USELESS:
        return ""
    return t[:40]


def resolve_or_search(src, bases=None, wide=False) -> str:
    """解析图片；解析不到、又看着像搜索词 → **联网搜一张**。

    这是"自动配图"能成立的关键一步：模型只需要说「图书馆 书架 找书」，
    不用自己去找图片链接。
    """
    p = resolve(src, bases)
    if p:
        return p
    if looks_like_query(src):
        return search_one(src, want_wide=wide)
    return ""


def save_data_url(s: str, name: str = "") -> str:
    """把 data:image/... 或纯 base64 存成文件（用户附件用）。"""
    import base64
    s = str(s or "")
    if not s:
        return ""
    body = s.split(",", 1)[1] if s.startswith("data:") and "," in s else s
    try:
        raw = base64.b64decode(body, validate=False)
    except Exception:
        return ""
    if not raw:
        return ""
    ext = ".png"
    if raw[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = ".webp"
    elif raw[:3] == b"GIF":
        ext = ".gif"
    # ⚠️ 按内容哈希命名 ⇒ 同一张图反复附也不会有多份
    key = hashlib.sha1(raw).hexdigest()[:16]
    p = os.path.join(chat_dir(), key + ext)
    if not os.path.exists(p):
        try:
            with open(p, "wb") as f:
                f.write(raw)
        except Exception:
            return ""
    return ensure_insertable(p)


def save_chat_images(images_b64, limit: int = 6) -> list:
    """把本轮对话附件里的图片落盘，返回本地路径列表。

    为什么要落盘：模型能看到图，但**拿不到图的字节** ——
    用户说"把这张图放到 PPT 里"时，只有磁盘上的文件才能被排版库使用。
    落盘的路径会写进提示词，模型就能照着引用。
    """
    out = []
    for s in list(images_b64 or [])[:limit]:
        try:
            p = save_data_url(s)
        except Exception:
            p = ""
        if p:
            out.append(p)
    return out
