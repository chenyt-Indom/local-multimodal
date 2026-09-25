# -*- coding: utf-8 -*-
"""回归测试：假产物（HTML 冒充 pptx）与幻觉下载链接。

背景（2026-09-25 用户报「模型生成的 PPT 打不开」）：
    `data/library/文档/学生工作/入团申请书.pptx` 只有 1517 字节，
    文件头是 `<p s` —— 里面是一段 **HTML**，根本不是 pptx。
    两条根因：
      ① `doclib.write_file()` 是纯文本写入、**不校验扩展名**，
         模型把 HTML 当文本写进 .pptx 名字里，静默成功；
      ② 模型**凭空捏造产物**：回「✅已为您完成…共 12 页」+ 假的下载链接，
         可它一个工具都没调，文库目录是空的。

跑法：python test_fake_artifact.py
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.parse
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_fake_")
CFG = os.path.join(ROOT, "config.json")
import json                                                          # noqa: E402
cfg = json.load(open(CFG, encoding="utf-8"))
json.dump(cfg, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import doclib                                           # noqa: E402
from backend import main as M                                        # noqa: E402
from backend import tools as T                                       # noqa: E402

PASS = FAIL = 0


def check(n, ok, d=""):
    global PASS, FAIL
    print(("  [OK] " if ok else "  [!!] ") + n + ("  " + str(d) if d else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


HTML = ('<p style="text-align:center; font-size:34px">陈宇桐同志入团申请报告</p>'
        '<table border=1><tr><td>基本信息</td></tr></table>')

print("=" * 58)
print("假产物与幻觉链接")
print("=" * 58)

print("\n【一】文本不许写办公文件（写了就是打不开的假货）")
for rel in ("文档/学生工作/入团申请书.pptx", "报告.docx", "数据.xlsx",
            "旧格式.ppt", "旧格式.doc", "旧格式.xls"):
    r = doclib.write_file(rel, HTML)
    check("拒绝 %s" % rel, not r.get("ok"),
          (r.get("error") or "")[:38].replace("\n", " "))
    check("  且没在磁盘上留下这个文件", not os.path.isfile(doclib.file_path(rel)))

print("\n【二】非办公文件照常可写（别误伤）")
for rel, text in (("笔记.md", "# 标题\n正文"), ("a.py", "print(1)"),
                  ("data.csv", "a,b\n1,2"), ("报告.txt", "纯文本")):
    r = doclib.write_file(rel, text)
    check("可写 %s" % rel, bool(r.get("ok")), r.get("rel") or r.get("error"))

print("\n【三】真产物必须是真 pptx（走的是二进制通道，不能被上面拦住）")
ev = []
T.dispatch("make_pptx", {"title": "测试演示稿",
                         "slides": [{"layout": "content", "title": "第一页",
                                     "bullets": ["要点一", "要点二"]}]}, ev, {})
import glob                                                          # noqa: E402
pptx = glob.glob(os.path.join(TMP, "**", "*.pptx"), recursive=True)
check("make_pptx 产出了文件", bool(pptx), "%d 个" % len(pptx))
check("产物是**真** pptx（zip 格式）", bool(pptx) and all(zipfile.is_zipfile(p) for p in pptx),
      [(os.path.basename(p), zipfile.is_zipfile(p)) for p in pptx])
real_rel = os.path.relpath(pptx[0], os.path.join(TMP, "data", "library")).replace(os.sep, "/") if pptx else ""

print("\n【四】正文里声称的下载链接要逐个核对（幻觉链接）")


def link(rel):
    return "/api/doclib/download?rel=" + urllib.parse.quote(rel)


text = ("已完成！%s 和 %s 和 %s" % (
    link(real_rel or "真的.pptx"),
    link("文档/学生工作/假的入团申请书.pptx"),
    link("伪造的报告.docx")))
fakes = M._fake_doclib_links(text)
check("真的文件**没有**被误报", (real_rel not in fakes) and ("真的.pptx" not in fakes) if real_rel else True,
      "误报: %s" % [f for f in fakes if real_rel and f == real_rel])
check("假的文件全部被抓到",
      ("文档/学生工作/假的入团申请书.pptx" in fakes) and ("伪造的报告.docx" in fakes),
      fakes)
check("percent-encoded 的中文名能正确还原",
      "伪造的报告.docx" in fakes, "（未 unquote 的话这里会是 %E4%BC%AA...）")
check("正文没有链接时返回空", M._fake_doclib_links("没有链接的一段话") == [])

print("\n" + "=" * 58)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 58)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
