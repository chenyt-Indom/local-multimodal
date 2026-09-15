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
# OLE 复合文档（旧版二进制）：Excel/WPS 表格靠 BIFF 记录解析
OLE_EXTS = {".xls": "Excel 97-2003", ".et": "WPS 表格"}
# 老二进制格式：解析不了，但要说清楚
LEGACY_EXTS = {".doc": "Word 97-2003（.doc）",
               ".wps": "WPS 旧版文档（.wps）",
               ".ppt": "PowerPoint 97-2003（.ppt）",
               ".dps": "WPS 演示（.dps）"}

SUPPORTED_EXTS = TEXT_EXTS | {".pdf"} | set(ZIP_EXTS) | set(OLE_EXTS)

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
        if ext in OLE_EXTS:
            return _read_ole_sheet(path, limit)
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
    """Excel：共享字符串表 + 各工作表单元格，按**列位置**还原成表格。

    ⚠️ 必须按单元格引用（如 C2）定位列，不能只把非空值 join 起来 ——
    那样遇到空单元格整行就会**错位**，表格读出来是歪的。
    """
    shared = []
    if "xl/sharedStrings.xml" in names:
        try:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"(?is)<si>(.*?)</si>", sx):
                # <si> 里可能有多个 <t>（富文本被拆片），要拼起来
                parts = re.findall(r"(?is)<t[^>]*>(.*?)</t>", si)
                shared.append(_unescape("".join(parts)).strip())
        except Exception:
            pass

    # 工作表真实名称（xl/workbook.xml 里）
    sheet_names = []
    if "xl/workbook.xml" in names:
        try:
            wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
            sheet_names = [_unescape(m) for m in
                           re.findall(r"(?is)<sheet[^>]*name=\"([^\"]*)\"", wb)]
        except Exception:
            pass

    def a1_col(ref: str) -> int:
        """把 'C12' 里的列字母转成 0 基列号。"""
        letters = re.match(r"([A-Za-z]+)", ref or "")
        if not letters:
            return -1
        n = 0
        for ch in letters.group(1).upper():
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    def cell_val(xml: str) -> str:
        t = re.search(r'(?is)\bt="([^"]*)"', xml)
        t = (t.group(1) if t else "")
        if t == "inlineStr":
            it = re.search(r"(?is)<is>(.*?)</is>", xml)
            return _unescape(_TAG.sub("", it.group(1))).strip() if it else ""
        m = re.search(r"(?is)<v>(.*?)</v>", xml)
        if not m:
            return ""
        v = _unescape(m.group(1)).strip()
        if t == "s":                    # 共享字符串：值是下标
            try:
                return shared[int(v)]
            except Exception:
                return v
        if t == "b":
            return "是" if v == "1" else "否"
        if t == "e":
            return f"[错误{v}]"
        return v

    # 注意：生成器表达式和 key= 一起用时必须加括号，否则语法错误
    sheets = sorted([n for n in names
                     if re.match(r"xl/worksheets/sheet\d+\.xml$", n)],
                    key=lambda s: int(re.findall(r"\d+", s)[-1]))
    parts = []
    for idx, n in enumerate(sheets):
        try:
            sx = z.read(n).decode("utf-8", "ignore")
        except Exception:
            continue
        title = sheet_names[idx] if idx < len(sheet_names) else f"工作表{idx + 1}"
        lines = []
        for row in re.findall(r"(?is)<row[^>]*>(.*?)</row>", sx):
            cells = re.findall(r"(?is)<c\b[^>]*/>|<c\b[^>]*>.*?</c>", row)
            if not cells:
                continue
            slots = {}                  # 列号 → 值（按引用定位，避免空单元格错位）
            width = 0
            for c in cells:
                ref = re.search(r'\br="([A-Za-z]+\d+)"', c)
                ci = a1_col(ref.group(1)) if ref else width
                if ci < 0:
                    ci = width
                val = cell_val(c)
                if val != "":
                    slots[ci] = val
                width = max(width, ci + 1)
            if not slots:
                continue
            line = "\t".join(slots.get(i, "") for i in range(width)).rstrip()
            if line.strip():
                lines.append(line)
        if lines:
            parts.append(f"[{title}]\n" + "\n".join(lines))
        if sum(len(p) for p in parts) > limit:
            break
    return "\n\n".join(parts)[:limit]


# --------------------------------------------------------------------------
#  旧版二进制表格（.xls / .et）—— OLE 复合文档 + BIFF 记录
# --------------------------------------------------------------------------
#  为什么要自己写：.xls 是老格式，现成的解析库（xlrd/pandas）都没装，
#  而它在国内还大量存在（很多单位/学校的表格就是 .xls）。
#  做法：先从 OLE 容器里取出 Workbook 流，再按 BIFF 记录扫出单元格文字与数字。
#  不需要理解全部格式 —— 够"把表格内容读出来给模型看"即可。
# --------------------------------------------------------------------------
_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _read_ole_sheet(path: str, limit: int) -> tuple:
    raw = open(path, "rb").read()
    if not raw.startswith(_OLE_SIG):
        return "", "这不是标准的 .xls 文件（可能是改了扩展名的其它格式）"
    try:
        stream = _ole_extract_stream(raw, ("Workbook", "Book", "workbook"))
    except Exception as e:
        return "", f"读取表格容器失败：{type(e).__name__}: {e}"
    if not stream:
        return "", "表格里没有找到 Workbook 数据流（文件可能已损坏）"
    try:
        text = _biff_cells_to_text(stream, limit)
    except Exception as e:
        return "", f"解析表格记录失败：{type(e).__name__}: {e}"
    if not text.strip():
        return "", "表格里没抽出内容（可能是空表或加密文件）"
    return text, ""


def _ole_extract_stream(raw: bytes, want_names) -> bytes:
    """从 OLE2 复合文档里取出指定名称的数据流（只实现读取所需的最小集）。"""
    import struct
    if len(raw) < 512:
        return b""
    sect_size = 1 << struct.unpack_from("<H", raw, 0x1E)[0]
    mini_size = 1 << struct.unpack_from("<H", raw, 0x20)[0]
    num_fat = struct.unpack_from("<I", raw, 0x2C)[0]
    dir_start = struct.unpack_from("<I", raw, 0x30)[0]
    mini_cutoff = struct.unpack_from("<I", raw, 0x38)[0]
    mini_fat_start = struct.unpack_from("<I", raw, 0x3C)[0]
    difat_start = struct.unpack_from("<I", raw, 0x44)[0]
    num_difat = struct.unpack_from("<I", raw, 0x48)[0]

    def read_sector(sid):
        off = (sid + 1) * sect_size
        return raw[off:off + sect_size]

    # ---- 收集 FAT 扇区号（含 DIFAT 链）----
    fat_sids = list(struct.unpack_from("<109I", raw, 0x4C))
    nxt, guard = difat_start, 0
    while nxt not in (0xFFFFFFFF, 0xFFFFFFFE) and guard < num_difat + 8:
        sec = read_sector(nxt)
        vals = list(struct.unpack_from("<%dI" % (sect_size // 4), sec, 0))
        fat_sids.extend(vals[:-1])
        nxt = vals[-1]
        guard += 1
    fat_sids = [s for s in fat_sids[:num_fat] if s < 0xFFFFFFF0]

    fat = []
    for sid in fat_sids:
        sec = read_sector(sid)
        fat.extend(struct.unpack_from("<%dI" % (sect_size // 4), sec, 0))

    def chain(start):
        out, cur, guard = [], start, 0
        while cur < 0xFFFFFFF0 and guard < 100000:
            out.append(cur)
            if cur >= len(fat):
                break
            cur = fat[cur]
            guard += 1
        return out

    def read_chain(start):
        return b"".join(read_sector(s) for s in chain(start))

    # ---- 目录项 ----
    dir_raw = read_chain(dir_start)
    entries = []
    for i in range(0, len(dir_raw), 128):
        e = dir_raw[i:i + 128]
        if len(e) < 128:
            break
        name_len = struct.unpack_from("<H", e, 0x40)[0]
        if name_len < 2:
            continue
        name = e[:name_len - 2].decode("utf-16-le", "ignore")
        entries.append((name, e[0x42],
                        struct.unpack_from("<I", e, 0x74)[0],
                        struct.unpack_from("<Q", e, 0x78)[0]))

    # ---- 小流容器（小于 cutoff 的流存在 mini stream 里）----
    root = next((x for x in entries if x[1] == 5), None)
    mini_stream = read_chain(root[2]) if root and root[3] else b""
    mini_fat = []
    if mini_fat_start < 0xFFFFFFF0:
        mf = read_chain(mini_fat_start)
        mini_fat = list(struct.unpack_from("<%dI" % (len(mf) // 4), mf, 0))

    def read_mini(start, size):
        out, cur, guard = b"", start, 0
        while cur < 0xFFFFFFF0 and len(out) < size and guard < 100000:
            off = cur * mini_size
            out += mini_stream[off:off + mini_size]
            cur = mini_fat[cur] if cur < len(mini_fat) else 0xFFFFFFFE
            guard += 1
        return out[:size]

    for name, etype, start, size in entries:
        if etype != 2 or name not in want_names:      # 只要数据流
            continue
        if size < mini_cutoff:
            return read_mini(start, size)
        return read_chain(start)[:size]
    return b""


def _biff_cells_to_text(stream: bytes, limit: int) -> str:
    """扫 BIFF 记录，把单元格还原成按行分列的文字。"""
    import struct
    # ---- 先取共享字符串表（SST + 后续 CONTINUE）----
    shared, i, n = [], 0, len(stream)
    while i + 4 <= n:
        rid, sz = struct.unpack_from("<HH", stream, i)
        i += 4
        body = stream[i:i + sz]
        i += sz
        if rid == 0x00FC and sz >= 8:          # SST
            total = struct.unpack_from("<I", body, 0)[0]
            data = body[8:]
            # 字符串会被切在记录边界，必须把后续 CONTINUE 接上
            while i + 4 <= n:
                nid, nsz = struct.unpack_from("<HH", stream, i)
                if nid != 0x003C:              # CONTINUE
                    break
                data += stream[i + 4:i + 4 + nsz]
                i += 4 + nsz
            shared = _parse_sst(data, total)
            break

    # ---- 再扫单元格记录，按 (行, 列) 落位 ----
    rows, max_col, i = {}, 0, 0
    while i + 4 <= n:
        rid, sz = struct.unpack_from("<HH", stream, i)
        i += 4
        body = stream[i:i + sz]
        i += sz
        try:
            if rid == 0x00FD and sz >= 10:     # LABELSST：文本走共享字符串表
                r, c, _x, idx = struct.unpack_from("<HHHI", body, 0)
                v = shared[idx] if idx < len(shared) else ""
                if v:
                    rows.setdefault(r, {})[c] = v
                    max_col = max(max_col, c)
            elif rid == 0x0204 and sz >= 8:    # LABEL：直接内嵌字符串
                r, c, _x, ln = struct.unpack_from("<HHHH", body, 0)
                v = body[8:8 + ln].decode("gbk", "ignore").strip("\x00").strip()
                if v:
                    rows.setdefault(r, {})[c] = v
                    max_col = max(max_col, c)
            elif rid == 0x0203 and sz >= 14:   # NUMBER
                r, c, _x = struct.unpack_from("<HHH", body, 0)
                rows.setdefault(r, {})[c] = _num(struct.unpack_from("<d", body, 6)[0])
                max_col = max(max_col, c)
            elif rid == 0x027E and sz >= 10:   # RK：压缩数字
                r, c, _x, rk = struct.unpack_from("<HHHI", body, 0)
                rows.setdefault(r, {})[c] = _num(_rk(rk))
                max_col = max(max_col, c)
            elif rid == 0x00BD and sz >= 6:    # MULRK：一行多个压缩数字
                r, c0 = struct.unpack_from("<HH", body, 0)
                for k in range((sz - 6) // 6):
                    rk = struct.unpack_from("<I", body, 4 + k * 6 + 2)[0]
                    rows.setdefault(r, {})[c0 + k] = _num(_rk(rk))
                    max_col = max(max_col, c0 + k)
        except Exception:
            continue

    if not rows:
        # 结构解析不出来就退一步：把共享字符串全倒出来，
        # 至少保证"内容能被检索到"，总比什么都没有强
        return "；".join(s for s in shared if s)[:limit]

    lines = []
    for r in sorted(rows):
        d = rows[r]
        line = "\t".join(str(d.get(c, "")) for c in range(max_col + 1)).rstrip()
        if line.strip():
            lines.append(line)
        if sum(len(x) for x in lines) > limit:
            break
    return "\n".join(lines)[:limit]


def _num(v) -> str:
    """数字格式化：整数不要显示成 95.0。"""
    try:
        f = float(v)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return f"{f:g}"
    except Exception:
        return str(v)


def _rk(rk: int) -> float:
    """解码 BIFF 的 RK 压缩数字。

    低 2 位是标志：bit1=整数，bit0=是否要除以 100。
    整数时高 30 位就是数值；浮点时高 30 位是 IEEE754 双精度的前 30 位。
    """
    import struct
    if rk & 0x02:
        val = float(rk >> 2)
        if rk & 0x80000000:          # 负数（高 30 位是补码）
            val -= float(1 << 30)
        return val / 100.0 if rk & 0x01 else val
    bits = (rk & 0xFFFFFFFC) << 32
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


def _parse_sst(data: bytes, total: int) -> list:
    """解析 BIFF8 共享字符串表。

    每条：2 字节字符数 + 1 字节标志（bit0=UTF-16、bit2=有富文本、bit3=有扩展段）。
    """
    import struct
    out, i, n = [], 0, len(data)
    cap = max(total, 1) * 2 + 64
    while i + 3 <= n and len(out) < cap:
        try:
            ln = struct.unpack_from("<H", data, i)[0]
            flags = data[i + 2]
            i += 3
            rcount = extsz = 0
            if flags & 0x08:                      # 富文本：跟一个格式段数量
                rcount = struct.unpack_from("<H", data, i)[0]
                i += 2
            if flags & 0x04:                      # 扩展段
                extsz = struct.unpack_from("<I", data, i)[0]
                i += 4
            if flags & 0x01:                      # UTF-16
                s = data[i:i + ln * 2].decode("utf-16-le", "ignore")
                i += ln * 2
            else:                                 # 压缩（每字符 1 字节）
                s = data[i:i + ln].decode("latin-1", "ignore")
                i += ln
            i += rcount * 4 + extsz
            out.append(s)
        except Exception:
            break
    return out
