# -*- coding: utf-8 -*-
"""生成文库：模型自己写、自己改的文件的存放地（**和知识库严格分开**）。

为什么要物理隔离（而不是靠"这个文件是谁写的"来判权限）
----------------------------------------------------
- **知识库**（`data/kb`）是**用户自己的资料**，语义是"只读参考"：
  模型只能检索，**绝不能改**。用户把一份重要合同放进去，
  被模型"顺手改一下"是不可接受的。
- **生成文库**（`data/library`）是**模型的产出物**：作文、方案、脚本、表格……
  语义是"可读可写可删" —— 用户明确要求模型能主动写入 / 修改 / 删除这里的文件。

两种用途、两套权限，混在一起迟早出错。**物理分开最省心。**

安全设计
--------
1. `_safe_rel()` 收敛所有路径：挡 `../`、挡绝对路径与盘符，
   最后**再校验一次落点确实在库内** —— 模型一次"手滑"就能写到库外面去，必须挡住。
2. 覆盖写之前**自动备份**到 `_备份/`；删除是**移进 `_回收站/`** 而不是真删。
   模型是自动执行的，必须给用户留后悔的余地。
"""
import os
import re
import json
import time
import shutil

from . import config
from . import doc_extract

LIB_DIR = config.data("data", "library")   # 注意：和图片库 image_library.DIR 是两回事
TRASH_DIR = os.path.join(LIB_DIR, "_回收站")
BACKUP_DIR = os.path.join(LIB_DIR, "_备份")

TEXT_EXTS = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".csv",
             ".html", ".css", ".sql", ".sh", ".bat", ".ps1", ".yml", ".yaml",
             ".ini", ".cfg", ".log", ".java", ".c", ".cpp", ".go", ".rs", ".r"}


def ensure_dir() -> str:
    os.makedirs(LIB_DIR, exist_ok=True)
    return LIB_DIR


def _safe_rel(name: str) -> str:
    """把模型/用户给的文件名收敛成**库内**的安全相对路径。"""
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("文件名不能为空")
    # ⚠️ 带盘符要**明确拒绝**，不能悄悄剥掉。
    # 原来是把 `D:/笔记.md` 的 "D:" 静默删掉、存成库里的 `笔记.md` ——
    # 用户会以为文件写到了 D 盘，实际落在库里且改了名，这种"静默改写输入"
    # 比直接报错危险得多。
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError("不要写盘符（如 D:）—— 只能填生成文库内的相对路径")
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
    full = os.path.abspath(os.path.join(LIB_DIR, rel))
    root = os.path.abspath(LIB_DIR) + os.sep
    if not full.startswith(root):
        raise ValueError("路径越界（只能写在生成文库目录里）")
    return rel


def _abs(rel: str) -> str:
    return os.path.join(LIB_DIR, rel.replace("/", os.sep))


def _is_hidden(rel: str) -> bool:
    return any(p.startswith("_") for p in rel.split("/"))


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# =====================================================================
#  列举 / 读取
# =====================================================================
def list_files() -> list:
    """列出文库里的文件（跳过 `_回收站` / `_备份` 这类下划线目录）。"""
    ensure_dir()
    out = []
    for root, dirs, files in os.walk(LIB_DIR):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, LIB_DIR).replace(os.sep, "/")
            if _is_hidden(rel):
                continue
            try:
                st = os.stat(full)
            except Exception:
                continue
            ext = os.path.splitext(fn)[1].lower()
            out.append({"name": fn, "rel": rel, "size": st.st_size,
                        "ext": ext, "mtime": st.st_mtime,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                        "folder": os.path.dirname(rel) or ""})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def read_file(rel: str, limit: int = 200_000) -> dict:
    """读文库里的一个文件（PDF/Word/Excel 也能读，走 doc_extract）。"""
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = _abs(rel)
    if not os.path.exists(full):
        return {"ok": False, "error": "文件不存在：%s" % rel, "missing": True}
    text, note = doc_extract.extract_text(full, limit=limit)
    if not text and note:
        return {"ok": False, "error": note}
    return {"ok": True, "rel": rel, "name": os.path.basename(rel),
            "text": text, "chars": len(text), "note": note}


def tree_text(max_items: int = 200) -> str:
    """给模型看的文库清单（人话格式，别让它去猜有哪些文件）。"""
    files = list_files()
    if not files:
        return "（生成文库还是空的）"
    lines = []
    for f in files[:max_items]:
        lines.append("- %s  （%s，%d 字节约）" % (f["rel"], f["modified"], f["size"]))
    if len(files) > max_items:
        lines.append("…… 还有 %d 个文件" % (len(files) - max_items))
    return "\n".join(lines)


# =====================================================================
#  写入 / 备份 / 删除 / 复制
# =====================================================================
def _backup(full: str, rel: str) -> str:
    """覆盖前把原文件拷一份到 `_备份/`（按时间戳分目录，不覆盖旧备份）。"""
    if not os.path.exists(full):
        return ""
    safe = rel.replace("/", "__")
    d = os.path.join(BACKUP_DIR, _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, safe)
        shutil.copy2(full, dst)
        return os.path.relpath(dst, LIB_DIR).replace(os.sep, "/")
    except Exception:
        return ""


def write_file(rel: str, text: str, mode: str = "overwrite") -> dict:
    """写文本文件。mode=append 时追加。覆盖前自动备份。"""
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ensure_dir()
    full = _abs(rel)
    os.makedirs(os.path.dirname(full) or LIB_DIR, exist_ok=True)
    backup = _backup(full, rel) if (os.path.exists(full) and mode != "append") else ""
    try:
        with open(full, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rel": rel, "chars": len(text or ""),
            "mode": mode, "backup": backup}


def save_bytes(rel: str, data: bytes) -> dict:
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ensure_dir()
    full = _abs(rel)
    os.makedirs(os.path.dirname(full) or LIB_DIR, exist_ok=True)
    backup = _backup(full, rel) if os.path.exists(full) else ""
    try:
        with open(full, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rel": rel, "bytes": len(data), "backup": backup}


def delete_file(rel: str) -> dict:
    """删除 → 实际是**移进 `_回收站/`**（模型是自动执行的，得留后悔的余地）。"""
    try:
        rel = _safe_rel(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = _abs(rel)
    if not os.path.exists(full):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    d = os.path.join(TRASH_DIR, _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, rel.replace("/", "__"))
        shutil.move(full, dst)
        return {"ok": True, "rel": rel,
                "trash": os.path.relpath(dst, LIB_DIR).replace(os.sep, "/")}
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s: %s" % (type(e).__name__, e)}


def copy_file(rel: str, new_rel: str) -> dict:
    try:
        rel, new_rel = _safe_rel(rel), _safe_rel(new_rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    src, dst = _abs(rel), _abs(new_rel)
    if not os.path.exists(src):
        return {"ok": False, "error": "源文件不存在：%s" % rel}
    if os.path.isdir(src):
        return {"ok": False, "error": "只能复制文件，不能复制文件夹：%s" % rel}
    # ⚠️ 不覆盖同名文件。复制本来是"留个后路"的动作，允许静默覆盖的话，
    # 用户输入一个已存在的名字就会把那份文件**直接冲掉** ——
    # 而 write_file 覆盖前是会自动备份的，两者必须同样"不让人丢东西"。
    if os.path.exists(dst):
        return {"ok": False,
                "error": "已存在同名文件：%s（换个名字，或先去删掉它）" % new_rel}
    os.makedirs(os.path.dirname(dst) or LIB_DIR, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        return {"ok": False, "error": "复制失败：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "from": rel, "to": new_rel}


def backup_all() -> dict:
    """整库快照（用户或模型都可以主动调用）。"""
    ensure_dir()
    d = os.path.join(BACKUP_DIR, "全量-" + _stamp())
    try:
        os.makedirs(d, exist_ok=True)
        n = 0
        for f in list_files():
            src = _abs(f["rel"])
            dst = os.path.join(d, f["rel"].replace("/", "__"))
            shutil.copy2(src, dst)
            n += 1
        return {"ok": True, "count": n,
                "path": os.path.relpath(d, LIB_DIR).replace(os.sep, "/")}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def stats() -> dict:
    files = list_files()
    return {"count": len(files),
            "bytes": sum(f["size"] for f in files),
            "dir": LIB_DIR,
            "latest": files[0]["name"] if files else ""}

def file_path(rel: str) -> str:
    """取库内文件的绝对路径（下载/导出用）。越界会抛 ValueError。"""
    return _abs(_safe_rel(rel))
