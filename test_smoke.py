# -*- coding: utf-8 -*-
"""主要功能冒烟测试（在**临时数据目录**里跑，不碰用户的库）。

跑法：python test_smoke.py

覆盖：
  ① 工具注册完整性（schema 定义 / append / dispatch / 实现函数 四处是否对得上）
  ② 各工具真跑一遍：生成类（xlsx / docx / pptx）、文库、工作区、知识库、记忆、时间
  ③ 关键 HTTP 接口（对着真实运行的应用打）

⚠️ 为什么不用真实数据目录：生成类工具会往"生成文库"里写文件，
   冒烟测试跑完会污染用户看得见的库。所以拷一份临时目录再跑。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 临时数据目录 + 拷 config（⚠️ 不拷 config 会走 DEFAULT_CONFIG，得到假结论）
TMP = tempfile.mkdtemp(prefix="mm_smoke_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP

from backend import tools as T          # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


def call(name, args=None, ui=None):
    """跑一个工具，返回 (文本, ui 事件列表)。"""
    ev = ui if ui is not None else []
    try:
        return T.dispatch(name, args or {}, ev, {}), ev
    except Exception as e:
        return "EXC: %r" % (e,), ev


def main():
    print("=" * 62)
    print("主要功能冒烟测试（临时数据目录：%s）" % TMP)
    print("=" * 62)

    # ---------------- ① 工具注册 ----------------
    print()
    print("【1】工具注册完整性")
    schemas = T.make_schemas(web_enabled=True, kb_enabled=True,
                             code_exec=True, writing=False)
    names = [s.get("function", {}).get("name") for s in schemas]
    names = [n for n in names if n]
    check("make_schemas 能出工具清单", len(names) >= 30, "%d 个" % len(names))
    dup = [n for n in set(names) if names.count(n) > 1]
    check("没有重名工具", not dup, str(dup) if dup else "")
    for must in ("map_plan", "nearby_places", "connect_amap",      # connect_amap：请用户配高德 key
                 "make_pptx", "make_docx", "make_xlsx", "edit_office", "library",
                 "remember", "search_memory", "get_time"):
        check("已注册 %s" % must, must in names)

    # ---------------- ② 生成类工具 ----------------
    print()
    print("【2】生成类工具（真跑一遍）")
    txt, ev = call("make_xlsx", {
        "filename": "冒烟测试表",
        "sheets": [{"name": "清单",
                    "columns": ["名称", "数量", "单价"],
                    "rows": [["螺丝", 10, 2.5], ["螺母", 20, 1.2]],
                    "totals": True}]})
    ok = "xlsx" in txt.lower() or "表" in txt
    check("make_xlsx 生成成功", ok and not txt.startswith("EXC"), txt[:70].replace("\n", " "))
    check("make_xlsx 推了 library UI 事件",
          any(e.get("type") == "library" for e in ev))

    txt, ev = call("make_docx", {
        "filename": "冒烟测试文档",
        "blocks": [{"type": "heading", "text": "标题", "level": 1},
                   {"type": "para", "text": "正文 ==高亮== 一段。"},
                   {"type": "bullet", "items": ["要点一", "要点二"]},
                   {"type": "table", "columns": ["A", "B"], "rows": [["1", "2"]]}]})
    check("make_docx 生成成功", "docx" in txt.lower() and not txt.startswith("EXC"),
          txt[:70].replace("\n", " "))
    check("make_docx 推了 library UI 事件",
          any(e.get("type") == "library" for e in ev))

    txt, ev = call("make_pptx", {
        "filename": "冒烟测试幻灯片",
        "slides": [{"layout": "content", "title": "第一页",
                    "bullets": ["要点一", "要点二"]},
                   {"layout": "table", "title": "表格页",
                    "columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]}]})
    check("make_pptx 生成成功", "pptx" in txt.lower() and not txt.startswith("EXC"),
          txt[:70].replace("\n", " "))
    check("make_pptx 推了 library UI 事件",
          any(e.get("type") == "library" for e in ev))

    txt, _ = call("library", {"action": "list"})
    check("文库能列出刚生成的文件",
          "冒烟测试" in txt and not txt.startswith("EXC"), txt[:80].replace("\n", " "))

    txt, _ = call("edit_office", {"action": "inspect", "rel": "冒烟测试文档.docx"})
    check("edit_office 能 inspect 文档", not txt.startswith("EXC"),
          txt[:70].replace("\n", " "))

    # ---------------- ③ 其它工具 ----------------
    print()
    print("【3】其它工具")
    txt, _ = call("get_time")
    check("get_time 正常", not txt.startswith("EXC"), txt.strip()[:40])

    txt, _ = call("workspace_list")
    check("workspace_list 正常", not txt.startswith("EXC"), txt[:50].replace("\n", " "))

    txt, _ = call("search_knowledge", {"query": "测试"})
    check("search_knowledge 正常（知识库空也不报错）", not txt.startswith("EXC"),
          txt[:60].replace("\n", " "))

    txt, _ = call("remember", {"content": "冒烟测试写入的一条记忆"})
    check("remember 正常", not txt.startswith("EXC"), txt[:60].replace("\n", " "))

    txt, _ = call("search_memory", {"query": "冒烟测试"})
    check("search_memory 正常", not txt.startswith("EXC"), txt[:60].replace("\n", " "))

    # 地图工具（离线，只查内置表）
    txt, ev = call("map_plan", {"places": ["汕头大学"]})
    check("map_plan 正常", not txt.startswith("EXC"), txt[:60].replace("\n", " "))
    check("地图 UI 事件带 tile_source",
          any(e.get("type") == "map" and e.get("tile_source") for e in ev))

    txt, ev = call("nearby_places", {"place": "汕头大学", "category": "餐厅",
                                     "radius": 1500})
    check("nearby_places 正常（离线时如实说查不到）",
          not txt.startswith("EXC") and ("离线" in txt or "查到" in txt or "没" in txt),
          txt[:70].replace("\n", " "))

    # ---------------- ④ 开关门控 ----------------
    print()
    print("【4】开关门控")
    off = T.make_schemas(web_enabled=False, kb_enabled=False,
                         code_exec=False, writing=False)
    off_names = [s.get("function", {}).get("name") for s in off]
    check("关掉联网后没有 web_search", "web_search" not in off_names)
    check("关掉知识库后没有 search_knowledge", "search_knowledge" not in off_names)
    check("关掉代码后没有 run_python", "run_python" not in off_names)
    check("地图工具不受这些开关影响", "map_plan" in off_names and "nearby_places" in off_names)

    # ---------------- ⑥ 高德 key 的状态检测 ----------------
    print()
    print("【6】高德 key 状态检测")
    from backend import amap as A
    from backend import config as C
    C.save_config(dict(C.load_config(), amap_key=""))
    h = A.check_health(force=True, online=True)
    check("没配 key 时 configured=False、ok=False",
          h["configured"] is False and h["ok"] is False, h["message"])
    ok, msg = A.verify_key("deadbeefdeadbeefdeadbeefdeadbeef")
    check("无效 key 会被识破，并给出可读的原因", bool(msg) and len(msg) > 10,
          msg.split(chr(10))[0][:44])
    check("长度不对的 key 直接拦下（不浪费一次网络请求）",
          A.verify_key("abc")[0] is False, A.verify_key("abc")[1][:40])
    C.save_config(dict(C.load_config(), amap_key="a" * 32))
    h2 = A.check_health(force=True, online=True)
    check("配了但不可用时 configured=True 且 ok=False（前端据此变暗）",
          h2["configured"] is True and h2["ok"] is False, h2["key_hint"])
    # 断开：**key 必须留着**（用户明确要求"断开后不用重新输入"）
    C.save_config(dict(C.load_config(), amap_key="a" * 32, amap_enabled=False))
    check("断开后 key 还在（stored_key 非空）", bool(A.stored_key()))
    check("断开后 key() 返回空 → 全链路自动退回 OSM", A.key() == "")
    h3 = A.check_health(force=True, online=True)
    check("断开状态：configured=True 且 enabled=False（前端按钮据此变暗）",
          h3["configured"] is True and h3["enabled"] is False, h3["message"])
    C.save_config(dict(C.load_config(), amap_enabled=True))
    h4 = A.check_health(force=True, online=True)
    check("重新连接后 enabled 回来", h4["enabled"] is True)
    A.invalidate_health()

    # ---------------- ⑤ 跑着的应用（HTTP） ----------------
    print()
    print("【5】真实运行的应用（HTTP 接口）")
    BASE = "http://127.0.0.1:8000"
    for path, label in [("/api/frontend/version", "前端版本"),
                        ("/api/config", "配置"),
                        ("/api/map/stats", "地图状态"),
                        ("/api/map/search?q=%E6%B1%95%E5%A4%B4", "地名搜索")]:
        try:
            with urllib.request.urlopen(BASE + path, timeout=15) as r:
                body = r.read().decode("utf-8", "replace")
            check("GET %s（%s）" % (path, label), r.status == 200,
                  "%d 字节" % len(body))
        except Exception as e:
            check("GET %s（%s）" % (path, label), False, str(e)[:60])

    print()
    print("=" * 62)
    if FAIL:
        print("失败 %d 项 / 通过 %d 项" % (FAIL, PASS))
    else:
        print("全部通过 ✔  （%d 项）" % PASS)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
