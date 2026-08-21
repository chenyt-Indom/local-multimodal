# -*- coding: utf-8 -*-
"""本地文件读取能力。

让 AI 能够读取用户本地文件内容（文本 / 图片 / 常见文档）。
- 文本类：.txt .md .py .json .csv .log .ini .cfg .html .htm .xml .js .ts 等
- 图片类：交给 Qwen-VL 理解（返回 base64）
- 常见文档：.pdf（若装 pypdf 可读文本）
完全本地，用户可以传入任意合法的本机路径。
"""
import os
import re
import base64

TEXT_EXTS = {
    ".txt", ".md", ".py", ".json", ".csv", ".log", ".ini", ".cfg",
    ".html", ".htm", ".xml", ".js", ".ts", ".css", ".sh", ".bat",
    ".yaml", ".yml", ".toml", ".conf", ".sql", ".r", ".go", ".java",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".rb", ".php",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MAX_READ = 200_000  # 单文件最多读取字符，防止超大文件卡死


def read_text(path: str, max_chars: int = MAX_READ):
    encodings = ["utf-8", "gbk", "latin-1"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read(max_chars)
            return text
        except (UnicodeDecodeError, PermissionError):
            continue
    return None


def read_image_b64(path: str, max_side: int = 1024):
    """读取图片并压缩为 base64，供 Qwen-VL 理解。"""
    raw = open(path, "rb").read()
    img = __import__("PIL.Image", fromlist=["Image"]).open(os.fsdecode(path))
    img = img.convert("RGB")
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _read_pdf(path):
    """尝试用 pypdf 读取 PDF 文本；未安装则返回提示。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[提示] 未安装 pypdf，无法解析 PDF。"
    text = []
    try:
        reader = PdfReader(path)
        for page in reader.pages[:20]:
            text.append(page.extract_text() or "")
    except Exception as e:
        return f"[解析PDF出错: {e}]"
    return "\n".join(text)


def read_file(path: str) -> dict:
    """统一入口：按扩展名决定读取方式，返回结构化内容。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if os.path.isdir(path):
        # 目录则列出内容
        try:
            items = os.listdir(path)[:200]
            return {"ok": True, "type": "dir", "content": "\n".join(items), "path": path}
        except PermissionError as e:
            return {"ok": False, "error": f"无法读取目录: {e}"}
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        try:
            b64 = read_image_b64(path)
            return {"ok": True, "type": "image", "b64": b64, "path": path}
        except Exception as e:
            return {"ok": False, "error": f"图片读取失败: {e}"}
    if ext == ".pdf":
        return {"ok": True, "type": "text", "content": _read_pdf(path)[:MAX_READ], "path": path}
    if ext in TEXT_EXTS or True:  # 其他一律按文本尝试
        text = read_text(path)
        if text is None:
            return {"ok": False, "error": f"无法解码文件编码: {path}"}
        return {"ok": True, "type": "text", "content": text, "path": path}
    return {"ok": False, "error": f"不支持的文件类型: {ext}"}


def read_by_keywords(paths_or_dirs: list[str], keywords: str, max_files: int = 50) -> str:
    """在指定路径集里按关键词检索文件内容，返回匹配摘要。"""
    import glob
    results = []
    for target in paths_or_dirs:
        target = os.path.abspath(target)
        if os.path.isdir(target):
            candidates = []
            for ext in TEXT_EXTS:
                candidates += glob.glob(os.path.join(target, "**", "*" + ext), recursive=True)
            candidates = candidates[:max_files]
        elif os.path.isfile(target):
            candidates = [target]
        else:
            continue
        kws = [k.strip().lower() for k in keywords.split() if k.strip()]
        for c in candidates[:max_files]:
            text = read_text(c)
            if not text:
                continue
            low = text.lower()
            if kws and any(k in low for k in kws):
                # 截取命中上下文
                first = min((low.find(k) for k in kws if k in low), default=0)
                seg = text[max(0, first - 100): first + 200]
                results.append(f"--- {c} ---\n{seg}")
    return "\n\n".join(results[:30])