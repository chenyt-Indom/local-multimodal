# -*- coding: utf-8 -*-
"""开发工作区（Dev Workspace）—— 人机协同开发用的多文件目录。

为什么**不**复用「生成文库」（data/library）：
  · 生成文库是**成品归档**：一次任务产出一份，命名带语义，以"存"为主；
  · 工作区是**开发中的项目**：多文件、多级目录、会被反复读改写、要能跑。
两者语义不同。混在一起会出现"改一个文件变成存了新的一份"这种事，
协同开发就无从谈起。所以单开 data/workspace。

路径安全：所有对外接口都走 `safe_rel`，只允许工作区内的**相对路径**，
拒绝盘符、`..`、绝对路径 —— 否则模型一句 "../../config.json" 就能改到别处。
"""
import os
import shutil
import time

from . import config

# 单文件大小上限（协同开发里没有大文件，超过多半是模型写飞了）
MAX_TEXT = 2 * 1024 * 1024
# 首页/入口文件的候选名，供"预览"用
ENTRY_NAMES = ("index.html", "main.py", "app.py", "app.js", "index.js", "README.md")

_RECYCLE = "_回收站"


def root() -> str:
    """工作区根目录。

    位置跟着「知识库 / 生成文库 / 记忆」一起走（`<数据根>/data/workspace`）——
    这样备份、迁移、Docker 挂载都只要盯一个目录，不会多出一处散落的数据。
    """
    d = config.data("data", "workspace")
    os.makedirs(d, exist_ok=True)
    return d


def safe_rel(rel: str) -> str:
    """把外部传入的路径规范化成工作区内的相对路径；越界就抛 ValueError。"""
    r = str(rel or "").strip().replace("\\", "/")
    if not r:
        raise ValueError("路径不能为空")
    # ⚠️ 必须先挡掉开头的 / —— 实测 Windows 上 `os.path.isabs("/etc/passwd")`
    # 返回的是 **False**（新版 ntpath 要求"盘符+根"才算绝对），
    # 于是 "/etc/passwd" 会被 lstrip 成 "etc/passwd" 当成合法相对路径写进工作区。
    # 虽然没跳出工作区、不算越权，但"传绝对路径"这件事本身就该被明确拒绝。
    if r.startswith("/") or r.startswith("~"):
        raise ValueError("只接受工作区内的相对路径，不要以 / 或 ~ 开头")
    if os.path.isabs(r) or (len(r) > 1 and r[1] == ":"):
        raise ValueError("只接受工作区内的相对路径，不要传盘符或绝对路径")
    r = r.lstrip("/")
    parts = []
    for seg in r.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("路径不能包含 ..（不许跳出工作区）")
        parts.append(seg)
    if not parts:
        raise ValueError("路径不能为空")
    if parts[0] == _RECYCLE:
        raise ValueError("不能直接操作回收站")
    return "/".join(parts)


def abs_path(rel: str) -> str:
    return os.path.join(root(), safe_rel(rel).replace("/", os.sep))


def tree(max_files: int = 800) -> dict:
    """列出工作区里的全部文件（多级目录），按目录树返回。"""
    base = root()
    files = []
    for dirpath, dirnames, filenames in os.walk(base):
        # 回收站不展示；隐藏目录也跳过（.git 之类）
        dirnames[:] = [d for d in dirnames
                       if d != _RECYCLE and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            files.append({"rel": rel, "size": st.st_size,
                          "mtime": int(st.st_mtime),
                          "ext": os.path.splitext(fn)[1].lower().lstrip(".")})
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break
    files.sort(key=lambda f: f["rel"])
    return {"ok": True, "root": base, "files": files, "count": len(files)}


def read_text(rel: str) -> dict:
    p = abs_path(rel)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    if os.path.getsize(p) > MAX_TEXT:
        return {"ok": False, "error": "文件过大（>2MB），工作区不支持打开"}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    return {"ok": True, "rel": safe_rel(rel), "text": text, "chars": len(text)}


def write_text(rel: str, text: str) -> dict:
    """写入文件；覆盖前把旧版备份进回收站（协同开发里误覆盖代价很高）。"""
    r = safe_rel(rel)
    p = abs_path(r)
    os.makedirs(os.path.dirname(p) or root(), exist_ok=True)
    backup = ""
    if os.path.exists(p):
        try:
            _dir = os.path.join(root(), _RECYCLE, time.strftime("%Y%m%d"))
            os.makedirs(_dir, exist_ok=True)
            dst = os.path.join(_dir, "%s_%s%s" % (
                os.path.splitext(os.path.basename(p))[0],
                time.strftime("%H%M%S"), os.path.splitext(p)[1]))
            shutil.copy2(p, dst)
            backup = os.path.relpath(dst, root()).replace(os.sep, "/")
        except Exception:
            backup = ""
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text or "")
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    return {"ok": True, "rel": r, "chars": len(text or ""), "backup": backup}


def mkdir(rel: str) -> dict:
    try:
        d = abs_path(rel.rstrip("/"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    os.makedirs(d, exist_ok=True)
    return {"ok": True, "rel": safe_rel(rel.rstrip("/"))}


def remove(rel: str) -> dict:
    """删除 → 进回收站（**不真删**，协同开发里手滑是常态）。"""
    r = safe_rel(rel)
    p = abs_path(r)
    if not os.path.exists(p):
        return {"ok": False, "error": "不存在：%s" % r}
    dst = os.path.join(root(), _RECYCLE, time.strftime("%Y%m%d"),
                       "%d_%s" % (int(time.time()), os.path.basename(p)))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(p, dst)
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    return {"ok": True, "rel": r, "moved_to": os.path.relpath(dst, root()).replace(os.sep, "/")}


def rename(rel: str, to: str) -> dict:
    try:
        src, dst = abs_path(rel), abs_path(to)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(src):
        return {"ok": False, "error": "不存在：%s" % rel}
    if os.path.exists(dst):
        return {"ok": False, "error": "目标已存在：%s" % to}
    os.makedirs(os.path.dirname(dst) or root(), exist_ok=True)
    try:
        shutil.move(src, dst)
    except Exception as e:
        return {"ok": False, "error": "重命名失败：%s" % e}
    return {"ok": True, "rel": safe_rel(to)}
