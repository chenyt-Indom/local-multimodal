# -*- coding: utf-8 -*-
"""把文本（Markdown 风格）转成 .docx —— **只用标准库**，不引第三方依赖。

为什么要有它
------------
用户要"把作文转成 WPS 格式"。说明一下取舍：
- 旧的 `.wps` 是**私有二进制格式**，官方没公开规范，第三方库也写不了，
  硬造一个只会得到一个打不开的文件 —— 那是骗人。
- 而 **WPS Office 原生支持 .docx**（双击就开、能编辑、能再另存为 .wps）。
  所以这里生成 **.docx**，这是既正确又能用的做法。

实现方式：直接拼 OOXML（zip + XML）。和读取那边（doc_extract）对称，
好处是**不引入任何新依赖**，离线包里也不增加体积。

支持的写法（够写作文/报告/方案了）
    # 一级标题      ## 二级标题      ### 三级标题
    - 列表项        * 列表项         1. 列表项
    > 引用
    普通段落（自动识别空行分段）
    行内 **加粗**  和 `等宽代码`
"""
import io
import re
import zipfile
import datetime

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# 默认字体指定了 eastAsia，否则中文在 Word 里会退化成宋体/乱字距
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="%s">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体" w:cs="Times New Roman"/>
<w:sz w:val="24"/><w:szCs w:val="24"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="240"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="44"/><w:rFonts w:eastAsia="黑体"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/><w:rFonts w:eastAsia="黑体"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:pPr><w:spacing w:before="200" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/><w:rFonts w:eastAsia="黑体"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:pPr><w:spacing w:before="160" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/><w:rFonts w:eastAsia="黑体"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>
<w:pPr><w:ind w:left="420" w:hanging="210"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:pPr><w:ind w:left="420"/></w:pPr>
<w:rPr><w:i/><w:color w:val="595959"/></w:rPr></w:style>
</w:styles>""" % W


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _runs(text: str, mono_all: bool = False) -> str:
    """把一行文本转成若干个 run，支持 `**加粗**` 与 `` `等宽` ``。"""
    out = []
    # 按 **加粗** 和 `代码` 切分；不追求完整 Markdown，够用即可
    for piece in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text or ""):
        if not piece:
            continue
        bold = piece.startswith("**") and piece.endswith("**") and len(piece) > 4
        mono = piece.startswith("`") and piece.endswith("`") and len(piece) > 2
        body = piece[2:-2] if bold else (piece[1:-1] if mono else piece)
        rpr = ""
        rfonts = ""
        if bold:
            rpr += "<w:b/>"
        if mono or mono_all:
            rpr += "<w:rFonts w:ascii=\"Consolas\" w:hAnsi=\"Consolas\" w:eastAsia=\"宋体\"/><w:shd w:val=\"clear\" w:fill=\"F2F2F2\"/>"
        if rpr:
            rpr = "<w:rPr>" + rpr + "</w:rPr>"
        out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, _esc(body)))
    return "".join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _para(text: str, style: str = "") -> str:
    ppr = ('<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style) if style else ""
    return "<w:p>%s%s</w:p>" % (ppr, _runs(text))


def _split_blocks(text: str):
    """把文本切成块，并识别围栏代码块（代码块保持原样、等宽显示）。"""
    blocks, in_code, code_buf = [], False, []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                blocks.append(("code", "\n".join(code_buf)))
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue
        blocks.append(("line", line))
    if code_buf:
        blocks.append(("code", "\n".join(code_buf)))
    return blocks


def text_to_docx(text: str, title: str = "") -> bytes:
    """把 Markdown 风格文本转成 .docx 字节流。"""
    body = []
    if title:
        body.append(_para(title, "Title"))
    for kind, payload in _split_blocks(text):
        if kind == "code":
            for ln in payload.split("\n"):
                body.append("<w:p><w:pPr><w:pStyle w:val=\"ListParagraph\"/></w:pPr>%s</w:p>"
                            % _runs(ln, mono_all=True))
            continue
        line = payload.rstrip()
        s = line.strip()
        if not s:
            body.append("<w:p/>")
            continue
        if s.startswith("### "):
            body.append(_para(s[4:], "Heading3"))
        elif s.startswith("## "):
            body.append(_para(s[3:], "Heading2"))
        elif s.startswith("# "):
            body.append(_para(s[2:], "Heading1"))
        elif s.startswith("> "):
            body.append(_para(s[2:], "Quote"))
        elif re.match(r"^[-*+]\s+", s):
            body.append(_para("• " + re.sub(r"^[-*+]\s+", "", s), "ListParagraph"))
        elif re.match(r"^\d+[.)]\s+", s):
            body.append(_para(s, "ListParagraph"))
        elif s.startswith("---") or s.startswith("***"):
            body.append(_para("—" * 20, "Quote"))
        else:
            body.append(_para(s))
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<w:document xmlns:w="%s"><w:body>%s%s</w:body></w:document>'
           % (W, "".join(body), sect))
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>%s</dc:title><dc:creator>本地多模态助手</dc:creator>'
            '<cp:lastModifiedBy>本地多模态助手</cp:lastModifiedBy>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            '</cp:coreProperties>' % (_esc(title or "文档"), now, now))
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>本地多模态助手</Application></Properties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
    return buf.getvalue()
