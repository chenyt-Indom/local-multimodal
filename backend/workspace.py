# -*- coding: utf-8 -*-
"""开发工作区（Dev Workspace）—— 人机协同开发用的多项目目录。

为什么**不**复用「生成文库」（data/library）：
  · 生成文库是**成品归档**：一次任务产出一份，以"存"为主；
  · 工作区是**开发中的项目**：多文件、多级目录、会被反复读改写、要能跑。
两者语义不同。混在一起会出现"改一个文件变成存了新的一份"，协同开发就无从谈起。

目录结构：
    data/workspace/
        <项目名>/            ← 每个项目一个独立子目录，互不干扰
            app.py
            static/...
        _回收站/             ← 覆盖/删除的东西都进这里，不真删

**多项目**：模型工具跟随"当前项目"（记在 config.json 的 active_project），
前端可以随时切换 —— 切过去就是另一套完全独立的文件。

路径安全：所有对外接口都走 `safe_rel`，只允许**项目内的相对路径**，
拒绝盘符、`..`、`/` 开头、`~` 开头 —— 否则模型一句 "../../config.json" 就能改到别处。
"""
import io
import os
import re
import shutil
import sys
import time
import zipfile

from . import config

# 单文件大小上限（协同开发里没有大文件，超过多半是模型写飞了）
MAX_TEXT = 2 * 1024 * 1024
# 导入文件的上限（用户拖进来的素材可能有点大，但也不该无限）
MAX_IMPORT = 64 * 1024 * 1024
_RECYCLE = "_回收站"
DEFAULT_PROJECT = "default"
_NAME_RE = re.compile(r"^[^\\/:*?\"<>|\r\n]{1,48}$")
# 新建项目时忽略的目录名
_RESERVED = {_RECYCLE}


# --------------------------------------------------------------------- 基础
def base() -> str:
    """工作区总根（所有项目都在它下面）。"""
    d = config.data("data", "workspace")
    os.makedirs(d, exist_ok=True)
    return d


def safe_project(name: str) -> str:
    """校验项目名（它就是一级目录名，必须干净）。"""
    n = str(name or "").strip()
    if not n:
        raise ValueError("项目名不能为空")
    if n in _RESERVED or n.startswith("."):
        raise ValueError("这个项目名是保留名")
    if not _NAME_RE.match(n) or n in (".", ".."):
        raise ValueError("项目名不能包含 \\ / : * ? \" < > | 等字符")
    return n


# 当前项目。**必须用内存变量做权威**：
# 实测过——只靠 config.json 的话，config.load_config() 是带缓存的，
# 切换项目写进去了、读回来还是启动时那份，表现为"切了没反应、文件串项目"。
_ACTIVE = ""


def active_project() -> str:
    """当前项目（模型工具默认作用在这个项目上）。"""
    global _ACTIVE
    if _ACTIVE and os.path.isdir(os.path.join(base(), _ACTIVE)):
        return _ACTIVE
    try:
        cfg = config.load_config() or {}
        n = str(cfg.get("active_project") or "").strip()
        if n and os.path.isdir(os.path.join(base(), n)):
            _ACTIVE = n
            return n
    except Exception:
        pass
    # 没设置 / 目录不在 → 退到第一个存在的项目，都没有就用默认名
    ps = [p["name"] for p in projects()]
    _ACTIVE = ps[0] if ps else DEFAULT_PROJECT
    return _ACTIVE


def set_active_project(name: str) -> str:
    global _ACTIVE
    n = safe_project(name)
    os.makedirs(os.path.join(base(), n), exist_ok=True)
    _ACTIVE = n                       # 先认内存，config 只是持久化副本
    import logging as _lg
    _lg.getLogger("uvicorn.error").warning(
        "[ws] set_active_project -> %r (dir=%s base=%s)", n,
        os.path.isdir(os.path.join(base(), n)), base())
    try:
        cfg = config.load_config() or {}
        cfg["active_project"] = n
        config.save_config(cfg)
    except Exception:
        pass
    return n


def projects() -> list:
    """列出所有项目。"""
    out = []
    rootd = base()
    for name in sorted(os.listdir(rootd)):
        d = os.path.join(rootd, name)
        if not os.path.isdir(d) or name in _RESERVED or name.startswith("."):
            continue
        cnt, size, newest = 0, 0, 0
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames
                           if x != _RECYCLE and not x.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                cnt += 1
                size += st.st_size
                newest = max(newest, int(st.st_mtime))
        out.append({"name": name, "files": cnt, "size": size, "mtime": newest})
    return out


def create_project(name: str) -> dict:
    try:
        n = safe_project(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    d = os.path.join(base(), n)
    if os.path.isdir(d):
        return {"ok": False, "error": "同名项目已存在：%s" % n}
    os.makedirs(d, exist_ok=True)
    return {"ok": True, "name": n}


def delete_project(name: str) -> dict:
    """删项目 → 进回收站（不真删；协同开发里手滑是常态）。"""
    try:
        n = safe_project(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    d = os.path.join(base(), n)
    if not os.path.isdir(d):
        return {"ok": False, "error": "项目不存在：%s" % n}
    dst = os.path.join(base(), _RECYCLE, time.strftime("%Y%m%d"),
                       "%d_%s" % (int(time.time()), n))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(d, dst)
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    if active_project() == n:
        ps = [p["name"] for p in projects()]
        set_active_project(ps[0] if ps else DEFAULT_PROJECT)
    return {"ok": True, "name": n}


def rename_project(old: str, new: str) -> dict:
    try:
        o, n = safe_project(old), safe_project(new)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    src, dst = os.path.join(base(), o), os.path.join(base(), n)
    if not os.path.isdir(src):
        return {"ok": False, "error": "项目不存在：%s" % o}
    if os.path.exists(dst):
        return {"ok": False, "error": "目标项目名已存在：%s" % n}
    try:
        os.rename(src, dst)
    except Exception as e:
        return {"ok": False, "error": "重命名失败：%s" % e}
    if active_project() == o:
        set_active_project(n)
    return {"ok": True, "name": n}


def root(proj: str = "") -> str:
    """某个项目的根目录；proj 为空则用当前项目。"""
    try:
        p = safe_project(proj) if str(proj or "").strip() else active_project()
    except ValueError:
        p = active_project()          # 名字不合法就退回当前项目，别把接口打成 500
    d = os.path.join(base(), p)
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------- 路径
def safe_rel(rel: str) -> str:
    """把外部传入的路径规范化成项目内的相对路径；越界就抛 ValueError。"""
    r = str(rel or "").strip().replace("\\", "/")
    if not r:
        raise ValueError("路径不能为空")
    # ⚠️ 必须先挡掉开头的 / —— 实测 Windows 上 `os.path.isabs("/etc/passwd")`
    # 返回的是 **False**（新版 ntpath 要求"盘符+根"才算绝对），
    # 于是 "/etc/passwd" 会被 lstrip 成 "etc/passwd" 当成合法相对路径写进去。
    # 虽然没跳出工作区、不算越权，但"传绝对路径"这件事本身就该被明确拒绝。
    if r.startswith("/") or r.startswith("~"):
        raise ValueError("只接受项目内的相对路径，不要以 / 或 ~ 开头")
    if os.path.isabs(r) or (len(r) > 1 and r[1] == ":"):
        raise ValueError("只接受项目内的相对路径，不要传盘符或绝对路径")
    r = r.lstrip("/")
    parts = []
    for seg in r.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("路径不能包含 ..（不许跳出项目）")
        parts.append(seg)
    if not parts:
        raise ValueError("路径不能为空")
    if parts[0] == _RECYCLE:
        raise ValueError("不能直接操作回收站")
    return "/".join(parts)


def abs_path(rel: str, proj: str = "") -> str:
    return os.path.join(root(proj), safe_rel(rel).replace("/", os.sep))


# --------------------------------------------------------------------- 文件
def tree(proj: str = "", max_files: int = 1200) -> dict:
    """列出项目里的全部文件（多级目录）。"""
    try:
        p = safe_project(proj) if str(proj or "").strip() else active_project()
    except ValueError:
        p = active_project()
    base_dir = root(p)
    files = []
    for dirpath, dirnames, filenames in os.walk(base_dir):
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
            rel = os.path.relpath(full, base_dir).replace(os.sep, "/")
            files.append({"rel": rel, "size": st.st_size,
                          "mtime": int(st.st_mtime),
                          "ext": os.path.splitext(fn)[1].lower().lstrip(".")})
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break
    files.sort(key=lambda f: f["rel"])
    return {"ok": True, "project": p, "root": base_dir,
            "files": files, "count": len(files)}


def read_text(rel: str, proj: str = "") -> dict:
    p = abs_path(rel, proj)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    if os.path.getsize(p) > MAX_TEXT:
        return {"ok": False, "error": "文件过大（>2MB），开发台不支持打开"}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    return {"ok": True, "rel": safe_rel(rel), "text": text, "chars": len(text)}


def stream_write(rel: str, text: str, proj: str = "") -> dict:
    """**边生成边落盘**专用：只写文件，**不备份、不记"待审阅"改动**。

    为什么必须单独开一条路，不能复用 write_text：
    AI 写文件的正文是**逐字流出来的**，我们把已经生成的部分先落盘，
    编辑器（内置真 VS Code）的文件监视器就能看到代码一点点"长出来"。
    但 write_text **每写一次就把旧版备份进回收站、并记一条待审阅** ——
    按 40 字一刷算，写一个 3000 字的文件会塞进去几十上百份垃圾备份，
    "AI 改动"列表也会被刷爆。
    所以这里的定位是"给编辑器看的进度流"；**真正的落盘仍然是生成结束时
    那一次 write_text（带备份与改动记录）**。
    """
    try:
        r = safe_rel(rel)
        p = abs_path(r, proj)
        os.makedirs(os.path.dirname(p) or root(proj), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text or "")
        return {"ok": True, "rel": r, "chars": len(text or "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def write_text(rel: str, text: str, proj: str = "", by: str = "user") -> dict:
    """写入文件；覆盖前把旧版备份进回收站，并把这次改动记进"待审阅"。

    `by` 区分是谁改的（"ai" / "user"）—— 前端据此只看 AI 的改动、逐个审阅。
    """
    r = safe_rel(rel)
    p = abs_path(r, proj)
    os.makedirs(os.path.dirname(p) or root(proj), exist_ok=True)
    before = ""
    existed = os.path.exists(p)
    if existed:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                before = f.read()
        except Exception:
            before = ""
    backup = ""
    if existed:
        try:
            _dir = os.path.join(base(), _RECYCLE, time.strftime("%Y%m%d"))
            os.makedirs(_dir, exist_ok=True)
            dst = os.path.join(_dir, "%s_%s%s" % (
                os.path.splitext(os.path.basename(p))[0],
                time.strftime("%H%M%S"), os.path.splitext(p)[1]))
            shutil.copy2(p, dst)
            backup = os.path.relpath(dst, base()).replace(os.sep, "/")
        except Exception:
            backup = ""
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text or "")
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    rec = record_change(r, before, text or "", by=by, project=proj)
    return {"ok": True, "rel": r, "chars": len(text or ""), "backup": backup,
            "change_id": rec.get("id")}


# ----------------------------------------------------- 改动记录（可审阅/可撤销）
# 为什么不做"先弹 diff、批准才落盘"：那样模型写完就没法立刻跑验证，
# **写→跑→看报错→改** 的回路会断掉（实测过：它必须能跑才能自己修对）。
# 所以采用"先落盘 + 全程留痕 + 一键撤销"—— 等价于 Trae 的接受/拒绝，
# 但不牺牲自动迭代能力。
_CHANGES = []          # [{id, rel, before, after, ts, by, project}]
_CHANGE_CAP = 200


def record_change(rel: str, before: str, after: str, by: str = "user",
                  project: str = "") -> dict:
    if before == after:
        return {}
    rec = {"id": "c%d" % (int(time.time() * 1000) % 100000000),
           "rel": rel, "before": before[:200000], "after": after[:200000],
           "ts": int(time.time()), "by": by,
           "project": project or active_project(),
           "created": not before}
    _CHANGES.append(rec)
    del _CHANGES[:-_CHANGE_CAP]
    return rec


def changes(limit: int = 50) -> list:
    """最近的改动，新的在前（不带全文，省得前端被大文件噎住）。"""
    out = []
    for c in reversed(_CHANGES[-limit:]):
        out.append({"id": c["id"], "rel": c["rel"], "ts": c["ts"], "by": c["by"],
                    "project": c["project"], "created": c.get("created", False),
                    "before_chars": len(c["before"]), "after_chars": len(c["after"])})
    return out


def change_detail(cid: str) -> dict:
    for c in _CHANGES:
        if c["id"] == cid:
            return {"ok": True, **c}
    return {"ok": False, "error": "找不到这条改动（可能已被清理）"}


def revert(cid: str) -> dict:
    """撤销一条改动：把文件恢复到改动前。"""
    for c in _CHANGES:
        if c["id"] != cid:
            continue
        try:
            p = abs_path(c["rel"], c.get("project") or "")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        try:
            if c.get("created"):
                # 这条改动是"新建文件" → 撤销就是删掉它（进回收站）
                return remove(c["rel"], c.get("project") or "")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(c["before"])
        except Exception as e:
            return {"ok": False, "error": "撤销失败：%s" % e}
        _CHANGES.remove(c)
        return {"ok": True, "rel": c["rel"], "chars": len(c["before"])}
    return {"ok": False, "error": "找不到这条改动"}


def clear_changes(only_project: str = "") -> int:
    """把记录标记为已读过（保留记录本身，只影响前端的"未读"计数）。"""
    keep = [c for c in _CHANGES
            if only_project and c.get("project") != only_project]
    dropped = len(_CHANGES) - len(keep)
    _CHANGES[:] = keep
    return dropped


def mkdir(rel: str, proj: str = "") -> dict:
    try:
        d = abs_path(rel.rstrip("/"), proj)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    os.makedirs(d, exist_ok=True)
    return {"ok": True, "rel": safe_rel(rel.rstrip("/"))}


def remove(rel: str, proj: str = "") -> dict:
    """删除 → 进回收站（**不真删**）。"""
    r = safe_rel(rel)
    p = abs_path(r, proj)
    if not os.path.exists(p):
        return {"ok": False, "error": "不存在：%s" % r}
    dst = os.path.join(base(), _RECYCLE, time.strftime("%Y%m%d"),
                       "%d_%s" % (int(time.time()), os.path.basename(p)))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(p, dst)
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    return {"ok": True, "rel": r,
            "moved_to": os.path.relpath(dst, base()).replace(os.sep, "/")}


def rename(rel: str, to: str, proj: str = "") -> dict:
    try:
        src, dst = abs_path(rel, proj), abs_path(to, proj)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(src):
        return {"ok": False, "error": "不存在：%s" % rel}
    if os.path.exists(dst):
        return {"ok": False, "error": "目标已存在：%s" % to}
    os.makedirs(os.path.dirname(dst) or root(proj), exist_ok=True)
    try:
        shutil.move(src, dst)
    except Exception as e:
        return {"ok": False, "error": "重命名失败：%s" % e}
    return {"ok": True, "rel": safe_rel(to)}


def import_bytes(rel: str, data: bytes, proj: str = "") -> dict:
    """把上传/拖进来的文件写进项目（**二进制安全**，图片/压缩包也能进）。"""
    r = safe_rel(rel)
    if len(data) > MAX_IMPORT:
        return {"ok": False, "error": "文件太大（>64MB）"}
    p = abs_path(r, proj)
    os.makedirs(os.path.dirname(p) or root(proj), exist_ok=True)
    try:
        with open(p, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    return {"ok": True, "rel": r, "bytes": len(data)}


def norm_upload_name(filename: str) -> str:
    """把浏览器给的原始文件名收敛成安全的相对路径（只留基本名）。"""
    n = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    n = re.sub(r'[\\/:*?"<>|\r\n]+', "_", n).strip(". ")
    return n or ("file_%d" % int(time.time()))


# ------------------------------------------------- 运行（流式，像终端那样实时出字）
# 为什么单独做一套：原来的实现是 `subprocess.run(capture_output=True)` ——
# **跑完才拿得到输出**。于是计时器、服务器这类长任务在你眼里就是"一直正在执行"；
# 而且**一旦超时被强杀，那 25 秒里打印的东西全被丢掉**（实测：番茄钟跑满 25 秒，
# 界面显示"（没有输出）"）。流式版边跑边推，超时也保留已产出的内容。
_RUNS = {}          # run_id -> Popen


def start_run(rel: str, proj: str = "", run_id: str = "") -> dict:
    """启动工作区里的 .py，返回 Popen —— 输出由调用方边读边推给前端。

    与 `tools.run_file` 的分工：
      · 这个给**用户手点「▶ 运行」**用：**不设超时**，要不要停由用户按「■ 停止」决定；
      · `run_file` 给**模型工具**用：必须封顶，否则一个死循环就把智能体挂住了。
    """
    import subprocess as _sp
    if not rel.lower().endswith(".py"):
        return {"ok": False, "error": "目前只能直接运行 .py 文件。"}
    try:
        p = abs_path(rel, proj)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        return {"ok": False, "error": "读取失败：%s" % e}

    from . import tools as _tools          # 延迟导入，避免模块级循环依赖
    risky = _tools.scan_risky(code)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"          # ⚠️ 关键：不加这个子进程会缓冲，看不到实时输出
    env.pop("MM_DATA_DIR", None)
    try:
        proc = _sp.Popen(
            # -u 同样是为了**关掉子进程缓冲**，否则 print 会攒成一块再吐
            [sys.executable, "-X", "utf8", "-u", os.path.basename(p)],
            cwd=os.path.dirname(p) or ".", env=env,
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s" % e}
    _RUNS[run_id] = proc
    return {"ok": True, "proc": proc, "rel": safe_rel(rel), "risky": risky}


def stop_run(run_id: str = "") -> dict:
    """停掉正在跑的进程；不给 id 就把所有在跑的停掉。"""
    targets = [run_id] if run_id and run_id in _RUNS else list(_RUNS)
    if not targets:
        return {"ok": False, "error": "当前没有正在运行的进程"}
    n = 0
    for k in targets:
        proc = _RUNS.pop(k, None)
        if proc is None:
            continue
        try:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()
            n += 1
        except Exception:
            pass
    return {"ok": True, "stopped": n}


def reap_runs() -> None:
    """清掉已经结束的进程记录。"""
    for k in [k for k, v in _RUNS.items() if v.poll() is not None]:
        _RUNS.pop(k, None)


def kill_all_runs() -> None:
    """起新任务 / 退出时把残留进程收干净。"""
    stop_run("")


def running_count() -> int:
    reap_runs()
    return len(_RUNS)


# --------------------------------------------------------------- 用外部专业 IDE 打开
def _find_ide() -> tuple:
    """找本机已装的 PyCharm / VS Code，返回 (可执行文件, 名字)。

    JetBrains 装在哪，最靠谱的线索是它自己在
    `%LOCALAPPDATA%\\JetBrains\\PyCharm<版本>\\.home` 里写下的安装路径
    （本机就是靠它找到 `D:\\PyCharm 2026.1.3` 的）——比猜目录名稳。
    """
    import glob
    cands = []
    home_root = os.path.join(os.path.expanduser("~"), "AppData", "Local", "JetBrains")
    for d in glob.glob(os.path.join(home_root, "PyCharm*")):
        f = os.path.join(d, ".home")
        if os.path.isfile(f):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    root = fh.read().strip()
            except Exception:
                continue
            for exe in ("bin/pycharm64.exe", "bin/pycharm.exe", "bin/pycharm.sh"):
                cands.append((os.path.join(root, exe.replace("/", os.sep)), "PyCharm"))
    for pat, name in (
            (r"C:\Program Files\JetBrains\PyCharm*\bin\pycharm64.exe", "PyCharm"),
            (r"C:\Program Files (x86)\JetBrains\PyCharm*\bin\pycharm64.exe", "PyCharm"),
            (r"D:\PyCharm*\bin\pycharm64.exe", "PyCharm"),
            (r"C:\Users\*\AppData\Local\Programs\PyCharm*\bin\pycharm64.exe", "PyCharm"),
            (r"C:\Program Files\Microsoft VS Code\Code.exe", "VS Code"),
            (r"C:\Users\*\AppData\Local\Programs\Microsoft VS Code\Code.exe", "VS Code")):
        for hit in glob.glob(pat):
            cands.append((hit, name))
    for exe, name in cands:
        if exe and os.path.isfile(exe):
            return exe, name
    return "", ""


def open_in_ide(proj: str = "") -> dict:
    """用外部专业 IDE（PyCharm / VS Code）打开当前项目。"""
    import subprocess as _sp
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    d = root(p)
    exe, name = _find_ide()
    if not exe:
        return {"ok": False, "error": "没找到 PyCharm / VS Code。"
                                      "可以在资源管理器里手动打开项目文件夹。",
                "path": d}
    try:
        _sp.Popen([exe, d], close_fds=True)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s" % e, "path": d}
    return {"ok": True, "ide": name, "exe": exe, "path": d, "project": p}


def focus_ide_window(proj: str = "") -> dict:
    """把本机 IDE（PyCharm）的窗口**拉到前台**。

    为什么要这一步：AI 写代码不是"模拟按键敲进 PyCharm"（那需要装 PyCharm 插件），
    而是**在磁盘上逐字写文件**，由 IDE 检测到外部改动后自己重载显示 ——
    效果一样，但不用插件、不怕丢断点、改的也确实是同一份文件。
    而 JetBrains 什么时候去查磁盘上的改动，**跟窗口是否活跃有关**：
    不拉前台的话，有时要用户自己点一下 PyCharm 窗口才会刷新。
    所以 AI 开始写某个文件时把它拉到前台，用户就能**看着**代码一个字一个字长出来。

    找不到窗口就静默返回（IDE 没开 / 平台不支持），绝不能让这一步挡住生成。
    """
    try:
        import ctypes
        from ctypes import wintypes
        exe, _name = _find_ide()
        if not exe:
            return {"ok": False, "error": "本机没找到 IDE"}
        # ⚠️ **按进程认窗口，别按标题认**：实测 PyCharm 的窗口标题是
        # 「1 – dwf.py」（"项目名 – 文件名"），**压根不含 "PyCharm"** ——
        # 按标题匹配会永远找不到窗口，而且不会报错，只是"这个功能好像没生效"。
        want = os.path.basename(exe).lower()          # 例如 pycharm64.exe
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True          # 没标题的多半是隐藏窗口，跳过
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            # 取这个窗口所属进程的可执行文件名
            h = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
            if not h:
                return True
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    if os.path.basename(buf.value).lower() == want:
                        found.append(hwnd)
                        return False         # 找到一个就够了
            finally:
                kernel32.CloseHandle(h)
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return {"ok": False, "error": "没找到 IDE 窗口（可能还没打开）"}
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE：最小化了要先还原
        else:
            user32.ShowWindow(hwnd, 5)     # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ide_status(proj: str = "") -> dict:
    """本机装了哪个 IDE、当前项目在哪个目录。

    **只探测，不启动** —— 用户没点之前不该擅自弹出个 PyCharm 窗口（又慢又突兀）。
    前端拿它把按钮文案写成「🧠 PyCharm」并显示项目路径。
    """
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    d = root(p)
    try:
        exe, name = _find_ide()
    except Exception:
        exe, name = "", ""
    return {"ok": True, "name": name or "", "exe": exe or "",
            "path": d, "project": p}


def open_folder(proj: str = "") -> dict:
    """在资源管理器里打开项目文件夹（找不到 IDE 时的兜底）。"""
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    d = root(p)
    try:
        if os.name == "nt":
            os.startfile(d)          # noqa: S606 —— Windows 专用，这里是预期行为
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open", d])
    except Exception as e:
        return {"ok": False, "error": str(e), "path": d}
    return {"ok": True, "path": d, "project": p}


# ------------------------------------------------- 内置 code-server（真正的 VS Code）
# 为什么接它：用户要"更专业的工作台"。断点调试、变量监视、重构、代码导航、
# 终端、Git 面板、扩展市场 —— 这些 VS Code 打磨了十几年，自研一个半成品不划算。
# code-server 是 VS Code 的**服务端**（自带 Node，绿色包），跑在 127.0.0.1 上，
# 用网页打开就是完整的 VS Code，**全程离线**。
_CODE_SERVER = {"proc": None, "port": 0, "project": ""}


def find_code_server() -> str:
    """找到内置的 code-server 可执行文件（没有就返回空）。"""
    import glob
    base = os.path.join(config.res_root(), "vendor")
    for pat in ("code-server-*/bin/code-server.cmd",
                "code-server-*/bin/code-server",
                "code-server/bin/code-server.cmd",
                "code-server/bin/code-server"):
        hits = sorted(glob.glob(os.path.join(base, pat.replace("/", os.sep))))
        for h in hits:
            if os.path.isfile(h):
                return h
    return ""


def code_server_status() -> dict:
    """状态查询。

    ⚠️ 不能只看 `proc.poll()`：code-server 在 Windows 上是通过 `.cmd` 拉起的，
    句柄活着**不代表端口还在服务**（实测遇到过：状态说 running、端口却没人监听，
    用户点开就是一个打不开的地址）。所以**再探一次端口**，双重确认。
    """
    import socket
    p = _CODE_SERVER.get("proc")
    port = _CODE_SERVER.get("port") or 0
    alive = bool(p is not None and p.poll() is None)
    if alive and port:
        reachable = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.8)
                reachable = (s.connect_ex(("127.0.0.1", port)) == 0)
        except Exception:
            reachable = False
        if not reachable:
            alive = False
            _CODE_SERVER.update({"proc": None, "port": 0, "project": ""})
    elif not alive:
        _CODE_SERVER.update({"proc": None, "port": 0, "project": ""})
    return {"ok": True, "installed": bool(find_code_server()), "running": alive,
            "port": port if alive else 0,
            "project": _CODE_SERVER.get("project") or "",
            "url": ("http://127.0.0.1:%d/" % port) if alive else ""}


def _cs_state_file() -> str:
    return config.data("data", ".code_server.json")


def reap_orphan_code_server() -> dict:
    """清掉**上一次应用运行时留下的** code-server。

    ⚠️ 踩过的坑：code-server 在 Windows 上是通过 `.cmd` 拉起的，
    `terminate()` 只杀掉外层那个壳，**真正的 node 子进程会活下来**。
    于是重启应用后：旧进程还占着 8810 → 新进程只能用 8812 → 再重启又漂到 8813，
    越积越多、白吃内存，端口也一路乱跑。
    所以把 PID 记到磁盘上，下次启动先按 PID 精确回收。
    """
    import json as _json
    f = _cs_state_file()
    killed = 0
    try:
        if os.path.isfile(f):
            with open(f, "r", encoding="utf-8") as fh:
                st = _json.load(fh) or {}
            pid = int(st.get("pid") or 0)
            if pid:
                try:
                    import subprocess as _sp
                    _sp.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                            capture_output=True, timeout=15)
                    killed = 1
                except Exception:
                    pass
            os.remove(f)
    except Exception:
        pass
    _CODE_SERVER.update({"proc": None, "port": 0, "project": ""})
    return {"ok": True, "killed": killed}


def _remember_cs(pid: int, port: int, project: str) -> None:
    import json as _json
    try:
        with open(_cs_state_file(), "w", encoding="utf-8") as fh:
            _json.dump({"pid": pid, "port": port, "project": project}, fh)
    except Exception:
        pass


def _forget_cs() -> None:
    try:
        f = _cs_state_file()
        if os.path.isfile(f):
            os.remove(f)
    except Exception:
        pass


def stop_code_server() -> dict:
    p = _CODE_SERVER.get("proc")
    if p is not None:
        try:
            import subprocess as _sp
            # 连带子进程一起杀（只 terminate 外层壳的话，node 会活下来）
            _sp.run(["taskkill", "/PID", str(p.pid), "/F", "/T"],
                    capture_output=True, timeout=15)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _forget_cs()
    _CODE_SERVER.update({"proc": None, "port": 0, "project": ""})
    return {"ok": True}


# code-server（内置 VS Code）的默认设置。
# 为什么要由我们写：VS Code 出厂那套默认值在这个应用里**是错的** ——
# 实测用户一打开开发台就看到两个莫名其妙的东西：
#   ① 一个「GitHub Personal Access Token」输入框糊在屏幕中间
#      （VS Code 自带的 Copilot/GitHub 认证想登录，可我们要求**完全离线**）；
#   ② 右侧一个「Build with Agent / Describe what to build」面板 ——
#      那是 VS Code 自带的 AI 聊天，它有自己的一套模型，跟我们不是一回事，
#      用户看到会以为"怎么有两个 AI"。
# 这里的定位很明确：**这个应用里写代码的 AI 只有一个，就是我们自己的本地模型**。
_VS_SETTINGS = {
    # 打开就是干净的编辑区，别停在欢迎页
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.enableExperiments": False,
    "workbench.welcomePage.walkthroughs.openOnInstall": False,
    # 关掉 VS Code 自带的 AI 聊天（多套候选键名，不同版本叫法不同，未知键无害）
    "chat.disableAIFeatures": True,
    "chat.commandCenter.enabled": False,
    "chat.editor.enabled": False,
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    # 别弹 GitHub 登录
    "github.gitAuthentication": False,
    "git.autofetch": False,
    "git.confirmSync": False,
    "git.openRepositoryInParentFolders": "never",
    # 一切离线
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
}


def _vs_user_dir() -> str:
    """code-server 放 `settings.json` 的目录（各平台默认位置不一样）。"""
    cands = []
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA") or ""
        if la:
            cands.append(os.path.join(la, "code-server", "Data", "User"))
    home = os.path.expanduser("~")
    cands.append(os.path.join(home, ".local", "share", "code-server", "User"))
    cands.append(os.path.join(home, ".local", "share", "code-server", "User"))
    for c in cands:
        if os.path.isdir(os.path.dirname(c)):
            return c
    return cands[0]


def _ensure_vs_settings() -> None:
    """把上面那套设置**合并**进 code-server 的 settings.json（保留用户已有的键）。

    每次启动都写一遍：这样"改掉那些怪东西"是可复现的，
    而不是靠手动改一次文件（Docker 版、换台机器都得重新来一遍）。
    """
    import json as _json
    try:
        p = os.path.join(_vs_user_dir(), "settings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        cur = {}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    cur = _json.load(fh) or {}
            except Exception:
                cur = {}
        if not isinstance(cur, dict):
            cur = {}
        if all(cur.get(k) == v for k, v in _VS_SETTINGS.items()):
            return                      # 已经是我们的设置，不动它
        cur.update(_VS_SETTINGS)
        with open(p, "w", encoding="utf-8") as fh:
            _json.dump(cur, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass                            # 写不进去也不该拦住 VS Code 启动


def start_code_server(proj: str = "") -> dict:
    """给当前项目起一个内置 VS Code（code-server），返回可访问地址。

    ⚠️ 只绑 `127.0.0.1` + `--auth none`：**只有本机能连**，外网碰不到。
    这在本机单用户桌面应用里是常规做法（等于"你自己电脑上的 VS Code"）。
    """
    import glob
    import socket
    import subprocess as _sp
    exe = find_code_server()
    if not exe:
        return {"ok": False, "error": "没有内置的 code-server（vendor/ 下没找到）"}
    # 先把"出厂默认值里那些在本应用里不对的"设置写进去（关自带 AI 聊天、
    # 关 GitHub 登录、关欢迎页、一切离线）—— 见 _VS_SETTINGS 的说明。
    _ensure_vs_settings()
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    d = root(p)

    cur = code_server_status()
    if cur.get("running") and cur.get("project") == p:
        return {"ok": True, "url": cur["url"], "port": cur["port"], "project": p,
                "note": "already running"}
    stop_code_server()

    def _free(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    port = 0
    for cand in range(8810, 8840):
        if _free(cand):
            port = cand
            break
    if not port:
        return {"ok": False, "error": "8810~8839 都被占用了"}

    env = dict(os.environ)
    env["PORT"] = str(port)
    env.pop("MM_DATA_DIR", None)
    # 监听地址：源码模式绑回环就够（只有本机能连）；容器里得绑 0.0.0.0，
    # 再由 `docker run -p 127.0.0.1:8810:8810` 只映射到宿主回环 —— 两头都不对外。
    bind = str(env.get("MM_CODE_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    args = [exe, "--bind-addr", "%s:%d" % (bind, port), "--auth", "none",
            "--disable-telemetry", "--disable-update-check", d]
    try:
        if exe.endswith(".cmd"):
            proc = _sp.Popen(args, cwd=d, env=env, shell=False,
                             stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        else:
            proc = _sp.Popen(args, cwd=d, env=env,
                             stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s" % e}

    # 等它真的监听上（首次启动要几秒）
    for _ in range(80):
        time.sleep(0.25)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                _CODE_SERVER.update({"proc": proc, "port": port, "project": p})
                _remember_cs(proc.pid, port, p)
                return {"ok": True, "url": "http://127.0.0.1:%d/" % port,
                        "port": port, "project": p, "path": d}
        if proc.poll() is not None:
            return {"ok": False, "error": "code-server 异常退出（可能被杀软拦了）"}
    try:
        proc.terminate()
    except Exception:
        pass
    return {"ok": False, "error": "启动超时（20 秒内没监听上）"}


# --------------------------------------------------------------- 语法检查（调试）
def check_py(rel: str, proj: str = "") -> dict:
    """对 .py 做**语法检查**，返回可直接喂给编辑器的诊断。

    这是"调试能力"里最省事也最有用的一环：写错了当场在编辑器里标红，
    不用等到点运行才看到报错。
    """
    r = read_text(rel, proj)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error"), "diagnostics": []}
    code = r.get("text") or ""
    try:
        compile(code, rel, "exec")
        return {"ok": True, "diagnostics": []}
    except SyntaxError as e:
        return {"ok": True, "diagnostics": [{
            "line": int(e.lineno or 1), "col": int(e.offset or 1),
            "message": str(e.msg or "语法错误"), "severity": "error"}]}
    except (ValueError, MemoryError) as e:
        # 源码里含 NUL 之类：也算语法问题
        return {"ok": True, "diagnostics": [{
            "line": 1, "col": 1, "message": "无法编译：%s" % e, "severity": "error"}]}


# --------------------------------------------------------------- 上传到代码托管平台
def _git(args: list, cwd: str, timeout: int = 120) -> tuple:
    import subprocess
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "找不到 git（请确认已安装并加入 PATH）"
    except Exception as e:
        return -1, "", "%s: %s" % (type(e).__name__, e)


def git_push(proj: str = "", repo: str = "", message: str = "",
             branch: str = "main") -> dict:
    """把当前项目提交并推送到代码托管平台（GitHub / Gitee / 自建 Git 都行）。

    · 项目还没有 .git 就 `git init`；
    · 有改动就提交（没改动也能推，用于首次推送）；
    · 远端用 `repo` 覆盖（空则沿用已有的 origin）；
    · **认证交给系统里的 git 凭据**（SSH key / credential helper），
      本项目不保存、也不经手任何密码或 token。
    """
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    d = root(p)
    branch = (str(branch or "").strip() or "main")
    repo = str(repo or "").strip()
    message = str(message or "").strip() or ("更新 %s" % time.strftime("%Y-%m-%d %H:%M"))

    logs = []
    if not os.path.isdir(os.path.join(d, ".git")):
        rc, out, err = _git(["init"], d)
        logs.append("git init: " + (out or err or "ok"))
        if rc != 0:
            return {"ok": False, "error": err, "logs": logs}
    # 首次使用 git 的机器可能没配 user.name/email，提交会直接失败 —— 兜一个默认值
    rc, nm, _ = _git(["config", "user.name"], d)
    if rc != 0 or not nm:
        _git(["config", "user.name", "local-multimodal"], d)
        _git(["config", "user.email", "local@localhost"], d)
        logs.append("已补默认 git 身份（仅本项目）")

    if repo:
        rc, out, err = _git(["remote", "get-url", "origin"], d)
        if rc == 0:
            _git(["remote", "set-url", "origin", repo], d)
            logs.append("origin 已更新为 " + repo)
        else:
            _git(["remote", "add", "origin", repo], d)
            logs.append("origin 已设为 " + repo)

    _git(["add", "-A"], d)
    rc, out, err = _git(["commit", "-m", message], d)
    if rc == 0:
        logs.append("已提交：" + (out.splitlines()[0] if out else message))
    else:
        logs.append("无需提交（" + (err.splitlines()[0] if err else "工作区干净") + "）")

    _git(["branch", "-M", branch], d)
    rc, out, err = _git(["push", "-u", "origin", branch], d, timeout=300)
    if out:
        logs.append(out[-800:])
    if rc != 0:
        tip = err or out
        if "couldn't find remote ref" in tip or "has no commits" in tip:
            tip += "\n（远端仓库还是空的？先推一次就行）"
        if "Authentication failed" in tip or "Permission denied" in tip:
            tip += "\n（认证失败：请确认这台机器的 git 凭据/SSH key 已配好）"
        return {"ok": False, "error": tip, "logs": logs, "branch": branch}
    return {"ok": True, "branch": branch, "logs": logs}


def export_zip(proj: str = "") -> tuple:
    """把整个项目打成 zip，返回 (bytes, 文件名)。"""
    p = safe_project(proj) if str(proj or "").strip() else active_project()
    buf = io.BytesIO()
    base_dir = root(p)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = [d for d in dirnames
                           if d != _RECYCLE and not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base_dir).replace(os.sep, "/")
                try:
                    z.write(full, rel)
                except Exception:
                    continue
    return buf.getvalue(), "%s.zip" % p
