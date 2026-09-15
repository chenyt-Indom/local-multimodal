# -*- coding: utf-8 -*-
"""本地多模态助手 —— FastAPI 后端服务
用法:
    py -3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或直接:
    py -3 run.py
"""
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import os
import sys
import base64
import io
import json
import time
import asyncio
import datetime
import threading
from . import (config, ollama_client, memory, kb, file_tools, video, web_tools,
               t2i, tools, voice, sessions, image_library)

app = FastAPI(title="本地多模态助手", version="1.0.0")
client = ollama_client.OllamaClient()

# 启动时就把知识库目录建好 —— 用户可以直接往里拷 .txt / .md，
# 不用先"导入"一次才知道往哪放。
try:
    kb.ensure_dir()
except Exception:
    pass

# 最近一次出现过的图片（base64），供后续「把这张图改成…」直接微改，免去重新导入。
# 单用户桌面应用，保存最近一张即可；换新图时自动覆盖。
_LAST_IMAGE: dict = {"b64": None, "ts": 0.0, "ttl": 3600.0}


def _remember_image(images: list) -> None:
    """记住本轮图片，供下一轮引用。"""
    if images:
        _LAST_IMAGE["b64"] = images[-1]
        _LAST_IMAGE["ts"] = time.time()


def _recent_image() -> list:
    """取回最近一张仍在有效期内的图片（列表形式，未过期才返回）。"""
    b64 = _LAST_IMAGE.get("b64")
    if b64 and (time.time() - _LAST_IMAGE.get("ts", 0)) < _LAST_IMAGE.get("ttl", 3600):
        return [b64]
    return []


# 启动时做一次轻量清理：移除「太久未用 + 几乎没内容」的僵尸会话。
# 有实际内容的会话一律保留；真正重要的信息由长期记忆承载，不靠聊天记录堆积。
try:
    _cleaned_sessions = sessions.cleanup_old()
except Exception:
    _cleaned_sessions = 0


def _now_str() -> str:
    """返回本地当前时间的中文描述，供注入系统提示，让模型具备时间感知。"""
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日（星期{wd}），{now.strftime('%H:%M:%S')}"


# 简单问题识别：命中则收紧生成长度，避免模型对"你好"这类问题长篇思考。
# qwen3-vl 的 thinking 无法通过 API 关闭（think=false /no_think 均实测无效，
# 提示词引导反而让它想更多），**限制 num_predict 是唯一有效手段**：
# 实测「你好」从 24.3s 降到 3.9s。
_COMPLEX_HINTS = (
    "写", "画", "生成", "做一份", "方案", "报告", "代码", "脚本", "文件", "搜索",
    "查一下", "查查", "总结", "翻译", "分析", "对比", "设计", "规划", "计划",
    "记住", "帮我", "为什么", "怎么", "如何", "详细", "解释", "列出", "整理",
    # 图片相关一律走完整模式：微改需要输出工具调用，收紧长度会被思考吃光
    "图", "照片", "图片", "这张", "那张", "刚才", "上面", "改成", "换成", "修改",
)


def _is_simple_question(text: str) -> bool:
    """判断是否属于可快速作答的简单问题。

    注意：只做**文本**层面的判断；调用方还需确认本轮没有图片上下文
    （见 chat()），否则模型可能来不及输出工具调用。
    """
    t = (text or "").strip()
    if not t or len(t) > 30:
        return False
    return not any(k in t for k in _COMPLEX_HINTS)


# 简单问题的生成长度上限（思考+回答总量）。512 足以覆盖问候/常识问答，
# 又能把"先想很久"压到 3~5 秒；复杂问题仍用配置里的完整配额。
SIMPLE_MAX_TOKENS = 512

# 联网场景的生成长度下限：要把搜索结果喂给模型 + 让它逐条列出来源链接，
# token 消耗远高于普通问答。给少了就会出现"搜索完了但没输出回答"。
WEB_MAX_TOKENS = 4096

# 输出长度天花板：空回答重试时加倍，但不能无限涨
# （上下文窗口还要留给提示词与历史，超出只会让 Ollama 截断提示词）
MAX_TOKENS_CEILING = 8192

# 送入模型的历史消息上限（约 40 轮）。
# 之前是 40 条（20 轮），实测**玩"成语接龙"这种多轮小游戏时不够**：
# 超过 20 轮以后，第 1 轮的内容会被裁掉，用户回头问"第一轮接的是什么"，
# 模型只能说"忘记了"。轮次密集的短对话并不占多少 token，放宽到 80 条。
MAX_CONTEXT_MESSAGES = 80


def _est_tokens(text) -> int:
    """粗略估算 token 数：中文约 1.2 字/token，其余约 3.5 字符/token。

    只需要"够准到能做预算"，不追求精确——故意偏保守（宁可少带历史）。
    """
    if not text:
        return 0
    s = str(text)
    cn = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return int(cn / 1.2) + int((len(s) - cn) / 3.5) + 1


def _history_digest(dropped: list, per_msg: int = 90, cap: int = 1500) -> str:
    """把被裁掉的早期对话压成一段摘要，避免模型"完全不记得开头"。

    为什么不用 LLM 做摘要：那要额外一轮推理（几秒起步），还会把首字延迟拉长；
    而这里的目标只是"别忘光"。逐条抽取 + 截断就够用了 ——
    对"成语接龙"这类短消息场景几乎是无损的，长消息也留住了开头信息。

    ⚠️ 标签必须写「用户 / 助手」，不能写「我 / 你」。
    实测：写成"我/你"时模型会搞混指代，把摘要里的内容当成了别的东西，
    用户问"第一轮接的是什么"照样答错。系统提示里没有"我/你"的明确指向，
    换成角色名才不会有歧义。

    注意：这是**兜底**，不是主力。真正的历史仍然尽量完整保留（见 MAX_CONTEXT_MESSAGES）。
    """
    if not dropped:
        return ""
    lines, total, rnd = [], 0, 0
    pending_user = None
    for m in dropped:
        role = m.get("role")
        text = " ".join(str(m.get("content") or "").split())
        if not text:
            continue
        if len(text) > per_msg:
            text = text[:per_msg] + "…"
        if role == "user":
            pending_user = text
            rnd += 1
            continue
        if role == "assistant":
            if pending_user is not None:
                lines.append(f"【第{rnd}轮】用户：{pending_user}　｜　助手：{text}")
                pending_user = None
            else:
                lines.append(f"（早期）助手：{text}")
        if total + len(text) > cap:
            break
        total += len(text)
    if pending_user is not None:
        lines.append(f"【第{rnd}轮】用户：{pending_user}")
    if not lines:
        return ""
    return ("【较早对话摘要｜必须记住】以下内容是本次对话**开头**的部分，"
            "因为长度限制已移出上下文，但你**仍然要记得**。\n"
            "用户若问起「最开始」「第一轮」「开头」聊了什么，就依据本段回答：\n"
            + "\n".join(lines))


def _trim_history_to_budget(messages: list, sys_prompt: str, tool_schemas: list,
                            cfg: dict) -> tuple:
    """按上下文预算裁剪历史消息。

    token 账：num_ctx = 系统提示 + 工具定义 + 历史 + 本轮输出 + 检索材料。
    系统提示/工具定义/检索材料都是"写死的开销"，唯一能压缩的就是历史，
    所以这里从最旧的开始丢，直到装得下。

    **返回 (保留的历史, 被丢掉的历史)**。
    丢掉的那部分不是直接扔 —— 调用方会把它压成「较早对话摘要」注入，
    否则用户回头问"开头聊了什么"，模型会一脸茫然（实测踩过）。
    """
    try:
        ctx_limit = int(cfg.get("num_ctx") or 8192)
        reserve_out = int(cfg.get("max_tokens") or 2048)
    except Exception:
        ctx_limit, reserve_out = 8192, 2048

    import json as _json
    overhead = (_est_tokens(sys_prompt)
                + _est_tokens(_json.dumps(tool_schemas, ensure_ascii=False)))
    # 联网时，工具结果（8 条检索结果 + 若干篇网页正文）会在工具循环里
    # 追加进上下文，此时还不知道具体多大 —— 按实测约 3600 token 预留，
    # 否则"历史 + 检索材料"一起会撑爆窗口，Ollama 直接截断提示词。
    search_reserve = 3800 if cfg.get("web_enabled") else 0
    budget = ctx_limit - reserve_out - overhead - search_reserve - 512
    if budget <= 0:
        # 连固定开销都快占满了：只带最近 2 条，别把提示词撑爆
        return messages[-2:], messages[:-2]

    kept, used = [], 0
    for i, m in enumerate(reversed(messages)):
        if len(kept) >= MAX_CONTEXT_MESSAGES:
            break
        t = _est_tokens(m.get("content") or "")
        if kept and used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    dropped = messages[: len(messages) - len(kept)]
    return kept, dropped


# 触发自动记忆的信号词：出现这些词说明用户可能透露了值得长期记住的信息。
# 只在命中时才做后台提炼，避免每轮都多跑一次模型。
#
# 之前这张表太窄（只有"记住/我是/我的…"），结果用户说
# 「我平时用 Python」「以后别用英文回答我」「我们公司用的是飞书」
# 这类明显该记的信息，因为没命中关键词而**根本没进长期记忆**。
# 这里按"身份 / 偏好 / 约定 / 项目"四类补全。
_MEMORY_SIGNALS = (
    # 显式要求
    "记住", "别忘", "记一下", "记录一下", "提醒我", "以后", "下次", "今後",
    # 身份 / 背景
    "我叫", "我是", "我的名字", "叫我", "我在", "我们公司", "我们单位", "我们学校",
    "我读", "我学", "专业", "职业", "岗位", "生日", "年龄", "岁",
    # 偏好 / 习惯
    "我喜欢", "我不喜欢", "我习惯", "我平时", "我一般", "我通常", "偏好",
    "讨厌", "不喜欢", "受不了", "最好", "不要用", "别用", "要用",
    # 约定 / 约定俗成
    "约定", "规定", "统一", "默认", "一律", "每次都要", "以后都", "风格",
    "语气", "格式", "称呼",
    # 项目 / 工作
    "我的项目", "我在做", "我们在做", "正在开发", "技术栈", "用的是什么",
    "环境是", "部署在", "服务器", "版本是",
    # 经历 / 成果 —— 这类信息**天然不带任何标记词**，是漏记最多的一类：
    # 用户讲自己的经历时不会说"记住"，只会平铺直叙"我做过…"。
    "做过", "搞过", "写过", "参加过", "拿过", "获过", "得过", "考过", "学过",
    "我用过", "我熟悉", "我负责", "我参与", "我曾经", "我以前", "以前在",
    "比赛", "获奖", "拿奖", "证书", "实习", "兼职", "项目经验", "实验室",
    # 技能 / 工具
    "我会用", "我会写", "熟练", "习惯用", "常用",
)


def _looks_memorable(text: str) -> bool:
    return any(k in (text or "") for k in _MEMORY_SIGNALS)


# ---------------------------------------------------------------
#  后台记忆提炼的调度（防抖）
# ---------------------------------------------------------------
# 以前是"命中信号词才提炼 + 每 4 轮兜底一次"，两个毛病：
#   · 只分析**最后一条用户消息**，前面几轮讲的重要信息（尤其经历类）永远轮不到；
#   · 兜底要等满 4 轮，聊天一停就再也不会提炼了。
#
# 现在改成：**每轮结束都安排一次提炼**，但做防抖 ——
# 提炼要占用同一个 Ollama 模型（实测约 25 秒），立刻跑会把紧接着的下一条消息堵在队列里。
# 所以：用户停手 6 秒后再提炼；若一直不停嘴，最迟 45 秒也强制提炼一次。
# 每次带上**最近几轮**对话，前几轮讲的事不会漏。
_MEM_DEBOUNCE_SEC = 6.0        # 停手这么久才动
_MEM_FORCE_AFTER_SEC = 45.0    # 连续聊天时，最迟这么久必须提炼一次
_MEM_WINDOW_TURNS = 8          # 每次最多带上最近 8 条消息（约 4 轮）

_mem_pending: dict = {}
_mem_lock = threading.Lock()


def _schedule_memory_extract(session: str, turns: list, model: str, cfg: dict,
                             urgent: bool = False) -> None:
    """安排一次后台记忆提炼（防抖 + 串行，详见上面的说明）。"""
    now = time.time()
    with _mem_lock:
        st = _mem_pending.get(session) or {}
        old_timer = st.get("timer")
        if old_timer:
            old_timer.cancel()
        first_ts = float(st.get("first_ts") or now)
        buf = (list(st.get("turns") or []) + list(turns or []))[-_MEM_WINDOW_TURNS:]
        st["turns"] = buf
        st["first_ts"] = first_ts
        st["timer"] = None
        if st.get("running"):
            # 上一轮提炼还没跑完：这次的内容已经并入缓冲区，等它跑完再来
            _mem_pending[session] = st
            return
        delay = 0.0 if (urgent or (now - first_ts) >= _MEM_FORCE_AFTER_SEC) else _MEM_DEBOUNCE_SEC
        t = threading.Timer(delay, _run_memory_extract, args=(session, model, cfg))
        t.daemon = True
        st["timer"] = t
        _mem_pending[session] = st
        t.start()


def _run_memory_extract(session: str, model: str, cfg: dict) -> None:
    with _mem_lock:
        st = _mem_pending.get(session) or {}
        turns = list(st.get("turns") or [])
        st["turns"] = []
        st["first_ts"] = time.time()
        st["running"] = True
        _mem_pending[session] = st
    try:
        _auto_extract_memory(session, turns, model, cfg)
    except Exception:
        pass
    finally:
        with _mem_lock:
            st = _mem_pending.get(session) or {}
            st["running"] = False
            _mem_pending[session] = st


# 抽取调用的输出预算。
# ⚠️ **这里是"该记的没记住"的真正原因，务必看清**：
# qwen3 系列是思考型模型，做这类抽取时会**先思考 2400~2600 字**才动笔。
# 实测（qwen3-vl:8b，同一段输入）：
#     num_predict = 300  → done_reason=length，content **一个字都没有**
#     num_predict = 500  → 同上（_sweep_one 原来是这个值）
#     num_predict = 1500 → 同上
#     num_predict = 4000 → done_reason=stop，要点正常输出
# 老代码给 300，于是**每一次自动提炼都在"空结果"上静默 return** ——
# 只有用户明确说"记住"时走的 remember 工具（聊天路径预算大）才写得进去。
# 试过 /no_think、系统提示写"不要思考"、把指令写得极简：**全都压不住它**，
# 唯一的解法就是把思考的额度给够。
_EXTRACT_TOKENS = 3000
_EXTRACT_TOKENS_RETRY = 6000


def _llm_extract(prompt: str, model: str, cfg: dict,
                 budget: int = _EXTRACT_TOKENS) -> str:
    """跑一次"要点抽取"调用，返回模型正文；拿不到就返回空串。

    两个必须守住的点：
    1. **num_ctx 必须与聊天一致**（所以直接复用 cfg）—— Ollama 一旦发现
       num_ctx 与已加载的不同，就会卸载并重载模型（约 5 秒），
       而**重载会中断正在进行的生成**。老代码写死 4096 就是这么把回答打断的。
    2. 预算要够思考用（见 _EXTRACT_TOKENS）；万一还是被截断又没出正文，
       自动把预算翻倍重试一次 —— **绝不能静默失败**。
    """
    params = dict(cfg)
    params["temperature"] = 0.2
    params["max_tokens"] = budget
    for _ in range(2):
        try:
            resp = client.chat([{"role": "user", "content": prompt}], model=model,
                               stream=False, params=params)
            data = resp.json() if hasattr(resp, "json") else resp
            msg = data.get("message") or {}
            text = (msg.get("content") or "").strip()
            if text:
                return text
            # 正文为空：多半是思考把额度吃光了 → 加预算再来一次
            if data.get("done_reason") != "length":
                return ""
        except Exception:
            return ""
        params["max_tokens"] = _EXTRACT_TOKENS_RETRY
    return ""


# 判定"该不该记"的标准。要点是**把判断权交给模型**，而不是靠关键词 ——
# 关键词必然漏（"我做过一个考勤系统"里没有任何"记住/我是"）。
_EXTRACT_PROMPT = """你是"用户档案整理员"。阅读下面的对话，挑出值得**长期留存**的用户信息。

【必须提取】（只要出现就写下来）
· 身份背景：姓名、年龄、职业、学校/单位、专业、居住地、家庭情况
· 经历成果：做过什么项目、参加过什么比赛或活动、干过什么工作、拿过什么奖
· 技能工具：会哪些编程语言、用过什么软件/框架/硬件、熟练程度
· 偏好习惯：喜欢/讨厌什么、希望怎么回答、惯用工作方式
· 约定规则：要求以后都遵守的规则、称呼、格式、语气
· 目标计划：想学什么、打算做什么、正在准备什么、答应过要做的事
· 目标进展：之前提过的目标/计划有了新进展（做完了、没做成、改主意了、放弃了）

【绝对不要提取】
· 寒暄闲聊（你好、谢谢、哈哈）和临时指令（"再短一点""换个说法"）
· 一次性的具体提问（"帮我查天气""这段代码哪错了"）
· 助手自己说的话、搜索结果、知识库资料
· 【已知信息】里已经有的内容 —— **绝对不要重复记**

【记成长期还是短期】问自己一句话：换个话题还成立吗？
· 成立 → 长期（跨所有对话通用）
· 只在当前这件事里成立 → 短期（只服务这个对话）

【输出格式】每行一条，**只写有变化的内容**：
· 新信息 → `长期|具体事实` 或 `短期|具体事实`
· 已有信息要改动 → `更新|旧要点|新要点`
  ↳「旧要点」**必须照抄**【已知信息】里的原句（至少前 10 个字），照抄才能改到它
· 已有信息作废 → `删除|旧要点`（同样是照抄原句）

【目标与计划 · 特别重要】
· 用户说「想/打算/准备/计划/以后要…」→ 记成目标，写清 **做什么 + 大致时间 + 当前状态**：
  例 `长期|计划2027年6月考网络工程师，目前还没开始`
· 用户后来提到同一件事的进展 → **必须用 `更新|` 改写原来那条，绝对不要新增一条**：
  例 `更新|计划2027年6月考网络工程师|已报名，2027年3月开班，正在看教材`
· 只有改写，档案才不会自相矛盾 —— 「打算考」和「已经考完」并存会让模型答错话
· 用户没说不做了，就别因为"还没做"而删掉目标；那正是需要被记住的事

【怎么算值得记】问自己：这句话**三个月后**还用得上吗？
· 用得上 → 记；只是一次性问答、闲聊、临时要求 → 不记
· 一条不超过 50 字，第三人称陈述句，**保留姓名、数字、名称、技术栈**
· 确实没有任何变化，就只输出两个字：无

【已知信息】（不要重复这些）
{long}

{short}

【本轮对话】
{convo}"""


# 提炼结果里的"动词"：模型除了新增，还可以改已有条目、删掉作废条目。
# 这三个词决定了**目标类信息能不能跟着进展走**（见 memory.py 里 update_long 的说明）。
_VERB_REVISE = ("更新", "修改", "修正", "改为", "调整", "推进")
_VERB_FORGET = ("删除", "作废", "取消", "遗忘", "移除", "无效")
_KIND_LONG = ("长期", "全局")
_KIND_SHORT = ("短期", "当前对话", "本对话", "本会话", "本次")


def _clean_extract_line(raw: str) -> str:
    line = raw.strip().lstrip("-*• ").strip("`").strip()
    while line[:1].isdigit():
        line = line[1:].lstrip("、.)． ").strip()
    if line.startswith("【") and "】" in line:
        inner = line.split("】", 1)
        if inner[1].strip():
            line = inner[1].strip()
    return line


def _split_extract_line(line: str) -> list:
    # 竖线优先（标准格式），其次容忍「长期：xxx」这种中文冒号写法
    for sep in ("|", "｜", "：", ":"):
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    return [line]


def _forget_point(old: str, kind: str, session: str) -> bool:
    if kind in _KIND_LONG:
        return memory.drop_long(old)
    if kind in _KIND_SHORT:
        return memory.drop_short(session, old)
    return memory.drop_long(old) or memory.drop_short(session, old)


def _revise_point(old: str, new: str, kind: str, session: str) -> bool:
    new = (new or "").strip()
    if not new:
        return False
    if kind in _KIND_LONG:
        return memory.update_long(old, new) != "skipped"
    if kind in _KIND_SHORT:
        return memory.update_short(session, old, new) != "skipped"
    # 没写类别：先在长期里找、再在短期里找；两处都没有就按新信息追加（进短期，更安全）
    if memory.update_long(old, new, append_missing=False) == "replaced":
        return True
    if memory.update_short(session, old, new, append_missing=False) == "replaced":
        return True
    return memory.merge_short(session, new)


def _apply_extract(text: str, session: str, limit: int = 4) -> int:
    """把提炼结果按行并入记忆，返回真正生效的条数。

    支持三种指令（模型写法可能不规范，这里尽量宽容）：
      长期|要点 / 短期|要点      → 新增
      更新|旧要点|新要点         → 改写已有条目（**目标的进展就靠这个**）
      删除|旧要点                → 作废已有条目
    没写类别的新信息一律进**短期** —— 宁可少进一点长期记忆，
    也不要让乱七八糟的东西污染跨所有对话生效的全局档案。
    """
    if not text:
        return 0
    kept = 0
    for raw in text.splitlines():
        line = _clean_extract_line(raw)
        if not line or line.startswith("#"):
            continue
        if line.startswith("无") and len(line) <= 8:
            continue
        parts = _split_extract_line(line)
        verb = parts[0].strip().strip("【】[]（）() ")
        rest = [p.strip().strip("【】[]（）() ") for p in parts[1:]]
        kind = ""
        if rest and (rest[0] in _KIND_LONG or rest[0] in _KIND_SHORT):
            kind = rest.pop(0)          # 容忍 "更新|长期|旧|新" 这种多写一层的写法
        if verb in _KIND_LONG or verb in _KIND_SHORT:
            verb, kind = "", verb       # "长期|要点"：第一段写的其实是类别
        try:
            if any(v in verb for v in _VERB_FORGET) and rest:
                if _forget_point(rest[0], kind, session):
                    kept += 1
            elif any(v in verb for v in _VERB_REVISE) and len(rest) >= 2:
                if _revise_point(rest[0], " ".join(rest[1:]), kind, session):
                    kept += 1
            else:
                val = (" ".join(rest) if rest else verb).strip().strip("，。；;、 ")
                if len(val) < 3:
                    continue
                val = val[:120]
                ok = memory.merge_long(val) if kind in _KIND_LONG else memory.merge_short(session, val)
                if ok:
                    kept += 1
        except Exception:
            continue
        if kept >= limit:
            break
    return kept


def _auto_extract_memory(session: str, turns: list, model: str, cfg: dict) -> None:
    """把**最近几轮**对话里的要点提炼进记忆（后台线程，不阻塞回复）。

    长期记忆是"久远对话可被清理"的前提 —— 要点沉淀下来后，
    老的聊天记录才可以安全裁剪或清除。**所以释放归档前必须先跑一遍这个沉淀**，
    否则还没提炼的内容会跟着归档一起消失。

    为什么带"最近几轮"而不是只看最后一条消息：
    用户常常在闲聊中顺口讲出自己的经历（"我之前做过一个考勤系统…"），
    紧接着又聊别的。只看最后一条，这类信息必然漏掉。

    为什么要把【已知信息】喂给模型：不然它会把同一件事反复写成不同措辞，
    长期记忆里堆出一串"同义句"（实测见过同一个人被记了三遍），白白吃掉 15000 字上限。
    """
    if not turns:
        return
    convo = "\n".join(
        "%s：%s" % ("用户" if m.get("role") == "user" else "助手",
                    str(m.get("content") or "")[:300])
        for m in turns if (m.get("content") or "").strip())
    if not convo:
        return
    known_long = memory.get_long().strip() or "（暂无）"
    known_short = memory.get_short(session).strip() or "（暂无）"
    prompt = _EXTRACT_PROMPT.format(long=known_long[-2500:], short=known_short[-800:],
                                    convo=convo)
    text = _llm_extract(prompt, model, cfg)
    _apply_extract(text, session, limit=4)



# 允许本地界面跨域访问（浏览器 debug 时用）
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# 前端静态目录（打包后为只读打包资源）
FRONTEND_DIR = config.res("frontend")


# ---------- 数据模型 ----------
class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    images_b64: list[str] | None = None   # 附加到本轮 user 消息的图片
    docs: list[dict] | None = None        # 拖进来的文档：[{name, text}]
    stream: bool = True
    session_id: str | None = None         # 会话标识，用于历史会话透视归档


# ---------- 图片保存 ----------
class SaveImageRequest(BaseModel):
    b64: str | None = None                # 图片 base64 正文（无 data: 前缀）
    filename: str | None = None           # 期望文件名（自动去重）
    mime: str | None = None               # 如 image/png
    subdir: str | None = None             # 可选子目录，默认 saved_images


@app.post("/api/save_image")
def save_image(req: SaveImageRequest):
    """把图片（base64）保存到应用可写的本地目录，返回最终绝对路径。

    解决桌面端 pywebview 里 <a download + data URI> 无法保存的问题：
    图片由后端写盘，前端用返回路径提示用户，并可通过系统的
    os.startfile 打开对应文件夹。
    """
    import re, time, uuid
    if not req.b64:
        raise HTTPException(400, "缺少图片数据")
    try:
        raw = base64.b64decode(req.b64)
    except Exception as e:
        raise HTTPException(400, f"base64 解码失败: {e}")
    if not raw:
        raise HTTPException(400, "图片数据为空")

    subdir = req.subdir or "saved_images"
    root = config.data(subdir)
    os.makedirs(root, exist_ok=True)

    base_name = (req.filename or "image").strip() or "image"
    ext = (req.mime or "image/png").split("/")[-1].split(";")[0].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    if not base_name.lower().endswith(f".{ext}"):
        base_name += f".{ext}"

    safe = re.sub(r"[^\w.\-]", "_", base_name, flags=re.UNICODE) or "image.png"
    path = os.path.join(root, safe)
    stem, e = os.path.splitext(safe)
    i = 1
    while os.path.exists(path):
        path = os.path.join(root, f"{stem}_{i}{e}")
        i += 1
    with open(path, "wb") as f:
        f.write(raw)
    return {"ok": True, "path": path.replace("/", "\\"),
            "filename": os.path.basename(path)}


class OpenFolderRequest(BaseModel):
    path: str | None = None


def _is_container() -> bool:
    """是否运行在容器里（Docker 会创建 /.dockerenv）。"""
    if os.environ.get("MM_IN_CONTAINER"):
        return True
    return os.path.exists("/.dockerenv")


def _host_path(container_path: str) -> str:
    """把容器内路径映射回宿主机的实际路径。

    容器里没法打开宿主机的文件管理器，但可以把「宿主机上对应哪个目录」
    告诉用户（部署脚本会把该目录写进 MM_HOST_DATA_DIR）。
    """
    base = (os.environ.get("MM_HOST_DATA_DIR") or "").rstrip("/\\")
    if not base:
        return container_path
    root = config.data_root().rstrip("/\\")
    if not container_path.startswith(root):
        return container_path
    rel = container_path[len(root):].replace("/", os.sep).lstrip("/\\")
    return os.path.join(base, rel) if rel else base


# 宿主机上的「打开文件夹」小助手（open-folder-agent.ps1）会使用这两个文件：
#   .open_folder_agent    心跳，用于判断它是否在运行
#   .open_folder_request  请求，容器把路径写进去，由它真正弹出文件夹
_AGENT_BEAT = ".open_folder_agent"
_AGENT_REQ = ".open_folder_request"


def _host_agent_alive(max_age: float = 20.0) -> bool:
    """宿主机上的小助手是否在运行（靠心跳文件的更新时间判断）。"""
    try:
        beat = os.path.join(config.data_root(), _AGENT_BEAT)
        return (time.time() - os.path.getmtime(beat)) < max_age
    except Exception:
        return False


def _request_host_open(target: str) -> bool:
    """把「打开这个目录」的请求写进挂载目录，交给宿主机的小助手执行。"""
    try:
        req = os.path.join(config.data_root(), _AGENT_REQ)
        with open(req, "w", encoding="utf-8") as f:
            f.write(target)
        return True
    except Exception:
        return False


@app.post("/api/open_folder")
def open_folder(req: OpenFolderRequest = None):
    """在系统文件管理器中打开指定路径（默认打开 saved_images 目录）。"""
    return _open_folder_impl((req.path if req and req.path else None)
                             or config.data("saved_images"))


@app.post("/api/kb/open_folder")
def kb_open_folder():
    """打开「知识库」文件夹 —— 用户把 .txt/.md 丢进去就能被检索。"""
    kb.ensure_dir()
    return _open_folder_impl(kb.KB_DIR)


@app.post("/api/library/open_folder")
def library_open_folder():
    """打开「图片库」所在的文件夹。

    打开的目录就是图片实际保存的目录（image_library.DIR），
    保证「看到的」和「存进去的」是同一个地方。
    """
    return _open_folder_impl(image_library.DIR)


def _open_folder_impl(target: str) -> dict:
    """在系统文件管理器中打开一个目录（容器内交由宿主机小助手代劳）。

    - 源码/桌面方式运行：直接调用系统文件管理器
      （Windows explorer / macOS open / Linux xdg-open）
    - 容器方式运行：容器内调不起宿主机的文件管理器，改为把请求写进挂载的数据目录，
      由宿主机上的小助手代为弹出文件夹；若小助手没在运行，
      则退化成「把宿主机真实路径给用户复制」。
    """
    # 注意顺序：必须先判断是不是文件再 makedirs。
    # 若先对"文件路径"调 os.makedirs(exist_ok=True)，路径存在但不是目录时
    # 仍会抛 FileExistsError → 接口 500（前端表现为 r.json() 解析失败）。
    if os.path.isfile(target):
        target = os.path.dirname(target)
    os.makedirs(target, exist_ok=True)

    if _is_container():
        if _host_agent_alive() and _request_host_open(target):
            return {"ok": True, "opened": True, "path": target,
                    "host_path": _host_path(target), "via": "host-agent"}
        return {"ok": True, "opened": False, "path": target,
                "host_path": _host_path(target),
                "detail": "运行在容器里，无法直接打开宿主机的文件夹"}

    try:
        import subprocess
        if os.name == "nt":
            subprocess.Popen(["explorer", target.replace("/", "\\")])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return {"ok": True, "opened": True, "path": target}
    except FileNotFoundError:
        return {"ok": True, "opened": False, "path": target,
                "detail": "当前系统没找到文件管理器，请手动打开上面的路径"}
    except Exception as e:
        raise HTTPException(500, f"打开文件夹失败: {e}")


# ---------- 健康 / 配置 ----------
@app.get("/api/health")
def health():
    return client.health()


@app.get("/api/config")
def get_config():
    return {"config": config.load_config(), "defaults": config.DEFAULT_CONFIG}


@app.post("/api/config")
def update_config(body: dict):
    merged = config.load_config()
    for k, v in body.items():
        if k in config.DEFAULT_CONFIG:
            merged[k] = v
    config.save_config(merged)
    return {"ok": True, "config": merged}


@app.get("/api/net/check")
def net_check():
    """检测本机是否已连接互联网。

    供前端「联网」开关做前置校验：未联网时不允许点亮开关。
    用 TCP 连通性判断（比 HTTP 更快、不受代理/重定向干扰），
    多目标**并行**探测，任意一个通即视为已联网——断网时也能快速返回。
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    targets = [
        ("223.5.5.5", 53),        # 阿里公共 DNS
        ("www.bing.com", 443),
        ("www.baidu.com", 443),
    ]

    def _probe(host_port):
        host, port = host_port
        try:
            conn = socket.create_connection((host, port), timeout=2.0)
            conn.close()
            return host_port
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        for hit in pool.map(_probe, targets):
            if hit:
                return {"online": True, "via": f"{hit[0]}:{hit[1]}"}
    return {"online": False, "error": "无法连接互联网"}


# ---------- 模型管理 ----------
@app.get("/api/models")
def list_models():
    try:
        return {"ok": True, "models": client.list_models()}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/pull")
def pull_model(body: dict):
    name = body.get("model") or config.load_config()["default_model"]
    try:
        client.pull_model(name)
        return {"ok": True, "message": f"模型 {name} 下载完成"}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------- 对话 ----------
MAX_TOOL_ROUNDS = 10  # 单次对话内最多连续调用工具轮次，防止死循环


# =====================================================================
#  代码能力：专用代码模型的路由
# =====================================================================
# 为什么要换模型（实测数据）：默认的 qwen3-vl:8b 是**视觉**模型，
# 写代码时"思考"会失控 —— 同一道 LRU 缓存的题，有时 9 秒正常出代码，
# 有时陷入原地重复的思考死循环（"但是，题目没有说明，所以我们可以不处理"反复刷屏），
# 121 秒后 done_reason=length、正文一个字都没有；3 道题只过 1 道。
# qwen2.5-coder 是**非思考型**的代码专用模型，不存在这个问题。
#
# 路由是**按轮**的：这一轮像写代码就用代码模型，下一轮闲聊自动回到视觉模型。
# 代价是切换模型要重新加载（12GB 显存放不下两个模型），所以只在真需要时才切。
_CODE_HINTS = (
    "写代码", "代码", "脚本", "函数", "程序", "报错", "bug", "调试", "跑一下",
    "正则", "sql", "算法", "数据结构", "排序", "递归", "爬虫", "接口", "重构",
    "python", "javascript", "typescript", "java", "c++", "c#", "golang", "rust",
    "html", "css", "shell", "bash", "bat", "powershell", "json", "api",
    "帮我实现", "实现一个", "写一段", "写个", "单元测试", "帮我改这段",
)

_model_tags_cache = {"t": 0.0, "names": set()}


def _installed_models() -> set:
    """本机已下载的 Ollama 模型名（缓存 60 秒，别每轮都去问一次）。"""
    now = time.time()
    if _model_tags_cache["names"] and now - _model_tags_cache["t"] < 60:
        return _model_tags_cache["names"]
    names = set()
    try:
        import urllib.request
        url = str(config.load_config().get("ollama_url")
                  or "http://127.0.0.1:11434").rstrip("/")
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            for m in (json.loads(r.read().decode()).get("models") or []):
                n = str(m.get("name") or "").strip()
                if n:
                    names.add(n)
                    if ":" not in n:
                        names.add(n + ":latest")
    except Exception:
        return _model_tags_cache["names"]
    _model_tags_cache.update(t=now, names=names)
    return names


def _is_code_task(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    if "```" in t:
        return True
    return any(k in t for k in _CODE_HINTS)


def _route_code_model(cfg: dict, text: str, fallback: str):
    """代码类请求换用专用代码模型，返回 (模型名, 给用户看的提示)。"""
    if not cfg.get("code_auto_route", True):
        return fallback, ""
    want = str(cfg.get("code_model") or "").strip()
    if not want or want == fallback:
        return fallback, ""
    if not _is_code_task(text):
        return fallback, ""
    if want not in _installed_models():
        # 还没下载 → 静默用回默认模型。配置名留着，用户下载后自动生效，不用改设置。
        return fallback, ""
    return want, "已切到代码模型 %s（专用代码模型，不思考、写代码更稳）" % want


class _SystemPrompt:
    """WorkBuddy 风格系统提示：精简注入分层记忆，给出工具使用引导。"""

    @staticmethod
    def build(last_user_text: str, session: str = "", docs: list = None):
        cfg = config.load_config()
        parts = [
            # 时间感知：让模型始终知道"今夕是何年何时"，避免说"不知道今天日期"
            "当前时间：" + _now_str(),
            "你是本地多模态助手，像一位能干的项目助理。你的所有处理都在用户本机完成，注意保护隐私。",
            "你可以调用以下工具来完成具体任务，而不仅是空谈：\n"
            "- 视觉识别（直接看，不要调用工具）：当图片/视频已经附在当前对话中（用户拖入/上传），直接用你自身的多模态视觉能力识别、描述或分析其内容即可，绝对不要为「看图」调用任何工具。只有以下四种情况才需要调用工具：\n"
            "  ① 用户想要**真实存在**的图片，如「找张 xx 的图」「搜一下 xx 图片」「xx 长什么样」「来点 xx 壁纸」→ 调用 web_image_search（到网上搜索现成的真实图片）；\n"
            "  ② 用户要求「画图/生成图片/AI 绘画/画一只 xx」这类**从零创作**→ 调用 generate_image（prompt 用英文描述），绝不能只口头描述，只有调用工具才算完成；\n"
            "  ③ 用户要求对某张图做局部修改（如「把这张图的背景改成夜晚」「给猫戴帽子」）→ 调用 edit_image；用户拖入本轮的图片优先，或填 source 为本地图片路径；\n"
            "  ④ 用户给的是一个本地文件路径、要你读取该文件 → 调用 read_file。\n"
            "- ★ 务必分清「搜图」与「生成图」：用户说「找/搜/看看」→ web_image_search（搜真实图片，不绘制）；"
            "用户说「画/生成/制作」→ generate_image（AI 创作）。两者结果来源完全不同，绝不能混淆。\n"
            "- ⚠️ **工具没给结果时不要编**：工具返回「没找到／失败／0 条」时，就**如实**告诉用户没拿到，"
            "并说明可能原因（关键词不合适、站点防盗链等）以及可以怎么再试。"
            "**绝不能**换个说法把没发生的事说得像发生了，也不要凭印象去描述图片内容或补充未经核实的事实。\n"
            "- ⚠️ **不要把无关的记忆／知识库内容硬扯进回答**：只有确实相关时才引用；"
            "没有相关资料就直说没有，不要为了显得「有依据」而牵强关联。\n"
            "- 图片库：用户说「保存这张/存起来/收进图库」时调用 save_image_to_library（index 填本轮第几张）。\n"
            "- 文件系统：浏览用户目录、读取任意本地文件、按关键词搜索、写入或修改文件。\n"
            "- 记忆：记忆按【分区文段】整体维护（工作背景/个人背景/当前关注/近期动态…）。遇到值得长期记住的用户稳定信息、偏好、关键事实时，主动调用 remember 把对应分区的**整段文段**重写成合并新旧信息后的最新版（**自主判断，只记重要的，不要把所有问答都写入**）；"
            "当用户问「你还记得吗/我们之前说过」或需要历史信息时调用 search_memory。\n"
            "- 时间：需要当前日期时间时调用 get_time。\n"
            + (
                (
                    "- 联网搜索：用户已开启「联网」开关，你有 web_search 工具可主动联网检索。\n"
                    "  凡是涉及**最新/实时/近期**信息的问题（新闻时事、股价行情、软件新版本、"
                    "赛事比分，或你不确定、知识可能已过时的内容），**绝不要回答「我无法联网」"
                    "或凭记忆猜测**，而应主动调用 web_search 获取真实网页结果，再据此用中文总结回答"
                    "并注明来源。日常闲聊、写作、翻译、代码等不需要联网的任务不要调用。\n"
                    "- 天气：**一律用 get_weather 工具**，不要用 web_search。"
                    "搜索引擎对天气只会返回「XX天气预报_15天」这类网站导航页，给不出真实数值；"
                    "get_weather 直接返回气温、降水概率、风速等结构化数据。\n"
                    "- 查机构/单位的对外公开信息（性质、地址、招生章程、招聘公告、年报、办事指南等）："
                    "web_search 会自动追加官方站定向检索（site:gov.cn / site:edu.cn / site:org.cn），"
                    "优先返回官网结果。关键词里带上机构**全称**效果最好。\n"
                )
                if cfg.get("web_enabled")
                else (
                    "- 本机当前处于**离线模式**（用户未开启「联网」开关），无法访问互联网。"
                    "若用户需要最新信息，请提示其打开界面顶部的「联网」开关，"
                    "不要编造实时数据。\n"
                )
            )
            + "调用工具后，根据工具返回结果继续作答。能直接完成的就动手，不要只建议。\n"
            "- **一轮里可以同时发起多个工具调用**：如果几件事彼此不依赖"
            "（例如同时要「查知识库」+「联网搜索」+「看时间」），"
            "就**在同一条回复里一起发出**，不要一个一个轮着来 —— "
            "它们会被并行执行，总耗时只取决于最慢的那个，比串行快得多、也更准。\n"
            "  只有当下一个工具的**参数依赖**上一个工具的结果时，才分轮调用。\n"
            "- **知识库优先**：用户自己的资料（知识库）比网上搜到的更贴合其领域，"
            "两者都有相关内容时以知识库为准，联网只用来补时效性信息。\n"
            "- **回答要有实质内容，不要只给结论**：\n"
            "  · 涉及知识性/分析性的问题，一般写 300 字以上，用「小标题 + 分点」组织；\n"
            "  · 把材料里的**具体信息**（名称、数字、时间、条款）写出来，不要笼统概括；\n"
            "  · 单纯打招呼、确认、道谢这类寒暄则简短回一句即可，不要刻意拉长。\n"
            "- 若检索到的材料足以支撑推断，可以给出**你自己的分析和见解**，\n"
            "  但必须让用户分得清「材料里写的」和「你的推断」——后者要明说是推断。",
        ]
        # 长期记忆：**只注入当前对话的那一块** + 极简全局偏好。
        # 对话之间互不相通 —— 切到别的对话就换一份记忆。
        if cfg.get("memory_enabled", True):
            ctx = memory.build_context(session, query=last_user_text)
            if ctx:
                parts.append(ctx)
                # 目标/计划类记忆的用法：当背景用，别当催命符；有进展就更新。
                parts.append(
                    "【关于记忆里的目标与计划】\n"
                    "- 记忆里可能有用户的目标、计划、答应过要做的事。把它们当**已知背景**，\n"
                    "  不要每轮都追问进展，也不要每次回答都提一遍。\n"
                    "- 但话题相关时可以自然地关心一句，或提醒关键时间点（临近报名/考试/截止）。\n"
                    "- 用户说「做完了 / 没做成 / 改主意了 / 不打算做了」时，**立刻更新记忆**：\n"
                    "  用 remember 工具，action=\"update\"，old 填记忆里的原句，content 填最新状态；\n"
                    "  彻底放弃的用 action=\"forget\" 删掉。**别让档案里留着过期目标。**\n"
                    "- 用户透露**新目标**时，除了记进记忆，还要给**可执行的规划建议**：\n"
                    "  拆成几步、每步做什么、大致什么时间做，并指出最容易卡住的地方。\n"
                    "  不要只说「加油」「坚持就是胜利」这种空话。")
        # 用户拖进来的文档：正文直接给模型，让它能针对内容回答
        if docs:
            blocks, budget = [], 60000      # 总字数上限，避免把上下文撑爆
            for d in docs[:5]:
                text = str((d or {}).get("text") or "").strip()
                name = str((d or {}).get("name") or "文档").strip()
                if not text:
                    continue
                take = text[:max(0, budget)]
                budget -= len(take)
                blocks.append(f"《{name}》\n{take}")
                if budget <= 0:
                    break
            if blocks:
                parts.append(
                    "【用户本轮拖入的文档】—— 请**直接依据这些内容**回答；"
                    "用户问文档里的事时不要说自己看不到文件，也不要凭空编造文中没有的内容：\n\n"
                    + "\n\n---\n\n".join(blocks))
        # RAG 知识库
        if cfg.get("rag_enabled"):
            ctx = kb.build_rag_context(last_user_text, top_k=cfg.get("rag_top_k", 4))
            if ctx:
                parts.append(ctx)
        parts.append("回答请使用中文，简洁、直接、可执行。")
        return "\n\n".join(p for p in parts if p)


async def _stream_lines(resp, session: str = ""):
    """在子线程里读阻塞的 `requests.iter_lines`，通过队列交回事件循环。

    ⚠️ **为什么必须这样绕一层**（实测踩过）：
    `StreamingResponse` 要的是 async 生成器，但 `requests.iter_lines()` 是**阻塞**的。
    直接在 async 生成器里 for 循环读它，会把事件循环整个占住 ——
    uvicorn 拿不到执行机会去 flush socket 缓冲，
    于是所有分块**憋到最后一次性发出去**。
    表现就是用户看到的：发完消息后长时间没反应，然后思考和回答"一股脑"全冒出来。
    （实测 547 个分块全部在同一时刻到达；而直连 Ollama 首块只要 0.4s。）

    改成"子线程阻塞读 + 队列传递"后，事件循环始终空闲，每来一块就能立刻推给前端。

    「终止任务」是怎么实现的：前端 abort 掉 fetch → 连接断开 → Starlette 关掉本生成器
    → 走到下面的 `finally` → `resp.close()` 掐断到 Ollama 的连接 → 模型随即停止生成。
    所以**不需要额外的停止接口**，关键是这个 finally 必须真的执行到。
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _worker():
        try:
            for ln in resp.iter_lines(decode_unicode=False):
                loop.call_soon_threadsafe(_q_put, q, ln)
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(_q_put, q, None)   # 结束哨兵

    threading.Thread(target=_worker, daemon=True, name="ollama-stream").start()
    _register_stream(session, resp)
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        # 用户点了「终止」，或者连接断了：立刻掐掉到 Ollama 的连接，
        # 否则子线程会一直读下去、模型也会一直生成（白烧显卡）。
        _unregister_stream(session, resp)
        try:
            resp.close()
        except Exception:
            pass


def _q_put(q, item) -> None:
    """往队列里塞数据；满了就丢最旧的，绝不阻塞读取线程。"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(item)
        except Exception:
            pass


# ---------- 正在进行的生成（供「终止任务」使用）----------
# 正常情况下前端 abort 掉连接就够了；这里额外留一份引用，
# 是为了「连接没断干净」时也能主动掐掉上游，不至于让模型白跑。
_active_streams: dict = {}
_active_lock = threading.Lock()


def _register_stream(session: str, resp) -> None:
    with _active_lock:
        _active_streams.setdefault(session or "", []).append(resp)


def _unregister_stream(session: str, resp) -> None:
    with _active_lock:
        lst = _active_streams.get(session or "")
        if not lst:
            return
        try:
            lst.remove(resp)
        except ValueError:
            pass
        if not lst:
            _active_streams.pop(session or "", None)


def _stop_streams(session: str = "") -> int:
    """掐断指定会话（空字符串=全部）正在进行的上游连接，返回停掉的数量。"""
    with _active_lock:
        targets = (list(_active_streams.items()) if not session
                   else [(session, _active_streams.get(session, []))])
        killed = 0
        for _sid, lst in targets:
            for resp in list(lst):
                try:
                    resp.close()
                    killed += 1
                except Exception:
                    pass
        return killed


@app.post("/api/chat")
async def chat(req: ChatRequest):
    cfg = config.load_config()
    model = req.model or cfg["default_model"]
    model_note = ""
    images = list(req.images_b64 or [])
    _remember_image(images)          # 记住本轮图片，供后续「把这张图改成…」直接引用
    messages = list(req.messages)
    # ⚠️ 这里**不要**再按条数截断历史！
    # 曾经这里有一句 `messages = messages[-MAX_CONTEXT_MESSAGES:]`，
    # 后来裁剪逻辑统一挪到了 _trim_history_to_budget（它还会把丢掉的部分
    # 压成「较早对话摘要」注入）。两处并存时，这里先把历史砍掉，
    # 下面的裁剪就无内容可丢 → 摘要恒为空 → 用户回头问"第一轮"照样答不上。
    # 条数上限由 _trim_history_to_budget 内部统一处理。

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    # 本轮像写代码 → 换专用代码模型（只影响这一轮，下一轮自动回默认模型）
    if not req.model:
        model, model_note = _route_code_model(cfg, last_user, model)

    # 简单问题收紧生成长度：把"先思考很久"压到几秒（qwen3-vl 无法真正关闭思考）。
    # 但以下情况**绝不能**收紧，否则模型来不及输出工具调用或总结（表现为"思考中断、没有回答"）：
    #   - 有图片上下文（可能要微改）
    #   - 联网模式已开启（要搜索 + 深度阅读 + 引用来源，最耗 token）
    web_on = bool(cfg.get("web_enabled"))
    simple_q = (_is_simple_question(last_user)
                and not images and not _recent_image() and not web_on)
    if simple_q:
        cfg["max_tokens"] = min(int(cfg.get("max_tokens") or 2048), SIMPLE_MAX_TOKENS)
    elif web_on:
        # 联网场景给足空间：材料多、要求写得详细
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 2048), WEB_MAX_TOKENS)

    # 系统提示与工具定义属于**固定开销**，和聊天历史抢同一个上下文窗口，
    # 所以要先算出来，才能知道还剩多少空间给历史。
    # session 必须提前取到：记忆是按对话隔离的，注入时必须知道是哪个对话。
    session = req.session_id or ""
    sys_prompt = _SystemPrompt.build(last_user, session, req.docs)
    if cfg.get("code_exec_enabled"):
        sys_prompt += (
            "\n\n【本地执行代码】你有一个 run_python 工具，可以在用户电脑上**真跑** Python。\n"
            "- 凡是要精确计算、处理数据、验证算法、换算日期/单位、测试正则的，"
            "**都要先跑一遍再回答**，不要靠心算（心算很容易错，尤其是数字和日期）。\n"
            "- 拿到的输出是真实结果，请依据它作答；如果代码报错，先说明错在哪、"
            "给出修正后的代码并**再跑一次**。\n"
            "- 回答里保留代码（用户要的是代码），但结论必须来自真实运行结果。")
    tool_schemas = tools.make_schemas(cfg.get("web_enabled", False),
                                      cfg.get("rag_enabled", False),
                                      cfg.get("code_exec_enabled", False))

    # ---------- 上下文预算：按剩余空间裁剪历史 ----------
    # 工具定义本身就有 3000~4500 token，系统提示约 1000；
    # 联网时还要塞进"8 条结果 + 若干篇网页正文"，动辄上万 token。
    # 不按预算裁剪的话很容易超过 num_ctx，Ollama 会**直接截断提示词**
    # （模型可能看不到系统提示或本轮问题）→ 表现为回答莫名其妙或干脆没有。
    #
    # ⚠️ 注意保存用的是原始 messages —— 必须是**完整**历史。
    # 曾经的写法是裁剪后直接覆盖 messages，结果每次落盘都只存下裁剪后的部分，
    # 早期对话被永久删除（重启后恢复出来就是残的）。裁剪只影响"这一轮发给模型什么"。
    all_messages = list(messages)
    messages, dropped = _trim_history_to_budget(messages, sys_prompt, tool_schemas, cfg)
    digest = _history_digest(dropped)

    # 联网搜索：以前这里会**预先**跑一次搜索并把结果塞进上下文，
    # 但模型自己也会调用 web_search 工具 —— 两条路径重复执行，结果两批材料叠加，
    # 极易撑爆上下文窗口（表现为"搜完了却没有回答"）。
    # 而且预检索那批材料既没有相关性过滤，也没有深度阅读，质量更差。
    # 现在改成只给一句**提醒**，真正的检索统一走 web_search 工具。
    if cfg.get("web_enabled") and _wants_search(last_user):
        messages.append({"role": "system", "content": (
            "用户这句话看起来需要最新信息。请先调用 web_search 工具检索，"
            "再基于检索结果作答；不要在未检索的情况下凭记忆回答时效性内容。")})

    async def gen():
        # 每轮对话的本地工作消息序列 = system + 用户历史
        #
        # ⚠️ 较早对话摘要**必须并进第一条 system 消息**，不能再加一条 system。
        # 实测：Ollama 的 qwen 系对话模板只取第一条 system，第二条会被**静默丢弃** ——
        # 摘要写进去了、接口也没报错，但模型压根看不到，症状就是"还是说忘记了"。
        full_sys = sys_prompt + ("\n\n" + digest if digest else "")
        working = [{"role": "system", "content": full_sys}] + messages
        final_text = ""
        final_thinking = ""
        # 换了模型就提前吱一声，免得用户以为"怎么这次回复的口吻变了"
        if model_note:
            yield json.dumps({"note": model_note}) + "\n"
        # session 已在上面定义（记忆按对话隔离，需要提前拿到）

        # 空回答重试：qwen3-vl 的思考会吃掉大量 token，偶尔会「想完了但没来得及写正文」，
        # 表现为思考戛然而止、界面什么都没有。这种情况按上面配置重跑一次并加倍配额。
        # 注意：用独立的 gen_params 而不是改 cfg —— 在 gen() 里给 cfg 赋值会让它
        # 变成局部变量，导致前面读取 cfg 时报 UnboundLocalError。
        retried_empty = False
        gen_params = dict(cfg)

        for _round in range(MAX_TOOL_ROUNDS):
            # 工具调用中间轮不再重复附图片
            attach_images = images if _round == 0 else None
            try:
                resp = client.chat(working, model=model, stream=True,
                                   images_base64=attach_images, params=gen_params,
                                   tools=tool_schemas)
            except ollama_client.OllamaError as e:
                yield json.dumps({"error": str(e)} | {"__end": True}) + "\n"
                return

            round_msg = {"content": "", "thinking": None, "model": model}
            tool_calls = None
            done_reason = ""
            # 注意：必须用 _stream_lines（子线程读 + 队列），
            # 不能直接 for line in resp.iter_lines() —— 见它的注释
            async for line in _stream_lines(resp, session):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("error"):
                    yield json.dumps({"error": obj["error"], "__end": True}) + "\n"
                    return
                m = obj.get("message") or {}
                if m.get("thinking"):
                    round_msg["thinking"] = (round_msg["thinking"] or "") + m["thinking"]
                    final_thinking += m["thinking"]
                    # 实时透出思考增量（即使在折叠状态下也要持续刷新进度）
                    yield json.dumps({"message": {"thinking": m["thinking"]}}) + "\n"
                if m.get("content"):
                    round_msg["content"] += m["content"]
                    final_text += m["content"]
                    # 仅把用户可见的文本增量透出
                    yield json.dumps({"message": {"content": m["content"]}}) + "\n"
                if "message" in obj:
                    tc = m.get("tool_calls")
                    if tc:
                        tool_calls = tc
                # Ollama 在最终块里给 done_reason：
                #   "stop"   = 正常结束
                #   "length" = 撞到 num_predict 上限被截断（评测数正好等于配额）
                # 这是判断"回答是否被思考吃光"的**可靠信号**，比猜正文长不长准得多。
                if obj.get("done"):
                    done_reason = obj.get("done_reason") or ""

            if not tool_calls:
                # 被思考吃光配额：Ollama 明确告诉我们 done_reason=length，
                # 说明撞到了 num_predict 上限。典型表现是"思考到一半就断、没有回答"
                # （思考把配额用尽，正文一个字都没来得及写）。
                # 这不是模型坏了，纯粹是配额给少了——加倍重试一次。
                truncated = (done_reason == "length")
                if truncated and not retried_empty:
                    retried_empty = True
                    boosted = max(int(gen_params.get("max_tokens") or 2048) * 2, 4096)
                    gen_params["max_tokens"] = min(boosted, MAX_TOKENS_CEILING)
                    yield json.dumps({"note": (
                        "上一次生成被输出长度上限截断（思考占用过多），"
                        "正在以更长的配额重试…")}) + "\n"
                    continue
                if truncated and not round_msg["content"].strip():
                    # 重试后仍被思考吃光：如实告知，避免用户看到空白一脸茫然
                    yield json.dumps({"note": (
                        "回答被输出长度上限截断。可尝试把问题问得更具体，"
                        "或在界面调大「最大生成长度」。")}) + "\n"
                break  # 本轮无工具调用，得到最终答复

            # ---------- 执行工具（Agent loop）----------
            # 1) 把 assistant 的 tool_calls 加入工作序列
            working.append({"role": "assistant", "content": round_msg["content"] or "",
                            "tool_calls": tool_calls})
            # 2) 逐个执行
            ui_events = []
            # images：本轮拖入的图（否则复用最近一张）
            # shown_images：本轮已展示给用户的图，供「保存到图库」工具按序号引用
            # session：记忆按对话隔离，写记忆的工具必须知道当前是哪个对话
            ctx = {"images": images or _recent_image(), "shown_images": [],
                   "session": session}

            # 先把本轮所有工具调用解析出来，并逐个通知前端"开始执行"
            calls = []
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                calls.append((name, args))
                yield json.dumps({"tool_start": {"name": name, "args": args}}) + "\n"

            # ---------- 并行执行本轮工具 ----------
            # 模型经常一轮里同时要好几样东西（查知识库 + 联网搜索 + 看时间…）。
            # 串行执行时总耗时是它们**相加**；并行后只取决于最慢的那个，
            # 对"多工具协同"的任务体感差别很明显。
            # 每个工具用**独立的 ui_events 列表**，避免并发写入互相污染，
            # 最后再按原顺序合并，保证前端看到的次序稳定。
            async def _run_one(idx, name, args):
                ev = []
                try:
                    # dispatch 里都是阻塞逻辑（网络/文件/推理），必须丢线程池，
                    # 否则会占住事件循环、把流式输出又憋成"一次性返回"。
                    res = await asyncio.to_thread(tools.dispatch, name, args, ev,
                                                  dict(ctx))
                except Exception as e:
                    res = f"[工具执行失败] {name}：{e}"
                return idx, name, res, ev

            if len(calls) > 1:
                yield json.dumps({"tool_parallel": len(calls)}) + "\n"
            gathered = await asyncio.gather(
                *[_run_one(i, n, a) for i, (n, a) in enumerate(calls)])
            for _idx, name, result, ev in sorted(gathered, key=lambda x: x[0]):
                # 把本次新产生的图片登记下来，后续工具（如保存到图库）可按序号引用
                for e in ev:
                    if e.get("type") == "image":
                        ctx["shown_images"].append(e)
                ui_events.extend(ev)
                # 注意：Ollama 的 tool 消息用 tool_name 关联调用，不是 tool_calls/tool_call_id，
                # 否则模型读不到工具返回内容（会误答"没查到/无法联网"）。
                working.append({"role": "tool", "content": result, "tool_name": name})
            # 3) 把前端副作用事件透出
            for ui in ui_events:
                yield json.dumps({"ui": ui}) + "\n"

        # 对话结束：归档历史会话（供记忆检索）+ 保存会话文件（供重启后恢复）
        # **必须用 all_messages（完整历史）**，不能用裁剪后的 messages，
        # 否则每轮都会把早期对话从磁盘上抹掉（见上面的注释）。
        full = all_messages + [{"role": "assistant", "content": final_text}]
        try:
            memory.save_transcript(session, full)
        except Exception:
            pass
        try:
            sessions.save_messages(session, full)
            sessions.prune(session)       # 过长则自动裁剪，避免记录无限膨胀
        except Exception:
            pass

        # 自动记忆：把本轮要点提炼进记忆（后台，且**等用户停手再做**，不跟聊天抢显卡）。
        # 每轮都安排，覆盖最近几轮内容；命中信号词（尤其"我做过/参加过"这类经历）则立即做。
        # 有了这层沉淀，久远的聊天记录才能安全清理。
        if cfg.get("auto_memorize", True):
            try:
                _schedule_memory_extract(session, full[-_MEM_WINDOW_TURNS:], model, cfg,
                                         urgent=_looks_memorable(last_user))
            except Exception:
                pass
        yield json.dumps({"done": True, "text": final_text, "thinking": final_thinking}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/chat/stop")
def chat_stop(body: dict | None = None):
    """终止当前正在进行的生成（界面上的「■ 终止」按钮）。

    正常情况下前端 abort 掉 fetch、连接一断，服务端就会顺势掐掉上游；
    这里再兜一道：即使连接没断干净，也能立刻让模型停下来。
    """
    session = str(((body or {}).get("session_id")) or "").strip()
    killed = _stop_streams(session)
    return {"ok": True, "stopped": killed}


def _wants_search(text: str) -> bool:
    """判断用户话语是否隐含需要联网搜索的意图。"""
    import re
    return bool(re.search(r"搜索|查一下|网上查|最新消息|实时新闻|搜一下|去网上|联网查", text, re.I))


# ---------- 记忆 API ----------
# 结构：**一份长期记忆（所有对话共享） + 每个对话一份短期记忆（互相独立）**。
# 长期记忆放"换任何对话都成立"的信息（身份/偏好/约定），所以新开对话不会不认识你；
# 短期记忆放"这件事"的上下文，切对话就换一份，并且可以单独释放。
@app.get("/api/memory")
def memory_get(session_id: str = ""):
    """一次取回两块：长期记忆（全局）+ 该对话的短期记忆。"""
    sid = (session_id or "").strip()
    lng = memory.get_long_doc()
    sht = memory.get_short_doc(sid) if sid else {"content": "", "updated_at": 0}
    return {"ok": True, "session_id": sid,
            "long": lng["content"], "long_updated_at": lng["updated_at"],
            "short": sht["content"], "short_updated_at": sht["updated_at"],
            "long_cap": memory.LONG_CAP, "short_cap": memory.SHORT_CAP}


@app.post("/api/memory")
def memory_set(body: dict):
    """保存记忆。scope=long 存长期记忆，否则存该对话的短期记忆。"""
    scope = str((body or {}).get("scope") or "short").lower()
    content = str((body or {}).get("content") or "")
    if scope in ("long", "global"):
        d = memory.set_long(content)
        return {"ok": True, "scope": "long", "content": d["content"]}
    sid = str((body or {}).get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "缺少 session_id")
    d = memory.set_short(sid, content)
    return {"ok": True, "scope": "short", "session_id": sid, "content": d["content"]}


@app.delete("/api/memory")
def memory_clear(session_id: str = "", scope: str = "short"):
    """清空记忆。scope=long 清长期记忆；session 清某个对话的短期记忆；否则清全部短期。"""
    scope = (scope or "short").lower()
    if scope == "long":
        memory.set_long("")
        return {"ok": True, "cleared": "long"}
    if scope == "session":
        sid = (session_id or "").strip()
        if not sid:
            raise HTTPException(400, "缺少 session_id")
        memory.set_short(sid, "")
        return {"ok": True, "cleared": "session", "session_id": sid}
    r = memory.release("short")
    return {"ok": True, "cleared": "all_short", **r}


@app.get("/api/memory/usage")
def memory_usage():
    """各部分占用情况：每个会话的历史、记忆、归档、进程内存。

    用途有二：让用户看清"到底是哪块在占地方"；给一键释放提供依据。
    """
    import os as _os
    from . import sessions as sm

    def _tsize(path):
        try:
            return _os.path.getsize(path)
        except Exception:
            return 0

    mu = memory.usage()
    shorts = memory._load_shorts()
    items, sess_bytes = [], 0
    for s in sm.list_sessions():
        sid = s.get("id") or ""
        msgs = sm.get_messages(sid)
        chars = sum(len(str(m.get("content") or "")) for m in msgs)
        b = _tsize(sm._path(sid))
        sess_bytes += b
        items.append({
            "id": sid, "title": s.get("title") or "", "updated": s.get("updated") or "",
            "messages": len(msgs), "chars": chars,
            "tokens": _est_tokens("".join(str(m.get("content") or "") for m in msgs)),
            "bytes": b, "trimmed": int(s.get("trimmed") or 0),
            # 该对话的短期记忆（跟着对话走，一眼看出哪个对话记了什么）
            "memory_chars": len(str((shorts.get(sid) or {}).get("content") or "")),
        })
    items.sort(key=lambda x: x["bytes"], reverse=True)

    mem_bytes = _tsize(memory.LONG_FILE) + _tsize(memory.SHORT_FILE)

    # 归档 transcript（记忆的"底稿"，释放前要先把它沉淀掉，见 memory_release）
    tr_bytes, tr_files = 0, 0
    if _os.path.isdir(memory.TRANSCRIPT_DIR):
        for fn in _os.listdir(memory.TRANSCRIPT_DIR):
            if fn.endswith(".jsonl"):
                tr_files += 1
                tr_bytes += _tsize(_os.path.join(memory.TRANSCRIPT_DIR, fn))

    return {"ok": True,
            "sessions": items,
            "totals": {
                "sessions_bytes": sess_bytes, "transcript_bytes": tr_bytes,
                "memory_bytes": mem_bytes, "disk_bytes": sess_bytes + tr_bytes + mem_bytes,
                "session_count": len(items),
                "message_count": sum(i["messages"] for i in items),
                "transcript_files": tr_files,
                # 记忆：长期一份 + 短期按对话
                "long_chars": mu["long_chars"], "long_cap": mu["long_cap"],
                "short_chars": mu["short_chars"], "short_used": mu["short_used"],
                "short_cap": mu["short_cap"],
                "memory_chars": mu["long_chars"] + mu["short_chars"],
                "rss_bytes": _process_rss(),
                # 界面/接口用的关键阈值，避免前端写死数字
                "max_keep": sm.MAX_KEEP, "keep_recent": sm.KEEP_RECENT,
                "context_messages": MAX_CONTEXT_MESSAGES,
            }}


def _process_rss() -> int:
    """当前进程常驻内存（容器里就是应用实际吃掉的 RAM）。

    Linux 读 /proc；拿不到就返回 0（界面显示"—"即可，不影响其它功能）。
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


@app.post("/api/memory/release")
def memory_release(body: dict):
    """一键释放占用。**默认只清缓存，绝不动长期记忆。**

    分级（从最不重要的开始，够用就停）：
      0. **先把归档里还没沉淀的重要内容提炼进记忆**（关键！）
      1. 归档 transcript —— 沉淀完才能删
      2. 空会话 / 很久没聊且内容极少的僵尸会话
      3. 把过长的会话历史裁到最近若干条（对话记录，不是记忆）
    记忆本身（每个对话那一块 + 全局偏好）**永远不动**。
    """
    import os as _os
    from . import sessions as sm

    scope = str((body or {}).get("scope") or "auto").lower()
    sid = str((body or {}).get("session_id") or "").strip()
    confirm_memory = bool((body or {}).get("confirm_memory"))
    do_sweep = bool((body or {}).get("sweep", True))
    freed = 0
    detail = []

    def _tsize(p):
        try:
            return _os.path.getsize(p)
        except Exception:
            return 0

    # ---- 清空单个会话的历史上下文（记忆不受影响）----
    if scope == "session":
        if not sid:
            return {"ok": False, "reason": "未指定 session_id"}
        # 删历史前也先沉淀一遍，否则这段对话里没被记下的要点就没了
        swept = 0
        if do_sweep:
            try:
                swept = _sweep_one(sid)
            except Exception:
                swept = 0
        before = _tsize(sm._path(sid))
        sm.save_messages(sid, [])
        after = _tsize(sm._path(sid))
        msg = f"已清空该对话的 {_fmt_bytes(before)} 历史"
        if swept:
            msg += f"（先沉淀了 {swept} 条要点进记忆，不会丢）"
        return {"ok": True, "freed_bytes": max(0, before - after),
                "swept": swept, "detail": [msg], "memory_intact": True}

    # ---- 0) 沉淀：把归档里还没进记忆的重要内容提炼出来 ----
    # 这一步是"释放不会丢重要信息"的关键。归档是提炼记忆的原始素材，
    # 而自动提炼只在命中关键词或每 4 轮触发，所以直接删归档 =
    # **把还没被记下的重要内容永久丢掉**。
    swept_total = 0
    if do_sweep and scope in ("auto", "caches"):
        try:
            known = {s.get("id") for s in sm.list_sessions()}
            swept_total = _sweep_all(known_sids=known)
        except Exception as e:
            detail.append(f"沉淀归档时出错（已跳过，不会丢数据）：{e}")
        if swept_total:
            detail.append(f"先从归档里沉淀了 {swept_total} 条要点进记忆")

    # ---- 1) 归档 transcript（沉淀之后才删）----
    if scope in ("auto", "caches"):
        if _os.path.isdir(memory.TRANSCRIPT_DIR):
            n_del = 0
            for fn in _os.listdir(memory.TRANSCRIPT_DIR):
                if not fn.endswith(".jsonl"):
                    continue
                p = _os.path.join(memory.TRANSCRIPT_DIR, fn)
                freed += _tsize(p)
                try:
                    _os.remove(p)
                    n_del += 1
                except Exception:
                    pass
            if n_del:
                detail.append(f"清理归档记录 {n_del} 个（已先沉淀要点）")

    # ---- 2) 僵尸会话（很久没聊且几乎没内容）----
    if scope in ("auto", "caches"):
        n = sm.cleanup_old(days=30, max_count=2)
        if n:
            detail.append(f"清理 {n} 个几乎没用过的空会话")

    # ---- 3) 裁剪过长的会话历史 ----
    if scope in ("auto", "sessions", "caches"):
        for s in sm.list_sessions():
            sid2 = s.get("id")
            msgs = sm.get_messages(sid2)
            if len(msgs) <= sm.MAX_KEEP:
                continue
            before = _tsize(sm._path(sid2))
            sm.prune(sid2)
            freed += max(0, before - _tsize(sm._path(sid2)))
            detail.append(f"「{(s.get('title') or '')[:12]}」历史裁到最近 {sm.KEEP_RECENT} 条")

    # ---- 4) 记忆本身：不参与释放 ----
    # 记忆（每个对话那一块 + 全局偏好）是**全库里最该保住的东西**，
    # 所以任何释放都明确不碰它。只有用户在界面上主动清空才会没。
    detail.append("记忆（各对话记忆块 + 全局偏好）已完整保留")

    # ---- 5) 短期记忆：可选释放（它是"缓存"性质的，长期记忆仍然保留）----
    short_freed = 0
    if scope == "short":
        # 释放前先把归档沉淀一轮，否则短期记忆一清、对话里的结论就真没了
        if do_sweep:
            try:
                swept_total += _sweep_all(
                    known_sids={s.get("id") for s in sm.list_sessions()})
            except Exception:
                pass
        r = memory.release("short")
        short_freed = int(r.get("freed_chars") or 0)
        detail.append(f"已释放全部短期记忆（{short_freed} 字）")
        detail.append("长期记忆（身份/偏好/约定）已保留")

    return {"ok": True, "freed_bytes": freed,
            "swept": swept_total, "short_freed_chars": short_freed,
            "detail": detail or ["没有需要释放的内容"],
            "memory_intact": True,
            "hint": "短期记忆与缓存已释放；长期记忆（身份/偏好/约定）始终保留。"}


# =====================================================================
#  归档沉淀：把 transcript 里还没进记忆的重要内容提炼出来
#  —— "释放不会丢重要信息"的关键保障
# =====================================================================
_SWEEP_MAX_SESSIONS = 8      # 单次释放最多沉淀几个对话，避免等太久
_SWEEP_MIN_MSGS = 4          # 少于这么多条的归档不值得单独跑一次模型


def _sweep_one(session: str, cfg: dict = None, model: str = None) -> int:
    """把某个对话的归档沉淀进它的记忆，返回新增要点条数。

    用一次轻量模型调用把归档里的稳定信息抽成要点，合并进该对话的记忆。
    失败就返回 0（**宁可少记，也不能因为沉淀失败就把数据删了**）。
    """
    if not session:
        return 0
    msgs = memory.read_transcript(session, limit=120)
    if len(msgs) < _SWEEP_MIN_MSGS:
        return 0
    cfg = cfg or config.load_config()
    model = model or cfg.get("default_model")
    convo = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{str(m['content'])[:220]}"
        for m in msgs[-60:])
    prompt = (
        "下面是一段历史对话。请提取其中值得记住的内容，"
        "**这段对话记录马上就要被删除**，只保留你提取的要点。\n"
        "· 要提取：用户透露的身份/偏好/约定、正在做的事、得出的结论、关键数据\n"
        "· 不要提取：寒暄、一次性的提问、助手自己的解释性内容\n"
        "· 输出格式（每行一条）：类别|要点\n"
        "  类别只能二选一：\n"
        "    长期 —— 换任何对话都成立（身份、姓名、职业、长期偏好、约定）\n"
        "    短期 —— 只跟这件事有关（当前项目、本次结论、临时设定）\n"
        "· 要点直接写事实（含名称/数字/技术栈），每条不超过 60 字\n"
        "· 最多 6 条；确实没有值得留存的内容就只输出：无\n"
        "· 下面【已知信息】里已经有的，不要再重复提取\n\n"
        "【已知信息】\n长期记忆：" + (memory.get_long().strip()[-2500:] or "（暂无）")
        + "\n短期记忆：" + (memory.get_short(session).strip()[-800:] or "（暂无）")
        + "\n\n【对话】\n" + convo
    )
    # 输出预算不足会让提炼静默失败（见 _EXTRACT_TOKENS），
    # 所以这里统一走 _llm_extract（它会自动在截断时加预算重试）。
    text = _llm_extract(prompt, model, cfg)
    return _apply_extract(text, session, limit=6)


def _sweep_all(known_sids=None) -> int:
    """对所有归档做一次沉淀。返回新增要点总数。

    只处理"对话还在"的归档 —— 对话已删的归档没有记忆可挂，
    提取出来也无处安放（那部分属于用户主动删除，本就该消失）。
    """
    import glob as _glob
    total, done = 0, 0
    try:
        cfg = config.load_config()
    except Exception:
        return 0
    for path in _glob.glob(os.path.join(memory.TRANSCRIPT_DIR, "*.jsonl")):
        if done >= _SWEEP_MAX_SESSIONS:
            break
        sid = os.path.basename(path)[:-6]
        if known_sids is not None and sid not in known_sids:
            continue          # 对话已删，没必要再沉淀
        try:
            got = _sweep_one(sid, cfg)
        except Exception:
            got = 0
        total += got
        done += 1
    return total


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.delete("/api/memory/{sid}")
def memory_delete_by_sid(sid: str):
    """按对话删除记忆（对话被删时也会自动调到这里）。"""
    memory.delete_short(sid)
    return {"ok": True, "session_id": sid}


# ---------- 知识库 AI API ----------
class UploadBody(BaseModel):
    name: str
    content: str


@app.get("/api/kb")
def kb_list():
    """列出知识库文档，并带上目录路径（前端要显示"该把文件放哪"）。"""
    docs = kb.list_documents()
    return {"ok": True, "documents": docs, "dir": kb.KB_DIR,
            "enabled": bool(config.load_config().get("rag_enabled"))}


@app.post("/api/kb")
def kb_save(body: UploadBody):
    doc = kb.save_document(body.name, body.content)
    return {"ok": True, "document": doc}


@app.post("/api/kb/upload")
async def kb_upload(file: UploadFile):
    """上传文档到知识库。

    **按原始字节存盘**，不做任何解码 —— PDF / Word 都是二进制，
    以前 decode("utf-8", errors="replace") 会把文件解坏，存进去等于垃圾。
    解析交给读取时按扩展名走 doc_extract。
    """
    data = await file.read()
    doc = kb.save_bytes(file.filename or "未命名", data)
    return {"ok": True, "document": doc}


@app.post("/api/doc/extract")
async def doc_extract_upload(file: UploadFile):
    """从上传的文档里抽取文本（拖拽文档到聊天框时调用）。

    只抽文本返回，**不落盘** —— 用户只是"想让模型看这份文档"，
    并不一定要收进知识库。想留下的可以再用 /api/kb/upload。
    """
    import tempfile
    from . import doc_extract
    name = file.filename or "未命名"
    data = await file.read()
    if not data:
        return {"ok": False, "name": name, "error": "文件是空的"}
    tmp = os.path.join(tempfile.gettempdir(),
                       f"mm_doc_{int(time.time() * 1000)}_"
                       + os.path.basename(name).replace("..", ""))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        text, note = doc_extract.extract_text(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return {"ok": bool(text.strip()), "name": name,
            "chars": len(text), "text": text, "note": note,
            "supported": doc_extract.is_supported(name),
            "error": "" if text.strip() else (note or "没能从这份文档里读出文字")}


@app.delete("/api/kb/{name}")
def kb_delete(name: str):
    if not kb.delete_document(name):
        raise HTTPException(404, "文档不存在")
    return {"ok": True}


@app.post("/api/kb/search")
def kb_search(body: dict):
    q = body.get("query") or ""
    results = kb.search(q, top_k=body.get("top_k", 5))
    return {"ok": True, "results": results}


# ---------- 本地文件读取 API ----------
class FileReadBody(BaseModel):
    path: str
    mode: str = "auto"   # auto / text / image


@app.post("/api/file/read")
def file_read(body: FileReadBody):
    result = file_tools.read_file(body.path)
    return result


@app.post("/api/file/scan")
def file_scan(body: dict):
    """按关键词扫描目录/文件集合，返回匹配片段。"""
    paths = body.get("paths") or [body.get("path", "")]
    keywords = body.get("keywords", "")
    content = file_tools.read_by_keywords(paths, keywords)
    return {"ok": True, "content": content[:8000]}


# ---------- 视频读取 API ----------
class VideoBody(BaseModel):
    path: str


@app.post("/api/video/analyze")
def video_analyze(body: VideoBody):
    """抽取视频关键帧并让模型理解。若请求带 frames 则只做抽帧返回。
    默认返回抽帧结果；前端拿到后走 /api/chat 让 Qwen-VL 分析。"""
    result = video.extract_frames(body.path)
    return result


# ---------- 联网搜索 + 外部 API ----------
class WebSearchBody(BaseModel):
    query: str
    top_k: int = 5


@app.post("/api/web/search")
def web_search(body: WebSearchBody):
    if not config.load_config().get("web_enabled"):
        return {"ok": False, "error": "联网搜索开关未开启，请在界面打开"}
    results = web_tools.web_search(body.query, body.top_k)
    return {"ok": True, "results": results}


@app.post("/api/web/call")
def web_call(body: dict):
    """调用已配置的外部 API 工具。"""
    cfg = config.load_config()
    result = web_tools.call_external_api(cfg, body.get("tool", ""), body.get("params", {}))
    return result


@app.get("/api/web/tools")
def web_tools_list():
    return {"ok": True, "tools": web_tools.BUILTIN_TOOLS}


# ---------- 文生图 API ----------
class T2IBody(BaseModel):
    prompt: str
    negative_prompt: str = ""
    steps: int = 4
    width: int = 512
    height: int = 512
    hd: bool = False          # 是否做 4 倍超分放大（512→2048）


@app.post("/api/t2i/generate")
def t2i_generate(body: T2IBody):
    result = t2i.generate(body.prompt, body.negative_prompt, body.steps,
                          body.width, body.height, hd=body.hd)
    return result


@app.get("/api/t2i/capability")
def t2i_capability():
    """文生图能力探测：是否可用、是否支持高清放大、跑在什么设备上。

    注意：文生图与图片微改共用同一个 torch 环境，设备是同一种，
    因此这里的 device 对两者都适用。
    """
    dev = t2i.device_info()
    return {"ok": True, "hd_available": t2i.available_upscale(),
            "device": dev["device"], "kind": dev["kind"], "gpu": dev["gpu"],
            "torch": dev["torch"], "note": dev["note"],
            "forced": dev.get("forced", "auto"),
            "can_gpu": dev.get("can_gpu", False),
            "reason": dev.get("reason", "")}


@app.post("/api/t2i/device")
def t2i_set_device(body: dict):
    """切换绘图设备（CPU / GPU）。

    切换会**先校验设备能否真的运算**，再把绘图引擎实际加载到目标设备上；
    两步都通过才算成功。失败时保持原状并返回当前模式与具体原因 ——
    绝不"假装切换成功"、等到绘图时才报错。
    """
    mode = (body or {}).get("mode", "")
    result = t2i.set_device(mode)
    if not result.get("ok"):
        # 失败也返回 200：这是"操作结果"而不是接口错误，
        # 前端需要结构化的 reason 才能把原因讲清楚。
        return {"ok": False, "mode": result.get("mode", "cpu"),
                "device": result.get("device", "cpu"),
                "gpu": result.get("gpu"), "torch": result.get("torch"),
                "reason": result.get("reason") or "切换失败",
                "verified": False,
                "forced": result.get("mode", "cpu"),
                "requested": result.get("requested", mode)}
    return {"ok": True, "mode": result["kind"], "device": result["device"],
            "gpu": result["gpu"], "torch": result["torch"],
            "note": result.get("verify_note") or result.get("note") or "",
            "verified": bool(result.get("verified")),
            "forced": result.get("forced", mode),
            "requested": result.get("requested", mode)}


@app.post("/api/t2i/unload")
def t2i_unload():
    t2i.unload()
    return {"ok": True}


# ---------- 多会话管理 ----------
_legacy_migrated = False


def _migrate_memory_once(active_sid: str) -> None:
    """把老的"多分区"记忆搬进当前对话（只做一次）。

    记忆从"全局多分区"改成了"每个对话一块"，老数据必须搬过去，不能丢。
    放在这里而不是模块导入时执行，是因为迁移需要知道"搬给哪个对话"，
    而默认对话是第一次调 /api/sessions 才建出来的。
    """
    global _legacy_migrated
    if _legacy_migrated:
        return
    _legacy_migrated = True
    try:
        r = memory.migrate_legacy(active_sid)
        if r.get("migrated"):
            print(f"[memory] 老记忆已迁移：长期 {r.get('long', 0)} 条 / "
                  f"短期 {r.get('short', 0)} 条")
    except Exception as e:
        print(f"[memory] 旧记忆迁移失败（不影响使用）：{e}")


@app.get("/api/sessions")
def session_list():
    """列出所有会话（按最近更新倒序）。首次调用会自动创建一个默认会话。"""
    sessions.ensure_default()
    items = sessions.list_sessions()
    if items:
        _migrate_memory_once(items[0].get("id") or "")
    return {"ok": True, "sessions": items}


@app.post("/api/sessions")
def session_create(body: dict | None = None):
    """新建会话。"""
    item = sessions.create((body or {}).get("title"))
    return {"ok": True, "session": item}


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    """读取某个会话的完整消息（前端切换会话时用于恢复）。"""
    return {"ok": True, "id": sid, "messages": sessions.get_messages(sid)}


@app.put("/api/sessions/{sid}")
def session_save(sid: str, body: dict):
    """保存会话消息（前端每次对话后调用，保证重启后能恢复）。"""
    msgs = body.get("messages") or []
    info = sessions.save_messages(sid, msgs)
    return {"ok": True, "session": info}


@app.post("/api/sessions/{sid}/rename")
def session_rename(sid: str, body: dict):
    return {"ok": sessions.rename(sid, (body or {}).get("title") or "")}


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    # 记忆按对话隔离，对话没了它的记忆也没必要留着（否则会攒一堆孤儿记忆）
    sessions.delete(sid)
    memory.delete_short(sid)
    return {"ok": True}


# ---------- 图片库（搜到的图 / 生成的图统一留存）----------
@app.get("/api/library/images")
def library_list():
    return {"ok": True, "images": image_library.list_images(),
            "stats": image_library.stats()}


@app.post("/api/library/images")
def library_save(body: dict):
    """保存图片到图库（前端「保存到图库」按钮 / 模型 save_image_to_library 工具）。"""
    data = body.get("b64") or body.get("url") or ""
    if not data:
        return {"ok": False, "error": "缺少图片数据"}
    # 只给了远程 URL 时由后端下载，避免前端跨域
    if data.startswith("http"):
        raw = web_tools.download_image(data)
        if not raw:
            return {"ok": False, "error": "图片下载失败（可能被防盗链拦截）"}
        data = raw
    meta = image_library.save_image(data, name=body.get("name") or "",
                                    source=body.get("source") or "",
                                    origin=body.get("origin") or "web")
    if not meta.get("ok", True):
        return meta
    return {"ok": True, "image": meta}


@app.get("/api/library/images/{iid}/raw")
def library_raw(iid: str):
    """直接返回图片字节流（供前端 <img> 标签加载，避免 base64 膨胀）。"""
    p = image_library.get_path(iid)
    if not p:
        return Response(status_code=404)
    ext = os.path.splitext(p)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/png")
    with open(p, "rb") as f:
        return Response(content=f.read(), media_type=mime)


@app.get("/api/library/images/{iid}")
def library_get(iid: str):
    b64 = image_library.get_b64(iid)
    if not b64:
        return {"ok": False, "error": "图片不存在"}
    return {"ok": True, "id": iid, "b64": b64}


@app.post("/api/library/images/{iid}/rename")
def library_rename(iid: str, body: dict):
    return {"ok": image_library.rename(iid, (body or {}).get("name") or "")}


@app.delete("/api/library/images/{iid}")
def library_delete(iid: str):
    image_library.delete(iid)
    return {"ok": True}


# ---------- 语音输入（唤醒词「小千小千」+ 离线流式识别）----------
_voice_clients: set = set()
_voice_loop = None


def _broadcast_voice(event: dict) -> None:
    """把语音事件推给所有已连接的前端（从后台线程安全投递）。"""
    loop = _voice_loop
    if loop is None or loop.is_closed():
        return
    data = json.dumps(event, ensure_ascii=False)
    for ws in list(_voice_clients):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
        except Exception:
            pass


_voice = voice.get_listener(_broadcast_voice)


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    """前端语音通道：下发 start/stop/status，上游推送识别事件。"""
    global _voice_loop
    await ws.accept()
    _voice_loop = asyncio.get_running_loop()
    _voice_clients.add(ws)
    try:
        welcome = {"type": "status"}
        welcome.update(_voice.status())
        await ws.send_text(json.dumps(welcome, ensure_ascii=False))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            action = msg.get("action")
            if action == "start":
                result = _voice.start()
            elif action == "stop":
                result = _voice.stop()
            else:
                result = _voice.status()
            await ws.send_text(json.dumps({"type": "ack", "result": result},
                                          ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _voice_clients.discard(ws)


@app.get("/api/voice/status")
def voice_status():
    return _voice.status()


@app.post("/api/voice/start")
def voice_start():
    return _voice.start()


@app.post("/api/voice/stop")
def voice_stop():
    return _voice.stop()


# ---------- 前端 ----------
@app.middleware("http")
async def _no_cache_pages(request, call_next):
    """让前端文件**每次回来校验**，而不是直接用缓存。

    ⚠️ 踩过这个坑：改了 app.js（比如给拖拽加了"文档"分支），
    但界面行为一点没变 —— 因为浏览器 / WebView 直接吃了缓存的旧文件，
    看起来就像"功能根本没做"，白白怀疑代码。
    加上 no-cache 后，浏览器每次会带 ETag 回来问一次：
    文件没变仍是 304（几乎不花时间），变了就立刻拿到新的。
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/")
def index():
    resp = FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")