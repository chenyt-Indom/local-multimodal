# -*- coding: utf-8 -*-
"""Agent 工具注册表 —— 让 Qwen 具备主动调用能力。

工具分三类：
- 文生图：generate_image（对话中让大模型调用；中文 prompt 自动翻译成英文解决偏离问题）
- 文件系统：list_directory / read_file / search_files / write_file / append_file
- 记忆：memory_store / memory_search

每个工具执行后返回一段文本给模型（模型据此继续思考），
若有需要前端展示的副作用（例如生成的图片），则推入 ui_events，
由后端流式传给前端渲染。
完全本地运行。
"""
from __future__ import annotations
import os
import re
import sys
import time
import glob
import json
import logging
import urllib.parse

from . import t2i
from . import file_tools
from . import memory as memory_mod
from . import doclib as library_mod
from . import docx_write


# =====================================================================
#  工具 Schema（发给模型）
# =====================================================================
# =====================================================================
#  开发工作区工具（人机协同开发）
# =====================================================================
# 为什么必须单独给"工作区"工具，而不是让模型用 read_file/write_file 传绝对路径：
# 那样模型得先猜出工作区在哪，实测它猜不准，还会把文件写到应用安装目录里去。
# 工作区工具的路径一律是**相对路径**，由后端拼，模型不可能写到外面。
_WS_PACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_pack",
        "description": ("【打包】把当前项目的**所有文件**打成一个 zip，给用户一个能直接点的下载链接。"
                        "用户说「打包 / 导出 / 我要拿走 / 发我一份」时用它。"
                        "返回里带下载链接，**原样告诉用户**即可。"),
        "parameters": {"type": "object", "properties": {}},
    },
}

# ---------- PPT 生成 ----------
# 用户 2026-09-17 提的需求：「模型能生成 PPT 吗？面向各领域的都要能排。」
# 设计要点：**模型只填内容，不写代码** —— 8B 模型即兴写 python-pptx 代码
# 又慢又容易错，每次效果还不一样。这里让它输出「标题 + 要点」，排版交给
# backend/pptx_maker.py，稳定性高一个量级。
_MAKE_PPTX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "make_pptx",
        "description": (
            "【做PPT】把内容排成一份真正的 .pptx 演示文稿，返回可直接点击的下载链接。"
            "用户说「做个PPT / 写个演示稿 / 汇报材料 / 答辩PPT / 方案演示 / 讲解稿」时，"
            "**第一个就该想到本工具**。\n"
            "⚠️ 标题和内容**必须严格按用户这一轮说的主题**写，别被记忆或前文里的别的主题带跑。\n"
            "⚠️ 生成后只把**下载链接**给用户，**不要描述界面操作步骤**（没有那些按钮）。\n"
            "**你只管想内容**，排版由工具完成，不用写代码。\n"
            "· 每页用 layout 选版式（不写就按内容自动判断）：\n"
            "  content 标题+要点(默认) / two_col 左右两栏(用 left/right，可配 left_title/right_title)\n"
            "  / image_right|image_left 图文并排 / image_full 整页大图\n"
            "  / table 表格(给 table:{header,rows}) / **chart 图表**\n"
            "    (给 chart:{kind:\"bar|barh|line|pie|doughnut|area\", categories:[...],\n"
            "     series:[{name,values}], labels:true 显示数值, title 图表标题} —— 柱状/条形/折线/饼图/圆环/面积)\n"
            "  / cards 卡片组(给 cards:[{title,text}])\n"
            "  / stats 大数字(给 stats:[{value,label}]) / steps 流程步骤(给 steps:[{title,text}])\n"
            "  / timeline 时间线(给 items:[{title,text}]) / quote 整页引言(给 quote:{text,from})\n"
            "  / toc 目录(给 items 字符串数组) / section 章节过渡页(只要 title)\n"
            "· 配图：每页写 image（本地路径 / 图片库 id / 网图网址），"
            "**或者只写 image_query 让它自动联网搜一张合适的图插进去** ——\n"
            "  用户说「配点图 / 图文并茂 / 找张图放上去」时，就给相关页写 image_query。\n"
            "· 装饰：每页可加 decor，可选 page_number / band 侧边色带 / corner 角标圆 / dots 圆点；\n"
            "  还可写 bg_image（整页背景图，会自动压一层半透明蒙版保证文字可读）、"
            "logo（本页角落小图标）。全篇统一加 logo 用顶层 logo 参数。\n"
            "· 微调：每页可直接写 accent 强调色 / bg 底色 / title_color / title_size / "
            "body_size / card_bg / title_align（也可放进 style 对象里）。\n"
            "· 单条要点微调：bullets 里可以写 {\"text\":\"...\",\"bold\":true,\"color\":\"C53030\","
            "\"size\":18} 这样的对象，只影响那一条。\n"
            "· **标注**（用户说「标出重点 / 加标注 / 突出一下」时用）："
            "要点对象加 \"hl\": true 会加荧光笔高亮，想指定颜色就写 \"hl\":\"FFD9D9\"；"
            "每页还能写 badge（右上角小标签，如「重点」「必考」「KPI」）、"
            "caption（页脚小字题注，如「数据来源：…」「图 1 系统架构」）。\n"
            "· 要点以「- 」或两个空格开头＝二级条目。配色 blue/green/warm/purple/mono/red。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "封面主标题"},
                "subtitle": {"type": "string", "description": "封面副标题，可选，一句话点题"},
                "author": {"type": "string", "description": "封面落款，如「姓名 · 单位 · 日期」，可选"},
                "theme": {"type": "string",
                          "description": "配色：blue(默认) / green / warm / purple / mono / red"},
                "filename": {"type": "string",
                             "description": "保存的文件名，不用带 .pptx 后缀；不给就用标题"},
                "end_text": {"type": "string", "description": "结尾页文字，默认「谢谢观看」"},
                "cover_image": {"type": "string",
                                "description": "封面整页背景图（路径 / 图片库 id / 网址 / 搜索词）"},
                "logo": {"type": "string",
                         "description": "每页角落的 logo 小图（路径 / 图片库 id / 网址）"},
                "logo_pos": {"type": "string",
                             "description": "logo 位置：tr 右上(默认) / tl 左上 / br 右下 / bl 左下"},
                "slides": {
                    "type": "array",
                    "description": "每一页，按顺序排。建议 5~15 页，每页要点 3~6 条。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "这一页的标题"},
                            "layout": {"type": "string",
                                       "description": "版式，见工具说明；不写按内容自动判断"},
                            "bullets": {
                                "type": "array",
                                "description": "要点列表；字符串，或 {text,bold,color,size} 对象",
                                "items": {"type": "string"},
                            },
                            "left": {"type": "array", "items": {"type": "string"},
                                     "description": "two_col 左栏要点"},
                            "right": {"type": "array", "items": {"type": "string"},
                                      "description": "two_col 右栏要点"},
                            "left_title": {"type": "string", "description": "two_col 左栏小标题"},
                            "right_title": {"type": "string", "description": "two_col 右栏小标题"},
                            "image": {"type": "string",
                                      "description": "图片：本地路径 / 图片库 id / 网图网址 / "
                                                     "文库里的文件名"},
                            "image_query": {"type": "string",
                                            "description": "**没图但想要图**时的联网搜索词，"
                                                           "如「校园 图书交换 活动」。"
                                                           "系统会自动搜一张插进来"},
                            "bg_image": {"type": "string",
                                         "description": "整页背景图（同上来源）；"
                                                        "会自动加蒙版，文字转白色"},
                            "logo": {"type": "string", "description": "本页角落 logo，可选"},
                            "image_caption": {"type": "string", "description": "图片说明，可选"},
                            "badge": {"type": "string",
                                      "description": "右上角小标签，如「重点」「必考」「KPI」，可选"},
                            "caption": {"type": "string",
                                        "description": "页脚小字题注，如「数据来源：…」"
                                                       "「图 1 系统架构」，可选"},
                            "table": {"type": "object",
                                      "description": "layout=table 时用：{header:[...],rows:[[...]]}"},
                            "chart": {"type": "object",
                                      "description": "layout=chart 时用：{kind,categories,"
                                                     "series:[{name,values}],labels,title}"},
                            "cards": {"type": "array", "description":
                                      "layout=cards 时用：[{title,text}]，2~4 张"},
                            "stats": {"type": "array", "description":
                                      "layout=stats 时用：[{value,label}]，2~4 个"},
                            "steps": {"type": "array", "description":
                                      "layout=steps 时用：[{title,text}]，2~5 步"},
                            "items": {"type": "array",
                                      "description": "layout=timeline/toc 时用，见工具说明"},
                            "quote": {"type": "object",
                                      "description": "layout=quote 时用：{text,from}"},
                            "decor": {"type": "array", "items": {"type": "string"},
                                      "description": "装饰：page_number/band/corner/dots"},
                            "accent": {"type": "string", "description": "本页强调色，如 C53030"},
                            "bg": {"type": "string", "description": "本页底色，如 FFF9F9"},
                            "notes": {"type": "string", "description": "演讲者备注，可选"},
                            "section": {"type": "boolean",
                                        "description": "true＝章节过渡页（只有大标题）"},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
    },
}

# ---------- Word 文档生成 ----------
_MAP_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "map_plan",
        "description": (
            "【地图】查地点、规划路线，并把结果**画成一张地图卡片**发给用户看。\n"
            "用户说「帮我规划路线 / 怎么走 / 从A到B多远多久 / 找一下某个地方在哪 / "
            "这几个地方在地图上的位置」时用它。\n"
            "· 只查地点：给 places:[\"广州塔\", \"汕头大学\"]（会自动找到坐标并在地图上打点）。\n"
            "· 只规划路线：给 route:{\"from\":\"广州塔\",\"to\":\"广州白云机场\",\"mode\":\"driving\"}。\n"
            "· 两者可以一起给：先把途经点标出来，再画路线。\n"
            "· mode 可选 driving(驾车，默认) / foot(步行) / bike(骑行) / transit(公交)。\n"
            "  · **transit** 走高德公交换乘，返回**具体线路、票价、换乘站** —— "
            "用户问「坐几路车 / 怎么换乘 / 公交多久」时用它。\n"
            "  · 配了高德 key 时：驾车带**实时路况**，步行/骑行是**真实路径**。\n"
            "  · 没配 key（自动回退 OSRM）时：免费路网只给驾车路径、步行骑行时间是估算，"
            "返回里会带 note，**必须把这话转告用户**，不能让他以为是真实步行路线。\n"
            "· 中长途会自动给**多条备选路线**并挑一条推荐，返回里带 routes 与 reason。"
            "把「为什么推荐这条」照实说出来（那是真实差距，别自己加戏）；只有一条可行路线时也别硬凑。\n"
            "· 用户问天气、或要把出行情况讲清楚时给 weather:true。\n"
            "· **用户说了在哪个城市时，一定把 city 填上**（比如「广州市内有什么商场」"
            "→ city:\"广州\"）。不填的后果实测很离谱：往地图上标「天河城」会被解析到"
            "「江西省南昌市进贤县天河城」—— 那儿真有个同名村子。\n"
            "· 返回里有真实距离与用时，**照实念给用户，不要自己算**。\n"
            "⚠️ **只有用 mode=transit 拿到的东西才能讲公共交通**。没走 transit 就"
            "不许编「坐 X 路公交、票价 Y 元、每 Z 分钟一班」这种具体线路（实测模型真的会编）；"
            "最多说一句「这段距离也可以考虑公共交通」。\n"
            "⚠️ 驾车时间在配了高德 key 时**已经包含实时路况**，可以照实说「这个时间已含实时路况」；"
            "但**不要**编「XX 路段现在堵」这种具体路况描述 —— 接口不给这个。"
            "地图卡片会自己显示，你只要用一两句话把结论说清楚。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "places": {"type": "array", "items": {"type": "string"},
                           "description": "要在地图上标出的地点名（中文即可），可选"},
                "city": {"type": "string",
                         "description": "限定城市（如「广州」）。用户提到具体城市时**务必填**，"
                                        "用来防止同名地点被解析到外省，可选"},
                "route": {"type": "object",
                          "description": "{from: 起点地名（或 lat,lon）, to: 终点, "
                                         "mode: driving/foot/bike}"},
                "zoom": {"type": "integer", "description": "地图缩放级别 3~18，默认自动"},
                "weather": {"type": "boolean",
                            "description": "true＝同时查出发地此刻与预计抵达时段的天气"},
            },
            "required": [],
        },
    },
}


_NEARBY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "nearby_places",
        "description": (
            "【地图】查某个地点**周围**有哪些场所（按半径+类别），并在图上标出来。\n"
            "用户说「附近有什么吃的 / 这周围有没有便利店 / 附近哪能买药 / "
            "找一下附近的银行」这类**周边搜索**时用它。\n"
            "· 和 map_plan 的分工：map_plan 是「我要去某地，怎么走」；"
            "nearby_places 是「这一带有什么」。\n"
            "· **凡是问「哪里有什么 XX / 有哪些 YY」的，都用这个工具去查真实数据**，"
            "不要凭自己知道的城市地标凑几个扔给 map_plan —— 那是编的，而且经常给错。\n"
            "  「广州市内有什么商场」→ place:\"广州市\", category:\"购物中心\", radius:30000；"
            "「汕头有什么好吃的」→ place:\"汕头市\", category:\"餐厅\", radius:20000。\n"
            "· 参数 {\"place\": \"汕头大学\", \"category\": \"餐厅\", \"radius\": 1500}；"
            "place 也接受 \"23.35,116.68\" 这种坐标，也可以是城市名。\n"
            "· category 用中文日常说法即可：餐厅 / 咖啡馆 / 便利店 / 超市 / 购物中心 / "
            "药店 / 医院 / 银行 / 加油站 / 停车场 / 酒店 / 学校 / 公交站 / 公园 / 厕所…\n"
            "· radius 单位米，默认 1500，最大 50000。\n"
            "  问「整座城市」就给 20000~40000；问「XX 附近」给 1000~3000。\n"
            "  「整个城市里有什么」填 20000~50000；「某学校附近有什么」填 1000~3000。\n"
            "⚠️⚠️ **不要为了找「评分」改用 web_search** —— 本工具在高德模式下"
            "**直接返回真实评分（rating）和人均消费（cost）**。"
            "用户问「附近评分最高的餐厅 / 哪家评价好」时，"
            "**就用本工具**（可以把 radius 放大到 3000 拿更多候选），"
            "拿到结果后按 rating 从高到低排，**不要跑去联网搜点评网站**。\n"
            "· 数据源：配了高德 key 时走**高德地图**（POI 全、**有真实评分和人均消费**）；"
            "没配或离线时回退 OpenStreetMap（**没有评分**）。\n"
            "⚠️ **返回里带 rating 就照实念** —— 那是真实评分，可以直接用来推荐和排序；"
            "带 cost 的是人均消费。\n"
            "⚠️ **没有 rating 字段时绝不许自己编评分、星级或评价**"
            "（「评分 4.5」「口碑很好」「味道不错」这类就是编的）。\n"
            "⚠️ **只准转述返回里确实有的字段**（名字/距离/评分/人均/地址/电话/营业时间），"
            "**不要补数据里没有的东西** —— 实测模型会顺口加「校内主干道旁」"
            "「校门对面」这类位置描述和「学生常去」这类评价，那全是编的。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string",
                          "description": "中心地点名（中文即可），或 \"纬度,经度\""},
                "category": {"type": "string",
                             "description": "要找什么：餐厅/便利店/药店/银行…"},
                "radius": {"type": "integer", "description": "半径（米），默认 1500"},
                "limit": {"type": "integer", "description": "最多返回几个，默认 20"},
            },
            "required": ["place", "category"],
        },
    },
}


_MAKE_XLSX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "make_xlsx",
        "description": (
            "【做表格】把数据排成一份真正的 .xlsx（Excel / WPS 都能打开），返回可点下载链接。\n"
            "用户说「做个表格 / Excel / 统计表 / 对比表 / 数据表 / 报表 / 汇总表 / 台账 / 预算表」"
            "或给了你一堆数据要整理时，用它。\n"
            "**你只填数据，不用写代码。** 排版（表头配色、隔行底纹、冻结首行、筛选、合计行、"
            "数字/货币/百分比格式、数据条、图表）都由工具做。\n"
            "· 一个工作簿可放多张表：给 sheets 数组，每张表一个 name。\n"
            "· 每张表：header（表头）+ rows（二维数组）。数字就写数字（别加千分位或￥符号）。\n"
            "· formats：每列的格式关键字 —— text / int / number / money / percent / date；"
            "也可以直接写 Excel 格式串。\n"
            "· 想要合计行给 total_row:true（默认会跳过「单价/比率」这类不该求和的列，"
            "也可以自己指定 total_cols:[列号…]）；\n"
            "· 想要图表给 chart:{kind: column|line|pie|doughnut|area, title, "
            "categories_col: 按哪列分类, value_cols:[数值列号…]}；\n"
            "· 还有 freeze（冻结首行）/ autofilter（筛选）/ zebra（隔行底色）/ "
            "conditional:{col,type:data_bar|3_color_scale|duplicate} / title（大标题，跨列合并）/ "
            "note（表下方小字说明，如数据来源）。\n"
            "配色主题 theme：blue(默认) / green / warm / purple / mono / red。"
            "生成后把返回的下载链接**原样**给用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "保存的文件名，不用带 .xlsx；不给就用 title"},
                "theme": {"type": "string",
                          "description": "配色：blue(默认) / green / warm / purple / mono / red"},
                "sheets": {
                    "type": "array",
                    "description": "一个工作表一项，按顺序排。先给明细表、再给汇总表（可选）。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "工作表名（页签名）"},
                            "title": {"type": "string",
                                      "description": "表内大标题（跨列合并的粗体一行），可选"},
                            "header": {"type": "array", "items": {"type": "string"},
                                       "description": "表头文字"},
                            "rows": {"type": "array",
                                     "description": "数据行，每行一个数组；数字写数字类型",
                                     "items": {"type": "array"}},
                            "formats": {"type": "array", "items": {"type": "string"},
                                        "description": "每列格式：text/int/number/money/"
                                                       "percent/date，或直接写 Excel 格式串"},
                            "widths": {"type": "array", "items": {"type": "number"},
                                       "description": "每列列宽（字符数），不给就自动估算"},
                            "total_row": {"type": "boolean", "description": "是否加合计行"},
                            "total_cols": {"type": "array", "items": {"type": "integer"},
                                           "description": "只对这些列求和（列号从 0 开始）"},
                            "total_label": {"type": "string", "description": "合计行文字，默认「合计」"},
                            "note": {"type": "string", "description": "表下方小字说明，如「数据来源：…」"},
                            "chart": {"type": "object",
                                      "description": "{kind: column|line|pie|doughnut|area, "
                                                     "title, categories_col, value_cols:[…], "
                                                     "position:'A12' 可选（默认放表下方）}"},
                            "conditional": {"type": "object",
                                            "description": "{col: 列号, type: data_bar|"
                                                           "3_color_scale|duplicate}"},
                            "freeze": {"type": "boolean", "description": "冻结表头（默认 true）"},
                            "autofilter": {"type": "boolean", "description": "加筛选（默认 true）"},
                            "zebra": {"type": "boolean", "description": "隔行浅底色（默认 true）"},
                        },
                        "required": ["header", "rows"],
                    },
                },
            },
            "required": ["sheets"],
        },
    },
}


_MAKE_DOCX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "make_docx",
        "description": (
            "【写文档】把内容排成一份真正的 .docx（Word）文档，返回可直接点击的下载链接。"
            "用户说「写个文档 / 报告 / 方案 / 总结 / 说明书 / 通知 / 论文 / 材料 / 写成 Word」时，"
            "**第一个就该想到本工具**。\n"
            "⚠️ 不要用 `library` 先写 .md 再让用户自己导出 —— 本工具一次成型，直接给成品。\n"
            "⚠️ 生成后只把**下载链接**给用户，**不要描述界面操作步骤**（没有那些按钮）。\n"
            "**你只管想内容**，排版由工具完成，不用写代码。\n"
            "blocks 是内容块列表，按顺序排，每块一个 type：\n"
            "  heading 标题（配 level 1~4）/ para 段落（text）\n"
            "  bullet 无序列表（items 数组）/ number 有序列表（items）\n"
            "  quote 引用（text + 可选 from）/ callout 提示框（tag + text，带底色和色条）\n"
            "  table 表格（header 数组 + rows 二维数组 + 可选 caption）\n"
            "  image 图片（src 给路径/图片库 id/网图网址；**没有图就改给 query 搜索词**，\n"
            "        系统会联网搜一张插进来，可配 caption 图注、style.width 控宽度 cm）\n"
            "  / code 代码块（text）/ divider 分隔线\n"
            "  pagebreak 分页 / toc 目录 / end 结束语\n"
            "· 正文里可用 **加粗**、*斜体*、`等宽`、==高亮== 做局部强调"
            "（==高亮== 是荧光笔，用来标出重点句）。\n"
            "· 列表里以「- 」或两个空格开头＝二级条目。\n"
            "· 每一块都能带 style 做微调：size 字号 / color 颜色 / bold / align"
            "(left|center|right|justify) / indent 缩进 / bg 底色 / line 行距。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "文档标题"},
                "subtitle": {"type": "string", "description": "副标题，做封面时用"},
                "author": {"type": "string", "description": "署名，如「姓名 · 单位」"},
                "date_text": {"type": "string", "description": "落款日期文字，如「2026 年 9 月」"},
                "theme": {"type": "string",
                          "description": "配色：blue(默认) / green / warm / purple / mono / red"},
                "font": {"type": "string",
                         "description": "字体：yahei 雅黑(默认) / song 宋体正文+黑体标题 / kai 楷体"},
                "filename": {"type": "string", "description": "文件名，不用带 .docx 后缀"},
                "cover": {"type": "boolean", "description": "是否生成封面页（标题居中）"},
                "header": {"type": "string", "description": "页眉文字，可选"},
                "logo": {"type": "string",
                         "description": "页眉右侧的 logo 小图（路径 / 图片库 id / 网址），可选"},
                "toc": {"type": "boolean",
                        "description": "是否插入目录页（打开文档时自动生成）"},
                "blocks": {
                    "type": "array",
                    "description": "内容块列表，见工具说明",
                    "items": {"type": "object",
                              "properties": {"type": {"type": "string"}},
                              "required": ["type"]},
                },
            },
            "required": ["title", "blocks"],
        },
    },
}

# ---------- 已有文档 / PPT 的读取与修改 ----------
_EDIT_OFFICE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_office",
        "description": (
            "【改文档/改PPT】查看和修改生成文库里的 .docx / .pptx。"
            "用户说「改一下这个PPT / 这份文档」「把第3页标题换掉」「加一页」「换个配色」"
            "「标题改大一点」「正文加一段」时用它。\n"
            "⚠️ **改之前必须先 action=inspect 看结构**：它会列出每一页/每一段的编号和现有文字，"
            "ops 里的编号就用它给的那些，别自己猜（猜错就改到别的地方）。\n"
            "action=edit 时给 ops，常用操作：\n"
            "  {\"op\":\"replace_text\",\"find\":\"旧字\",\"replace\":\"新字\",\"slide\":3}  全文或指定页查找替换\n"
            "  {\"op\":\"set_text\",\"slide\":3,\"shape\":1,\"text\":\"新文字\"}  改某个元素的文字\n"
            "  {\"op\":\"set_text_style\",\"slide\":3,\"shape\":1,\"size\":34,\"color\":\"C53030\",\"bold\":true}\n"
            "  {\"op\":\"add_text\",\"slide\":3,\"text\":\"...\",\"x\":0.9,\"y\":6.3,\"w\":6,\"h\":0.5,\"size\":16,\"color\":\"...\",\"align\":\"center\"}\n"
            "  {\"op\":\"add_image\",\"slide\":3,\"src\":\"图片路径|图片库id|网图网址\",\"x\":1,\"y\":1.5,\"w\":5,\"h\":4}\n"
            "    也可以不给 src 而给 {\"query\":\"搜索词\"} → 自动联网搜一张插进去\n"
            "  {\"op\":\"add_shape\",\"slide\":3,\"kind\":\"rect|round|oval\",\"x\":1,\"y\":1,\"w\":2,\"h\":1,\"fill\":\"2E75B6\"}\n"
            "  {\"op\":\"delete_shape\",\"slide\":3,\"shape\":2} / {\"op\":\"set_bg\",\"slide\":3,\"color\":\"FFF7E6\"}\n"
            "  {\"op\":\"set_notes\",\"slide\":3,\"text\":\"备注\"} / {\"op\":\"set_theme\",\"theme\":\"green\"} 整份换配色\n"
            "  {\"op\":\"duplicate_slide\",\"slide\":3} / {\"op\":\"move_slide\",\"slide\":3,\"to\":1} / {\"op\":\"delete_slide\",\"slide\":3}\n"
            "  {\"op\":\"add_slide\",\"slide_spec\":{...和 make_pptx 的一页同格式...}}\n"
            "Word 文档用它：\n"
            "  {\"op\":\"replace_text\",\"find\":\"旧\",\"replace\":\"新\"} / {\"op\":\"set_para\",\"index\":12,\"text\":\"新内容\"}\n"
            "  {\"op\":\"set_para_style\",\"index\":4,\"size\":26,\"color\":\"2D6A4F\",\"bold\":true,\"align\":\"center\"}\n"
            "  {\"op\":\"insert_para\",\"after\":20,\"block\":{...和 make_docx 的块同格式...}}\n"
            "  {\"op\":\"append\",\"block\":{...}} / {\"op\":\"delete_para\",\"index\":30} / "
            "{\"op\":\"set_header\",\"text\":\"...\"}\n"
            "改完把结果原样告诉用户（工具会返回改了什么）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rel": {"type": "string",
                        "description": "生成文库里的文件名，带扩展名，如「机器学习入门.pptx」"},
                "action": {"type": "string",
                           "description": "inspect＝只看结构（不改），edit＝执行修改"},
                "ops": {"type": "array",
                        "description": "修改操作清单，见工具说明",
                        "items": {"type": "object"}},
            },
            "required": ["rel"],
        },
    },
}

_WS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_list",
        "description": ("【开发工作区】列出工作区里现有的文件（人机协同开发的项目目录）。"
                        "要动某个已有文件之前，先用它看清有哪些文件。"),
        "parameters": {"type": "object", "properties": {}},
    },
}

_WS_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_read",
        "description": ("【开发工作区】读取工作区里的一个文件（传**相对路径**，如 app.py）。"
                        "⚠️ 修改任何已有文件之前**必须先读它**，在真实内容上改 —— "
                        "用户可能刚在前端的开发台里手改过，凭记忆重写会把他的改动冲掉。"),
        "parameters": {"type": "object",
                       "properties": {"rel": {"type": "string",
                                              "description": "工作区内的相对路径，如 src/app.py"}},
                       "required": ["rel"]},
    },
}

_WS_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_write",
        "description": ("【开发工作区】把内容写进工作区文件（存在则覆盖，旧版自动进回收站）。"
                        "**要给整份文件内容**，不要只给片段。写完用户会在「开发台」里看到。"
                        "⚠️ **只用来写「非代码」内容（README、配置、数据、txt/md/json），"
                        "或者你自己动手改一两行**；"
                        "**要写/改一个代码文件（.py/.js/.ts/.html/.java…）请改用 write_code** ——"
                        "由本机专用代码模型来写，代码质量和长度都更有保障。"),
        "parameters": {"type": "object",
                       "properties": {
                           "rel": {"type": "string", "description": "工作区内的相对路径"},
                           "text": {"type": "string", "description": "文件的完整内容"}},
                       "required": ["rel", "text"]},
    },
}


_WS_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_code",
        "description": (
            "【写代码专用 · **只要目标文件是代码（.py/.js/.ts/.html/.java/.go/.rs…）就用它**】"
            "把「要写什么」交给本机的**专用代码模型**，由它写出代码并**直接落到项目文件**。"
            "**你不需要、也不应该自己把代码敲出来** —— 你只负责决定「写哪个文件、要什么功能」。"
            "改已有文件时：先 workspace_read 读一遍，再在这里说明要改成什么样。"
            "**写完它会顺手跑一遍并把真实输出一起还给你**，"
            "所以你**不用再单独调 workspace_run**（多绕一轮要多花一分钟）。"
            "只有它跑失败了、你要换个方式再试时才自己调。"
            "⚠️ 报错要重写时：把**完整报错 + 要改成什么样**一次说清再调，"
            "**最多重写 2 次**；之后如实说明哪里还不行，别无限重试。"
            "（例外：只改一两个字符、或写 README/配置/数据这类非代码文件，才用 workspace_write。）"),
        "parameters": {"type": "object",
                       "properties": {
                           "rel": {"type": "string",
                                   "description": "要写入的项目内相对路径，如 app.py"},
                           "instruction": {"type": "string",
                                           "description": "需求：这个文件要做什么 / 要改成什么样。说清楚。"},
                           "context": {"type": "string",
                                       "description": "可选：补充背景（相关接口、数据格式、约束等）"},
                           "run": {"type": "boolean",
                                   "description": "写完是否顺手跑一遍（.py 默认 true）。"
                                                  "真实运行结果会一并返回给你，"
                                                  "所以**不要再单独调 workspace_run**。"}},
                       "required": ["rel", "instruction"]},
    },
}


_WS_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_run",
        "description": ("【开发工作区】运行工作区里的一个 .py 文件，拿到**真实输出与报错**。"
                        "工作目录就是该文件所在目录，所以脚本里的相对路径是对的。"
                        "写完文件后**用它验证**，不要凭空猜运行结果。"
                        "⚠️ 它只能跑「算完就退出」的程序（默认最多 60 秒）。"
                        "**计时器 / 服务器 / 游戏 / 图形界面这类要一直跑的程序不要用它跑完整** ——"
                        "工具只会跑前几秒做冒烟测试，**那不是报错、也不说明代码有问题**，"
                        "更**不要为了绕过它去改代码**（加线程、加 signal 都没用）。"
                        "这种程序写好直接交给用户，让用户点代码卡片上的「▶ 运行」（那里不设时限）。"),
        "parameters": {"type": "object",
                       "properties": {
                           "rel": {"type": "string",
                                   "description": "工作区内的相对路径，如 app.py"},
                           "args": {"type": "string",
                                    "description": "可选：命令行参数（空格分隔），如 \"add 张三 138\"。"
                                                   "argparse 这类工具**不给参数就什么都不做**，"
                                                   "要验它们就得传参数。"},
                           "stdin": {"type": "string",
                                     "description": "可选：预先喂给程序的标准输入，**一行对应一次 input()**。"
                                                    "程序里用了 input() 就必须给，否则会读到 EOF 而报错。"}},
                       "required": ["rel"]},
    },
}


_WEB_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_read",
        "description": ("【联网读页】把某个网址的**正文**抓下来读（搜索结果只给摘要，"
                        "需要细节时用它点进去看）。可以一次给多个网址。"
                        "查官方文档、看报错讨论、核对版本差异时用它。"),
        "parameters": {"type": "object",
                       "properties": {
                           "urls": {"type": "array", "items": {"type": "string"},
                                    "description": "要读的网址，1~5 个"},
                           "limit": {"type": "integer",
                                     "description": "每个页面最多取多少字，默认 1800"}},
                       "required": ["urls"]},
    },
}

_GITHUB_PUSH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "github_push",
        "description": ("【上传到代码托管平台】把当前开发项目提交并推送到 GitHub / Gitee / "
                        "自建 Git。仓库地址由用户提供（如 git@github.com:user/repo.git）。"
                        "认证使用本机已配置的 git 凭据，应用不保存任何 token。"
                        "**这是把代码发到外网的操作，必须先获得用户同意。**"),
        "parameters": {"type": "object",
                       "properties": {
                           "repo": {"type": "string",
                                    "description": "远端仓库地址（留空则沿用已有的 origin）"},
                           "message": {"type": "string", "description": "本次提交说明"},
                           "branch": {"type": "string", "description": "分支，默认 main"}},
                       "required": []},
    },
}


# ---------------------------------------------------------------- 项目管理
# 「完全自动开发平台」的一组工具：建项目 / 列项目 / 切项目 / 建目录 / 删 / 移动。
#
# ⚠️⚠️ 这 6 个的**处理函数和 dispatch 路由早就写好了，但 schema 一直没加到这里** ——
# 于是只有"代码模型的文本协议"（_TEXT_TOOL_DOCS）看得到它们，
# **默认模型（原生工具调用）根本看不到**。
# 用户反馈"模型没有全自动操作平台的能力"，根因就是这个：
# 平时聊天用的是默认模型，它手里只有 list / read / run / write 四个工具，
# 建项目、改名、删除这些它压根不知道有。
# **教训：加一个工具要同时改四处 —— schema、dispatch、处理函数、以及
#   对应的提示词（原生走 make_schemas，文本协议走 _TEXT_TOOL_DOCS）。漏一处就是白加。**
_WS_PROJECT_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "workspace_new_project",
            "description": ("【开发工作区】新建一个项目并**立刻切进去**，之后的相对路径都相对它。"
                            "**从零开始做东西时第一步就调它**；同名项目已存在时会直接切过去，不会报错。"),
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string",
                                                   "description": "项目名，如 todo-app"}},
                           "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_projects",
            "description": "【开发工作区】列出所有项目（· 是当前项目）。不确定现在在哪个项目里就先调它。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_use_project",
            "description": "【开发工作区】切换到另一个已有项目。",
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string", "description": "项目名"}},
                           "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_mkdir",
            "description": ("【开发工作区】新建一个目录。"
                            "注意：workspace_write 写文件时**父目录会自动创建**，"
                            "只有确实要一个空目录时才需要它。"),
            "parameters": {"type": "object",
                           "properties": {"rel": {"type": "string",
                                                  "description": "相对路径，如 assets"}},
                           "required": ["rel"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete",
            "description": ("【开发工作区】删除项目里的文件或目录（会进 _回收站，能捞回来）。"
                            "清理临时文件、删掉写错的文件时用它。"),
            "parameters": {"type": "object",
                           "properties": {"rel": {"type": "string", "description": "相对路径"}},
                           "required": ["rel"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_move",
            "description": "【开发工作区】重命名或移动文件 / 目录。",
            "parameters": {"type": "object",
                           "properties": {
                               "rel": {"type": "string", "description": "原路径"},
                               "to": {"type": "string", "description": "新路径"}},
                           "required": ["rel", "to"]},
        },
    },
]


def make_schemas(web_enabled: bool = False, kb_enabled: bool = False,
                 code_exec: bool = False, writing: bool = False) -> list:
    """返回工具 schema 列表。

    web_enabled=True 时才暴露联网搜索工具——保证"开关不开不联网"的约定：
    关着的时候模型连工具都看不到，自然不会去联网。
    kb_enabled 同理：关着就不给知识库工具。
    code_exec 同理：关着就不给"本地跑代码"的工具（默认关，避免模型擅自执行代码）。

    writing=True 是**长文创作**场景，只给最必要的几个工具（见下面）。
    """
    if writing:
        # 写作文/方案这类任务只需要「问细节」和「存文件」，
        # 其余工具（画图、搜图、文件系统、跑代码…）这轮根本用不上。
        # 砍掉它们的收益很实在：18 个工具的 schema ≈ 5800 token，
        # 而长文生成既要思考又要写几百上千字，额度本来就很紧张。
        picked = [_ASK_USER_SCHEMA, _LIBRARY_SCHEMA]
        if kb_enabled:
            picked.insert(0, _KB_SCHEMA)      # 写东西时查用户资料是常见需求
        return picked
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "web_image_search",
                "description": (
                    "【联网搜图】到网上找**已经存在**的真实图片并展示原图。\n"
                    "用户说「找/搜/看看……的图」「……长什么样」「来点……壁纸」时用它。\n"
                    "与 generate_image 的区别：本工具=找现成的真实图，不绘制；"
                    "generate_image=AI 从零画。说「画/生成/绘制」时用 generate_image。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "名词短语关键词，如「埃菲尔铁塔」「橘猫」。**越短越准**：1~2 个词最好；不要塞「详细特征/介绍/怎么样/高清大图」这类修饰词，那样会搜出无关内容。一次搜不到或结果不对时，**换个更常见的叫法/学名再搜一次**（如「维纳斯捕蝇草」→「捕蝇草」），而不是就此收工"},
                        "n": {"type": "integer", "description": "想要几张，默认 4，最多 8。用户嫌少时可以调大或再搜一次"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_image_to_library",
                "description": "把刚刚搜到/生成的一张图片保存进本地图片库，供以后随时调用。一般在用户说「保存这张」「存起来」「收进图库」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "要保存的是本轮展示的第几张图（从 1 开始），默认 1"},
                        "name": {"type": "string", "description": "保存后的名称，可选，留空自动命名"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "根据描述生成图片（文生图）。prompt 必须是详细具体的**英文**描述，生成后直接展示给用户。"
                               "⚠️ 用户只说了主体、没说风格/用途/氛围/尺寸时（例如「画一只狐狸」），"
                               "**先调 ask_user 问一轮再画** —— 画一次要几十秒，风格选错就得重画；"
                               "用户已经说清风格与用途时直接画，不要再问。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "详细的英文图片描述（SDXL 风格 prompt，英文）"},
                        "negative_prompt": {"type": "string", "description": "英文负面描述，可选，例如 'low quality, blurry, watermark'"},
                        "size": {"type": "integer", "enum": [512, 768], "description": "图片边长，默认512"},
                        "hd": {"type": "boolean", "description": "是否高清放大（默认 false）。当用户要求「高清/高分辨率/4K/画质好点/放大」时设为 true：会额外做 4 倍超分（512→2048），耗时约多 5 秒"},
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_image",
                "description": "对一张已有图片做局部微改（图生图），如「把背景改成夜晚」「戴上帽子」。source 填本地路径；若用户本轮拖入的图要微改则不填 source。prompt 用英文，并注明保持其他部分不变。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "英文描述要做的修改，含 keep the rest unchanged 之类约束"},
                        "source": {
                            "type": "string",
                            "description": "（可选）本地图片绝对路径；不填则使用用户本轮拖入对话的那张图"},
                        "negative_prompt": {
                            "type": "string",
                            "description": "英文负面描述，可选，例如 'low quality, blurry, distorted'"},
                        "strength": {
                            "type": "number",
                            "description": "修改强度 0~1，默认0.6；0.3=轻微微调，0.8=大改"},
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "列出指定目录下的一级内容（文件/子目录）。用于浏览用户本机文件系统。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录绝对路径，例如 C:\\Users\\xxx\\Documents"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取本机指定路径的文件（文本/代码/PDF）。仅当用户给出本地文件路径时用。图片/视频若已附在对话中，直接用视觉能力看，不要调用本工具。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "在指定目录（递归）或单文件中按关键词搜索匹配的内容片段。用于帮用户在本机查找资料。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录或文件绝对路径"},
                        "keyword": {"type": "string", "description": "要搜索的关键词"},
                    },
                    "required": ["path", "keyword"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "写入或覆盖创建用户本机的一个文本文件。返回写入结果。可用于创建/写入代码、文档等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "content": {"type": "string", "description": "要写入的完整文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "append_file",
                "description": "向已有文本文件末尾追加内容（不会覆盖原有内容），若无该文件则新建。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "content": {"type": "string", "description": "要追加的文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remember",
                "description": "写入或**修改**记忆。**长期记忆**跨所有对话共享（身份、姓名、职业、长期偏好、约定、目标计划、他身边的人事物、以及他未来可能的意图）；**短期记忆**只在当前对话生效（正在做的项目、本次讨论的结论、临时设定）。\n⚠️ **长期记忆是「关于这个人」的档案**：写之前问一句，这条对了解他、或对他以后要做的事有用吗？\n· 不许存：寒暄、临时问答、**一次性查询的结果**（查到的天气/评分/路线/价格/比分…）、**本应用自己的操作说明**（某功能怎么用、文件放哪）—— 这些跟他无关，明天就过期。\n· 该存：由这次提问能看出的**他的偏好/处境/打算**（例：查了餐厅评分 → 记「挑餐厅在意评分」，而不是把评分本身记下来）。\n系统会自动去重，不需要你重写旧内容。\n特别注意 action：add=新增（默认）；update=**改写已有条目**；forget=作废删除（用户明确说不做了/弄错了）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "要记住的一条具体事实（第三人称、直接、可读，60 字以内）。action=update 时填**修改后**的新内容"},
                        "scope": {
                            "type": "string",
                            "enum": ["long", "short"],
                            "description": "long=长期记忆（跨对话通用，放身份/偏好/约定/目标计划）；short（默认）=短期记忆（只在这个对话用，放当前项目/本次结论）"},
                        "action": {
                            "type": "string",
                            "enum": ["add", "update", "forget"],
                            "description": "add（默认）=新增；update=改写已有条目；forget=删除已有条目"},
                        "old": {
                            "type": "string",
                            "description": "action=update/forget 时必填：要改动的那条记忆的**原句**（照抄，至少前 10 个字），用来定位是哪一条"},
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "检索我已拥有的长期记忆和过往对话，把最相关的信息读出来，以便回答用户（例如“我之前想让你……”“你还记得……吗”）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要检索的查询"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "返回当前本地日期与时间。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    # 知识库工具只在「知识库」开关打开时暴露 —— 与联网同样的约定：
    # 开关关着，模型连工具都看不到，自然就不会去翻资料。
    schemas.append(_LIBRARY_SCHEMA)
    schemas.append(_ASK_USER_SCHEMA)
    schemas.append(_CONNECT_AMAP_SCHEMA)
    # 开发工作区：人机协同开发用（多文件项目，相对路径，后端拼绝对路径）
    schemas.append(_WS_LIST_SCHEMA)
    schemas.append(_WS_READ_SCHEMA)
    schemas.append(_WS_WRITE_SCHEMA)
    # 项目管理（建项目/切项目/建目录/删/改名）——「全自动开发平台」必需。
    # ⚠️ 这几个以前**只给了代码模型的文本协议**，默认模型看不到，用户会觉得"它没这个能力"。
    schemas.extend(_WS_PROJECT_SCHEMAS)
    schemas.append(_WS_PACK_SCHEMA)
    # 生成 PPT：纯本机操作（不联网、不执行用户代码），所以**不挂任何开关**，
    # 始终可用 —— 和「记忆」「文库」一样的待遇。
    schemas.append(_MAKE_PPTX_SCHEMA)
    # 写 Word 文档 + 改已有的 Word/PPT（同样不联网、不跑用户代码，始终可用）。
    schemas.append(_MAKE_DOCX_SCHEMA)
    schemas.append(_MAKE_XLSX_SCHEMA)
    schemas.append(_MAP_PLAN_SCHEMA)
    schemas.append(_NEARBY_SCHEMA)
    schemas.append(_EDIT_OFFICE_SCHEMA)
    # ⚠️ `write_code`（让专用代码模型代写代码）**暂时不启用** ——
    # 用户 2026-09-16 试过之后要求换回"按轮切换代码模型"的架构。
    # 原因：这台机器 12GB 显存装不下两个模型，每调一次 write_code 就要重新加载大脑，
    # 实测一个任务绕了 10 分钟。代码（`_do_write_code`）保留着，没删 ——
    # 想启用只需把下面这行加回来：
    #     schemas.append(_WS_CODE_SCHEMA)
    if code_exec:
        # 跑工作区文件同样属于"在本机执行代码"，跟着同一个开关走
        schemas.append(_WS_RUN_SCHEMA)
    if kb_enabled:
        schemas.append(_KB_SCHEMA)
    if code_exec:
        schemas.append(_run_py_schema())
    if web_enabled:
        schemas.append(_WEATHER_SCHEMA)   # 天气走数据 API，比搜索可靠得多
        schemas.append(_WEB_SEARCH_SCHEMA)
        schemas.append(_WEB_READ_SCHEMA)  # 搜到之后点进去读正文
    else:
        # ⚠️ 联网搜图走的是外网，必须跟着「联网」开关一起关。
        # 之前它写死在基础列表里，关着联网也能搜图 —— 与"关着不联网"的约定矛盾。
        schemas = [s for s in schemas
                   if s["function"]["name"] != "web_image_search"]
    # 上传到代码托管平台：属于"对外发布"，始终暴露但执行前必须问用户
    schemas.append(_GITHUB_PUSH_SCHEMA)
    return schemas


# ---------- 本地代码执行（"离线计算"）：受「本地算代码」开关控制 ----------
# 为什么要它：模型写代码不难，难的是**算对**。让它把代码真跑一遍，
# 数字、日期、正则匹配结果都是真算出来的，而不是"看着像"。
# 默认关闭 —— 打开后模型写的代码会在用户电脑上执行，风险由用户判断。
_SCI_LIBS = ("numpy", "scipy", "pandas", "matplotlib", "sympy")
_sci_cache = None


def available_science_libs() -> list:
    """当前环境里**真正能用**的科学计算库。

    用 find_spec 探测而不是真的 import —— 只为判断有没有，不值得拖慢启动。
    为什么要动态探测（实测）：源码环境和容器的库**不一样**，容器里就没有
    scipy / matplotlib。描述里写死库名会让模型写出跑不起来的代码，
    所以按本机实际能力告诉它。
    """
    global _sci_cache
    if _sci_cache is None:
        import importlib.util
        ok = []
        for m in _SCI_LIBS:
            try:
                if importlib.util.find_spec(m):
                    ok.append(m)
            except Exception:
                pass
        _sci_cache = ok
    return _sci_cache


def _run_py_schema() -> dict:
    libs = available_science_libs()
    libs_txt = "、".join(libs) if libs else "（本机没有额外的科学计算库，只能用标准库）"
    return {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "【本地执行 Python】在用户电脑上**真跑**一段 Python 代码并返回输出。\n"
                "什么时候用：需要精确计算、处理数据、验证自己写的算法对不对、"
                "做日期/单位换算、正则匹配测试等 —— 凡是「算出来比想出来更可靠」的场景都用它。\n"
                "怎么用：把完整可运行的代码放进去，用 print() 输出你要看的结果；\n"
                "不要写文件以外的东西到磁盘（会在临时目录里执行）。\n"
                "⚠️ 代码里**有 `input()` 又没给 `stdin`** → 程序会读到 EOF 直接报错，"
                "**那不代表你的代码写错了**：要么用 stdin 把输入按行喂进去，"
                "要么改成从命令行参数/常量取输入。\n"
                "⚠️ 它**只能跑「算完就退出」的代码**（默认最多 60 秒）。"
                "**计时器 / 服务器 / 游戏 / 图形界面这类要一直跑的程序不要用它跑完整** —— "
                "工具只会跑前几秒做冒烟测试，那是**正常的、不是代码报错**；"
                "这种程序写好交给用户，让用户点代码卡片上的「▶ 运行」（那里不设时限）。\n"
                "可用库：标准库 + " + libs_txt + "。\n"
                "拿到输出后**依据真实结果**回答用户，不要把输出原样贴给用户就完事。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string",
                             "description": "完整可运行的 Python 代码，用 print() 打印要看的中间结果"},
                    "stdin": {"type": "string",
                              "description": "可选：预先喂给程序的标准输入，**一行对应一次 input()**。"
                                             "代码里用了 input() 就必须给，否则会读到 EOF 而报错"
                                             "（那不代表代码写错了）。"},
                },
                "required": ["code"],
            },
        },
    }


# ---------- 生成文库（模型自己的产出物）----------
# 和知识库**严格分开**：知识库是用户的资料、只读；这里是模型产出、可读可写可删。
_LIBRARY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "library",
        "description": (
            "【生成文库】存放**你自己产出的纯文本**文件（.md / .txt / .py / .json 等）。\n"
            "🚫 **用户要 Word 文档或 PPT 时，不要用本工具。** 直接调 make_docx / make_pptx /\n"
            "   edit_office —— 它们一次成型（封面/目录/表格/版式都有），比\"先写 .md、再让用户\n"
            "   自己去导出\"好得多。**也不要指挥用户点界面按钮**（界面上没有那些按钮）。\n"
            "   本工具的 export_docx 只用于补救：文库里的 .md **已经写好了**，现在要转成 Word。\n"
            "⚠️ 和「知识库」是两个不同的地方，别搞混：\n"
            "  · 知识库 = 用户的资料，**只能读、绝不能改**；\n"
            "  · 生成文库 = 你的产出，**可以写、可以改、可以删**。\n"
            "用 action 指定动作：\n"
            "· list —— 列出文库的目录结构（按文件夹分组；不知道有什么就先列一下）\n"
            "· read —— 读文件正文（name），name 可以是 `文件夹/文件名`\n"
            "· write —— 写入/覆盖（name + content）；name 带 .md/.txt/.py/.json 等后缀\n"
            "  ★ name 可以带**子文件夹**，如 `作文/议论文/环境.md`、`代码/爬虫/main.py`。\n"
            "    文件多了就按文件夹归类（用户按课程/项目分组时，跟着它的结构走），\n"
            "    别全堆在根目录 —— 也不用为了分层硬造文件夹，几个文件平铺就够了。\n"
            "· append —— 追加到文件末尾（name + content）\n"
            "· delete —— 删除（会移进回收站，可恢复）\n"
            "· copy —— 复制成新文件（name + new_name）\n"
            "· backup —— 整库备份\n"
            "· export_docx —— 把**已经存在于文库里的文本文件**（.md/.txt）转成排版好的\n"
            "  Word 文档（name，自动加 .docx 后缀；可选 theme / toc）。\n"
            "  ⚠️ 只用于「先写好了正文、现在想转成 Word」的场景。\n"
            "  如果用户一上来就要一份 Word（「写个报告/方案」），**直接用 make_docx**，\n"
            "  它一次成型（封面/目录/表格/提示框都有），比先写文本再转更省事。\n"
            "  Word 文档 WPS 也能直接打开，用户要「WPS 格式 / Word 文档」都能满足。\n"
            "什么时候写进文库：用户**明确要一个文件**时（「写成文档」「给我一个 Python 模块」"
            "「导出成 Word」「保存到文件」）。\n"
            "什么时候不写：只是让你「写篇作文 / 拟个方案」→ **直接写在回答里**就行，"
            "别自作主张建文件；不过长文写完可以问一句要不要存进文库。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "read", "write", "append", "delete",
                                    "copy", "backup", "export_docx"]},
                "name": {"type": "string",
                         "description": "文件名（可带子目录，如 作文/我的大学.md）。不要写盘符或 .."},
                "content": {"type": "string", "description": "write/append 时要写入的正文"},
                "new_name": {"type": "string", "description": "copy 时的目标文件名"},
                "theme": {"type": "string", "description":
                          "export_docx 的配色：blue/green/warm/purple/mono/red"},
                "toc": {"type": "boolean",
                        "description": "export_docx 时是否插入目录页"},
            },
            "required": ["action"],
        },
    },
}


# ---------- 请用户配置高德 key ----------
_CONNECT_AMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "connect_amap",
        "description": (
            "【连接高德地图】请用户把他的高德开放平台 key 填进来，填完**立刻生效**。\n"
            "什么时候用：**用户要用地图，而本机还没配高德 key**（联网开着，但地图只能退回"
            "OpenStreetMap —— 中国的店铺几乎查不到、也没有评分/路况/公交）。\n"
            "这时**先调这个工具请他填**，不要直接说做不到，也不要默默用着弱底图不吭声。\n"
            "话术要点（用户不知道为什么要填）：填了之后能搜到全国的小店、有**真实评分**、"
            "有**实时路况**、有**公交换乘**、步行骑行是真实路径；不填也能用，只是数据很弱。\n"
            "用户填完这个工具会**当场验证** key 是否可用，并把结果告诉你；"
            "如果验证失败，按它给的原因再请他重填一次。\n"
            "⚠️ 用户明确说「不用 / 就这样吧」时，就按没有 key 继续做事，别再反复问。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "为什么现在需要它（一句话，会显示给用户看），可选"},
            },
            "required": [],
        },
    },
}


# ---------- 反问用户（材料不足时先把细节问清楚）----------
# ⚠️ 这个工具的定位是**通用的**：任何"要产出东西"的任务（文档/文章/表格/PPT/代码/图片…）
#    只要关键前提没说清都可以先问一次。以前描述里只写"创作类任务"，
#    还明确写着"纯技术或计算任务别用" —— 结果是**做代码/画图时从来不问**
#    （2026-09-21 用户反馈："生成内容时模型不会在前端二次询问"）。
_ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "【向用户提问】在界面上弹出一个问答框，让用户补充信息，他填完你继续做。\n"
            "什么时候用：**这一轮要产出东西、而关键前提没说清**时 —— 先问清楚再动手，"
            "比硬猜一个交付物强得多。适用于各类有交付物的任务：\n"
            "  · 文章/作文/报告/总结 → 用途、给谁看、篇幅、文体、要突出的重点、时间或背景\n"
            "  · Word/Excel 表格 → 给谁看、要哪些列、数据从哪来、统计口径\n"
            "  · PPT/演示 → 讲给谁、多少页、侧重点、风格或配色\n"
            "  · 代码/程序/小工具/网页 → 用什么语言、跑在什么环境、输入输出长什么样、"
            "要不要界面、有没有样例数据\n"
            "  · 图片/绘图 → 风格（写实/插画/扁平…）、画面主体与细节、尺寸或比例、用途\n"
            "⚠️ **必须调用这个工具来问，不要在回答里用文字提问** ——"
            "用户在弹框里填比在对话框里一条条回方便得多。\n"
            "⚠️⚠️ **提问不会结束这一轮对话**：用户填完，答复会作为工具结果回到你手里，"
            "你**接着把东西做出来**（该写的写、该做的做）；"
            "不要停下来等用户再发一条消息，也不要只回一句「好的」就收工。\n"
            "⚠️ **问题个数不限，也可以分多轮问**（用户 2026-09-22 明确要求）——但要守两条：\n"
            "  · **绝不重复**：已经问过、用户已经答过的，一个字都别再问；\n"
            "  · **每个都要是关键问题**：问了能改变你怎么做，才值得问；凑数的、"
            "无关紧要的、你自己能查到或能合理默认的，都不要问。\n"
            "  分多轮是允许的：拿到答复后如果**又冒出新的关键疑问**，可以再问一轮；"
            "但不要没完没了 —— 该知道的都知道了就动手做。\n"
            "⚠️ **也不要为了「少打扰」而跳过关键问题** —— 方向问错了，比多问一句代价大得多。\n"
            "（当前「快速 / 深度」询问模式见系统提示：快速模式问最关键的几条就开工，"
            "深度模式可以把每个方向都问透。）\n"
            "什么时候**别问**（这几种直接做）：用户已经把要求说清楚了；"
            "只是问个问题 / 查资料 / 解释概念；算个数这类一次性计算；"
            "关键信息你自己查得到（search_knowledge / web_search）；"
            "打招呼、闲聊、表达情绪的对话。\n"
            "每题给 2-4 个选项（用户也可以不选、自己写）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "要问的问题（个数不限，按重要性从高到低排；宁可少而精，别凑数）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "要问的问题，一句话"},
                            "header": {"type": "string", "description": "短标签，4-8 个字"},
                            "options": {"type": "array", "items": {"type": "string"},
                                        "description": "2-4 个候选答案"},
                            "multi": {"type": "boolean", "description": "是否可多选"},
                        },
                        "required": ["question"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}


# 知识库检索工具（受「知识库」开关控制）
_KB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": (
            "检索本地知识库（用户导入的领域文档）。**这是用户自己的资料，"
            "优先级高于联网搜索**：涉及专业领域、内部规范、项目/产品资料时，先来这里查。\n"
            "· 建议先用 list_all=true 看**目录结构**（按文件夹分组、带开头摘要），"
            "再针对性检索；\n"
            "· 一次没查到就换关键词再查，允许多轮检索；\n"
            "· 需要时效性信息时，可以**在同一轮里同时调用本工具和 web_search**，"
            "用知识库答内部细节、用网络补最新情况。\n"
            "（若系统提示里已出现「知识库资料」，说明自动检索已命中，不必重复查。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "检索关键词。用文档里可能出现的原词/术语效果最好"},
                "list_all": {"type": "boolean",
                             "description": "true = 列出全部文档的标题与开头（想先摸清有哪些资料时用）"},
            },
            "required": [],
        },
    },
}


# 天气工具：走结构化数据源，不要用搜索引擎
_WEATHER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "查询某地天气（实时 + 未来逐日预报）。"
            "**天气一律用这个，不要用 web_search**（搜索引擎只给天气网站导航页，没有数值）。"
            "支持中文城市名，**只覆盖中国大陆**。"
            "⚠️ 数据源**只有中国气象局**（经高德地图）：实况是气象站观测、预报是气象台产品。"
            "本机没配高德 key 时**查不了**（返回里会说明，请让用户点顶栏的「高德 key」填一个，"
            "或调用 connect_amap），**不要改用搜索、也不要凭印象编天气**。"
            "返回值里有「数据源」和「观测时间」，回答时要如实转述是几点观测的。"
            "⚠️⚠️ 高德只提供 **4 天**预报，且**没有体感温度、降水概率、降水量** ——"
            "结果**开头会写明「本次没有这些数据」**，那就**一个数字都不许编**，"
            "连 0、未知这类占位数字也别写。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，如「北京」「上海」，可带省份消歧：「广东 深圳」。",
                },
                "days": {
                    "type": "integer",
                    "description": ("预报天数（**从今天起共几天，含今天**），默认 3、最多 16。"
                                    "⚠️ 它**不是「你要哪一天」**：用户问「明天」「后天」时"
                                    "**千万别填 1**（那会只剩今天），不填就行。"
                                    "数据源最多只给 4 天，填大了也只有 4 天。"),
                },
            },
            "required": ["city"],
        },
    },
}


# 联网搜索工具（仅当用户在前端打开"联网"开关时才注入给模型）
_WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索最新信息：新闻时事、近期事件、实时数据（股价/汇率/比分）、"
            "不确定或可能过时的内容、需要查证的事实。会真的联网检索并返回网页摘要。"
            "查天气请用 get_weather；查机构公开信息时关键词带上**机构全称**效果最好。"
            "闲聊、写作、翻译、代码等无需联网的任务不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "搜索关键词，2~4 个核心词，不要写成完整句子、不要塞年月日"
                        "（会被带偏成日历类结果）。中文提问用中文关键词。"
                        "限定官方来源可加 site:（如「深圳大学 招生章程 site:edu.cn」）。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "检索条数，默认 10（多引擎聚合）；想覆盖更广可调到 15~20",
                },
                "deep": {
                    "type": "integer",
                    "description": "对前 N 条结果抓取网页正文供精读，默认 4。"
                                   "需要更深入的细节（条款、数字、名单）时可调大到 6~8；"
                                   "只想快速了解概况可设 0。",
                },
            },
            "required": ["query"],
        },
    },
}


# =====================================================================
#  工具执行
# =====================================================================
def _ui(events, event):
    """收集要推送给前端展示的副作用事件（直接存原始事件，由主循环包 {"ui":event}）。"""
    events.append(event)


def _tool_call_msg(name, args) -> str:
    args_s = json_dumps(args)
    return f"[工具已调用] {name}({args_s})"


def json_dumps(o) -> str:
    try:
        return __import__("json").dumps(o, ensure_ascii=False)
    except Exception:
        return str(o)


def dispatch(name: str, arguments: dict, ui_events: list, context: dict) -> str:
    """执行一个工具调用，返回给模型的文本。ui_events 收集前端副作用。"""
    if name == "workspace_list":
        return _do_workspace_list()
    if name == "workspace_read":
        return _do_workspace_read(arguments)
    if name == "workspace_write":
        return _do_workspace_write(arguments, ui_events)
    if name == "workspace_run":
        return _do_workspace_run(arguments, ui_events)
    # 「写代码」单独交给专用代码模型（见 _do_write_code 的说明：那是实测出来的分工）
    if name == "write_code":
        return _do_write_code(arguments, ui_events, context)
    # ---- 下面这组是"完全自动开发项目"必需的：建项目 / 建目录 / 删 / 移动 / 切项目 ----
    # 这些能力 workspace 模块**早就有了**（create_project / mkdir / remove / rename），
    # 只是以前没交给模型 —— 所以它只能改已有文件，没法从零把项目搭起来。
    if name == "workspace_new_project":
        return _do_workspace_new_project(arguments, ui_events)
    if name == "workspace_projects":
        return _do_workspace_projects()
    if name == "workspace_use_project":
        return _do_workspace_use_project(arguments, ui_events)
    if name == "workspace_mkdir":
        return _do_workspace_mkdir(arguments, ui_events)
    if name == "workspace_delete":
        return _do_workspace_delete(arguments, ui_events)
    if name == "workspace_move":
        return _do_workspace_move(arguments, ui_events)
    if name == "workspace_pack":
        return _do_workspace_pack(arguments)
    if name == "make_pptx":
        return _do_make_pptx(arguments, ui_events)
    if name == "make_docx":
        return _do_make_docx(arguments, ui_events)
    if name == "make_xlsx":
        return _do_make_xlsx(arguments, ui_events)
    if name == "map_plan":
        return _do_map_plan(arguments, ui_events, context)
    if name == "nearby_places":
        return _do_nearby_places(arguments, ui_events, context)
    if name == "edit_office":
        return _do_edit_office(arguments, ui_events)
    if name == "web_read":
        return _do_web_read(arguments)
    if name == "github_push":
        return _do_github_push(arguments, context)
    if name == "web_image_search":
        return _do_web_image_search(arguments, ui_events)
    if name == "get_weather":
        return _do_get_weather(arguments)
    if name == "save_image_to_library":
        return _do_save_image_to_library(arguments, context)
    if name == "generate_image":
        return _do_generate_image(arguments, ui_events)
    if name == "edit_image":
        return _do_edit_image(arguments, ui_events, context)
    if name == "list_directory":
        return _do_list_directory(arguments)
    if name == "read_file":
        return _do_read_file(arguments, ui_events)
    if name == "search_files":
        return _do_search_files(arguments)
    if name == "write_file":
        return _do_write_file(arguments)
    if name == "append_file":
        return _do_append_file(arguments)
    if name == "remember":
        return _do_remember(arguments, context)
    if name == "search_memory":
        return _do_search_memory(arguments)
    if name == "search_knowledge":
        return _do_search_knowledge(arguments)
    if name == "run_python":
        return _do_run_python(arguments, ui_events, context)
    if name == "library":
        return _do_library(arguments, ui_events)
    if name == "ask_user":
        return _do_ask_user(arguments, context)
    if name == "connect_amap":
        return _do_connect_amap(arguments, context)
    if name == "get_time":
        return time.strftime("%Y-%m-%d %H:%M:%S (%A)")
    if name == "web_search":
        return _do_web_search(arguments, ui_events)
    return f"[未知工具] {name}"


# ---------- 联网搜索 ----------
# 查询里常见的"干扰词"：搜索引擎对这类限定词很敏感，会大幅降低召回质量
_NOISE_PATTERNS = [
    r"是公办还是民办", r"公办还是民办", r"公办\s*民办", r"是公办的吗", r"是民办的吗",
    r"是什么", r"怎么样", r"怎样", r"怎么", r"有哪些", r"是多少", r"为什么",
    r"怎么回事", r"如何",
    r"的参数", r"参数配置", r"规格", r"详细介绍", r"介绍一下", r"请问",
]

# 用户口语化的"意图尾巴"：出现在句末时几乎没有检索价值，却会把引擎带偏。
# 例如「2026年人工智能技术应用专业的就业前景如何？请详细分析」——
# 不剥掉后半句时，引擎会去匹配"2026年…分析"，返回国务院节假日通知之类的垃圾；
# 只留「人工智能技术应用专业的就业前景」，召回质量立刻正常。
_TRAILING_INTENT = (
    "请详细分析", "详细分析一下", "详细分析", "分析一下", "帮我分析",
    "请详细说明", "详细说明一下", "详细说明", "说明一下",
    "请详细介绍一下", "详细介绍一下", "请介绍一下", "介绍一下",
    "解释一下", "讲讲", "说说", "谈谈", "聊一聊", "告诉我",
    "帮我看看", "帮我看一下", "帮我查查", "帮我查一下", "我想知道", "想知道",
    "怎么做", "怎么办", "要注意什么", "有什么建议",
)


# 意图从句的起始词：形如「并给出你的分析」「，请详细说明」这类尾巴要整段丢掉
_INTENT_HEAD = (
    "请", "帮我", "麻烦", "分析", "说明", "解释", "给出", "提供", "介绍",
    "讲讲", "说说", "谈谈", "告诉我", "指出", "评价", "对比", "推荐",
)
_TRAILING_CLAUSE_SEP = r"[，,、；;。.]+|以及|并且|还有|并|和"


def _strip_trailing_intent(s: str) -> str:
    """反复剥掉句末的口语化意图短语 / 意图从句。

    例：'深圳大学2026年招生有什么新变化？请详细说明并给出你的分析'
        → 先按并列词切掉「并给出你的分析」→ 再剥掉「请详细说明」
        → '深圳大学 招生有什么新变化'
    """
    import re
    changed = True
    while changed:
        changed = False
        t = (s or "").strip(" 　,，。.、；;：:！!？?")
        # 1) 句末的并列意图从句（「并给出你的分析」这种）
        parts = re.split("(" + _TRAILING_CLAUSE_SEP + ")", t)
        while len(parts) >= 3:
            last = parts[-1].strip()
            if last and last.startswith(_INTENT_HEAD):
                parts = parts[:-2]
                changed = True
            else:
                break
        t = "".join(parts).strip()
        # 2) 句末的固定意图短语
        for p in _TRAILING_INTENT:
            if t.endswith(p):
                t = t[: -len(p)].strip(" 　,，。.、；;：:！!？?")
                changed = True
        # 3) 句末的孤立问句助词
        t2 = re.sub(r"[吗呢吧啊呀嘛]$", "", t).strip()
        if t2 != t:
            t, changed = t2, True
        # 4) 空格分隔的孤立意图词：如「…就业前景 分析」。
        #    要求**前面有空格**才剥，否则会误伤「数据分析」这类连写词。
        m = re.match(r"^(.*\s)(\S{2,4})$", t)
        if m and m.group(2) in _INTENT_HEAD:
            t, changed = m.group(1).strip(), True
        s = t
    return s


def _simplify_query(q: str) -> str:
    """去掉年份/月日/疑问词/口语尾巴，得到更通用的检索词。

    实测：搜索引擎对"广州民航职业技术学院 公办 民办"这类长查询召回很差，
    而只留主体词"广州民航职业技术学院"时能正常返回官网与百科。
    """
    import re
    s = _strip_trailing_intent(q)
    s = re.sub(r"\d{4}\s*年", " ", s)
    s = re.sub(r"\d{1,2}\s*月", " ", s)
    s = re.sub(r"\d{1,2}\s*日", " ", s)
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    s = re.sub(r"[？?！!。，,、；;：:]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedupe(items: list) -> list:
    """按标题前 18 个有效字符去重。"""
    import re
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it.get("title", ""))[:18]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _relevant(query: str, results: list) -> bool:
    """粗略判断检索结果是否与查询相关（看实词命中比例）。"""
    import re
    words = [w for w in re.split(r"[\s,，、/]+", query) if len(w) >= 2]
    if not words:
        return True
    text = " ".join((r.get("title", "") + " " + r.get("desc", "")) for r in results)
    hits = sum(1 for w in words if w in text)
    return hits >= max(1, len(words) // 3)


def _do_web_search(arguments, ui_events):
    """联网检索：多引擎 + 多查询变体聚合 + 相关性过滤 + 深度阅读正文。

    深度阅读（deep read）是关键一步：只给模型 140 字的搜索摘要，它写不出
    有内容的回答；把前几条结果的**正文**抓下来喂给它，才有材料"总结 + 分析 + 展开"。
    """
    from concurrent.futures import ThreadPoolExecutor

    query = (arguments.get("query") or "").strip()
    if not query:
        return "联网搜索失败：未提供 query。"
    try:
        top_k = int(arguments.get("top_k") or 10)
    except Exception:
        top_k = 10
    top_k = max(1, min(top_k, 20))

    # 深度阅读前 N 条正文；正文字数上限
    # 注意：这些量直接决定注入提示词的体积。单条正文每条几百 token，
    # 抓太多会把上下文挤爆（也拖慢推理），因此克制：
    #   4 篇 × 800 字 ≈ 2400 token，加上 8 条标题/链接/摘要 ≈ 1200 token，
    #   合计约 3600 token —— 与主流程 _trim_history_to_budget 里的预留量对齐。
    deep_n = max(0, min(int(arguments.get("deep") if arguments.get("deep") is not None else 4), 8))
    deep_chars = 800

    from . import web_tools

    # 查询变体：原查询 + 精简主体词（+ 去数字版 + 机构类查询追加"官方站定向"）
    variants = [query]
    simple = _simplify_query(query)
    if simple and simple != query and len(simple) >= 2:
        variants.append(simple)

    # 变体 3：把残留的数字也去掉。
    # 「2026 人工智能技术应用 就业前景」这类查询里的年份会把引擎引向
    # 「2026年政府工作报告」「2026年节假日安排」等政策新闻，而"去数字"版本
    # 往往能召回真正讲专业前景的文章。实测对结果数量提升明显。
    import re as _re
    if _re.search(r"\d", simple or query):
        nodigit = _re.sub(r"\d+\s*[年月日]?", " ", simple or query)
        nodigit = _re.sub(r"\s+", " ", nodigit).strip()
        if len(nodigit) >= 2 and nodigit not in variants:
            variants.append(nodigit)

    # 查"某单位对外公开情况"时，普通检索会被百科/聚合站/同名地名淹没。
    # 这里额外跑一次 `主体词 site:gov.cn`（或 edu.cn / org.cn）定向检索，
    # 结果基本就是官网本身。实测对机构类查询的提升最明显。
    body = simple or query
    official_hint = web_tools.official_site_hint(body)

    jobs = [(v, "web") for v in variants]
    if official_hint:
        jobs.append((body, "official"))

    def _run(job):
        q, kind = job
        if kind == "official":
            return web_tools.search_official(q, top_k)
        return web_tools.web_search(q, n=top_k)

    try:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            batches = list(pool.map(_run, jobs))
    except Exception as exc:
        return f"联网搜索失败：{exc}"

    # 逐变体做相关性过滤（剔除"广州市_百度百科"这类泛化无关结果）
    # keep_min 给到 6：材料太少时模型写不出有内容的回答。宁可多留几条让模型自己取舍
    # —— 它已被明确告知"材料弱就如实说明"，比只给 3 条更好。
    filtered = []
    for (q, kind), b in zip(jobs, batches):
        filtered.append(web_tools.filter_relevant(
            q, b, keep_min=4 if kind == "official" else 6))

    # 组装优先级：原查询 → 官方站定向 → 精简查询
    order = [0] + [i for i, (_, k) in enumerate(jobs) if k == "official"] \
        + [i for i in range(1, len(jobs)) if jobs[i][1] != "official"]
    results, seen_idx = [], set()
    for i in order:
        if i in seen_idx or i >= len(filtered):
            continue
        seen_idx.add(i)
        results = _dedupe(results + filtered[i])
    # 扩大检索范围：不再只留 8 条，多给些材料让模型有得比、有得选
    cap = max(top_k, 12)
    results = results[:cap]

    # 相关度偏低时，明确告诉模型"这次检索不可靠"，避免它硬编内容
    confidence = web_tools.best_relevance(query, results)

    used = f"{query}（含精简检索：{simple}）" if len(variants) > 1 else query
    if official_hint:
        used += f"；已定向官方站 site:{official_hint}"

    # ---------- 深度阅读：抓前 N 条的正文 ----------
    # 摘要只有一两百字，模型据此只能写得很短。抓正文才能"总结 + 分析 + 展开"。
    pages = {}
    if deep_n and results:
        urls = [r.get("url") for r in results[:deep_n] if r.get("url")]
        try:
            pages = web_tools.fetch_pages(urls, limit=deep_chars, workers=5, timeout=8)
        except Exception:
            pages = {}

    # ---------- 把来源清单作为独立事件发给前端（聊天气泡里不再重复列链接）----------
    src_items = []
    for i, r in enumerate(results, 1):
        url = (r.get("url") or "").strip()
        try:
            site = urllib.parse.urlparse(url).netloc
        except Exception:
            site = ""
        src_items.append({
            "i": i,
            "title": (r.get("title") or "").strip(),
            "url": url,
            "site": site,
            "engine": r.get("engine") or "",
            "read": url in pages,      # 是否已深度阅读（前端可标注）
        })
    _ui(ui_events, {"type": "sources", "query": used, "items": src_items})

    # 输出给模型的材料：标题 + 完整链接 + 搜索摘要 + 正文节选
    lines = [f"【联网检索结果】检索词：{used}　共 {len(results)} 条，"
             f"其中 {len(pages)} 条已抓取网页正文供你精读。"]
    useful = 0
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("desc") or "").strip()
        if title and title not in ("搜索失败",) and "未获取到搜索结果" not in title and url:
            useful += 1
        lines.append(f"[{i}] {title}")
        if url:
            lines.append(f"    链接：{url}")
        if desc:
            lines.append(f"    摘要：{desc[:200]}")
        body = pages.get(url)
        if body:
            lines.append(f"    正文：{body}")
    if useful == 0:
        lines.append("（未获取到有效搜索结果。请如实告诉用户本次联网检索失败，不要编造内容；"
                     "可以建议用户换个更具体的说法再试。）")
    else:
        if confidence < 0.3:
            lines.append("（注意：本次检索结果与问题的匹配度较低，可能没有命中要点。"
                         "请如实说明「未检索到直接相关信息」，并建议用户换个关键词或提供更具体的名称，"
                         "不要用这些弱相关结果硬凑答案。）")
        lines.append(
            "请基于以上检索结果，用中文写一份**充实、有分析的回答**。要求：\n"
            "1. **篇幅要够**：不要只写两三句结论。一般 400~900 字，信息多的可更长。\n"
            "2. **结构清晰**：用「小标题 + 分点」组织，便于阅读；\n"
            "3. **先总结、再分析、后展开**：\n"
            "   · 先用一两句给出核心结论；\n"
            "   · 再把各条材料的**具体信息**（名称、数字、时间、条款、名单等）提炼出来，\n"
            "     不要笼统概括，要落到细节；\n"
            "   · 有「正文」字段的条目是已抓取的网页正文，**优先从中提取细节**；\n"
            "   · 不同来源说法不一致时，指出来并说明差异所在；\n"
            "4. **给出你自己的判断**：单独一小段「几点看法」或类似小标题，\n"
            "   基于材料做推断和评价（如适用性、风险、值得注意之处）。\n"
            "   **属于你的推断要明确说是推断**，不要和检索到的事实混为一谈；\n"
            "5. **标注来源**：引用了哪条材料，就在该句末尾用 [序号] 标注（如「……[2]」）；\n"
            "6. **不要再写「信息来源」「参考资料」这类列表** —— "
            "界面已经在回答下方单独提供了可展开的来源清单，重复列出是冗余；\n"
            "7. 只能引用上面真实出现过的条目，**绝不编造链接、数字或事实**。\n"
            "   特别注意：**不要编造电话号码、地址、邮箱、文号、日期等联系方式或标识**。\n"
            "   材料里没有就别写；更不要用「020-XXXXXXX」这种占位式写法凑数——\n"
            "   要提供联系方式就写「建议从官网获取」，否则会误导用户；\n"
            "8. 若材料确实不足以回答某部分，就如实说明「检索结果未涉及」，不要凭空补充。\n"
            "注意：当前时间以系统提示中的时间为准；涉及时效的信息请说明材料日期。")
    return "\n".join(lines)


# ---------- 文生图 ----------
_IMAGE_PROMPT_BOOST = (
    "professional photography, highly detailed, sharp focus, "
    "vivid colors, 8k, cinematic lighting, masterpiece, best quality"
)


# ---------- 联网搜图（找现成的真实图片，区别于文生图）----------
def _do_get_weather(arguments):
    """查天气：走结构化数据 API，不经过搜索引擎。"""
    from . import web_tools
    city = (arguments.get("city") or "").strip()
    if not city:
        return "请提供要查询的城市名。"
    try:
        days = int(arguments.get("days") or 3)
    except Exception:
        days = 3
    return web_tools.format_weather(web_tools.weather(city, days))


def _do_web_image_search(arguments, ui_events):
    """联网搜索真实图片，下载原图后展示给前端。"""
    import base64 as _b64
    from . import web_tools

    query = (arguments.get("query") or "").strip()
    if not query:
        return "联网搜图失败：未提供搜索关键词（query）。"
    try:
        n = int(arguments.get("n") or 4)
    except Exception:
        n = 4
    n = max(1, min(n, 8))

    # 多要一些候选再筛：搜索结果里总有一部分因为**防盗链或链接失效**下载不下来，
    # 只按 n 条去取的话，实际能展示的会明显少于预期 —— 用户反馈过"搜图图片太少"。
    try:
        results = web_tools.image_search(query, n=max(n * 3, 12))
    except Exception as exc:
        return f"联网搜图失败：{exc}"
    if not results:
        return (f"联网搜图没有找到「{query}」的图片。"
                "请**如实**告诉用户没搜到，不要凭印象去描述图片长什么样。")

    shown, tried, lines = 0, 0, [f"【联网搜图】关键词：{query}"]
    for r in results:
        if shown >= n:
            break
        tried += 1
        # ⚠️ 用结果自带的 referer（各图库的自家 referer，如 image.so.com），
        # 拿不到才退回来源页 —— 360 的图只认前者，给来源页会 403。
        referer = r.get("referer") or r.get("source") or None
        raw = web_tools.download_image(r["url"], referer=referer)
        if not raw and r.get("thumb"):
            # 原图被防盗链挡住时退一步用缩略图 —— 缩略图通常挂在允许外链的 CDN 上，
            # 虽然小一些，但总比"一张都显示不出来"强。
            raw = web_tools.download_image(r["thumb"], referer=referer)
        if not raw:
            continue
        shown += 1
        mime = "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif raw[:4] == b"RIFF":
            mime = "image/webp"
        elif raw[:3] == b"GIF":
            mime = "image/gif"
        _ui(ui_events, {
            "type": "image", "mime": mime,
            "b64": _b64.b64encode(raw).decode("utf-8"),
            "prompt": r.get("title") or query,
            "url": r.get("url") or "",
            "source": r.get("source") or "",
            "origin": "web",              # ← 前端据此标注「网上搜到的」
        })
        lines.append(f"[{shown}] {r.get('title') or '(无标题)'}")
        if r.get("source"):
            lines.append(f"    来源页：{r['source']}")
        lines.append(f"    图片直链：{r['url']}")

    if shown == 0:
        return (f"联网搜图失败：找到 {len(results)} 个候选，但图片都没能下载下来"
                "（多为目标站点的防盗链）。请**如实**说明没取到图片，"
                "不要改口去描述图片内容，也不要编造物种特征。")

    lines.append(
        f"已把 {shown} 张**网上搜索到的真实原图**展示给用户（这是搜索结果，不是你画的）。"
        f"（本轮共检查 {tried} 个候选，其余因防盗链或链接失效未能取到。）"
        "请用中文简要说明找到了什么内容，并提示：可点图片下方「保存到图库」留存。"
        "**只描述实际看到的内容，不要补充未经核实的事实**（搜图搜不到生物学特征）。")
    return "\n".join(lines)


def _do_save_image_to_library(arguments, context):
    """把本轮展示过的某张图片存入本地图片库。"""
    from . import image_library

    try:
        idx = max(1, int(arguments.get("index") or 1))
    except Exception:
        idx = 1

    pool = (context or {}).get("shown_images") or []
    if not pool:
        return "保存失败：本轮还没有展示过任何图片。请先搜图或生成图片再来保存。"
    if idx > len(pool):
        return f"保存失败：本轮只展示了 {len(pool)} 张图，不存在第 {idx} 张。"

    item = pool[idx - 1]
    meta = image_library.save_image(
        item.get("b64") or item.get("url") or "",
        name=arguments.get("name") or "",
        source=item.get("source") or item.get("prompt") or "",
        origin=item.get("origin") or "web")
    if not meta.get("ok", True):
        return f"保存失败：{meta.get('error')}"
    return (f"已保存第 {idx} 张图到图片库，名称为「{meta['name']}」（id={meta['id']}）。"
            "用户可在左侧「图片库」面板随时查看、调用或删除。")


def _do_generate_image(arguments, ui_events):
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "错误：未提供图片描述（prompt）。"
    negative = (arguments.get("negative_prompt") or "").strip() or "low quality, blurry, watermark, text, deformed"
    size = int(arguments.get("size") or 512)
    hd = bool(arguments.get("hd"))
    # 提升 SD 对 prompt 的遵循度：追加质量词
    boosted = prompt + ", " + _IMAGE_PROMPT_BOOST
    t2i.unload()  # 确保显存空闲
    start = time.time()
    result = t2i.generate(boosted, negative_prompt=negative, steps=4,
                          width=size, height=size, hd=hd)
    cost = time.time() - start
    if not result.get("ok"):
        return (f"图片生成失败：{result.get('error')}。"
                f"请把冒号后的具体原因**原样**转告用户，不要改写成笼统说法。")
    # 把图片作为副作用发给前端展示；只把简短文本回给模型，避免占用上下文
    _ui(ui_events, {"type": "image", "mime": "image/png", "b64": result["b64"],
                    "prompt": prompt, "device": result.get("device"),
                    "model": result.get("model"), "cost_s": round(cost, 1),
                    "size": result.get("size"),
                    "origin": "gen"})     # ← 前端据此标注「AI 生成」
    real_size = result.get("size") or f"{size}x{size}"
    extra = f"（{result['hd_note']}）" if (hd and result.get("hd_note")) else ""
    return (f"已生成图片（{real_size}，{result.get('device')}，用 {round(cost,1)} 秒）{extra}。"
            f"生成的图片已经展示给用户。若用户想调整，可再次明确修改描述。")


def _do_edit_image(arguments, ui_events, context):
    """图片微改（图生图）：拿到一张参考图 + 修改描述，产出新图。"""
    import base64 as _b64
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "错误：未提供修改描述（prompt）。"
    negative = (arguments.get("negative_prompt") or "").strip() or \
        "low quality, blurry, watermark, distorted, deformed"
    strength = max(0.0, min(1.0, float(arguments.get("strength") or 0.6)))
    steps = int(arguments.get("steps") or 4)
    source = (arguments.get("source") or "").strip()

    # 1) 解析参考图：优先用 source 路径；否则用本轮对话拖入的那张图
    init_image = None
    if source:
        if not os.path.isfile(source):
            return f"错误：source 不是有效图片路径：{source}"
        init_image = source  # edit_image 支持路径
    else:
        imgs = (context or {}).get("images") or []
        if imgs:
            raw = imgs[0]
            if isinstance(raw, str) and "," in raw and raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            try:
                init_image = _b64.b64decode(raw)
            except Exception:
                init_image = None
    if init_image is None:
        return ("错误：无法确定要修改的图片。请把要改的图片拖入对话（作为本轮附件）后再让我微改，"
                "或通过 source 指定本地图片路径。")

    # 2) 提示措辞：强化"保持其余不变"
    boosted = prompt + ", keep the original layout and style, high detail"
    t2i.unload()
    start = time.time()
    result = t2i.edit_image(init_image, boosted, negative_prompt=negative,
                            steps=steps, strength=strength)
    cost = time.time() - start
    if not result.get("ok"):
        return (f"图片微改失败：{result.get('error')}。"
                f"请把冒号后的具体原因**原样**转告用户，不要改写成「暂时无法使用」这类"
                f"笼统说法 —— 用户需要看到真实原因才能判断问题在哪。")
    _ui(ui_events, {"type": "image", "mime": "image/png", "b64": result["b64"],
                    "prompt": "微改：" + prompt, "device": result.get("device"),
                    "model": result.get("model"), "cost_s": round(cost, 1)})
    return (f"已根据修改要求生成新图（用 {round(cost,1)} 秒）。原图已按描述微改并展示给用户。"
            f"若还要继续调整，请直接说明新的修改点。")


# ---------- 文件系统 ----------
def _do_list_directory(arguments):
    path = arguments.get("path") or ""
    if not path:
        return "错误：未提供目录路径。"
    if not os.path.isdir(path):
        return f"错误：不是有效目录：{path}（若它是文件，请用 read_file）"
    try:
        items = sorted(os.listdir(path))
    except PermissionError as e:
        return f"错误：无权限访问该目录：{e}"
    lines = []
    for it in items:
        full = os.path.join(path, it)
        kind = "[目录]" if os.path.isdir(full) else "  文件"
        try:
            size = os.path.getsize(full) if os.path.isfile(full) else ""
        except Exception:
            size = ""
        size_s = f"{size:,}B" if isinstance(size, int) else ""
        lines.append(f"{kind} {it} {size_s}")
    head = "\n".join(lines[:300])
    if len(lines) > 300:
        head += f"\n……（共 {len(lines)} 项，仅显示前 300 项）"
    return f"目录 {path} 的内容：\n{head}"


_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"}


def _do_read_file(arguments, ui_events):
    path = arguments.get("path") or ""
    if not path:
        return "错误：未提供文件路径。"
    # 视频：抽帧展示（让模型能“看”视频）
    if os.path.splitext(path)[1].lower() in _VIDEO_EXTS:
        from . import video as video_mod
        res = video_mod.extract_frames(path)
        if not res.get("ok"):
            return f"错误：{res.get('error')}"
        frames = res.get("frames", [])
        for i, b64 in enumerate(frames):
            _ui(ui_events, {"type": "image", "mime": "image/jpeg", "b64": b64,
                            "from": path, "frame": i + 1, "total": len(frames)})
        return (f"已读取视频并抽取 {len(frames)} 个关键帧展示给用户"
                f"（时长约{res.get('duration')}秒）。请综合这些画面描述视频内容。")
    result = file_tools.read_file(path)
    if not result.get("ok"):
        return f"错误：{result.get('error')}"
    if result.get("type") == "image":
        _ui(ui_events, {"type": "image", "mime": "image/jpeg", "b64": result["b64"], "from": path})
        return f"已读取图片并展示给用户：{path}。请基于这张图片回答。"
    # 文本/目录
    if result.get("type") == "dir":
        return f"这是目录，内容如下：\n{result.get('content','')}"
    text = result.get("content", "")
    if len(text) > 6000:
        text_short = text[:6000]
        return f"文件 {path} 的内容（截取前6000字符，共{len(text)}字符）：\n{text_short}"
    return f"文件 {path} 的内容：\n{text}"


def _do_search_files(arguments):
    path = arguments.get("path") or ""
    keyword = (arguments.get("keyword") or "").strip()
    if not path or not keyword:
        return "错误：需要同时提供 path 与 keyword。"
    if os.path.isfile(path):
        paths = [path]
    elif os.path.isdir(path):
        paths = []
        try:
            for ext in file_tools.TEXT_EXTS:
                paths += glob.glob(os.path.join(path, "**", "*" + ext), recursive=True)
        except Exception as e:
            return f"错误：扫描目录失败：{e}"
        paths = paths[:200]
    else:
        return f"错误：路径不存在：{path}"
    hits = []
    kws = [k.lower() for k in keyword.split()]
    for p in paths[:200]:
        text = file_tools.read_text(p)
        if not text:
            continue
        low = text.lower()
        if kws and any(k in low for k in kws):
            idx = min((low.find(k) for k in kws if k in low), default=0)
            seg = text[max(0, idx - 80): idx + 220].replace("\n", " ")
            hits.append(f"--- {p} ---\n…{seg}…")
    if not hits:
        return f"在 {path} 下未找到包含“{keyword}”的文件。"
    return "找到的匹配内容（最多返回30条）：\n\n" + "\n\n".join(hits[:30])


def _do_write_file(arguments):
    path = str(arguments.get("path") or "").strip()
    content = arguments.get("content") or ""
    if not path:
        return "错误：未提供文件路径。"
    # ⚠️ **必须绝对路径**。实测模型会传相对路径（"番茄钟/自定义时长深色模式.html"），
    # 而相对路径是按**进程当前目录**解析的 —— 结果文件被扔进应用的安装目录里
    # （实测：直接在源码仓库根目录下建了个「番茄钟/」文件夹）。
    # 更坑的是工具还回了「已写入文件：…（3580 字节）」，看起来像保存成功了，
    # 用户却根本找不着这个文件。宁可直接拒绝并告诉他该用哪个工具。
    if not os.path.isabs(path):
        return ("错误：write_file 只接受**绝对路径**（例如 D:\\项目\\out.py）。"
                "如果用户是想让你把内容存进「生成文库」，"
                "请改用 library 工具（action=write, name=文件名）。")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(path)
        return f"已写入文件：{path}（{size} 字节）"
    except Exception as e:
        return f"写入失败：{e}"


def _do_append_file(arguments):
    path = str(arguments.get("path") or "").strip()
    content = arguments.get("content") or ""
    if not path:
        return "错误：未提供文件路径。"
    # 同 write_file：相对路径会落到进程 CWD（应用安装目录）去，必须挡掉
    if not os.path.isabs(path):
        return ("错误：append_file 只接受**绝对路径**（例如 D:\\项目\\log.txt）。"
                "想写进「生成文库」请改用 library 工具（action=append）。")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"已追加内容到：{path}"
    except Exception as e:
        return f"追加失败：{e}"


# ---------- 记忆工具 ----------
def _do_remember(arguments, context=None):
    """写入 / 改写 / 删除记忆。

    scope=long 动长期记忆（跨对话共享），否则动当前对话的短期记忆。
    短期记忆按对话隔离，所以必须从 context 拿 session —— 拿不到就拒绝，
    免得内容写进了不知道哪个对话（等于丢失）。

    action 三种：
      add（默认）—— 新增一条
      update      —— 把已有条目改成新内容（**目标的进展就靠它**）
      forget      —— 删掉已有条目
    update/forget 必须给 old（原句），用来定位是哪一条；
    old 可以少写几个字（匹配是模糊的，见 memory._locate）。
    """
    content = (arguments.get("content") or arguments.get("section") or "").strip()
    action = (arguments.get("action") or "add").strip().lower()
    old = (arguments.get("old") or "").strip()
    scope = (arguments.get("scope") or "").strip().lower()
    if action in ("update", "forget") and not old:
        # 没给原句就没法定位 —— 直接把新内容当新条目存，总比丢掉强
        action, old = "add", ""
    if not content and action != "forget":
        return "错误：内容为空。"
    is_long = scope in ("long", "global")

    if is_long:
        if action == "update":
            r = memory_mod.update_long(old, content)
            return {"replaced": "已更新长期记忆里的那条。",
                    "added": "长期记忆里没找到那条，已作为新内容记下。",
                    "skipped": "内容和原来一样，没有改动。"}.get(r, "已处理。")
        if action == "forget":
            return ("已从长期记忆里删除那条。" if memory_mod.drop_long(old)
                    else "长期记忆里没找到那条，未改动。")
        ok = memory_mod.merge_long(content)
        return ("已记入长期记忆（所有对话都通用）。" if ok
                else "这条已经在长期记忆里了，没有重复记。")

    session = str(((context or {}).get("session")) or "").strip()
    if not session:
        return "错误：无法确定当前对话，记忆未写入。"
    if action == "update":
        r = memory_mod.update_short(session, old, content)
        return {"replaced": "已更新本对话记忆里的那条。",
                "added": "本对话记忆里没找到那条，已作为新内容记下。",
                "skipped": "内容和原来一样，没有改动。"}.get(r, "已处理。")
    if action == "forget":
        return ("已从本对话记忆里删除那条。" if memory_mod.drop_short(session, old)
                else "本对话记忆里没找到那条，未改动。")
    ok = memory_mod.merge_short(session, content)
    return ("已记入本次对话的短期记忆。" if ok else "这条已经在本对话记忆里了，没有重复记。")


# ---------- 本地代码执行（"离线计算"）----------
# 「一次性计算」的兜底上限（秒）。可在 config.json 里用 run_timeout 覆盖（5~600）。
# ⚠️ 必须封顶：一个死循环就能把智能体挂住。
RUN_TIMEOUT = 60
# 「持续运行」型程序（计时器 / 服务器 / 图形界面 …）只给一个**冒烟测试窗口**：
# 够证明它正常启动、前几秒的输出是对的，又不会让工具白等。
RUN_PROBE = 8


def run_timeout() -> int:
    """当前生效的一次性计算上限（秒）。读 config，读不到就用默认值。"""
    try:
        from . import config as _cfg
        v = int((_cfg.load_config() or {}).get("run_timeout") or RUN_TIMEOUT)
    except Exception:
        v = RUN_TIMEOUT
    return max(5, min(v, 600))


# ⚠️⚠️ **「跑不完」不等于「代码有问题」**（真实踩坑，必须记住）：
# 用户让模型写了个番茄钟，模型用工具跑它 → 时限到了被强杀 → 工具原话是
# "执行超过 25 秒，已被强制中止" → 模型**以为自己的代码写错了**，于是加线程、
# 加 signal.SIGALRM 反复重写（而 signal.alarm 在 Windows 上根本不存在），
# 三版代码越改越烂 —— 其实用户的原版代码一行都没错，番茄钟本来就该跑 2 小时。
# 所以：能一眼看出"这程序要一直跑"时，就只做几秒冒烟测试，
# 而且**必须把结论说成"这不是报错"**，并明确禁止模型去"绕过超时"。
_PERSIST_PATS = (
    (("while true", "while 1:", "while 1 :"),
     "里面有个不会结束的循环（while True）"),
    (("mainloop()", "mainloop ()", "turtle.done()"),
     "是图形界面程序（进入事件循环后不会返回）"),
    (("serve_forever", "http.server", "socketserver", "httpserver(",
      "uvicorn.run", "app.run(", "run_simple("),
     "是常驻服务器"),
    (("pygame.", "cv2.imshow", "cv2.videocapture"),
     "是窗口 / 摄像头程序"),
    (("schedule.every", "asyncio.start_server"),
     "是常驻的调度 / 服务程序"),
)


def looks_persistent(code: str) -> str:
    """这段代码是不是「要一直跑」的程序？是就返回人话原因，不是返回空串。

    判断**宁漏勿误**：漏判只是照旧按上限跑（和以前一样），
    误判却会把一次正常的长计算当成常驻程序、几秒就砍掉。
    """
    body = "\n".join(ln for ln in (code or "").splitlines()
                     if not ln.strip().startswith("#"))
    low = body.lower()
    for pats, reason in _PERSIST_PATS:
        if any(p in low for p in pats):
            return reason
    # 循环里在 sleep → 计时器 / 轮询（进程不会自己结束）。
    # 只在有 while 时才这么判：`for i in range(3): sleep(1)` 是有次数的等待，
    # 会自己结束，误判成常驻就会白砍掉。
    if "sleep(" in low and "while " in low:
        return "是计时器 / 轮询程序（循环里在 sleep，进程不会自己结束）"
    return ""


def _timeout_result(out, err, risky, persistent: str, limit: int, t0: float) -> dict:
    """被时限强杀后的统一结论。

    ⚠️ 对「持续运行」型程序，措辞必须让人**一眼看出不是报错**，
    并且顺带把"别改代码、别重跑"说死 —— 少了这句，模型就会去加线程、
    用 Windows 上不存在的 signal.alarm 重写（实测踩过）。
    """
    if persistent:
        note = ("（已运行 %d 秒仍在继续 —— 这是**持续运行**型的程序，"
                "**不是报错**，代码没有问题。）" % limit)
    else:
        note = ("执行超过 %d 秒，已被强制中止（上面是中止前已经打印的内容）。\n"
                "多半是死循环或卡住了，请检查循环的退出条件。" % limit)
    err = ((err or "").strip() + "\n" + note).strip()
    return {"needs_confirm": False, "risky": risky,
            "out": (out or "").strip(), "err": err, "rc": None,
            "seconds": round(time.time() - t0, 2),
            # persistent 只在"被冒烟窗口截断"时才有值，跑完了就一定是空
            "persistent": persistent,
            "seconds_limit": limit}

# 需要"先问用户"的操作。**不再一律拦截** ——
# 用户明确要求：发现危险操作先问一句，批准了就执行，而不是直接拒绝。
# 每项配一句人话理由，前端弹窗直接展示给用户看。
# 说明：这不是滴水不漏的沙箱（真正的沙箱要上容器/权限隔离），
# 而是一道"明显危险就先问一句"的闸门；开关默认关闭，风险由用户自己权衡。
_PY_RISKY = (
    (("shutil.rmtree", "os.removedirs", "os.rmdir", "os.remove", "os.unlink",
      "del /f", "del /s", "rmdir /s", "rm -rf", "format c:", "format d:"), "会删除文件或目录"),
    (("subprocess", "os.system", "os.popen", "os.exec", "os.startfile",
      "os.spawn", "os.fork"), "会启动外部程序"),
    (("winreg", "reg delete", "reg add"), "会修改 Windows 注册表"),
    (("shutdown", "reboot"), "可能影响系统开关机"),
    (("socket", "urllib", "requests", "httpx", "urlopen", "ftplib", "smtplib"),
     "会联网（这个模块本来是给'离线计算'用的）"),
    (("ctypes",), "会直接调用系统底层接口"),
    (("eval(", "exec("), "会动态执行字符串代码"),
)


def scan_risky(code: str) -> list:
    """返回代码里命中的风险项（人话描述）。空列表 = 没发现风险。

    注释行先剔掉 —— 免得"注释里提了一句 subprocess"也被当成风险。
    """
    scanned = "\n".join(ln for ln in (code or "").splitlines()
                         if not ln.strip().startswith("#"))
    low = scanned.lower()
    hits = []
    for pats, label in _PY_RISKY:
        if any(p in low for p in pats):
            hits.append(label)
    return hits


def run_code(code: str, allow_risky: bool = False, stdin_text: str = "") -> dict:
    """执行一段 Python，返回**结构化**结果（工具与前端接口共用）。

    字段：needs_confirm / risky / out / err / rc / seconds / persistent
    needs_confirm=True 表示"检测到风险但还没获批准"，**没有执行**。
    persistent 非空 = 这段代码是"要一直跑"的那种，只做了几秒冒烟测试，
    **时限到了不是报错**（见 looks_persistent 上面的说明）。

    stdin_text：**预先喂给程序的标准输入**（一行对应一次 input()）。
        ⚠️ 这个口子必须有：程序里有 `input()` 时没人喂就会立刻拿到 EOF ——
        实测模型写了个计时器，一问"请输入秒数"就直接
        `ValueError: invalid literal for int() with base 10: ''`，
        然后它误以为自己的代码写错了。
        不喂（空串）时管道会立刻关闭 → 照旧是 EOF，不会把智能体挂住。
    """
    code = str(code or "").strip()
    if not code:
        return {"needs_confirm": False, "risky": [], "out": "", "err": "代码为空",
                "rc": -1, "seconds": 0, "persistent": ""}
    risky = scan_risky(code)
    if risky and not allow_risky:
        return {"needs_confirm": True, "risky": risky, "out": "", "err": "",
                "rc": None, "seconds": 0, "persistent": ""}
    import tempfile
    import subprocess as _sp
    t0 = time.time()
    # 识别"要一直跑"的程序 → 只跑几秒冒烟测试，结论按"不是报错"说
    persistent = looks_persistent(code)
    limit = RUN_PROBE if persistent else run_timeout()
    with tempfile.TemporaryDirectory(prefix="mm_run_") as d:
        path = os.path.join(d, "snippet.py")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as exc:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                    "seconds": 0, "persistent": "", "err": "无法写入临时文件：%s" % exc}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # 不让被执行的代码摸到应用的数据目录
        env.pop("MM_DATA_DIR", None)
        try:
            # ⚠️ 用 Popen 而不是 subprocess.run：**run 的 TimeoutExpired 会把
            # 已经打印出来的内容一起丢掉**（实测：跑满时限的计时器在界面上
            # 显示成"（没有输出）"）。Popen 在强杀之后还能把已产出的输出收回来。
            # stdin 接管道（不是 DEVNULL）：程序里 input() 时要能喂进去；
            # 空输入时 communicate 会关掉它 → 仍是 EOF。
            p = _sp.Popen([sys.executable, "-X", "utf8", "-u", "snippet.py"],
                          cwd=d, env=env, stdin=_sp.PIPE,
                          stdout=_sp.PIPE, stderr=_sp.PIPE,
                          text=True, encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                    "seconds": round(time.time() - t0, 2), "persistent": "",
                    "err": "无法启动：%s: %s" % (type(exc).__name__, exc)}
        try:
            _stdin = str(stdin_text or "").replace("\r\n", "\n")
            # 结尾补一个换行：程序里最后一次 input() 才不会一直等
            if _stdin and not _stdin.endswith("\n"):
                _stdin += "\n"
            out, err = p.communicate(input=_stdin, timeout=limit)
            rc = p.returncode
        except _sp.TimeoutExpired:
            p.kill()
            try:
                out, err = p.communicate()      # 收尸并取回已产出的输出
            except Exception:
                out, err = "", ""
            return _timeout_result(out, err, risky, persistent, limit, t0)
        except Exception as exc:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                    "seconds": round(time.time() - t0, 2), "persistent": "",
                    "err": "%s: %s" % (type(exc).__name__, exc)}
    return {"needs_confirm": False, "risky": risky, "out": (out or "").strip(),
            "err": (err or "").strip(), "rc": rc, "persistent": "",
            "seconds": round(time.time() - t0, 2)}


def run_file(path: str, allow_risky: bool = False, args: str = "",
             stdin_text: str = "") -> dict:
    """运行**磁盘上真实存在的** .py 文件（工作区里的项目文件）。

    和 `run_code` 的区别：这里 **cwd 设成文件所在目录**，并且直接跑原文件 ——
    这样脚本里的相对路径（`open("data.txt")`）、`__file__` 都是对的。
    协同开发时"脚本 + 它的输入数据"就摆在同一个目录里，这样才跑得通；
    以前塞进临时目录跑，一律说"找不到文件"。

    风险扫描同样保留：检测到危险操作先问用户，不静默放行。
    """
    p = os.path.abspath(str(path or ""))
    if not os.path.isfile(p):
        return {"needs_confirm": False, "risky": [], "out": "", "rc": -1,
                "seconds": 0, "err": "文件不存在"}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as exc:
        return {"needs_confirm": False, "risky": [], "out": "", "rc": -1,
                "seconds": 0, "err": "读取失败：%s" % exc}
    risky = scan_risky(code)
    if risky and not allow_risky:
        return {"needs_confirm": True, "risky": risky, "out": "", "err": "",
                "rc": None, "seconds": 0, "persistent": ""}
    import subprocess as _sp
    t0 = time.time()
    # 计时器 / 服务器这类"要一直跑"的程序：只给几秒冒烟测试，
    # 并把结论说成"不是报错"（理由见 looks_persistent 上面的说明）。
    persistent = looks_persistent(code)
    limit = RUN_PROBE if persistent else run_timeout()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("MM_DATA_DIR", None)      # 被跑的代码不该摸到应用数据目录
    # 让子目录里的脚本也能 import 项目根的模块（Python 默认只把**脚本所在目录**
    # 放进 sys.path，而 AI 生成的项目常把工具模块放根目录、脚本放子目录）。
    try:
        from . import workspace as _ws2
        _pp = _ws2.root(_ws2.active_project())
        _old_pp = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = _pp + (os.pathsep + _old_pp if _old_pp else "")
    except Exception:
        pass
    try:
        # 用 Popen + communicate（而不是 subprocess.run）：超时分支里还能
        # **拿到已经打印出来的内容**。run 的 TimeoutExpired 会把缓冲一起丢掉，
        # 于是"跑满时限的计时器"在界面上显示成"（没有输出）"—— 实测踩过。
        # 命令行参数：像 argparse 这种工具，**不给参数就什么都不做** ——
        # 模型会以为"跑通了没问题"，其实根本没验到东西。让它可以传参。
        import shlex as _shlex
        _extra = []
        if str(args or "").strip():
            try:
                _extra = _shlex.split(str(args), posix=True)
            except ValueError:
                _extra = []
        pr = _sp.Popen([sys.executable, "-X", "utf8", "-u", os.path.basename(p)] + _extra,
                       cwd=os.path.dirname(p) or ".", env=env,
                       # stdin 也接管道：模型可以**预先喂输入**来测 input() 程序
                       # （communicate 结束时会把管道关掉，所以没喂输入的程序
                       #   会照旧拿到 EOF，不会挂住智能体）。
                       stdin=_sp.PIPE,
                       stdout=_sp.PIPE, stderr=_sp.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                "seconds": round(time.time() - t0, 2), "persistent": "",
                "err": "无法启动：%s: %s" % (type(exc).__name__, exc)}
    try:
        _stdin = str(stdin_text or "").replace("\r\n", "\n")
        # 结尾补一个换行：程序里最后一次 input() 才不会一直等
        if _stdin and not _stdin.endswith("\n"):
            _stdin += "\n"
        out, err = pr.communicate(input=_stdin, timeout=limit)
        rc = pr.returncode
    except _sp.TimeoutExpired:
        pr.kill()
        try:
            out, err = pr.communicate()          # 收尸并取回已产出的输出
        except Exception:
            out, err = "", ""
        return _timeout_result(out, err, risky, persistent, limit, t0)
    return {"needs_confirm": False, "risky": risky, "persistent": "",
            "out": (out or "").strip(), "err": (err or "").strip(),
            "rc": rc, "seconds": round(time.time() - t0, 2)}


def _do_workspace_list() -> str:
    from . import workspace as _ws
    t = _ws.tree()
    files = t.get("files") or []
    head = "【当前项目：%s】" % t.get("project")
    if not files:
        return head + "\n项目里还没有任何文件。可以直接用 workspace_write 新建。"
    lines = [head + "共 %d 个文件：" % len(files)]
    lines += ["· %s（%d 字节）" % (f["rel"], f["size"]) for f in files[:200]]
    if len(files) > 200:
        lines.append("…（只列了前 200 个）")
    return "\n".join(lines)


def _do_workspace_read(arguments) -> str:
    from . import workspace as _ws
    rel = str((arguments or {}).get("rel") or "").strip()
    r = _ws.read_text(rel)
    if not r.get("ok"):
        return "读取失败：%s" % r.get("error")
    text = r.get("text") or ""
    tip = "" if len(text) <= 6000 else "\n\n（文件较长，这里只显示前 6000 字）"
    return "【工作区文件 %s】共 %d 字：\n\n%s%s" % (rel, len(text), text[:6000], tip)


def _do_workspace_write(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    args = arguments or {}
    rel = str(args.get("rel") or args.get("name") or "").strip()
    text = args.get("text")
    if text is None:
        text = args.get("content") or ""
    try:
        # by="ai" → 前端只把"AI 的改动"列进待审阅，可一键撤销
        r = _ws.write_text(rel, str(text), by="ai")
    except ValueError as e:
        return "写入失败：%s" % e
    if not r.get("ok"):
        return "写入失败：%s" % r.get("error")
    if isinstance(ui_events, list):
        # 让前端开发台刷新文件树/打开的内容，并把这条改动标成"待审阅"
        ui_events.append({"type": "workspace", "act": "write", "rel": r["rel"],
                          "chars": r.get("chars", 0),
                          "change_id": r.get("change_id") or "",
                          "project": _ws.active_project()})
    return "已写入工作区文件：%s（%d 字）%s" % (
        r["rel"], r.get("chars", 0),
        "；旧版已备份进回收站" if r.get("backup") else "")


def _do_workspace_run(arguments, ui_events=None) -> str:
    """跑工作区里的 .py（cwd = 文件所在目录），把真实输出回给模型。"""
    from . import workspace as _ws
    rel = str((arguments or {}).get("rel") or "").strip()
    try:
        p = _ws.abs_path(rel)
    except ValueError as e:
        return "运行失败：%s" % e
    if not os.path.isfile(p):
        return "运行失败：工作区里没有这个文件 → %s" % rel
    if not rel.lower().endswith(".py"):
        return ("当前只能直接运行 .py 文件。如果你想验证网页，"
                "写好后让用户点开发台上的「🌐 预览」。")
    r = run_file(p, allow_risky=False, args=str((arguments or {}).get("args") or ""),
                 stdin_text=str((arguments or {}).get("stdin") or ""))
    if r.get("needs_confirm"):
        risk = "、".join(r.get("risky") or [])
        return ("这段代码里有需要用户确认的操作（%s），**没有执行**。"
                "请换成不涉及这些操作的写法，或先跟用户说明再试。" % risk)
    if isinstance(ui_events, list):
        # ⚠️ 一定要带 `rel`：前端要显示「AI 运行结果 · xxx.py」，
        # 不带的话用户不知道模型刚才跑的是哪个文件（实测反馈过）。
        ui_events.append({"type": "code", "rel": rel, "code": _read_text_safe(p),
                          "out": r.get("out") or "", "err": r.get("err") or "",
                          "rc": r.get("rc"), "seconds": r.get("seconds"),
                          "risky": r.get("risky") or []})
    head = "【运行 %s】\n" % rel
    text = head + _format_py_result(r)
    # 同上：有 input() 又没喂 stdin → 说清"不是代码错了"
    if "input(" in _read_text_safe(p) and not str((arguments or {}).get("stdin") or ""):
        text += ("\n\n⚠️ 这个程序里有 `input()`，而这次**没喂 stdin** —— "
                 "它读到的是 EOF（不是代码写错了）。要验证就重跑一次并带上 `stdin`"
                 "（一行对应一次输入），或者让用户点代码卡片上的 ▶ 运行、在输入框里自己输。")
    return text


# ---------------------------------------------------------------- 项目管理
# 「完全自动开发项目」必需的一组工具：建项目 / 列项目 / 切项目 / 建目录 / 删 / 移动。
# 底层能力 workspace 模块早就有（create_project / mkdir / remove / rename），
# 以前只是没交给模型 —— 结果它只能改已有文件，**没法从零把项目搭起来**。
# 有了这组，用户一句话就能让它自建项目、铺目录、写文件、跑、报错自己修。

def _do_workspace_projects() -> str:
    from . import workspace as _ws
    ps = _ws.projects()
    if not ps:
        return "现在一个项目都没有。可以直接用 workspace_new_project 建一个。"
    cur = _ws.active_project()
    lines = ["【开发工作区里的项目】（· 标记的是当前项目）"]
    for p in ps:
        mark = "· " if p.get("name") == cur else "  "
        lines.append("%s%s（%d 个文件）" % (mark, p.get("name"), p.get("files", 0)))
    return "\n".join(lines)


def _do_workspace_new_project(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    name = str((arguments or {}).get("name") or "").strip()
    if not name:
        return "建项目失败：要给一个项目名（如 todo-app）。"
    r = _ws.create_project(name)
    if not r.get("ok"):
        # ⚠️⚠️ **同名项目已经存在时，必须"切过去"，绝不能"报错返回"。**
        # 这是实测抓到的真 bug（原来就是直接 return 错误）：
        # 模型随后照常调 workspace_write，而当前项目**压根没切**，
        # 于是文件全落进了上一个项目 —— 用户看到的正是
        # "项目建了、里面是空的，文件跑到别的项目去了"。
        # 而用户/模型的意图很明确：叫这个名字的，就用它。
        if "已存在" in str(r.get("error") or ""):
            real = _ws.set_active_project(name)
            if isinstance(ui_events, list):
                ui_events.append({"type": "workspace", "act": "project",
                                  "project": real})
            return ("项目「%s」已经存在，已**直接切过去使用**（没有新建）。"
                    "接着用 workspace_write 往里写文件即可，路径相对项目根。"
                    % real)
        return "建项目失败：%s" % r.get("error")
    real = r.get("name") or name
    _ws.set_active_project(real)          # 建完就切过去，后续文件都写进新项目
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "project",
                          "project": real})
    return ("已新建项目「%s」并切入。接下来用 workspace_write 往里写文件"
            "（路径是**相对项目根**的，例如 main.py、src/util.py；"
            "父目录会自动创建，不用单独建目录）。" % real)


def _do_workspace_use_project(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    name = str((arguments or {}).get("name") or "").strip()
    try:
        real = _ws.set_active_project(name)
    except Exception as e:
        return "切项目失败：%s" % e
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "project", "project": real})
    return "已切到项目「%s」。之后的相对路径都相对于它。" % real


def _do_workspace_mkdir(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    rel = str((arguments or {}).get("rel") or "").strip()
    try:
        r = _ws.mkdir(rel)
    except ValueError as e:
        return "建目录失败：%s" % e
    if not r.get("ok"):
        return "建目录失败：%s" % r.get("error")
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "mkdir", "rel": r.get("rel")})
    return "已新建目录：%s" % r.get("rel")


def _do_workspace_delete(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    rel = str((arguments or {}).get("rel") or "").strip()
    try:
        r = _ws.remove(rel)
    except ValueError as e:
        return "删除失败：%s" % e
    if not r.get("ok"):
        return "删除失败：%s" % r.get("error")
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "delete", "rel": rel})
    return "已删除 %s（进了 _回收站，需要时能捞回来）" % rel


def _do_workspace_move(arguments, ui_events=None) -> str:
    from . import workspace as _ws
    a = arguments or {}
    rel = str(a.get("rel") or "").strip()
    to = str(a.get("to") or a.get("new") or "").strip()
    try:
        r = _ws.rename(rel, to)
    except ValueError as e:
        return "移动失败：%s" % e
    if not r.get("ok"):
        return "移动失败：%s" % r.get("error")
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "move",
                          "rel": rel, "to": r.get("rel")})
    return "已把 %s 移到 %s" % (rel, r.get("rel"))


def _do_workspace_pack(arguments=None) -> str:
    """把当前项目打包成 zip，给用户一个能直接点的下载链接。

    用户要求「通过 AI 打包」—— 所以做成工具，而不是界面按钮：
    界面上那个「📦 打包」随开发台一起去掉了，**能力保留在这里**。
    """
    from . import workspace as _ws
    a = arguments or {}
    proj = str(a.get("project") or "").strip() or _ws.active_project()
    try:
        data, name = _ws.export_zip(proj)
    except Exception as e:
        return "打包失败：%s" % e
    # 同时在磁盘上留一份：用户想直接在文件管理器里拿也行
    saved = ""
    try:
        out_dir = os.path.join(os.path.dirname(_ws.root(proj)), "_导出")
        os.makedirs(out_dir, exist_ok=True)
        fp = os.path.join(out_dir, name)
        with open(fp, "wb") as f:
            f.write(data)
        saved = fp
    except Exception:
        pass
    lines = ["已把项目「%s」打包好：%s（%.1f KB）。" % (proj, name, len(data) / 1024.0)]
    lines.append("下载链接（直接点就能存下来）：/api/ws/zip?proj=%s" % proj)
    if saved:
        lines.append("文件也在：%s" % saved)
    return "\n".join(lines)


# Windows 文件名里不允许的字符 —— 标题里常带「？」「：」这类，必须洗掉
_BAD_FN = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _do_make_pptx(arguments=None, ui_events=None) -> str:
    """把结构化内容生成 .pptx，存进生成文库，返回可点下载链接。

    ⚠️ 生成的是**二进制**文件，所以走 `doclib.save_bytes` + `/api/doclib/download`，
    不能用 `doclib.write_file`（那个是给文本用的，二进制会被当文字写坏）。
    """
    import tempfile

    from . import doclib as _dl
    from . import pptx_maker as _pp

    a = arguments or {}
    title = str(a.get("title") or "").strip()
    slides = _pick_slides(a)
    if not title:
        # 同上：模型忘给 title 时，用第一页的标题兜底，别让它反复重试
        for sl in slides:
            if str(sl.get("title") or "").strip():
                title = str(sl["title"]).strip()[:60]
                break
        if not title:
            title = str(a.get("filename") or "").strip() or "演示文稿"
    if not slides:
        return ("错误：缺少 slides（至少要有一页内容）。"
                "请先想好大纲：每页给一个 title 和几条 bullets。")

    base = str(a.get("filename") or title).strip()
    base = _BAD_FN.sub("", base).strip(" .")[:60] or "演示文稿"
    if not base.lower().endswith(".pptx"):
        base += ".pptx"

    tmp = os.path.join(tempfile.gettempdir(),
                       "mm_pptx_%d.pptx" % int(time.time() * 1000))
    r = _pp.build_pptx(
        tmp, title, slides,
        subtitle=str(a.get("subtitle") or ""),
        author=str(a.get("author") or ""),
        theme=str(a.get("theme") or "blue"),
        end_text=str(a.get("end_text") or "谢谢观看"),
        font=str(a.get("font") or "yahei"),
        img_bases=_img_bases(),
        logo=str(a.get("logo") or ""),
        cover_image=str(a.get("cover_image") or ""),
        logo_pos=str(a.get("logo_pos") or "tr"),
        logo_size=float(a.get("logo_size") or 0.5),
    )
    if not r.get("ok"):
        return "生成 PPT 失败：%s" % r.get("error")

    try:
        with open(tmp, "rb") as f:
            data = f.read()
    except Exception as e:
        return "读取生成的临时文件失败：%s" % e
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    w = _dl.save_bytes(base, data)
    if not w.get("ok"):
        return "写入生成文库失败：%s" % w.get("error")

    if isinstance(ui_events, list):
        # 让「生成文库」面板**立刻**刷新并选中这个文件。
        # ⚠️ 不推这个事件的话，文件明明已经落盘了、界面还停在旧列表，
        #    用户会以为"没存进文件库"（实测反馈）。
        ui_events.append({"type": "library", "act": "write", "rel": base})

    n_pages = r.get("slides") or len(slides)
    tip = ""
    if r.get("warnings"):
        tip = "\n（提示：%s）" % "；".join(r["warnings"][:3])
    return ("已生成 PPT《%s》——共 %d 页，%.0f KB。\n"
            "下载链接（**直接点就能存下来，原样给用户**）：\n"
            "/api/doclib/download?rel=%s\n"
            "（源文件也放在「生成文库」里，文件名 %s。"
            "之后要改它，用 edit_office 传这个文件名。）%s"
            % (title, n_pages, len(data) / 1024.0,
               urllib.parse.quote(base), base, tip))


def _img_bases() -> list:
    """图片搜索目录：模型给的图片常常只说个文件名。

    素材可能来自生成文库、当前工作区项目、图片库、用户存图、下载缓存 ——
    统一由 img_fetch 给出（顺序即优先级）。
    """
    from . import img_fetch
    return img_fetch.default_bases()


def _pick_blocks(a: dict) -> list:
    """取文档内容块，**容错字段名**。

    ⚠️ 实测模型经常不写 `blocks`，而是写它更习惯的 `content`（library 工具
    就是那个字段名）。只认 `blocks` 的话它会**反复重试同一个错** ——
    实测连续调了 5 次 make_docx 都因为参数名不对被拒。
    这里统一收口：content / sections / body / paragraphs 都当 blocks；
    给的是字符串就按 Markdown 解析（模型很爱整篇贴 Markdown）。
    """
    from . import docx_maker as _dm
    for key in ("blocks", "content", "sections", "body", "paragraphs"):
        v = a.get(key)
        if not v:
            continue
        if isinstance(v, str):
            if v.strip().startswith("{") or v.strip().startswith("["):
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            if isinstance(v, str):
                return _dm.markdown_blocks(v)
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            out = [x for x in v if isinstance(x, dict)]
            # 模型有时给 {"title":..,"content":..} 这种，补一个 type
            for x in out:
                if not x.get("type"):
                    x["type"] = ("table" if x.get("header") or x.get("rows")
                                 else "bullet" if x.get("items")
                                 else "para")
            return out
    return []


def _pick_slides(a: dict) -> list:
    """取 PPT 每页，同样容错字段名（pages / content / items）。"""
    for key in ("slides", "pages", "content", "items"):
        v = a.get(key)
        if not v:
            continue
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                continue
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _do_make_docx(arguments=None, ui_events=None) -> str:
    """把结构化内容生成 .docx，存进生成文库，返回可点下载链接。

    ⚠️ 生成的是**二进制**，必须走 `doclib.save_bytes` + `/api/doclib/download`，
    不能走 `doclib.write_file`（那是给文本用的，二进制会被写坏）。
    """
    import tempfile

    from . import doclib as _dl
    from . import docx_maker as _dm

    a = arguments or {}
    title = str(a.get("title") or "").strip()
    blocks = _pick_blocks(a)
    if not title:
        # ⚠️ 模型常常只给正文、忘了给 title，只报错会让它**反复重试同一个错**
        # （实测连试 3 次都因为没 title 被拒）。这里从第一个标题兜底取。
        for b in blocks:
            if str(b.get("type") or "").lower() in ("heading", "title", "h1") \
                    and str(b.get("text") or "").strip():
                title = str(b["text"]).strip()[:60]
                break
        if not title:
            title = str(a.get("filename") or "").strip() or "文档"
    if not blocks:
        return ("错误：缺少 blocks（文档内容），至少要有一个内容块。"
                "例如 [{\"type\":\"heading\",\"level\":1,\"text\":\"一、背景\"},"
                "{\"type\":\"para\",\"text\":\"正文…\"}]")

    base = str(a.get("filename") or title).strip()
    base = _BAD_FN.sub("", base).strip(" .")[:60] or "文档"
    if not base.lower().endswith(".docx"):
        base += ".docx"

    tmp = os.path.join(tempfile.gettempdir(),
                       "mm_docx_%d.docx" % int(time.time() * 1000))
    r = _dm.build_docx(
        tmp, title, blocks,
        subtitle=str(a.get("subtitle") or ""),
        author=str(a.get("author") or ""),
        date_text=str(a.get("date_text") or ""),
        theme=str(a.get("theme") or "blue"),
        font=str(a.get("font") or "yahei"),
        cover=bool(a.get("cover")),
        header=str(a.get("header") or ""),
        toc=bool(a.get("toc")),
        bases=_img_bases(),
        logo=str(a.get("logo") or ""),
    )
    if not r.get("ok"):
        return "生成文档失败：%s" % r.get("error")

    try:
        with open(tmp, "rb") as f:
            data = f.read()
    except Exception as e:
        return "读取生成的临时文件失败：%s" % e
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    w = _dl.save_bytes(base, data)
    if not w.get("ok"):
        return "写入生成文库失败：%s" % w.get("error")

    if isinstance(ui_events, list):
        # 同 make_pptx：让「生成文库」面板立刻刷新并选中它
        ui_events.append({"type": "library", "act": "write", "rel": base})

    tip = ""
    if r.get("warnings"):
        tip = "\n（提示：%s）" % "；".join(r["warnings"][:3])
    return ("已生成文档《%s》——共 %d 个内容块，%.0f KB。\n"
            "下载链接（**直接点就能存下来，原样给用户**）：\n"
            "/api/doclib/download?rel=%s\n"
            "（源文件也放在「生成文库」里，文件名 %s。"
            "之后要改它，用 edit_office 传这个文件名。）%s"
            % (title, r.get("blocks") or len(blocks), len(data) / 1024.0,
               urllib.parse.quote(base), base, tip))


def _do_make_xlsx(arguments=None, ui_events=None) -> str:
    """把模型给的数据排成 xlsx，存进生成文库并给下载链接。"""
    import tempfile
    from . import doclib as _dl          # 同其它 maker：函数内导入
    from . import xlsx_maker as _xl
    a = arguments or {}
    sheets = a.get("sheets") or []
    # 容错：模型可能写成 sheets 以外的名字，或者直接把单张表塞在顶层
    if not sheets:
        for k in ("data", "tables", "worksheets", "list"):
            if isinstance(a.get(k), list) and a[k]:
                sheets = a[k]
                break
    if not sheets and (a.get("header") or a.get("rows")):
        sheets = [{"name": a.get("sheet_name") or a.get("name") or "数据",
                   "header": a.get("header") or [], "rows": a.get("rows") or []}]
    for _i, _sh in enumerate(sheets):
        if isinstance(_sh, dict) and not str(_sh.get("name") or "").strip():
            _sh["name"] = "工作表%d" % (_i + 1)
    if not sheets:
        return ("错误：缺少 sheets（至少要有一张表）。例如 "
                '{"sheets":[{"name":"明细","header":["项目","数量","金额"],'
                '"rows":[["A",3,120.5]],"formats":["text","int","money"],"total_row":true}]}')

    # 标题兜底：没给 filename 就从第一张表的 title / name 取
    title = str(a.get("title") or a.get("filename") or "").strip()
    if not title:
        first = sheets[0] if isinstance(sheets[0], dict) else {}
        title = str(first.get("title") or first.get("name") or "数据表").strip()
    base = str(a.get("filename") or title).strip()
    base = _BAD_FN.sub("", base).strip(" .")[:60] or "数据表"

    if not base.lower().endswith(".xlsx"):
        base += ".xlsx"          # 扩展名必须带上，否则下载回来是个没法双击打开的文件

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        r = _xl.build_xlsx(tmp, sheets,
                           theme=str(a.get("theme") or "blue"),
                           author="本地多模态助手")
        if not r.get("ok"):
            return "生成表格失败：%s" % r.get("error")
        with open(tmp, "rb") as f:
            data = f.read()
    except Exception as e:
        return "生成表格失败：%s: %s" % (type(e).__name__, e)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass

    w = _dl.save_bytes(base, data)
    if not w.get("ok"):
        return "写入生成文库失败：%s" % w.get("error")

    if isinstance(ui_events, list):
        # 让「生成文库」面板立刻刷新并选中它（同 make_pptx / make_docx）
        ui_events.append({"type": "library", "act": "write", "rel": base})

    tip = ""
    if r.get("warnings"):
        tip = "\n（提示：%s）" % "；".join(r["warnings"][:3])
    return ("已生成表格《%s》——%d 张工作表、共 %d 行数据，%.0f KB。\n"
            "下载链接（**直接点就能存下来，原样给用户**）：\n"
            "/api/doclib/download?rel=%s\n"
            "（源文件也放在「生成文库」里，文件名 %s。之后要改它，用 edit_office 传这个文件名。）%s"
            % (title, r.get("sheets") or 0, r.get("rows") or 0, len(data) / 1024.0,
               urllib.parse.quote(base), base, tip))


def _do_transit(route: dict, ui_events, net: bool) -> str:
    """公交换乘结果 → 文本 + 地图卡片。

    ⚠️ 公交数据只有高德有。没配 key 或离线时**如实说查不了**，
       绝对不要编线路号、票价、班次（实测模型真会编「302路 票价2元」）。
    """
    from . import map_tools as _mt

    frm = str(route.get("from") or "")
    to = str(route.get("to") or "")
    r = _mt.plan_transit(frm, to, allow_net=net)
    if not r.get("ok"):
        return ("公交换乘没查成：%s\n"
                "⚠️ 如实告诉用户原因，**不要编造公交线路号和票价**。"
                % r.get("error")) + _amap_nudge(net)

    plans = r.get("plans") or []
    lines = ["· **%s → %s** 公交换乘方案（共 %d 个）："
             % (r["from"]["name"], r["to"]["name"], len(plans))]
    for i, p in enumerate(plans[:3]):
        lines.append("  %d）约 %.0f 分钟，票价 %.1f 元，步行 %d 米，换乘 %d 次%s"
                     % (i + 1, p["duration_s"] / 60.0, p["cost"], p["walking_m"],
                        p["transfers"], "　← **推荐**" if i == 0 else ""))
        for s in p["segments"][:8]:
            if s["type"] == "bus":
                alts = ("（也可坐 %s）" % "、".join(s["alts"])) if s.get("alts") else ""
                lines.append("       %s：%s → %s%s"
                             % (s["line"], s["from"] or "?", s["to"] or "?", alts))
            else:
                lines.append("       步行 %d 米" % s["distance_m"])

    if isinstance(ui_events, list):
        best = plans[0] if plans else {"points": []}
        ui_events.append({
            "type": "map", "online": net,
            # 底图用哪个（联网+有高德 key → 高德）。前端据此选瓦片端点，
            # 并决定打点要不要做 WGS-84 → GCJ-02 的换算。
            "tile_source": _mt.tile_source(net),
            "center": [r["from"]["lat"], r["from"]["lon"]],
            "zoom": 12,
            "markers": [{"name": r["from"]["name"], "lat": r["from"]["lat"],
                         "lon": r["from"]["lon"], "addr": "", "role": "from"},
                        {"name": r["to"]["name"], "lat": r["to"]["lat"],
                         "lon": r["to"]["lon"], "addr": "", "role": "to"}],
            "route": {"points": best.get("points") or [], "distance": "",
                      "duration": _mt.fmt_duration(best.get("duration_s") or 0),
                      "mode": "公交", "from": r["from"]["name"], "to": r["to"]["name"],
                      "routes": [], "straight": False, "transit": True},
            "transit": {"plans": [{"duration": _mt.fmt_duration(p["duration_s"]),
                                   "cost": p["cost"], "walking": p["walking_m"],
                                   "transfers": p["transfers"],
                                   "segments": p["segments"]} for p in plans[:3]]},
        })

    return ("公交换乘（卡片会显示给用户，把方案说清楚）：\n" + "\n".join(lines) +
            "\n· 上面这些线路、票价、换乘站都是高德返回的**真实数据**，可以照实说。")


def _amap_broken_prefix() -> str:
    """key 配了但**不可用**时，给工具结果**开头**加一句必须转告用户的话。

    ⚠️ 为什么放开头：实测 qwen3-vl:8b 对"结果末尾的附注"基本视而不见，
    但对开头的强指令会照做。key 坏了是用户**必须知道**的事 ——
    不然他只看到"地图怎么变难用了"，永远不知道要去重配。
    """
    try:
        from . import amap as _am
        h = _am.check_health(online=True)
    except Exception:
        return ""
    # 没配 / 用户主动断开 / 正常 → 都不用报警
    if not h.get("configured") or not h.get("enabled", True) or h.get("ok"):
        return ""
    msg = h.get("message") or "原因未知"
    return ("⚠️⚠️ **先跟用户说这件事**：他的高德 key 现在**用不了**（%s）——"
            "这次的结果只能退而用 OpenStreetMap 查，数据会弱一些。"
            "请**在回答开头用一两句话提醒他**，并让他点界面顶栏的「高德 key」重新配置，"
            "然后再讲结果。\n\n----------\n\n" % msg)


def _amap_nudge(net: bool) -> str:
    """联网但没配高德 key 时，在工具结果末尾塞一句强提醒。

    ⚠️ 为什么非得塞在这里、而不是只写在系统提示里：实测光在系统提示里写
    "先请用户填 key"，模型会**直接跳过**、拿 OSM 的弱数据就把活干完了
    （它觉得答案已经齐了）。而工具返回的文本是它紧接着要读的东西，
    写在这儿才真的会被照做。
    """
    if not net:
        return ""
    try:
        from . import amap as _am
        # ⚠️ 用 has_stored_key：用户**主动断开**时 key 还在，就别再催他填了
        if _am.has_stored_key():
            return ""
    except Exception:
        return ""
    L = chr(10)
    return (L + L + "⚠️ **本机还没配高德 key**，上面的数据来自 OpenStreetMap："
            "中国的店铺覆盖很稀、也没有评分（所以可能只有零星几条）。" + L +
            "请**先调用 connect_amap 请用户填一个 key**（他填完可以立刻重查，不用重启）；" + L +
            "回答里也提一句「现在用的是弱数据源，填个 key 能查到更多、还有评分」。" + L +
            "用户要是说不用，就按现状把结果给他，别再反复追问。")


# 每个进程只主动问一次 key。用户跳过之后就不再打扰 ——
# 反复弹同一个框比不弹更烦人。
_AMAP_ASKED = {"done": False}


def _ask_amap_key_once(context, net: bool) -> str:
    """联网、没配 key、而且还没问过 → 弹一次输入框请用户填，填完当场验证生效。

    返回一句"这次发生了什么"的说明（没发生就返回空串），会拼进工具结果里。

    ⚠️ 为什么由**工具**来问、而不是让模型自己调 connect_amap：实测把"先请用户填 key"
    写进系统提示、甚至塞进工具结果，qwen3-vl:8b 都会**直接无视**，
    拿 OpenStreetMap 的弱数据把活干完就算完。而这句问话必须真的弹出来，
    所以改成工具在干活前自己弹 —— ask 通道本来就是阻塞式的，正好能用。
    """
    if not net or _AMAP_ASKED["done"]:
        return ""
    try:
        from . import amap as _am
        if _am.has_stored_key():
            return ""
    except Exception:
        return ""
    ask = (context or {}).get("ask")
    if not callable(ask):
        return ""
    _AMAP_ASKED["done"] = True          # 先置位：即使用户跳过，也不再问第二次
    try:
        answers = ask({
            "title": "🗺️ 填一个高德 key，地图能力会好很多",
            "hint": ("本机还没配高德 key，现在只能查到 OpenStreetMap 的数据 —— "
                     "中国的店铺覆盖很稀、也没有评分，所以结果可能只有零星几条。" + chr(10) +
                     "填了之后：全国的小店都能搜到，还有真实评分、实时路况、公交换乘，"
                     "步行骑行也是真实路径。不填也能用，就是数据弱。" + chr(10) + chr(10) +
                     "申请（1 分钟）：console.amap.com → 手机号注册+实名 → 建应用 → 加 Key → "
                     "服务平台必须选「Web 服务」。不想填就直接关掉这个框。"),
            "questions": [{"question": "请粘贴高德开放平台的 key（32 位）：",
                            "header": "高德 key"}],
            "freeInput": True,
        })
    except Exception:
        return ""
    ans = ""
    for a in (answers or []):
        if isinstance(a, dict):
            ans = (a.get("answer") or "").strip() or ans
        else:
            ans = str(a or "").strip() or ans
    if not ans:
        return (chr(10) + "（用户没有填高德 key，本次结果来自 OpenStreetMap —— "
                "回答里如实说明数据源比较弱即可，别再追问。）")
    try:
        from . import config as _cfg
        ok, msg = _am.verify_key(ans)
        if not ok:
            return (chr(10) + chr(10)
                    + "（用户填的 key 没通过验证：%s" % msg
                    + chr(10)
                    + "请把这个原因原样告诉他，让他重新复制一个；"
                    + "本次结果仍是 OpenStreetMap 的。）")
        cfg = _cfg.load_config()
        cfg["amap_key"] = ans
        _cfg.save_config(cfg)
    except Exception as e:
        return (chr(10) + "（保存 key 时出错：%s）" % str(e)[:60])
    return (chr(10) + chr(10) + "✅ 用户刚填好了高德 key，**已经生效**（%s）。"
            "下面这些结果已经是用高德查的，直接照实说就行。" % msg)


def _do_map_plan(arguments=None, ui_events=None, context=None) -> str:
    """查地点 / 规划路线，并把地图数据推给前端画成卡片。

    **两种跑法**（一起跟随前端的「联网」开关）：
      · 联网 —— 实时上网查（**查完不留任何本地数据**）；
      · 离线 —— 只认本地：以前查过的地名/算过的路线 + 内置常用地名表，
               **一个网络请求都不发**。算不出真路线时只给直线距离，并说明白。
    """
    from . import map_tools as _mt

    a = arguments or {}
    places = a.get("places") or a.get("地点") or []
    if isinstance(places, str):
        places = [places]
    route = a.get("route") or {}
    if not isinstance(route, dict):
        route = {}
    # 容错：模型可能写成 from/to 平铺在顶层
    if not route and (a.get("from") or a.get("origin")):
        route = {"from": a.get("from") or a.get("origin"),
                 "to": a.get("to") or a.get("dest") or a.get("destination"),
                 "mode": a.get("mode") or a.get("travel_mode") or "driving"}
    if not places and not route:
        return ("错误：至少要给 places（要找的地点）或 route（要规划的路线）。"
                '例如 {"places":["广州塔"],"route":{"from":"广州塔","to":"白云机场"}}')

    net = _mt.online()
    # ⚠️ 干活之前先问一次高德 key（没配、联网、且本进程还没问过时）。
    #    必须放在这里：key 一旦填上，下面这次查询就直接走高德了。
    _key_note = _ask_amap_key_once(context, net)
    # 限定城市（模型从"广州市内有什么商场"里看出来就填 广州）。
    # ⚠️ 不填的后果实测很离谱：查「天河城」会被高德按**地址**解析到
    #    "江西省南昌市进贤县天河城"，而广州的正主反而出不来。
    city = str(a.get("city") or a.get("城市") or "").strip()
    markers, lines = [], []

    for q in [p for p in places if str(p).strip()][:12]:
        one = _mt.geocode_one(str(q), allow_net=net, city=city)
        if not one:
            if net:
                lines.append("· 「%s」没找到（换个更完整的名字试试，比如加上城市名）" % q)
            else:
                lines.append("· 「%s」没找到。现在是离线模式，只能查本地记录过的地点；"
                             "打开「联网」开关就能查到。" % q)
            continue
        if one.get("kind") == "builtin":
            src = "内置地名表"
        elif one.get("kind") == "amap_geocode" or one.get("kind") == "poi":
            src = "联网查到的"
        else:
            src = ""
        markers.append({"name": one["name"], "lat": one["lat"], "lon": one["lon"],
                        "addr": one.get("addr") or "", "query": str(q)})
        lines.append("· **%s** —— %s（%.5f, %.5f）%s"
                     % (one["name"], one.get("addr") or "—", one["lat"], one["lon"],
                        ("  〔%s〕" % src.strip()) if src.strip() else ""))
        if one.get("loose"):
            # 这不是"查到了"，是"按地址猜的"——同名地点会被猜错省份，必须说出来
            lines.append("  ⚠️ 「%s」**没有精确匹配到地点名**，上面这个地址是"
                         "**按地址解析**出来的，很可能只是同名的地方。"
                         "请把地图卡片上的位置跟用户核对一下；"
                         "要更准的话，让用户补上所在城市（或下次调用时把 city 填上）。"
                         % q)

    rinfo, approx, mode_cmp = None, None, None
    if route and route.get("from") and route.get("to"):
        # 公交换乘：高德有独立接口，和驾车/步行完全不是一回事，单独走一条路
        if str(route.get("mode") or "").strip().lower() in (
                "transit", "bus", "public", "公交", "地铁", "公共交通", "公交车"):
            return _do_transit(route, ui_events, net)
        r = _mt.plan_route(str(route.get("from")), str(route.get("to")),
                           str(route.get("mode") or "driving"), allow_net=net,
                           want_weather=bool(a.get("weather") or a.get("天气")),
                           city=city)
        mode_cn = {"driving": "驾车", "foot": "步行", "bike": "骑行"}.get(
            r.get("mode"), r.get("mode"))
        for k in ("from", "to"):
            if r.get(k):
                markers.append({"name": r[k]["name"], "lat": r[k]["lat"],
                                "lon": r[k]["lon"], "addr": "", "role": k})
        if r.get("ok"):
            rinfo = r
            routes = r.get("routes") or []
            tail = ""
            if len(routes) > 1:
                lines.append("· **从 %s 到 %s**（%s）共 %d 条可选路线%s"
                             % (r["from"]["name"], r["to"]["name"], mode_cn,
                                len(routes), tail))
                for rt in routes:
                    star = "　← **推荐**" if rt.get("recommended") else ""
                    lines.append("    %d）%s，约 %s%s"
                                 % (rt["idx"] + 1, _mt.fmt_distance(rt["distance_m"]),
                                    _mt.fmt_duration(rt["duration_s"]), star))
                rec = next((x for x in routes if x.get("recommended")), routes[0])
                if rec.get("reason"):
                    lines.append("    **为什么推荐这条**：%s" % rec["reason"])
            else:
                lines.append("· **从 %s 到 %s**：%s，约 %s（%s）%s"
                             % (r["from"]["name"], r["to"]["name"],
                                _mt.fmt_distance(r["distance_m"]),
                                _mt.fmt_duration(r["duration_s"]), mode_cn, tail))
            if r.get("estimated") and r.get("note"):
                lines.append("    ⚠️ %s" % r["note"])

            # 出行方式对比：用户选的不一定最合适（800 米也要开车、20 公里想走路）。
            # 复用同一套路网数据，三种都算一遍再比 —— 只有真数据才敢给建议。
            try:
                mode_cmp = _mt.compare_modes(str(route.get("from")),
                                             str(route.get("to")), allow_net=net)
            except Exception:
                mode_cmp = None
            if mode_cmp and mode_cmp.get("ok"):
                for k, v in (mode_cmp.get("modes") or {}).items():
                    lines.append("    · %s：%s，约 %s"
                                 % (_mt._MODE_CN.get(k, k),
                                    _mt.fmt_distance(v["distance_m"]),
                                    _mt.fmt_duration(v["duration_s"])))
                if mode_cmp.get("suggest") != r.get("mode"):
                    lines.append("    **出行方式建议**：这段路其实 %s 更合适 —— %s"
                                 % (mode_cmp["suggest_cn"], mode_cmp["suggest_reason"]))
                elif mode_cmp.get("suggest_reason"):
                    lines.append("    **你选的这个方式就是合适的**：%s"
                                 % mode_cmp["suggest_reason"])
                if mode_cmp.get("note"):
                    lines.append("    （%s）" % mode_cmp["note"])

            w = r.get("weather") or {}
            fw = (w.get("from") or {}).get("now") or {}
            tw = (w.get("to") or {}).get("day") or {}
            day_label = (w.get("to") or {}).get("day_label") or "当天"
            if fw.get("desc") or tw.get("desc"):
                parts = []
                if fw.get("desc"):
                    parts.append("出发地此刻 %s%s"
                                 % (fw["desc"],
                                    "、%.0f℃" % fw["temp"] if fw.get("temp") is not None else ""))
                if tw.get("desc"):
                    rng = ""
                    if tw.get("low") is not None and tw.get("high") is not None:
                        rng = "、%.0f~%.0f℃" % (tw["low"], tw["high"])
                    parts.append("抵达那天（%s）%s%s" % (day_label, tw["desc"], rng))
                lines.append("    **天气**：%s" % "；".join(parts))
                # ⚠️ 必须说清"抵达"给的是**当天**的预报，不是那个小时 ——
                #    高德没有逐小时接口，含糊过去模型就会编出"抵达时几点几分下雨"。
                lines.append("    （数据来自**中国气象局**：此刻是气象站实况，"
                             "抵达给的是**当天**的逐日预报，不是那一小时的确切天气；"
                             "转述时别把「当天」说成「到达时那一刻」）")
        else:
            approx = r.get("approx")
            lines.append("· **从 %s 到 %s**：%s"
                         % (r.get("from", {}).get("name", route.get("from")),
                            r.get("to", {}).get("name", route.get("to")),
                            r.get("error") or "没规划出来"))
            if approx:
                lines.append("  两地**直线**距离 %s，终点在起点的**%s**方向。"
                             "⚠️ 这是直线距离、不是实际道路距离，只能当大致参考。"
                             % (_mt.fmt_distance(approx["distance_m"]), approx["bearing"]))
            if r.get("hint"):
                lines.append("  %s" % r["hint"])

    if not markers:
        return "地图（%s）：什么都没查到。\n" % _mt.mode_text() + "\n".join(lines)

    # 地图卡片数据：中心点 + 缩放级别
    lats = [m["lat"] for m in markers]
    lons = [m["lon"] for m in markers]
    center = [sum(lats) / len(lats), sum(lons) / len(lons)]
    span = max(max(lats) - min(lats), max(lons) - min(lons))
    zoom = int(a.get("zoom") or 0)
    if not zoom:
        zoom = 14 if span < 0.02 else 13 if span < 0.06 else 11 if span < 0.3 \
               else 9 if span < 1.2 else 7 if span < 5 else 5

    if isinstance(ui_events, list):
        ui_events.append({
            "type": "map",
            "online": net,
            # 底图用哪个（联网+有高德 key → 高德）。前端据此选瓦片端点，
            # 并决定打点要不要做 WGS-84 → GCJ-02 的换算。
            "tile_source": _mt.tile_source(net),
            "center": center,
            "zoom": zoom,
            "markers": [{"name": m["name"], "lat": m["lat"], "lon": m["lon"],
                         "addr": m.get("addr") or ""} for m in markers],
            "route": ({"points": rinfo["points"],
                       "distance": _mt.fmt_distance(rinfo["distance_m"]),
                       "duration": _mt.fmt_duration(rinfo["duration_s"]),
                       "mode": {"driving": "驾车", "foot": "步行",
                                "bike": "骑行"}.get(rinfo["mode"], rinfo["mode"]),
                       "from": rinfo["from"]["name"],
                       "to": rinfo["to"]["name"],
                       "estimated": bool(rinfo.get("estimated")),
                       "note": rinfo.get("note") or "",
                       # 多条候选：前端按这个列表画多条线，推荐的那条高亮
                       "routes": [{"points": x["points"],
                                   "distance": _mt.fmt_distance(x["distance_m"]),
                                   "duration": _mt.fmt_duration(x["duration_s"]),
                                   "recommended": bool(x.get("recommended")),
                                   "reason": x.get("reason") or ""}
                                  for x in (rinfo.get("routes") or [])],
                       "weather": ({"from_name": ((rinfo.get("weather") or {}).get("from") or {}).get("name") or "",
                                    "from_now": ((rinfo.get("weather") or {}).get("from") or {}).get("now") or {},
                                    "to_name": ((rinfo.get("weather") or {}).get("to") or {}).get("name") or "",
                                    # ⚠️ 给的是**抵达那天的逐日预报**（不是那一小时）——
                                    #    高德没有逐小时接口，卡片上必须这么写。
                                    "to_day": ((rinfo.get("weather") or {}).get("to") or {}).get("day") or {},
                                    "to_day_label": ((rinfo.get("weather") or {}).get("to") or {}).get("day_label") or "",
                                    # 两个数都是**中国气象局**的（实况 + 逐日预报）
                                    "src": ((rinfo.get("weather") or {}).get("from") or {}).get("src") or ""}
                                   if rinfo.get("weather") else None),
                       "modes": [{"mode": _mt._MODE_CN.get(k, k),
                                  "distance": _mt.fmt_distance(v["distance_m"]),
                                  "duration": _mt.fmt_duration(v["duration_s"]),
                                  "estimated": bool(v.get("estimated")),
                                  "suggest": (k == (mode_cmp or {}).get("suggest"))}
                                 for k, v in ((mode_cmp or {}).get("modes") or {}).items()]
                                if (mode_cmp and mode_cmp.get("ok")) else [],
                       "suggest_reason": (mode_cmp or {}).get("suggest_reason") or "",
                       "straight": False} if rinfo else
                      ({"points": [], "straight": True,
                        "distance": _mt.fmt_distance(approx["distance_m"]),
                        "bearing": approx["bearing"],
                        "from": r["from"]["name"], "to": r["to"]["name"],
                        "mode": mode_cn} if approx else None)),
        })

    head = (_amap_broken_prefix() + "地图结果（%s；卡片会自动显示给用户，你只要把结论说清楚）：\n"
            "⚠️ 只能基于上面的数据说话。三种出行方式的对比数据都有，"
            "但**我们没有任何公交/地铁线路数据** —— "
            "不许编「坐 X 路公交 / 票价 Y 元 / 每 Z 分钟一班」这种具体线路信息，"
            "最多说一句「这段距离也可以考虑公共交通」。"
            % _mt.mode_text())
    return head + "\n" + "\n".join(lines) + _amap_nudge(net) + _key_note


def _do_nearby_places(arguments=None, ui_events=None, context=None) -> str:
    """查某个地点**周围**的场所，推给前端画成「场所清单 + 地图」。

    ⚠️ **刻意不输出任何评分** —— OSM 没有评分/评论数据。
       每条只带一句「这条记录全不全」的人话说明（info_note），
       前端也不显示分数，免得被当成口碑分。
    """
    from . import map_tools as _mt

    a = arguments or {}
    place = str(a.get("place") or a.get("地点") or a.get("center") or "").strip()
    category = str(a.get("category") or a.get("类别") or a.get("what") or "").strip()
    if not place:
        return '错误：缺少 place（中心地点），比如 "汕头大学" 或 "23.35,116.68"。'
    if not category:
        return '错误：缺少 category（要找什么），比如 "餐厅" "便利店" "药店"。'
    try:
        radius = int(a.get("radius") or a.get("半径") or 1500)
    except (TypeError, ValueError):
        radius = 1500
    try:
        limit = int(a.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20

    net = _mt.online()
    # ⚠️ 干活之前先问一次高德 key（没配、联网、且本进程还没问过时）。
    #    必须放在这里：key 一旦填上，下面这次查询就直接走高德了。
    _key_note = _ask_amap_key_once(context, net)
    c = _mt.geocode_one(place, allow_net=net)
    if not c:
        hint = ("现在是离线模式，本地没有这个地点的记录；打开「联网」开关就能查。"
                if not net else "换个更完整的名字试试（比如加上城市名）")
        return "没找到中心地点「%s」—— %s" % (place, hint)

    r = _mt.nearby(c["lat"], c["lon"], category, radius=radius, limit=limit,
                   allow_net=net)
    if not r.get("ok"):
        return "周边查询失败：%s" % (r.get("error"))

    items = r.get("items") or []
    rad = r.get("radius")
    head = (_amap_broken_prefix() + "周边搜索（要找：%s；中心：%s；半径 %d 米）"
            % (r.get("category"), c["name"], rad))
    if not items:
        return (head + "\n· 这一类在 %d 米内**一个都没查到**。\n"
                "  ⚠️ OpenStreetMap 是志愿者测绘，中国的小微店铺覆盖很稀疏，"
                "「查不到」不等于「没有」。可以换个说法、或把 radius 调大再试。" % rad)

    lines = []
    for it in items[:limit]:
        nm = it["name"] or "（这个点没有名字）"
        lines.append("· **%s** —— 距离 %s" % (nm, _mt.fmt_distance(it["dist_m"])))
        extra = []
        # 走高德时**有真实评分**，这是最该给用户看的东西，放最前面
        if it.get("rating"):
            extra.append("**评分 %s**" % it["rating"])
        if it.get("cost"):
            extra.append("人均 %s 元" % it["cost"])
        if it.get("addr"):
            extra.append("地址：" + it["addr"])
        if it.get("phone"):
            extra.append("电话：" + it["phone"])
        if it.get("hours"):
            extra.append("营业时间：" + it["hours"])
        if it.get("website"):
            extra.append("官网：" + it["website"])
        if extra:
            lines.append("  " + "；".join(extra))
        if it.get("info_note"):
            lines.append("  %s" % it["info_note"])

    tail = ("\n⚠️ **有评分就照实念**：返回里带 rating 字段的是**真实评分**（来自高德地图），"
            "直接告诉用户「评分 4.7」就行，也可以用来排序推荐；"
            "带 cost 的是人均消费。\n"
            "⚠️ **没有 rating 字段时绝不许自己编评分、星级或评价** —— "
            "OpenStreetMap 不提供评分数据（比如「口碑很好」「味道不错」这类都是编的）。\n"
            "⚠️ **只准转述返回里确实有的信息**（名字、距离、评分、人均、地址、电话、营业时间）。"
            "不要补数据里没有的东西 —— 实测模型会顺口加上「校内主干道旁」「校门对面」"
            "这类位置描述和「学生常去」这类评价，那都是编的。\n"
            "· 「没查到」不等于「没有」：换个别说法或把 radius 调大再试。")

    if isinstance(ui_events, list):
        ui_events.append({
            "type": "map",
            "online": net,
            # 底图用哪个（联网+有高德 key → 高德）。前端据此选瓦片端点，
            # 并决定打点要不要做 WGS-84 → GCJ-02 的换算。
            "tile_source": _mt.tile_source(net),
            "center": [c["lat"], c["lon"]],
            "zoom": 15 if rad <= 1200 else 14 if rad <= 3000 else 13 if rad <= 8000 else 12,
            "markers": [{"name": c["name"], "lat": c["lat"], "lon": c["lon"],
                         "addr": "", "role": "center"}] +
                       [{"name": m["name"] or "（无名）", "lat": m["lat"], "lon": m["lon"],
                         "addr": m.get("addr") or "",
                         "dist": _mt.fmt_distance(m["dist_m"]),
                         "rating": m.get("rating") or ""} for m in items[:limit]],
            "route": None,
            "nearby": {
                "category": r.get("category"),
                "center_name": c["name"],
                "radius": rad,
                "total": r.get("total"),
                "source": r.get("source") or "",
                "items": [{"name": m["name"] or "（无名）", "lat": m["lat"], "lon": m["lon"],
                           "dist": _mt.fmt_distance(m["dist_m"]),
                           "addr": m.get("addr") or "",
                           "phone": m.get("phone") or "",
                           "hours": m.get("hours") or "",
                           "rating": m.get("rating") or "",
                           "cost": m.get("cost") or "",
                           "note": m.get("info_note") or ""} for m in items[:limit]],
            },
        })

    return head + "\n" + "\n".join(lines) + tail + _amap_nudge(net) + _key_note


def _do_edit_office(arguments=None, ui_events=None) -> str:
    """查看 / 修改生成文库里的 .docx 或 .pptx。"""
    from . import doclib as _dl
    from . import office_edit as _oe

    a = arguments or {}
    rel = str(a.get("rel") or "").strip().strip('"')
    if not rel:
        return "错误：缺少 rel（要改的文件名，如「机器学习入门.pptx」）。"

    ext = os.path.splitext(rel)[1].lower()
    if ext not in (".docx", ".pptx"):
        # 只给了名字没给后缀时，去文库目录里找一个同名的
        cands = [f for f in _dl.list_files()
                 if os.path.splitext(str(f.get("rel") or f.get("name") or ""))[0] == rel
                 or str(f.get("name") or "").startswith(rel)]
        hit = ""
        for c in cands:
            n = str(c.get("rel") or c.get("name") or "")
            if os.path.splitext(n)[1].lower() in (".docx", ".pptx"):
                hit = n
                break
        if not hit:
            return ("错误：rel 要带 .docx 或 .pptx 后缀，或者在生成文库里有同名文件。"
                    "当前文库里的文档/PPT：%s"
                    % ("、".join(f.get("name") or f.get("rel") or ""
                                 for f in _dl.list_files()
                                 if str(f.get("name") or "").lower().endswith(
                                     (".docx", ".pptx"))) or "（还没有）"))
        rel = hit
        ext = os.path.splitext(rel)[1].lower()

    path = _dl.file_path(rel)
    if not os.path.exists(path):
        return ("找不到文件「%s」。生成文库里的文档/PPT有：%s"
                % (rel, "、".join(f.get("name") or f.get("rel") or ""
                                  for f in _dl.list_files()
                                  if str(f.get("name") or "").lower().endswith(
                                      (".docx", ".pptx"))) or "（还没有）"))

    action = str(a.get("action") or "inspect").strip().lower()
    if action in ("inspect", "read", "看", "查看", ""):
        return (_oe.inspect(path)
                + "\n\n（要改哪一处，就把上面方括号里的编号填进 ops 的 "
                  "slide/shape（PPT）或 index（Word）里。）")

    ops = a.get("ops") or []
    if not ops:
        return "错误：action=edit 时必须要给 ops（修改操作清单）。"

    # 改之前先备份一份 —— 改坏了用户还能捞回来
    try:
        _dl.copy_file(rel, "改前备份_%s" % os.path.basename(rel))
    except Exception:
        pass

    ok, msg, warns = _oe.edit(path, ops, img_bases=_img_bases())

    name = os.path.basename(rel)
    link = "/api/doclib/download?rel=%s" % urllib.parse.quote(rel)
    if not ok:
        return "修改失败：%s%s" % (msg, ("\n警告：" + "；".join(warns)) if warns else "")
    if isinstance(ui_events, list):
        # 改成功了才刷新文库面板（失败就不动，免得闪一下又没变化）
        ui_events.append({"type": "library", "act": "write", "rel": rel})
    out = ("已修改《%s》：%s。\n下载链接（原样给用户）：%s"
           % (name, msg, link))
    if warns:
        out += "\n⚠️ 有几处没做成：%s" % "；".join(warns[:5])
    out += "\n（改前版本备份在文库里，名字以「改前备份_」开头。）"
    return out


def _strip_code_fence(text: str) -> str:
    """去掉模型爱加的 ```python … ``` 外壳，拿到纯代码。"""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _do_write_code(arguments, ui_events=None, context=None) -> str:
    """把"要写什么"交给**专用代码模型**，由它写出代码并直接落盘。

    为什么要有这么个工具（**这是实测出来的架构结论，别改回去**）：
      · `qwen2.5-coder` **不支持 Ollama 的原生工具调用通道**，只能走"文本协议"
        （让它自己吐 ```tool 块）。实测多轮对话里它会**不吐工具块**、还会
        声称「我无法读取本地文件系统」，越聊越跑偏；
      · `qwen3-vl` / 默认模型是**原生支持工具调用**的，读文件、跑代码、做决策都稳。
    所以分工是：**大脑用默认模型（原生工具），写代码这一步单独交给代码模型。**
    额外的好处：整份代码不经过大脑的上下文 —— 省 token，也不会被转述时丢掉细节。

    **边收边落盘**：代码模型是流式回来的，每收到一块就先写进文件（预览），
    编辑器（内置 VS Code）盯着磁盘，于是照样能看到代码一个个字长出来。
    """
    from . import workspace as _ws
    from . import config as _config
    # ⚠️ ollama 客户端实例是 main.py 里的**单例**（`main.client`），
    # ollama_client 模块本身只有类 —— 直接写 `ollama_client.client` 会 AttributeError。
    # 用**函数内延迟导入**拿它：模块级导入会形成 tools ↔ main 的循环导入。
    from . import main as _main

    a = arguments or {}
    rel = str(a.get("rel") or "").strip()
    inst = str(a.get("instruction") or a.get("task") or "").strip()
    extra = str(a.get("context") or "").strip()
    if not rel:
        return "写代码失败：必须给出要写入的相对路径 rel（如 app.py）。"
    if not inst:
        return "写代码失败：必须说明要写什么 instruction，比如「一个倒计时脚本，从10数到0」。"
    try:
        rel = _ws.safe_rel(rel)
    except ValueError as e:
        return "写代码失败：%s" % e

    cfg = _config.load_config()
    model = str(cfg.get("code_model") or "").strip() or cfg.get("default_model")
    # ⚠️ 必须用 main._installed_models()（它把 /api/tags 的**字典列表**转成了名字列表）。
    # 直接 `model not in client.list_models()` 会因为拿字典跟字符串比而**永远不相等**，
    # 于是模型被悄悄换成默认模型（思考型）—— 输出额度全烧在思考上，最后一个字都没有，
    # 症状是"写代码失败：代码模型这次没有输出内容"。实测踩过。
    try:
        installed = _main._installed_models()
    except Exception:
        installed = []
    if installed and model not in installed:
        model = cfg.get("default_model")

    # 改已有文件 → 把原内容一并给代码模型，让它"在真实内容上改"
    old = ""
    try:
        r = _ws.read_text(rel)
        if r.get("ok"):
            old = (r.get("text") or "").strip()
    except Exception:
        old = ""

    lang = os.path.splitext(rel)[1].lstrip(".").lower() or "python"
    parts = ["你是资深程序员。请直接输出**完整、可运行**的代码，不要解释、不要 Markdown 说明。",
             "目标文件：%s（%s）" % (rel, lang), "需求：%s" % inst]
    if extra:
        parts.append("补充背景：%s" % extra)
    if old:
        parts.append("这是该文件**当前的内容**，请在此基础上修改，并输出修改后的**整份**内容：\n"
                     "```\n%s\n```" % old[:6000])
    parts.append("只输出代码本身（一个代码块或纯代码均可），不要写用法说明。")
    prompt = "\n\n".join(parts)

    params = {"temperature": 0.2,
              "max_tokens": int(cfg.get("code_max_tokens") or 8192),
              "num_ctx": int(cfg.get("num_ctx") or 8192)}
    buf, wrote_any, last_t, last_n = "", False, 0.0, 0
    try:
        resp = _main.client.chat([{"role": "user", "content": prompt}],
                               model=model, stream=True, params=params)
        for raw in resp.iter_lines():
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            line = raw.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            piece = ((o.get("message") or {}).get("content") or "")
            if piece:
                buf += piece
                now = time.time()
                # 节流预览：同文件每 60 字或每 0.3 秒写一次
                if now - last_t >= 0.30 or len(buf) - last_n >= 60:
                    prev = _strip_code_fence(buf)
                    if prev:
                        try:
                            _ws.stream_write(rel, prev)
                            wrote_any = True
                        except Exception:
                            pass
                    last_t, last_n = now, len(buf)
            if o.get("done"):
                break
    except Exception as e:
        if not wrote_any:
            return "写代码失败（调用代码模型出错）：%s" % e

    code = _strip_code_fence(buf)
    if not code.strip():
        return ("写代码失败：代码模型这次没有输出内容（可能是被截断）。"
                "把需求说得更具体一点，或换个文件名重试。")

    w = _ws.write_text(rel, code, by="ai")
    if not w.get("ok"):
        return "写代码失败：%s" % w.get("error")
    if isinstance(ui_events, list):
        ui_events.append({"type": "workspace", "act": "write", "rel": rel,
                          "chars": len(code), "project": _ws.active_project()})
    head = code.splitlines()[0][:60] if code.splitlines() else ""
    out = ("已把代码写进 %s（%d 字，模型=%s）。首行：%s\n"
           % (rel, len(code), model, head))
    # 【为什么把"跑一遍"并进来】两个模型在 12GB 显存里**装不下**，
    # 每调一次代码模型就会把大脑挤出去、下一轮大脑得**重新加载**（十几秒起）。
    # 原流程是 write_code → 大脑 → workspace_run → 大脑 ——
    # **一轮循环 = 两次模型切换 + 两轮大脑生成**，实测一个小工具绕了 5 轮、花了 10 分钟。
    # 写完顺手跑掉，等于把每轮的开销砍掉一半。
    want_run = a.get("run")
    if want_run is None:
        want_run = rel.lower().endswith(".py")
    if want_run and rel.lower().endswith(".py"):
        try:
            out += "\n【顺手跑了一遍，真实输出如下 —— **不用再单独调 workspace_run**】\n"
            out += _do_workspace_run({"rel": rel})
        except Exception as e:
            out += "\n（自动运行失败：%s，你可以用 workspace_run 手动再试）" % e
    return out


def _do_web_read(arguments) -> str:
    """联网读网页**正文**（搜索结果只给摘要，这一步才是"点进去看"）。"""
    from . import web_tools
    args = arguments or {}
    urls = args.get("urls") or args.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [str(u).strip() for u in urls if str(u).strip()][:5]
    if not urls:
        return "错误：没有给网址（urls）。"
    try:
        limit = int(args.get("limit") or 1800)
    except Exception:
        limit = 1800
    try:
        pages = web_tools.fetch_pages(urls, limit=limit)
    except Exception as e:
        return "抓取失败：%s" % e
    if not pages:
        return ("这些网址都没抓到正文（可能需要登录、纯 JS 渲染，或被反爬拦了）。"
                "换别的来源再试。")
    return "\n\n".join("【%s】\n%s" % (u, t) for u, t in pages.items())[:9000]


def _do_github_push(arguments, context=None) -> str:
    """把项目上传到代码托管平台 —— **对外发布，必须先经用户同意**。"""
    from . import workspace as _ws
    args = arguments or {}
    repo = str(args.get("repo") or "").strip()
    message = str(args.get("message") or "").strip()
    branch = str(args.get("branch") or "main").strip() or "main"
    ask = (context or {}).get("confirm")
    if not callable(ask):
        return ("上传到代码托管平台需要用户确认，但当前没有确认通道，**没有上传**。"
                "请让用户点开发台上的「⬆ 上传」按钮。")
    allowed = ask({"kind": "git_push",
                   "reason": "把当前项目上传到 %s（分支 %s）"
                             % (repo or "已有的 origin", branch)})
    if not allowed:
        return ("用户**拒绝了**这次上传，没有推送任何内容。"
                "如实说明即可，**不要**假装已经上传。")
    r = _ws.git_push(repo=repo, message=message, branch=branch)
    if not r.get("ok"):
        return "上传失败：\n%s" % r.get("error")
    return "上传成功（分支 %s）：\n%s" % (r.get("branch"),
                                        "\n".join(r.get("logs") or [])[-900:])


def _read_text_safe(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _format_py_result(r: dict) -> str:
    """把执行结果整理成给模型看的文本。"""
    lines = ["【代码执行结果】"]
    out = r.get("out") or ""
    err = r.get("err") or ""
    # ⚠️ 这段必须放在**最前面**（实测：放结尾 qwen3-vl 根本不看）。
    # 真实踩坑：番茄钟被时限强杀 → 模型以为是自己的代码错了 → 加线程、加
    # signal.SIGALRM 重写了三版（signal 在 Windows 上根本不存在），
    # 其实原代码一行都没错。所以这里把"不是报错、别改它、交给用户跑"说死。
    persistent = str(r.get("persistent") or "")
    if persistent:
        # ⚠️ 「有输出」和「一个字都没有」必须分开说：前者说明它启动正常，
        # 后者是真卡住（死循环）的典型征兆 —— 一律说成"没问题"会放过真 bug。
        smoke = ("冒烟测试里它已经跑出了下面的输出，说明**启动正常、在正常干活**。"
                 if out else
                 "这 %s 秒里它**一个字都没打印**。如果它本来就该有输出"
                 "（比如倒计时、日志），那**很可能是卡住了（比如死循环）**，"
                 "要查一下循环的退出条件；如果它本来就不打印，那是正常的。"
                 % (r.get("seconds_limit") or RUN_PROBE))
        lines.append(
            "⏱ **先说结论：这算不上报错，代码大概率没问题。**\n"
            "这段程序**没能在 %s 秒的冒烟窗口里结束**（%s），工具里等不到它跑完 —— "
            "这类程序本来就该一直跑，冒烟测试只是为了确认它能正常启动。%s\n"
            "⛔ **不要为了让它更快结束去改结构**（加线程、加 signal 都不会让它变快），"
            "也**不要反复重跑**同一个程序。\n"
            "（唯一的例外：你**确定**它本该几秒内就结束 —— 那才去检查循环的退出条件。）\n"
            "✅ 把这个程序交给用户：告诉用户「代码已写好，点代码卡片上的 ▶ 运行 就能完整跑」"
            "—— 那里不设时限，会一直跑、随时能停。"
            % (r.get("seconds_limit") or RUN_PROBE, persistent, smoke))
    if out:
        if len(out) > 4000:
            out = "（输出过长，只保留最后 4000 字）\n" + out[-4000:]
        lines.append("标准输出：\n" + out)
    if err:
        head = "运行提示：\n" if r.get("rc") in (0, None) else "报错信息：\n"
        lines.append(head + err[-1500:])
    if not out and not err:
        lines.append("（代码没有输出任何内容 —— 别忘了用 print() 把结果打出来）")
    if r.get("rc") not in (0, None):
        lines.append("（退出码 %s，说明代码报错了；请先修正再给出结论）" % r.get("rc"))
    if r.get("risky"):
        lines.append("（这次执行包含用户已批准的操作：%s）" % "、".join(r["risky"]))
    if persistent:
        lines.append("请如实告诉用户「程序本身没问题，工具里只能跑前几秒；"
                     "点 ▶ 运行 就能完整跑」，**不要去改它**。")
    else:
        lines.append("请**依据上面的真实输出**回答用户；如果代码报错，先说清错在哪并给出修正后的代码。")
    return "\n\n".join(lines)


def _do_run_python(arguments, ui_events=None, context=None):
    """在本机真跑一段 Python，把 stdout 拿回来。

    为什么要真跑：模型"心算"很容易出错（数字、日期、正则尤其明显），
    而代码跑一遍的结果是**确定的**。这也是"离线计算"的落点。

    ⚠️ 检测到危险操作时**不是直接拒绝，而是先问用户**（用户明确要求）：
    批准了就执行，拒绝才放弃。没有确认通道时（例如脚本里直接调用）默认**不执行**。
    """
    a = arguments or {}
    code = str(a.get("code") or "").strip()
    # `stdin`：把程序里 input() 要读的内容**预先喂进去**（一行一次）。
    # ⚠️ 没有这个口子的话，凡是带 input() 的程序一跑就是
    # `EOFError` / `ValueError: invalid literal for int(): ''`，
    # 模型会误以为自己的代码写错了（实测就是这么绕了好几轮的）。
    _stdin = str(a.get("stdin") or "")
    r = run_code(code, allow_risky=False, stdin_text=_stdin)
    if r.get("needs_confirm"):
        ask = (context or {}).get("confirm")
        risk = "、".join(r.get("risky") or [])
        if not callable(ask):
            return ("这段代码里有需要用户确认的操作（%s），但当前没有可用的确认通道，**没有执行**。\n"
                    "请改成不涉及这些操作的写法。" % risk)
        allowed = ask({"kind": "python", "code": code,
                       "risky": r.get("risky") or [],
                       "reason": "检测到：" + risk})
        if not allowed:
            return ("用户**拒绝了**这次执行（原因：%s）。\n"
                    "请换一种不涉及这些操作的写法；如果确实必须这么做，"
                    "先把你要做什么、为什么这么做说清楚，等用户同意再试。\n"
                    "⚠️ **绝对不要编造执行结果或用模拟数据冒充真实输出** ——"
                    "那会让用户以为结果是真的。如实说明「被拒绝了」即可。" % risk)
        r = run_code(code, allow_risky=True, stdin_text=_stdin)
    # 把这次执行的代码与结果推给前端 → 界面渲染成"可直接编辑重跑"的代码卡片
    if isinstance(ui_events, list):
        ui_events.append({"type": "code", "code": code, "stdin": _stdin,
                          "out": r.get("out") or "", "err": r.get("err") or "",
                          "rc": r.get("rc"), "seconds": r.get("seconds"),
                          "risky": r.get("risky") or []})
    text = _format_py_result(r)
    # 程序里有 input() 而这次没喂输入（或喂了还报 EOF）→ 把原因说清楚，
    # 免得模型又去"修"一份本来没错的代码。
    if "input(" in code and (not _stdin or r.get("rc") not in (0, None)):
        text += ("\n\n⚠️ 这段代码里有 `input()`。工具运行的时候**没人在旁边打字**，"
                 "所以要么把要输入的内容按行放进 **stdin** 参数再跑一次，"
                 "要么就告诉用户「点代码卡片上的 ▶ 运行，运行当中可以随时输入」"
                 "（那边的输入框就是给交互程序用的）。**不要**因此断定代码写错了。")
    return text


# =====================================================================
#  生成文库工具
# =====================================================================
def _do_library(arguments, ui_events=None):
    """生成文库的增删改查 + 导出 Word。"""
    action = str((arguments or {}).get("action") or "list").strip().lower()
    name = str((arguments or {}).get("name") or "").strip()
    content = (arguments or {}).get("content")
    new_name = str((arguments or {}).get("new_name") or "").strip()
    content = "" if content is None else str(content)

    def _notify(kind, **kw):
        if isinstance(ui_events, list):
            ui_events.append(dict(type="library", act=kind, **kw))

    try:
        if action == "list":
            return ("【生成文库】目录结构（按文件夹分组）：\n"
                    + library_mod.tree_text(max_items=120)
                    + "\n\n写文件时 name 可以带子文件夹（如 `作文/第二版.md`），"
                      "用文件夹归类更好找。")

        if action == "read":
            r = library_mod.read_file(name)
            if not r.get("ok"):
                return "读取失败：%s" % r.get("error")
            _notify("read", rel=r["rel"], content=r.get("text") or "")
            head = r.get("text") or ""
            if r.get("chars", 0) > 20000:
                head = head[:20000] + "\n……（内容很长，已截断）"
            return "【%s】共 %d 字：\n\n%s" % (r["rel"], r.get("chars", 0), head)

        if action in ("write", "append"):
            r = library_mod.write_file(name, content,
                                       "append" if action == "append" else "overwrite")
            if not r.get("ok"):
                return "写入失败：%s" % r.get("error")
            _notify("write", rel=r["rel"], chars=r.get("chars", 0))
            tip = "（原有内容已自动备份）" if r.get("backup") else ""
            return ("已%s到生成文库：%s（%d 字）%s。\n"
                    "告诉用户文件已经存好了、存在生成文库面板里，他可以在那里打开、编辑、"
                    "导出 Word 或删除。" % ("追加" if action == "append" else "写入",
                                            r["rel"], r.get("chars", 0), tip))

        if action == "delete":
            r = library_mod.delete_file(name)
            if not r.get("ok"):
                return "删除失败：%s" % r.get("error")
            _notify("delete", rel=r["rel"])
            return "已删除 %s（实际移到了回收站 _回收站/，需要的话可以恢复）。" % r["rel"]

        if action == "copy":
            r = library_mod.copy_file(name, new_name)
            if not r.get("ok"):
                return "复制失败：%s" % r.get("error")
            _notify("write", rel=r["to"], chars=0)
            return "已复制：%s → %s" % (r["from"], r["to"])

        if action == "backup":
            r = library_mod.backup_all()
            if not r.get("ok"):
                return "备份失败：%s" % r.get("error")
            return "已备份 %d 个文件到 %s" % (r.get("count", 0), r.get("path", ""))

        if action == "export_docx":
            rd = library_mod.read_file(name)
            if not rd.get("ok"):
                return "导出失败：%s" % rd.get("error")
            base = rd["rel"]
            out = re.sub(r"\.(md|markdown|txt|text)$", "", base, flags=re.I) + ".docx"
            if out == base:
                out = base + ".docx"
            doc_title = os.path.splitext(os.path.basename(base))[0]
            text = rd.get("text") or ""
            # 优先走完整排版模块（认 Markdown 的标题/列表/表格/引用，样式统一）；
            # 万一它不可用，退回原来的纯标准库实现，保证这条路永远不断。
            try:
                import tempfile

                from . import docx_maker as _dm
                tmp = os.path.join(tempfile.gettempdir(),
                                   "mm_exp_%d.docx" % int(time.time() * 1000))
                r = _dm.build_docx_text(tmp, doc_title, text,
                                        theme=str((arguments or {}).get("theme")
                                                  or "blue"),
                                        font=str((arguments or {}).get("font")
                                                 or "yahei"),
                                        toc=bool((arguments or {}).get("toc")))
                if not r.get("ok"):
                    raise RuntimeError(r.get("error") or "生成失败")
                with open(tmp, "rb") as f:
                    data = f.read()
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            except Exception as e:
                logging.getLogger("uvicorn.error").warning(
                    "排版模块导出失败，退回简易版：%s", e, exc_info=True)
                data = docx_write.text_to_docx(text, title=doc_title)
            w = library_mod.save_bytes(out, data)
            if not w.get("ok"):
                return "导出失败：%s" % w.get("error")
            _notify("write", rel=w["rel"], chars=0)
            return ("已导出 Word 文档：%s（%d 字节）。WPS 和 Word 都能直接双击打开。\n"
                    "告诉用户去生成文库面板下载/打开它。" % (w["rel"], w.get("bytes", 0)))

        return "未知的 action：%s（可用 list/read/write/append/delete/copy/backup/export_docx）" % action
    except Exception as e:
        return "文库操作失败：%s: %s" % (type(e).__name__, e)


# =====================================================================
#  请用户配置高德 key
# =====================================================================
def _do_connect_amap(arguments, context=None):
    """弹一个输入框请用户填高德 key，填完**当场验证**、通过就存下、立刻生效。

    为什么不直接让用户去改 config.json：那要求他找到文件、改 JSON、再重启应用，
    对新机器上手来说太重了。这里复用"问用户"那条通道，在界面上填一下就行。
    """
    from . import amap as _am
    from . import config as _cfg

    if _am.has_key():
        return "已经配好高德 key 了，直接用地图工具查就行，不用再问用户。"

    reason = str((arguments or {}).get("reason") or "").strip()
    ask = (context or {}).get("ask")
    if not callable(ask):
        return ("用户界面没连上，没法请他填 key。请直接告诉用户："
                "去 https://console.amap.com/dev/key/app 申请一个"
                "「**Web服务**」类型的 key，然后点界面顶栏的「高德 key」按钮填进去。")

    questions = [{
        "question": "请粘贴高德开放平台的 key（32 位，服务平台要选「Web 服务」）：",
        "header": "高德 key",
    }]
    answers = ask({
        "title": "🗺️ 填一个高德 key，地图能力会好很多",
        "hint": (reason + "\n" if reason else "") +
                "填了之后：能搜到全国的小店、有真实评分、有实时路况、有公交换乘，"
                "步行骑行也是真实路径。不填也能用，只是数据很弱（用的是 OpenStreetMap）。\n"
                "申请：console.amap.com → 实名 → 建应用 → 加 Key → "
                "**服务平台必须选「Web 服务」**。\n"
                "不想填就直接关掉这个框，我按没有 key 继续做。",
        "questions": questions,
        "freeInput": True,
    })
    ans = ""
    for a in (answers or []):
        if isinstance(a, dict):
            ans = (a.get("answer") or "").strip() or ans
        else:
            ans = str(a or "").strip() or ans
    if not ans:
        return ("用户没有填（可能直接关了弹框）。**按没有高德 key 继续做事** ——"
                "地图会退回 OpenStreetMap：能查大城市/道路/机场车站，但小店、评分、"
                "路况、公交都没有。在回答里如实说明这一点，不要再反复追问。")

    ok, msg = _am.verify_key(ans)
    if not ok:
        return ("用户填的 key 没通过验证：%s\n"
                "请把这个原因原样告诉他，并请他重新申请/重新复制一个 ——"
                "他也可以选择不填，那就按没有 key 继续。" % msg)
    cfg = _cfg.load_config()
    cfg["amap_key"] = ans
    _cfg.save_config(cfg)
    return ("✅ %s\n"
            "（key 已经保存并**立刻生效**，不用重启。接下来直接用 map_plan / nearby_places "
            "查就行；结果里会带真实评分、实时路况和公交换乘。）" % msg)


# =====================================================================
#  反问用户
# =====================================================================
# ⚠️ 只防"模型发疯"用的兜底上限，不是产品限制 —— 用户要求问题个数不限制。
# 20 个问题已经远超任何真实任务的需要，真到这一步说明模型在凑数，
# 提示词里已经写明"每个都要是关键问题"。
_ASK_HARD_CAP = 20


def _do_ask_user(arguments, context=None):
    """把问题弹到界面上，等用户回答。复用"确认弹窗"那条通道。"""
    qs = (arguments or {}).get("questions") or []
    if not isinstance(qs, list) or not qs:
        return "错误：没有可问的问题。"
    norm = []
    # ⚠️ 这里**不再截断成 4 个**。用户 2026-09-22 明确要求"每次询问的问题个数也可以不限制"。
    # 以前写 `qs[:4]`，第 5 个之后的问题会被**静默丢掉**（模型以为问了、用户根本没看到），
    # 比"问得多"糟得多。现在的约束放在**提示词**里：不许重复、每个都要是关键问题。
    # 兜底只防"模型发疯"（几十上百个问题会把弹框撑爆），给一个很高的上限。
    for q in qs[:_ASK_HARD_CAP]:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:4]
        norm.append({"question": text,
                     "header": str(q.get("header") or "").strip()[:12],
                     "options": opts, "multi": bool(q.get("multi"))})
    if not norm:
        return "错误：问题的格式不对，至少要有一个非空 question。"
    ask = (context or {}).get("ask")
    if not callable(ask):
        return ("现在没有可用的提问通道（用户界面没连上），先把最合理的默认方案做出来，"
                "并在回答里说明你假设了什么、哪些地方需要他补充。")
    answers = ask({"questions": norm,
                   "hint": ("🔍 深度询问：模型可以多轮追问，问透为止。"
                            "填完它就接着做；不想答的直接跳过。"
                            if str((context or {}).get("ask_mode") or "") == "deep"
                            else "💬 快速了解：只问最关键的几条，答完就开工。"
                                 "不想答的直接跳过。")})
    if not answers:
        return ("用户没有回答（可能直接关掉了弹框）。请**按最合理的默认假设继续做**，"
                "并在回答开头明确写出你替他假设了哪些条件，方便他纠正。")
    lines = ["【用户补充的信息】"]
    for i, a in enumerate(answers, 1):
        if isinstance(a, dict):
            lines.append("%d. %s → %s" % (i, a.get("question", ""), a.get("answer", "")))
        else:
            lines.append("%d. %s" % (i, a))
    lines.append("⚠️ **这一轮还没有结束** —— 请**接着把东西做出来**"
                 "（该写的写、该做的做），不要只回一句「好的」"
                 "或停下来等用户再发一条消息。")
    lines.append("基于这些信息接着做。")
    # ⚠️ 不要写成"不要再问了" —— 用户 2026-09-22 明确要求**允许多轮询问**，
    # 只是不许问**重复的**。写死"只问一次"会逼模型在信息不足时硬做。
    lines.append("⚠️ 上面这些**已经问过、用户已经答过**的，一个字都不要再问。"
                 "但如果你根据答复**发现了新的关键疑问**（影响你怎么做的那种），"
                 "可以**再调用一次 ask_user 问一轮** —— 别硬猜着做；"
                 "没有新的关键疑问就一次做完。")
    return "\n".join(lines)


def _do_search_knowledge(arguments):
    """检索本地知识库。让模型**自己决定**要不要查、查什么。

    以前只有"每轮自动注入前 top_k 篇"，模型没法在需要时多查几轮；
    现在给它这个工具，它可以：先列目录看有哪些资料 → 再按关键词精查 →
    必要时换关键词再查一遍，甚至与联网搜索同时使用。
    """
    from . import kb as kb_mod
    docs = kb_mod.list_documents()
    if not docs:
        return ("知识库是空的。请告诉用户：把 .txt/.md 文档放进知识库文件夹即可，"
                "之后就能自动检索。")

    # 先摸清有哪些资料
    if arguments.get("list_all"):
        # 用树形输出（按文件夹分组）。原来的实现有两个毛病：
        #   · 只显示 basename，看不出文件在哪个子文件夹里
        #   · 用 os.path.join(KB_DIR, fn) 直接打开 —— 子目录里的文件**打不开**，
        #     开头摘要永远是空的（静默失败，最难查）
        return ("知识库的目录结构（按文件夹分组，`·` 后面是开头摘要）：\n"
                + kb_mod.tree_text()
                + "\n\n需要细节时用 query 针对性检索；"
                  "也可以按文件夹找 —— 文件名可带路径，如 `课程A/第一章/讲义.md`。")

    query = (arguments.get("query") or "").strip()
    if not query:
        return ("请给出 query 参数（检索关键词），或用 list_all=true 先列出知识库有哪些文档。")

    hits = kb_mod.search(query, top_k=int(arguments.get("top_k") or 6))
    if not hits:
        return (f"知识库里没有与「{query}」相关的内容。"
                f"（当前共 {len(docs)} 篇文档；可换关键词再试，或 list_all=true 看看都有什么）")
    lines = [f"知识库检索结果（关键词：{query}）："]
    for i, h in enumerate(hits, 1):
        # 带上所在文件夹，模型才能说清"出自哪一篇"，也能据此去翻同目录的其他资料
        title = h.get("rel") or h.get("filename") or h.get("doc_id") or "?"
        body = (h.get("content") or "").strip()
        lines.append(f"\n[{i}] 《{title}》\n{body[:1200]}")
    lines.append("\n（以上来自用户自己的知识库，比联网结果更贴合其领域；"
                 "回答时请优先采用，并注明出自哪一篇。）")
    return "\n".join(lines)


def _do_search_memory(arguments):
    query = arguments.get("query") or ""
    hits = memory_mod.search_all(query, top_k=5)
    if not hits:
        return "没有找到相关记忆信息。"
    lines = ["检索到的历史记忆："]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h.get('level','?')}]{h.get('content','')}")
    return "\n".join(lines)