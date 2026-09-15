# -*- coding: utf-8 -*-
"""文档取文：把各种格式的文档抽成纯文本，供知识库索引与"拖进来当资料"使用。

支持的格式与做法
----------------
| 格式 | 做法 |
|---|---|
| .txt .md .csv .json .log .py … | 直接读，按常见编码依次尝试 |
| .pdf | pypdf |
| .docx .xlsx .pptx | 本质是 zip + XML，**用标准库解析**，不引额外依赖 |
| .doc .wps（老二进制格式） | 无法可靠解析 —— 如实告知并给出转换建议，不硬凑 |

⚠️ 为什么 docx 之类不装 python-docx / openpyxl：
这些格式就是"zip 包着 XML"，用 `zipfile` + `re` 抽文字完全够用，
而少一个依赖，容器镜像就少一份体积、少一处离线装不上的风险。
"""
from __future__ import annotations

import os
import re
import zipfile

# 纯文本类扩展名
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
             ".ini", ".cfg", ".yaml", ".yml", ".xml", ".html", ".htm",
             ".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs",
             ".sql", ".sh", ".bat"}
# zip 包着的 Office 系列
ZIP_EXTS = {".docx": "word/document.xml",
            ".xlsx": "shared",          # 特殊处理
            ".pptx": "ppt/slides"}
# 老二进制格式：解析不了，但要说清楚
LEGACY_EXTS = {".doc": "Word 97-2003（.doc）",
               ".wps": "WPS 旧版（.wps）",
               ".xls": "Excel 97-2003（.xls）",
               ".ppt": "PowerPoint 97-2003（.ppt）",
               ".et": "WPS 表格（.et）",
               ".dps": "WPS 演示（.dps）"}

SUPPORTED_EXTS = TEXT_EXTS | {".pdf"} | set(ZIP_EXTS)

_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030", "utf-16", "latin-1")


def is_supported(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in SUPPORTED_EXTS


def extract_text(path: str, limit: int = 400_000) -> tuple:
    """从文件里抽文本，返回 (文本, 说明)。

    说明用来告诉前端/用户"这份文档是怎么读出来的"，
    读不了时也靠它给出明确原因（而不是返回空字符串让人一头雾水）。
    """
    ext = os.path.splitext(path)[1].lower()
    if not os.path.exists(path):
        return "", "文件不存在"

    try:
        if ext in TEXT_EXTS:
            return _read_text(path, limit), ""
        if ext == ".pdf":
            return _read_pdf(path, limit)
        if ext in (".docx", ".xlsx", ".pptx"):
            return _read_ooxml(path, ext, limit)
        if ext in LEGACY_EXTS:
            return "", (f"{LEGACY_EXTS[ext]} 是旧的二进制格式，无法直接读取文字。"
                        f"请用 WPS / Word 打开后「另存为」.docx 或 .pdf 再放进来。")
        # 未知扩展名：先当文本试试，能读出像样的内容就用
        txt = _read_text(path, limit)
        if txt and _looks_like_text(txt):
            return txt, ""
        return "", f"不支持的文件类型（{ext or '无扩展名'}）"
    except Exception as e:
        return "", f"解析失败：{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
#  纯文本
# --------------------------------------------------------------------------
def _read_text(path: str, limit: int) -> str:
    """按常见编码依次尝试。中文文档常见 gbk/gb18030，不能只试 utf-8。"""
    raw = open(path, "rb").read(limit * 2)
    for enc in _ENCODINGS:
        try:
            s = raw.decode(enc)
            if s.count("\ufffd") <= len(s) * 0.01:      # 乱码字符很少才认
                return s[:limit]
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")[:limit]


def _looks_like_text(s: str) -> bool:
    if not s:
        return False
    printable = sum(1 for c in s[:2000] if c.isprintable() or c in "\n\r\t")
    return printable / max(1, len(s[:2000])) > 0.85


# --------------------------------------------------------------------------
#  PDF
# --------------------------------------------------------------------------
def _read_pdf(path: str, limit: int) -> tuple:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return "", "缺少 PDF 解析库（pypdf）"
    try:
        reader = PdfReader(path)
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")          # 有些 PDF 是空密码加密的
            except Exception:
                return "", "这份 PDF 有密码保护，无法读取"
        parts, total = [], 0
        for i, page in enumerate(reader.pages):
            try:
                t = (page.extract_text() or "").strip()
            except Exception:
                t = ""
            if t:
                parts.append(f"[第{i + 1}页]\n{t}")
                total += len(t)
            if total > limit:
                break
        text = "\n\n".join(parts)
        if not text.strip():
            return "", ("PDF 里没抽出文字 —— 多半是**扫描件/图片版**。"
                        "这种情况需要 OCR，可先把图片拖进来让模型看图。")
        return text[:limit], ""
    except Exception as e:
        return "", f"PDF 解析失败：{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
#  OOXML（docx / xlsx / pptx）—— 纯标准库
# --------------------------------------------------------------------------
def _read_ooxml(path: str, ext: str, limit: int) -> tuple:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if ext == ".docx":
                return _docx_text(z, names, limit), ""
            if ext == ".pptx":
                return _pptx_text(z, names, limit), ""
            return _xlsx_text(z, names, limit), ""
    except zipfile.BadZipFile:
        return "", (f"这个 {ext} 文件不是标准格式（可能是旧版二进制或已损坏）。"
                    f"请用 WPS / Office 重新「另存为」{ext} 再试。")
    except Exception as e:
        return "", f"{ext} 解析失败：{type(e).__name__}: {e}"


_TAG = re.compile(r"<[^>]+>")


def _xml_text(xml: str) -> str:
    """把一段 XML 里的可见文字抽出来（段落/换行标签转成换行）。"""
    xml = re.sub(r"(?is)<(w:p|a:p|/w:p|/a:p)[^>]*>", "\n", xml)
    xml = re.sub(r"(?is)<(w:br|a:br)[^>]*/?>", "\n", xml)
    xml = re.sub(r"(?is)<(w:tab|a:tab)[^>]*/?>", "\t", xml)
    return _unescape(_TAG.sub("", xml))


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _docx_text(z, names, limit) -> str:
    if "word/document.xml" not in names:
        return ""
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    text = _clean(_xml_text(xml))
    # 表格文字也在 document.xml 里，会被一并带出
    return text[:limit]


def _pptx_text(z, names, limit) -> str:
    slides = sorted(n for n in names
                    if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
    parts = []
    for i, n in enumerate(slides, 1):
        try:
            t = _clean(_xml_text(z.read(n).decode("utf-8", "ignore")))
        except Exception:
            t = ""
        if t:
            parts.append(f"[第{i}页]\n{t}")
    return "\n\n".join(parts)[:limit]


def _xlsx_text(z, names, limit) -> str:
    """Excel：共享字符串表 + 各工作表里的引用，尽量还原成"表"的样子。"""
    shared = []
    if "xl/sharedStrings.xml" in names:
        try:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"(?is)<si>(.*?)</si>", sx):
                shared.append(_unescape(_TAG.sub("", si)).strip())
        except Exception:
            pass

    def cell_val(ref_xml: str) -> str:
        m = re.search(r'(?is)<v>(.*?)</v>', ref_xml)
        if not m:
            it = re.search(r"(?is)<is>(.*?)</is>", ref_xml)
            return _unescape(_TAG.sub("", it.group(1))).strip() if it else ""
        v = _unescape(m.group(1)).strip()
        if 't="s"' in ref_xml:            # 共享字符串：值是下标
            try:
                return shared[int(v)]
            except Exception:
                return v
        return v

    sheets = sorted(n for n in names
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    parts = []
    for i, n in enumerate(sheets, 1):
        try:
            sx = z.read(n).decode("utf-8", "ignore")
        except Exception:
            continue
        rows = []
        for row in re.findall(r"(?is)<row[^>]*>(.*?)</row>", sx):
            cells = [cell_val(c) for c in
                     re.findall(r"(?is)<c[^>]*>.*?</c>|<c[^>]*/>", row)]
            line = "\t".join(cells).strip()
            if line:
                rows.append(line)
        if rows:
            parts.append(f"[工作表{i}]\n" + "\n".join(rows))
        if sum(len(p) for p in parts) > limit:
            break
    return "\n\n".join(parts)[:limit]
