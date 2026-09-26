/* 本地多模态助手 —— 前端逻辑 */
(() => {
  const $ = (s) => document.querySelector(s);
  let images = [];          // 待发送附件 base64（含 data: 前缀）
  // ★ 涂抹选区（精确微改）：在这张图上涂过的地方才会被重画，别处一个像素不动。
  //   实测：整图微改（img2img）**改不准** ——「换衣服颜色」「加眼镜」在
  //   0.45/0.60/0.85 都没生效，0.85 换背景还会把脸重画；涂选区 + 局部重绘才行。
  let selMask = null;       // 选区蒙版（base64 PNG，无前缀；白=要改）
  let selMaskOn = null;     // 涂在哪张图上（data URL），用于「重涂」
  let docs = [];            // 待发送文档：[{name, text, chars}]（拖进来时抽取正文）
  let videoB64 = null;      // 待发送视频帧 base64 列表
  let streaming = false;
  // 每轮只提示一次「AI 正在写代码」，别刷屏
  let typingHintShown = false;
  let abortCtl = null;      // 用于「■ 终止」：中断当前的流式请求
  let aborted = false;      // 标记本轮是用户主动终止的（不是出错）
  const history = [];       // 会话消息（用于多轮上下文）
  // 会话标识（后端据此把历史会话落盘，供记忆检索）
  let sessionId = localStorage.getItem("ai_session_id") || "";
  const setSessionId = (sid) => {
    sessionId = sid || "";
    if (sessionId) localStorage.setItem("ai_session_id", sessionId);
  };

  // 工具名 → 中文展示
  const TOOL_LABELS = {
    web_image_search: "🌐 联网搜图",
    save_image_to_library: "⭐ 保存图片",
    generate_image: "🎨 文生图",
    edit_image: "🖼 图片微改",
    list_directory: "📁 浏览目录",
    read_file: "📄 读取文件",
    search_files: "🔍 搜索文件",
    write_file: "✍️ 写入文件",
    append_file: "✎ 追加文件",
    remember: "🧠 更新记忆",
    search_memory: "💭 检索记忆",
    get_time: "🕐 获取时间",
    web_search: "🌐 联网搜索",
  };

  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const mainEl = $("#main");

  // ---------- 基础工具 ----------
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // 轻提示：顶部居中出现，2.6 秒后自动淡出
  function showToast(msg, kind = "") {    let wrap = document.getElementById("toastWrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "toastWrap";
      wrap.className = "toast-wrap";
      document.body.appendChild(wrap);
    }
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .25s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 280);
    }, 2600);
  }
  // 给开发台（studio.js，独立 IIFE）复用同一个提示条 —— 否则两套提示风格不一致
  window.__toast = showToast;

  // 输入框弹层（自建，不用原生 prompt）。
  // ⚠️ 为什么不能用 window.prompt：桌面窗口是 WebView2，它**不支持 prompt**
  // （静默返回 null，表现成"点了按钮什么都没发生"）。alert / confirm 是支持的，
  // 所以本项目里 confirm 可以照常用，prompt 一律用这个替代。
  // ⚠️ 必须放在**最外层**作用域：文库面板是一段独立的 IIFE，
  // 把这个函数定义在别处它访问不到（实测报 `showInputDialog is not defined`）。
  function showInputDialog(opts) {
    const o = opts || {};
    const escHtml = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    return new Promise((resolve) => {
      if (document.querySelector(".confirm-layer")) return resolve(null);
      const layer = document.createElement("div");
      layer.className = "confirm-layer";
      layer.innerHTML =
        '<div class="confirm-box">' +
        `<div class="confirm-title">${escHtml(o.title || "请输入")}</div>` +
        (o.tip ? `<div class="confirm-reason">${escHtml(o.tip)}</div>` : "") +
        `<input class="ask-free input-dialog" spellcheck="false" placeholder="${escHtml(o.placeholder || "")}" />` +
        '<div class="confirm-btns"><button class="btn ghost" data-act="cancel">取消</button>' +
        `<button class="btn primary" data-act="ok">${escHtml(o.okText || "确定")}</button></div></div>`;
      const input = layer.querySelector(".input-dialog");
      input.value = o.value || "";
      document.body.appendChild(layer);
      input.focus();
      input.select();
      const done = (val) => { layer.remove(); resolve(val); };
      layer.querySelector('[data-act="cancel"]').onclick = () => done(null);
      layer.querySelector('[data-act="ok"]').onclick = () => done((input.value || "").trim() || null);
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); done((input.value || "").trim() || null); }
        else if (e.key === "Escape") { e.preventDefault(); done(null); }
      };
    });
  }

  // 兜底：api() 失败时抛出的错误，如果某个调用点忘了 try/catch，
  // 也要让用户看见原因，绝不静默 —— 静默失败比报错难查得多。
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r && r.status) {
      e.preventDefault();
      showToast("操作失败：" + (r.message || r), "warn");
    }
  });
  // 复制文本到剪贴板。127.0.0.1 属于安全上下文，clipboard API 可直接用；
  // 另留一层 execCommand 兜底，兼容个别浏览器。
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        return true;
      } catch (e2) { return false; }
    }
  }

  // 统一请求「打开文件夹」：
  //   成功            → 提示已打开
  //   容器内无小助手  → 把宿主机真实路径复制到剪贴板
  // capEl 给了就写到它里面，否则弹 toast。
  async function openFolderRequest(url, options, capEl) {
    const say = (msg) => { if (capEl) capEl.textContent = msg; else showToast(msg, "ok"); };
    try {
      const r = await fetch(url, options);
      const d = await r.json().catch(() => ({ ok: false, detail: `服务端返回 ${r.status}（非 JSON）` }));
      if (!d.ok) { say("❌ " + (d.detail || "打开失败")); return; }
      if (d.opened === false) {
        const host = d.host_path || d.path;
        const copied = await copyText(host);
        say("📂 " + host + (copied ? "（已复制）" : ""));
        return;
      }
      say(d.via === "host-agent" ? "📂 正在打开所在文件夹…" : "📂 已打开：" + d.path);
    } catch (err) {
      say("❌ " + err);
    }
  }

  function showThinking(text) {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = `<div class="bubble thinking">⏳ ${esc(text)}</div>`;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
  // 回答里的**站内下载链接**渲染成可点按钮。
  // ⚠️ 为什么需要：气泡一直用 textContent（纯文本，防 XSS），而模型很爱写
  // Markdown 的 [点击下载](/api/doclib/download?rel=xxx)，用户看到的是一坨带
  // 方括号的原文，**点不动** —— 生成 PPT 这类功能等于废了一半。
  // 只认**站内路径**（以 /api/ 开头）：外链一律不自动变可点，免得模型随口
  // 编个网址就变成能点的（那种才是真危险）。
  // ⚠️ 必须放在顶层（不能塞进里面那个作用域）—— **实时回答**和**恢复历史
  // 记录**是两条不同的渲染路径，两边都要用得到。
  // 一个站内下载链接 = **两个动作**：下载到电脑 + 在「生成文库」里定位它。
  // 为什么要有第二个：文件其实**已经**在库里了（模型写完就落盘），可用户从聊天里
  // 只看到"下载"，感受上就是"没存进文件库"。给一个直接跳到它的入口才说得通。
  // 把模型写的站内链接归一化成**已经编码好**的 path+query。
  // ⚠️ 模型经常把文件名原样写成中文（`rel=我的照片展示.pptx`），
  //    直接拿去请求会失败（后端 http.client 发请求行时要按 ascii 编码，
  //    报 `'ascii' codec can't encode characters in position 29-34` —— 实测踩到）。
  //    交给浏览器解析一遍，它一定会给出编码正确的绝对地址。
  function fixApiUrl(url) {
    try {
      const u = new URL(url, location.href);
      if (u.origin !== location.origin) return url;   // 外链不动
      return u.pathname + u.search;
    } catch (e) {
      return url;
    }
  }

  function dlGroup(label, url) {
    let rel = "";
    if (url.indexOf("/api/doclib/download") === 0) {
      const m = url.match(/[?&]rel=([^&]+)/);
      if (m) { try { rel = decodeURIComponent(m[1]); } catch (e) { rel = m[1]; } }
    }
    return '<span class="dl-group">' +
      `<a class="dl-link" href="${url}" download>${label}</a>` +
      (rel ? `<button type="button" class="dl-open" data-rel="${escapeHtml(rel)}"` +
             ` title="在左侧「生成文库」里定位并选中它">📂 在文件库中打开</button>` : "") +
      "</span>";
  }

  // ⚠️⚠️ 「工具调用泄漏进正文」的最后一道闸（2026-09-21）。
  // 模型偶尔不按 Ollama 的原生格式调用，而是自己套一层 XML 包装写进正文，例如：
  //     <function-call>
  //     { "name": "search_knowledge", "arguments": { … } }
  //     </function-call>
  // 原生通道只认它自己模板里的 <tool_call>，认不出这个 → 后端以前也只在代码模型
  // 那轮做清理 → 这坨 JSON 就会**原样渲染给用户看**，而工具根本没执行。
  // 后端现在会摘掉并真去执行（见 _split_text_tool_calls）；这里再兜一层，防的是：
  // 历史会话回放、以及后端没认出来的变体（大小写/空格不同、只写了开标签）。
  // ⚠️ 定义在**外层作用域**：renderAnswerLinks（历史回放也走它）在 doSend 之外。
  const TOOL_LEAK_XML_RE = /<\s*\|?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*>[\s\S]*?(?:<\s*\/\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*>|$)/gi;
  const TOOL_LEAK_TAG_RE = /<\s*\|?\s*\/?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*\/?\s*>/gi;
  const TOOL_LEAK_FENCE_RE = /```(?:tool|tool_call)[ \t]*\r?\n[\s\S]*?```/g;
  // 「半个标签」：模型写到一半放弃，只剩 <function / <tool_call 就接正文了。
  // 实机复现过（2026-09-21）：它单独占一行，被流式推出去，正文开头冒出一截 `<function`。
  // ⚠️ 判据很窄 —— 只删"整行只有这么一截"和"出现在正文最开头"，免得吃掉讲标签用法的正常内容。
  // ⚠️ `call` 那截写成**可选**：碎片往往正是缺了它（模型刚打出 `<function` 就放弃了）。
  // 尾部只吃**标签名那种字符**，不能放 `[^>\n]` —— 那样会把同一行的正文整段吃掉。
  const TOOL_LEAK_STRAY_RE = /(^|\n)[ \t]*<[ \t]*\/?[ \t]*(?:function|tool)(?:[ _-]?calls?)?[ \t]*[A-Za-z0-9_.-]{0,24}>?[ \t]*(?=\n|$)/gi;
  const TOOL_LEAK_HEAD_RE = /^\s*<[ \t]*\/?[ \t]*(?:function|tool)(?:[ _-]?calls?)?(?=\n|[^\s>])/i;
  function stripToolLeak(text) {
    let s = String(text == null ? "" : text);
    if (s.indexOf("<") < 0 && s.indexOf("```tool") < 0) return s;   // 快路径
    s = s.replace(TOOL_LEAK_XML_RE, "").replace(TOOL_LEAK_TAG_RE, "");
    s = s.replace(TOOL_LEAK_FENCE_RE, "");
    s = s.replace(TOOL_LEAK_STRAY_RE, "$1").replace(TOOL_LEAK_HEAD_RE, "");
    return s.replace(/\n{3,}/g, "\n\n").trim();
  }

  function renderAnswerLinks(bubble, text) {
    const esc = escapeHtml(stripToolLeak(text));
    let html = esc.replace(/\[([^\]]{1,40})\]\((\/api\/[^\s)]+)\)/g,
      (_, label, url) => dlGroup(label, fixApiUrl(url)));
    // 裸路径也要变按钮。⚠️ 前面那个字符类别只写「空格和半角括号」——
    // 模型很爱写成「下载链接：/api/…」这种**全角冒号**开头的（实测踩到，
    // 结果链接没渲染成按钮，用户只能看到一长串路径）。
    // 同理尾部要挡住中文标点，否则会把句号、逗号一起吞进 URL 里。
    html = html.replace(
      /(^|[\s（(【\[:：,，、>])(\/api\/(?:doclib\/download|ws\/zip)[^\s<)）】\]，。；：、"'']*)/g,
      (_, pre, url) => `${pre}${dlGroup("点击下载", fixApiUrl(url))}`);
    bubble.innerHTML = html.replace(/\n/g, "<br>");
  }

  // 跳到「生成文库」并把这个文件选中、闪一下（列表长的时候也能一眼找到）
  async function openInLibrary(rel) {
    const panel = document.getElementById("doclibPanel");
    if (!panel) { showToast("界面上找不到「生成文库」面板", "warn"); return false; }
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    await loadDoclib(rel || "");          // 刷新列表 + 选中（内部会打上 active）
    let hit = null;
    document.querySelectorAll(".dl-item").forEach((el) => {
      if (el.dataset.rel === rel) hit = el;
    });
    if (hit) {
      hit.classList.add("dl-flash");
      setTimeout(() => hit.classList.remove("dl-flash"), 2600);
      showToast("已在生成文库中定位：" + rel, "ok");
    } else {
      showToast("文件库里没找到「" + rel + "」（可能已被删除或改名）", "warn");
    }
    return true;
  }
  window.__openInLibrary = openInLibrary;

  // ---------- 地图卡片 ----------
  // 后端把地点 / 路线算好（真实坐标、真实距离用时），前端只负责画。
  // 瓦片走后端代理 `/api/map/tile/{z}/{x}/{y}.png`（后端换镜像，并统一转发）。
  // ⚠️ 地图**不缓存**：后端响应 no-store，离线时瓦片直接 404、卡片不显示底图。
  // Leaflet 本身放在本地 vendor 里，不依赖外网 CDN。
  let mapSeq = 0;
  function makeMapCard(ui) {
    const wrap = document.createElement("div");
    wrap.className = "msg bot";
    const box = document.createElement("div");
    box.className = "map-card";
    wrap.appendChild(box);

    const rt = ui.route || null;
    const online = ui.online !== false;      // 后端会把当前模式带下来
    const info = document.createElement("div");
    info.className = "map-info";
    let html = `<span class="map-badge ${online ? "on" : "off"}">` +
      (online ? "联网" : "离线") + `</span>` +
      // 底图是哪家，直接标出来 —— 不然用户看不出这张图是"换了个地图"
      // （高德底图是中文路网图，和 OSM 那种清淡的志愿者图一眼就能分辨）
      `<span class="map-basemap">${ui.tile_source === "amap" ? "高德底图" : "OSM 底图"}</span>`;
    if (rt && rt.straight) {
      // 离线兜底：没有路网数据，只能给直线距离 —— 必须说清楚，
      // 否则用户会把"直线 6 公里"当成"开车 6 公里"。
      html += ` <b>${escapeHtml(rt.from || "")} → ${escapeHtml(rt.to || "")}</b>` +
        `　直线 ${escapeHtml(rt.distance || "")}` +
        (rt.bearing ? `，在起点${escapeHtml(rt.bearing)}方向` : "") +
        `<span class="map-warn">非实际道路</span>`;
    } else if (rt) {
      html += ` <b>${escapeHtml(rt.from || "")} → ${escapeHtml(rt.to || "")}</b>` +
        `　${escapeHtml(rt.mode || "")} ${escapeHtml(rt.distance || "")}` +
        `，约 ${escapeHtml(rt.duration || "")}`;
      if ((rt.routes || []).length > 1) {
        html += `<span class="map-alt">共 ${rt.routes.length} 条可选</span>`;
      }
      // 步行/骑行的距离其实来自驾车路网、时间只是估算 —— 不能不说
      if (rt.estimated) {
        html += `<span class="map-warn">时间为估算</span>`;
      }
    } else if (ui.nearby && (ui.nearby.items || []).length) {
      // 周边搜索：信息栏要说清"找的是什么、几家"，别只说"N 个地点"
      html += ` <b>${escapeHtml(ui.nearby.center_name || "")}</b> 周边 ` +
        `${ui.nearby.radius} 米内的${escapeHtml(ui.nearby.category || "")}` +
        `（${ui.nearby.total} 家）`;
    } else if ((ui.markers || []).length) {
      html += ` <b>${escapeHtml(ui.markers[0].name || "")}</b>` +
        ((ui.markers || []).length > 1 ? ` 等 ${ui.markers.length} 个地点` : "");
    }
    // 天气（只有请求里带 weather:true 才有）
    const wt = (rt && rt.weather) || null;
    if (wt) {
      const fn = wt.from_now || {}, td = wt.to_day || {};
      const bits = [];
      if (fn.desc) {
        bits.push("出发地此刻 " + escapeHtml(fn.desc) +
          (fn.temp != null ? " " + Math.round(fn.temp) + "℃" : ""));
      }
      if (td.desc) {
        // ⚠️ 写「抵达那天」而不是「抵达时」：高德没有逐小时接口，
        //    给的是**当天的逐日预报**。写成"抵达时"就等于骗人。
        bits.push("抵达那天（" + escapeHtml(wt.to_day_label || "当天") + "）" +
          escapeHtml(td.desc) +
          (td.low != null && td.high != null
            ? " " + Math.round(td.low) + "~" + Math.round(td.high) + "℃" : ""));
      }
      if (bits.length) {
        const src = wt.src || "";
        html += `<div class="map-weather">${bits.join("　·　")}` +
          (src ? `<span class="map-src">（数据源 ${escapeHtml(src)}）</span>` : "") +
          `</div>`;
      }
    }
    if (rt && rt.note) {
      html += `<div class="map-note">${escapeHtml(rt.note)}</div>`;
    }
    // 出行方式对比（后端用同一套路网算出来的真实数据）
    if (rt && (rt.modes || []).length) {
      const chips = rt.modes.map(function (m) {
        return `<span class="mode-chip${m.suggest ? " on" : ""}">` +
          `${escapeHtml(m.mode)} ${escapeHtml(m.distance)} · ${escapeHtml(m.duration)}` +
          (m.estimated ? "（估算）" : "") + `</span>`;
      }).join("");
      html += `<div class="map-modes">${chips}` +
        (rt.suggest_reason
          ? `<div class="map-note">建议：${escapeHtml(rt.suggest_reason)}</div>` : "") +
        `</div>`;
    }
    // 周边场所清单
    const nb = ui.nearby || null;
    if (nb && (nb.items || []).length) {
      const rows = nb.items.map(function (it) {
        const extra = [];
        if (it.cost) extra.push("人均 " + escapeHtml(it.cost) + " 元");
        if (it.addr) extra.push(escapeHtml(it.addr));
        if (it.phone) extra.push("电话 " + escapeHtml(it.phone));
        if (it.hours) extra.push("营业 " + escapeHtml(it.hours));
        // 走高德时有**真实评分** —— 这是用户最想看的
        const rt = it.rating
          ? `<span class="nb-rating">★ ${escapeHtml(it.rating)}</span>` : "";
        return `<li><b>${escapeHtml(it.name)}</b>` +
          `<span class="nb-dist">${escapeHtml(it.dist)}</span>` + rt +
          (extra.length ? `<div class="nb-extra">${extra.join("　·　")}</div>` : "") +
          (it.note ? `<div class="nb-note">${escapeHtml(it.note)}</div>` : "") +
          `</li>`;
      }).join("");
      // 信息栏已经写了「汕头大学 周边 2000 米内的餐厅（3 家）」，
      // 这里别再放一遍标题（实测会连着出现两遍，很啰嗦）
      html += `<div class="map-nearby">` +
        (nb.source === "amap"
          ? `<div class="nb-src">数据来自高德地图</div>` : "") +
        `<ul>${rows}</ul></div>`;
    }
    info.innerHTML = html || "地图";

    const canvas = document.createElement("div");
    canvas.className = "map-canvas";
    canvas.id = "mmMap" + (++mapSeq);

    box.appendChild(info);
    box.appendChild(canvas);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    const id = canvas.id;
    // 等这一帧排完再初始化，否则容器高度还是 0，地图画不出来
    requestAnimationFrame(() => initMapCard(id, ui));
    return wrap;
  }

  // 离线时地图上没有底图 → 给一句明白话，别让用户对着灰底猜"是不是坏了"
  function mapOfflineTip(el) {
    if (el.querySelector(".map-offline-tip")) return;
    const d = document.createElement("div");
    d.className = "map-offline-tip";
    d.innerHTML = "离线模式：不显示地图底图。" +
      "<br>地点、路线这些结论照样给；要看底图把顶栏的「联网」打开。";
    el.appendChild(d);
  }

  // ---------- 坐标系：WGS-84 → GCJ-02（火星坐标） ----------
  // 为什么必须有这个：高德底图是 **GCJ-02** 的，而我们的数据（GPS / OpenStreetMap）
  // 是 **WGS-84** 的，两者在国内差 **50~500 米**。直接把 WGS-84 的点画到高德底图上，
  // 视觉上就是"点和路整体错开一个街区" —— 看起来像数据查错了，其实只是没换算。
  // 这里跟后端 backend/amap.py 的算法保持一致（同一套公式，结果才是同一处）。
  const GCJ_A = 6378245.0;
  const GCJ_EE = 0.00669342162296594323;

  function gcjOutOfChina(lng, lat) {
    return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
  }
  function gcjTfLat(x, y) {
    let r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y +
      0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function gcjTfLng(x, y) {
    let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return r;
  }
  // 入参/出参都是 [lat, lon]（跟着 Leaflet 的习惯走，免得来回调顺序出错）
  function wgs2gcj(lat, lon) {
    if (gcjOutOfChina(lon, lat)) return [lat, lon];
    let dLat = gcjTfLat(lon - 105.0, lat - 35.0);
    let dLon = gcjTfLng(lon - 105.0, lat - 35.0);
    const rad = lat / 180.0 * Math.PI;
    let magic = Math.sin(rad);
    magic = 1 - GCJ_EE * magic * magic;
    const sq = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sq) * Math.PI);
    dLon = (dLon * 180.0) / (GCJ_A / sq * Math.cos(rad) * Math.PI);
    return [lat + dLat, lon + dLon];
  }

  function initMapCard(elId, ui) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (typeof L === "undefined") {
      el.innerHTML = '<div class="hint" style="padding:10px">地图组件没加载出来' +
        '（frontend/lib/leaflet 缺文件）</div>';
      return;
    }
    // ---- 底图：联网 + 配了高德 key → 高德；其余（离线 / 没配 key）→ OSM ----
    // 离线时其实两张底图都取不到（不缓存），但 tile_source 仍然要定下来 ——
    // 前端据此决定打点要不要做 WGS-84 → GCJ-02 换算，不能少。
    const useAmap = ui.tile_source === "amap";
    const toTile = useAmap
      ? function (la, lo) { return wgs2gcj(la, lo); }
      : function (la, lo) { return [la, lo]; };
    const ptsOf = function (arr) {
      return (arr || []).map(function (p) { return toTile(p[0], p[1]); });
    };
    const center = ui.center || [23.13, 113.26];
    const map = L.map(el, { zoomControl: true, attributionControl: true })
      .setView(toTile(center[0], center[1]), ui.zoom || 12);

    // 瓦片一律走后端代理（国内直连 OSM 官方瓦片经常超时，后端统一换了镜像）。
    // ⚠️ 后端**不做任何缓存**、响应带 no-store，所以离线时这里就是 404，
    //    要靠 tileerror 接住、给用户一句明白话。
    let tileErr = 0;
    const layer = L.tileLayer(
      useAmap ? "/api/map/amap/{z}/{x}/{y}.png" : "/api/map/tile/{z}/{x}/{y}.png", {
        minZoom: 3, maxZoom: 19,
        attribution: useAmap
          ? "地图数据 © 高德地图（GCJ-02）"
          : "地图数据 © OpenStreetMap 贡献者",
      });
    layer.on("tileerror", function () {
      tileErr++;
      if (ui.online === false && tileErr >= 5) mapOfflineTip(el);
    });
    layer.addTo(map);

    const span = [];
    (ui.markers || []).forEach(function (mk) {
      const p = toTile(mk.lat, mk.lon);
      const icon = L.divIcon({ className: "mm-pin", html: "<i></i>",
                               iconSize: [18, 18], iconAnchor: [9, 9] });
      const m1 = L.marker(p, { icon: icon, title: mk.name || "" }).addTo(map);
      const body = "<b>" + escapeHtml(mk.name || "") + "</b>" +
        (mk.addr ? "<br><span style='color:#666'>" + escapeHtml(mk.addr) + "</span>" : "");
      m1.bindPopup(body);
      span.push(p);
    });

    const rt = ui.route;
    if (rt && (rt.routes || []).length > 1) {
      // 多条候选：**推荐的那条蓝色加粗实线**，其余灰色虚线 —— 一眼看出主次。
      // 点线还能弹出各自的距离/时间/推荐理由。
      rt.routes.forEach(function (r) {
        if (!r.points || r.points.length < 2) return;
        const rec = !!r.recommended;
        const pts = ptsOf(r.points);       // 高德底图时这里会转成 GCJ-02
        const line = L.polyline(pts, {
          color: rec ? "#2e75b6" : "#9aa7b4",
          weight: rec ? 6 : 4,
          opacity: rec ? 0.9 : 0.65,
          dashArray: rec ? null : "7 7",
        }).addTo(map);
        line.bindPopup((rec ? "<b>推荐路线</b>" : "备选路线") + "<br>" +
          escapeHtml(r.distance || "") + "，约 " + escapeHtml(r.duration || "") +
          (r.reason ? "<br><span style='color:#666'>" + escapeHtml(r.reason) + "</span>" : ""));
        span.push.apply(span, pts);
      });
    } else if (rt && (rt.points || []).length > 1) {
      const pts = ptsOf(rt.points);
      L.polyline(pts, { color: "#2e75b6", weight: 5, opacity: 0.85 })
        .addTo(map);
      span.push.apply(span, pts);
    } else if (rt && rt.straight) {
      // 离线兜底：没有路网，用橙色虚线连两点，一眼就能和真路线区分开
      const ms = ui.markers || [];
      const pa = ms.filter(function (m) { return m.name === ui.route.from; })[0] || ms[0];
      const pb = ms.filter(function (m) { return m.name === ui.route.to; })[0] ||
                 ms[ms.length - 1];
      if (pa && pb) {
        L.polyline([toTile(pa.lat, pa.lon), toTile(pb.lat, pb.lon)],
                   { color: "#e08a2e", weight: 3, dashArray: "8 8", opacity: 0.9 })
          .addTo(map);
      }
    }
    try {
      if (span.length > 1) map.fitBounds(L.latLngBounds(span).pad(0.18));
      else if (span.length === 1) map.setView(span[0], ui.zoom || 14);
    } catch (e) { /* 只有一个点之类，保持默认视野即可 */ }
    // 卡片在聊天流里，容器尺寸可能要等一帧才稳定
    setTimeout(function () { try { map.invalidateSize(); } catch (e) {} }, 260);
    window.__mmMaps = (window.__mmMaps || []).concat([map]);
  }

  // 高德 key 面板：填了立刻生效，不用重启，也不用去改 config.json
  // ---------- 高德 key 的状态：按钮亮/暗 + 出问题主动提醒 ----------
  // 需求来自用户：配好了按钮要**亮起**，失效/被重置要**变暗**并催他重配，
  // 而且要**自动持续检测**、出异常就提醒。
  // ⚠️ 为什么要"持续检测"：key 会因为额度用满 / 控制台重置 / 服务端异常而
  //    不声不响地失效 —— 用户只会觉得"地图怎么突然变难用了"，根本不知道原因。
  let _amapState = null;          // 上一次的状态，用来判断"刚刚变坏了"
  let _amapSelfAction = false;    // 刚才是用户自己点的连接/断开 —— 那是主动行为，别报警
  let _amapSeq = 0;               // 请求序号：只认最后一次发出的那个请求的结果
  // force=true 时走 `?force=1`：**无视后端缓存、真调一次高德**。
  // 用在两个地方：① 用户刚把「联网」打开（那时必须重新确认 key 通不通）；
  //              ② 面板上的「重新验证」按钮。
  async function refreshAmapState(force) {
    const btn = document.getElementById("amapKeyBtn");
    if (!btn) return null;
    const seq = ++_amapSeq;
    let s = null;
    try {
      s = await api("/api/map/amap_status" + (force ? "?force=1" : ""));
    } catch (e) {
      return null;                // 读不到就别瞎标，保持现状
    }
    // ⚠️ 期间又发起了更新的请求（比如用户连着拨了两次「联网」开关）→
    //    这一份已经过期，丢掉。不加这个守卫的话，先发的慢请求**后到**，
    //    会把界面刷回旧状态，看着就是"按钮没跟上开关"。
    if (seq !== _amapSeq) return null;
    const prev = _amapState;
    _amapState = s;
    // ⚠️ 关掉「联网」开关时，高德**根本用不了**（一个请求都发不出去），
    //    所以按钮也要跟着变暗 —— 但**不能报红**：那不是 key 坏了，是用户自己关的。
    //    点它要给的是"先打开联网"的提示，不是"你的 key 有问题"。
    const online = s.online !== false;
    const armed = !!(s.configured && s.enabled) && online;   // 真正在用的状态
    // 五种样子：
    //   可用              → 亮起（绿 + ●）
    //   联网开着但不可用  → 变暗 + 红「!」
    //   用户主动断开      → 变暗（key 还留着，不报红）
    //   没配              → 变暗
    //   **联网关掉**      → 变暗（也不能报红）
    btn.classList.toggle("on", online && !!s.ok);
    btn.classList.toggle("bad", armed && !s.ok);
    // 文字保持固定，状态靠**颜色 + 右上角标记**表达（●=可用 / !=异常）——
    // 这样它和顶栏其它开关长得一样，不再是一个"跳出来"的按钮。
    btn.textContent = "高德 key";
    btn.title = !online
      ? "离线模式：高德用不了 —— 先打开顶栏的「联网」开关"
      : (s.ok
          ? ("已连接（" + (s.key_hint || "") + "）· 点一下查看、验证、断开或更换 key")
          : (armed
              ? ("⚠️ 当前不可用：" + (s.message || "原因未知") + " —— 点一下处理")
              : (s.configured
                  ? "已断开（key 还留着）· 点一下可以重新连接"
                  : "还没配高德 key。点一下配置：填了能搜到全国小店、有真实评分和实时路况")));

    // ⚠️ 只在"从可用变成不可用"的那一下提醒，不是每次都弹；
    //    而且**用户自己点断开 / 自己关联网时不要弹**（那是他要的，不是故障）。
    const self = _amapSelfAction;
    _amapSelfAction = false;
    if (!self && prev && prev.ok && !s.ok && s.enabled && online) {
      showToast("⚠️ 高德 key 现在用不了了：" + (s.message || "原因未知") +
                "　点顶栏「高德 key」处理一下", "warn");
    }
    return s;
  }
  window.__refreshAmapState = refreshAmapState;
  // 45 秒轮询一次（后端那边自己带节流：正常 15 分钟才真调一次高德）
  setInterval(refreshAmapState, 45000);
  setTimeout(refreshAmapState, 1200);

  // ⚠️ 地图**不做任何本地缓存**（2026-09-18 起），所以原来那个「地图缓存」按钮没了，
  //    换成这个 —— 它解决的才是真问题（新机器上没 key，地图只能退回 OpenStreetMap）。
  //
  // 面板的交互是按用户要求定的：
  //   · **连接 / 断开** 是个开关：断开**不删 key**（"重新输入太麻烦"），只是不用它；
  //   · 已经存下的 key **只读显示**，不能直接改；
  //   · 要换 key 得点「更改 API」：输入新的 → 验证成功才替换旧的 → 失败则原 key 一字不动；
  //   · 「重新验证」：无视缓存立刻真调一次高德，确认现在到底通不通。
  async function showAmapKey() {
    if (document.querySelector(".amap-layer")) return;
    await refreshAmapState();                 // 先刷新一下，弹出来的状态才是准的
    // 关着「联网」时点它：先把话说清楚，别让用户以为是 key 出问题了。
    // （面板照样打开 —— 状态、key、断开/更改 这些在离线时也要能看能管。）
    if (_amapState && _amapState.online === false) {
      showToast("现在是离线模式，高德用不了 —— 先打开顶栏的「联网」开关", "warn");
    }
    let full = "";
    try {
      const d = await api("/api/config");
      full = String(((d || {}).config || {}).amap_key || "");
    } catch (e) { /* 读不到就按没配处理，下面会说实话 */ }

    const layer = document.createElement("div");
    layer.className = "confirm-layer amap-layer";
    layer.innerHTML =
      '<div class="confirm-box">' +
      '<div class="confirm-title">🗺️ 高德地图</div>' +
      '<div class="amap-state"></div>' +
      '<div class="amap-keyrow">' +
      '<input class="amap-input" type="text" readonly />' +
      '<button class="btn ghost amap-change" type="button">更改 API</button>' +
      '</div>' +
      '<div class="amap-edit" style="display:none">' +
      '<div class="amap-edit-hint">粘贴**新的** key。验证通过才会替换掉现在这个；' +
      '验证不通过的话，原来的 key 不动。</div>' +
      '<input class="amap-newinput" type="text" placeholder="把新的 32 位 key 粘贴在这里" />' +
      '</div>' +
      '<div class="confirm-reason amap-tip">' +
      '配了 key 之后，地图能搜到全国的小店、有真实评分、有实时路况和公交换乘，' +
      '步行骑行也是真实路径。<br>断开或没配时退回 OpenStreetMap —— ' +
      '能查大城市/道路/机场车站，但小店、评分、路况、公交都没有。<br>' +
      '申请（1 分钟）：console.amap.com → 手机号注册+实名 → 建应用 → 加 Key → ' +
      '<b>服务平台必须选「Web 服务」</b>（选成「Web 端(JS API)」用不了）。' +
      '</div>' +
      '<div class="amap-msg"></div>' +
      '<div class="confirm-btns amap-btns"></div>' +
      '</div>';
    document.body.appendChild(layer);

    const stateEl = layer.querySelector(".amap-state");
    const input = layer.querySelector(".amap-input");
    const newInput = layer.querySelector(".amap-newinput");
    const editBox = layer.querySelector(".amap-edit");
    const keyRow = layer.querySelector(".amap-keyrow");
    const changeBtn = layer.querySelector(".amap-change");
    const tipEl = layer.querySelector(".amap-tip");
    const msg = layer.querySelector(".amap-msg");
    const btnsEl = layer.querySelector(".amap-btns");
    const say = (t, kind) => { msg.textContent = t; msg.className = "amap-msg " + (kind || ""); };
    let editing = false;

    function render() {
      const s = _amapState || {};
      const online = s.online !== false;
      const armed = !!(s.configured && s.enabled) && online;
      let line, cls;
      if (!online) {
        // ⚠️ 离线时**先解释清楚这是什么原因** —— 不然用户会以为是 key 坏了
        line = "🌐 离线模式 —— 高德现在用不了（打开顶栏的「联网」开关就会自动恢复）。" +
               (s.configured ? "key 还在，不用重输。" : "");
        cls = "";
      } else if (s.ok) {
        line = "✅ 配置成功，正在使用（" + (s.key_hint || "") + "）"; cls = "ok";
      } else if (armed) {
        line = "⚠️ 已配置但**当前不可用**：" + (s.message || "原因未知"); cls = "bad";
      } else if (s.configured) {
        line = "⏸ 已断开 —— **key 还留着**，点「连接」就能恢复（不用重新输入）"; cls = "";
      } else if (s.configured === false) {
        line = "未配置 —— 地图在用 OpenStreetMap（数据弱一些）"; cls = "";
      } else {
        line = "状态读取失败（后端没响应？）—— 可以点「更改 API」或「自动验证」重试"; cls = "bad";
      }
      stateEl.textContent = line;
      stateEl.className = "amap-state " + cls;

      // key 只读展示：有就显示完整 key（本地应用，方便核对）
      input.value = s.configured ? full : "";
      input.placeholder = s.configured ? "" : "（还没有配 key）";
      changeBtn.style.display = editing ? "none" : "";
      keyRow.style.display = editing ? "none" : "";
      editBox.style.display = editing ? "" : "none";
      tipEl.style.display = editing ? "none" : "";

      let html = '<button class="btn ghost" data-act="close">关闭</button>';
      if (editing) {
        html += '<button class="btn ghost" data-act="cancel">取消</button>' +
                '<button class="btn primary" data-act="save">验证并保存</button>';
      } else {
        // 「自动验证」：无视缓存，立刻真调一次高德（离线时没有意义，不显示）
        if (online && s.configured) {
          html += '<button class="btn ghost" data-act="verify">自动验证</button>';
        }
        if (s.configured) {
          html += '<button class="btn ' + (s.enabled ? "ghost" : "primary") +
                  '" data-act="toggle">' + (s.enabled ? "断开" : "连接") + '</button>';
        } else {
          html += '<button class="btn primary" data-act="change">填一个 key</button>';
        }
      }
      btnsEl.innerHTML = html;

      btnsEl.querySelector('[data-act="close"]').onclick = () => layer.remove();
      const c = btnsEl.querySelector('[data-act="cancel"]');
      if (c) c.onclick = () => { editing = false; say(""); render(); };
      const vf = btnsEl.querySelector('[data-act="verify"]');
      if (vf) vf.onclick = () => doVerify(vf);
      const t = btnsEl.querySelector('[data-act="toggle"]');
      if (t) t.onclick = () => doToggle();
      const ch = btnsEl.querySelector('[data-act="change"]');
      if (ch) ch.onclick = () => { editing = true; say(""); render(); newInput.focus(); };
      const sv = btnsEl.querySelector('[data-act="save"]');
      if (sv) sv.onclick = () => doSave();
      changeBtn.onclick = () => { editing = true; say(""); render(); newInput.focus(); };
    }

    async function doToggle() {
      const s = _amapState || {};
      const turnOn = !s.enabled;
      btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      say(turnOn ? "正在连接…" : "正在断开…", "");
      try {
        const r = await api("/api/map/amap_toggle", {
          method: "POST", body: JSON.stringify({ enabled: turnOn }) });
        if (r && r.ok) {
          _amapSelfAction = true;          // 这是我点的，别弹"key 用不了了"
          await refreshAmapState();
          say(r.message || (turnOn ? "已连接" : "已断开"), turnOn ? "ok" : "");
          showToast(turnOn ? "高德已连接" : "已断开高德（key 还留着）",
                    turnOn ? "ok" : "warn");
        } else {
          say((r && r.message) || "操作失败", "bad");
        }
      } catch (e) {
        say("连不上后端：" + String(e.message || e), "bad");
      } finally {
        btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        render();
      }
    }

    // 「自动验证」：无视后端缓存，**立刻真调一次高德**，把结论直接摆在面板上。
    // 为什么要这个按钮：key 会因为额度、控制台重置等原因不声不响失效，
    // 用户想"我确认一下现在到底通不通"时，不该只能等 15 分钟一次的自动复查。
    async function doVerify(btnEl) {
      if (btnEl) btnEl.disabled = true;
      say("正在连高德验证…（这一步会真的调一次高德接口）", "");
      try {
        const r = await api("/api/map/amap_status?force=1");
        _amapState = r;
        if (r && r.online === false) {
          say("现在是离线模式，没法验证 —— 先打开顶栏的「联网」开关。", "bad");
        } else if (r && r.ok) {
          say("✅ 验证通过：" + (r.message || "高德可以正常使用") +
              "\n（刚才实时查到：" + (r.key_hint || "") + "）", "ok");
          showToast("高德验证通过，一切正常", "ok");
        } else if (r && r.configured === false) {
          say("还没配高德 key —— 点「填一个 key」配一下。", "bad");
        } else {
          say("❌ 验证没通过：" + ((r && r.message) || "原因未知") +
              "\n（可以点「更改 API」换一个 key，或者先「断开」用回 OpenStreetMap）", "bad");
          showToast("高德验证没通过，看面板里的原因", "warn");
        }
      } catch (e) {
        say("验证请求失败：" + String(e.message || e), "bad");
      } finally {
        render();
      }
    }

    async function doSave() {
      const v = (newInput.value || "").trim();
      if (!v) { say("还没有填新的 key 呢。", "bad"); newInput.focus(); return; }
      const old = input.value || "";
      btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      say("正在连高德验证…（验证通过才会替换）", "");
      try {
        const r = await api("/api/map/amap_key", {
          method: "POST", body: JSON.stringify({ key: v }) });
        if (r && r.ok) {
          _amapSelfAction = true;
          const d = await api("/api/config").catch(() => null);
          full = String((((d || {}).config || {}).amap_key) || v);
          editing = false;
          newInput.value = "";
          await refreshAmapState();
          say(r.message || "已改用新的 key", "ok");
          showToast("高德 key 已更新", "ok");
        } else {
          // ⚠️ 失败时旧 key 必须原样不动 —— 把这句话说出来，用户才敢放心重试
          say(((r && r.message) || "验证失败") +
              "\n（原来的 key 没有被改动，还是 " + (old ? old.slice(0, 6) + "…" : "原来那个") + "）",
              "bad");
        }
      } catch (e) {
        say("连不上后端：" + String(e.message || e), "bad");
      } finally {
        btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        render();
      }
    }

    newInput.onkeydown = (e) => {
      if (e.key === "Enter") { const b = btnsEl.querySelector('[data-act="save"]'); if (b) b.click(); }
    };
    render();
  }
  // ---------- 采样参数（温度 / top-p）：拖动即生效 ----------
  // 为什么做成"滑条 + 建议值"而不是输入框：
  //   这两个参数**没有唯一正确答案**，用户需要的是"手感"（拖一下马上看效果）
  //   和一个锚点（建议值）。所以每条滑条旁边都标着建议值，点一下就能跳回去。
  //
  // ⚠️ 生效时机：后端每次 /api/chat 都会重新读 config（`cfg = config.load_config()`），
  //    所以**拖完立刻生效、不用重启** —— 这句话必须在界面上说明白，
  //    否则用户会以为"改了没反应"（这类误会以前在 num_ctx 上踩过）。
  const SAMPLE_META = {
    temperature: {
      name: "温度 temperature", min: 0, max: 2, step: 0.05, best: 0.6,
      less: "更严谨", more: "更发散",
      hint: "越高越发散：话更活、也更容易跑偏和胡编；越低越稳、越可复现。" +
            "思考型模型（Qwen3 这类）官方推荐 0.6 —— 写代码、要结论稳定时用 0.2~0.4，" +
            "头脑风暴可以 1.0~1.3；超过 1.5 基本就开始胡说八道了。",
    },
    top_p: {
      name: "采样范围 top-p", min: 0.1, max: 1, step: 0.01, best: 0.95,
      less: "更死板", more: "更野",
      hint: "只在「概率加起来到 top-p 的那一批词」里挑下一个词。0.95 既排掉长尾的胡话、" +
            "又保留一点灵活度；调到 1.0 连很离谱的词也会被选上（更野）；" +
            "低于 0.5 会变得死板，而且容易开始重复（能挑的词太少，来回绕）。",
    },
  };

  async function showSampling() {
    if (document.querySelector(".sample-layer")) return;      // 防重复弹层
    let cfg = {};
    try { cfg = ((await api("/api/config")) || {}).config || {}; } catch (e) { /* 读不到就用默认 */ }
    const cur = {};
    Object.keys(SAMPLE_META).forEach((k) => {
      const t = SAMPLE_META[k];
      const v = Number(cfg[k]);
      cur[k] = Number.isFinite(v) ? Math.min(t.max, Math.max(t.min, v)) : t.best;
    });

    const rowHtml = Object.keys(SAMPLE_META).map((k) => {
      const t = SAMPLE_META[k];
      // 建议刻度**按真实比例定位**（不是简单居中）——
      // 温度建议 0.6 落在 30% 处、top-p 建议 0.95 落在 94% 处，
      // 摆到真实位置上，"我现在偏左还是偏右"才一眼可见。
      const pct = ((t.best - t.min) / (t.max - t.min) * 100).toFixed(1);
      return '<div class="sample-row" data-key="' + k + '">' +
        '<div class="sample-head">' +
        '<span class="sample-name">' + t.name + '</span>' +
        '<span class="sample-right">' +
        '<b class="sample-val" data-key="' + k + '">' + cur[k].toFixed(2) + '</b>' +
        '<button type="button" class="sample-best" data-key="' + k + '" ' +
        'title="点一下跳回建议值">建议 ' + t.best + '</button>' +
        '</span></div>' +
        '<input class="sample-range" type="range" data-key="' + k + '" ' +
        'min="' + t.min + '" max="' + t.max + '" step="' + t.step + '" value="' + cur[k] + '" />' +
        '<div class="sample-scale"><span>' + t.less + ' ' + t.min + '</span>' +
        '<span class="sample-tick" style="left:' + pct + '%">▲ 建议 ' + t.best + '</span>' +
        '<span>' + t.max + ' ' + t.more + '</span></div>' +
        '<div class="sample-hint">' + t.hint + '</div>' +
        '</div>';
    }).join("");

    const layer = document.createElement("div");
    layer.className = "confirm-layer sample-layer";
    layer.innerHTML =
      '<div class="confirm-box sample-box">' +
      '<div class="confirm-title">🎛 采样参数</div>' +
      '<div class="sample-sub">拖动滑条 → <b>立刻生效，不用重启</b>（下一次提问就用新值）。' +
      '旁边标着<b>建议值</b>，点它可以直接跳回去。</div>' +
      rowHtml +
      '<div class="sample-msg"></div>' +
      '<div class="confirm-btns">' +
      '<button class="btn ghost sample-reset" type="button">恢复建议值</button>' +
      '<button class="btn primary sample-close" type="button">完成</button>' +
      '</div></div>';
    document.body.appendChild(layer);

    const msgEl = layer.querySelector(".sample-msg");
    const valEl = (k) => layer.querySelector('.sample-val[data-key="' + k + '"]');
    const rangeEl = (k) => layer.querySelector('.sample-range[data-key="' + k + '"]');

    // 数值离建议值远的时候标黄 —— 让"我是不是调歪了"一眼可见
    function paint(k) {
      const t = SAMPLE_META[k];
      const v = Number(rangeEl(k).value);
      valEl(k).textContent = v.toFixed(2);
      const far = Math.abs(v - t.best) > (t.max - t.min) * 0.18;
      valEl(k).classList.toggle("off", far);
    }

    // 拖动期间不刷请求：等停手 350ms 再存一次（否则一拖就是几十个 POST）
    const pending = {};
    let timer = null;
    async function flush() {
      const body = {};
      Object.keys(pending).forEach((k) => { body[k] = pending[k]; delete pending[k]; });
      if (!Object.keys(body).length) return;
      try {
        await api("/api/config", { method: "POST", body: JSON.stringify(body) });
        msgEl.textContent = "✅ 已保存：" + Object.entries(body)
          .map(([k, v]) => (SAMPLE_META[k] ? SAMPLE_META[k].name.split(" ")[0] : k) +
               " " + Number(v).toFixed(2)).join(" · ") + " —— 立即生效";
        msgEl.className = "sample-msg ok";
      } catch (e) {
        msgEl.textContent = "⚠ 保存失败：" + String(e.message || e);
        msgEl.className = "sample-msg err";
      }
    }
    function queue(k, v) {
      pending[k] = v;
      if (timer) clearTimeout(timer);
      timer = setTimeout(flush, 350);
    }
    function setVal(k, v, save) {
      rangeEl(k).value = v;
      paint(k);
      if (save) queue(k, v);
    }

    Object.keys(SAMPLE_META).forEach((k) => {
      paint(k);
      rangeEl(k).addEventListener("input", () => {
        paint(k);
        queue(k, Number(rangeEl(k).value));
      });
      layer.querySelector('.sample-best[data-key="' + k + '"]')
        .addEventListener("click", () => setVal(k, SAMPLE_META[k].best, true));
    });
    layer.querySelector(".sample-reset").addEventListener("click", () => {
      Object.keys(SAMPLE_META).forEach((k) => setVal(k, SAMPLE_META[k].best, false));
      queue("temperature", SAMPLE_META.temperature.best);
      pending.top_p = SAMPLE_META.top_p.best;
      if (timer) clearTimeout(timer);
      flush();
    });

    function close() { layer.remove(); }
    layer.querySelector(".sample-close").addEventListener("click", async () => {
      if (timer) clearTimeout(timer);
      await flush();                     // 关之前把没存完的补上，别丢改动
      close();
    });
    layer.addEventListener("click", (e) => { if (e.target === layer) close(); });
  }
  window.__showSampling = showSampling;

  window.__showAmapKey = showAmapKey;
  window.__makeMapCard = makeMapCard;      // 导出一下，方便排查"地图没画出来"

  // ---------- 站内下载：桌面壳里改走原生「另存为」 ----------
  // ⚠️ 这是用户实际报过的坑：桌面窗口是 WebView2，而 pywebview 的
  //    settings['ALLOW_DOWNLOADS'] **默认是 False** —— 后端在 DownloadStarting
  //    里直接 `args.Cancel = True`，把页面发起的所有下载**静默取消**。
  //    表现就是：点「点击下载」毫无反应，不报错、不提示，用户完全不知道成没成。
  //    （和「WebView2 不支持 window.prompt、静默返回 null」是同一类坑。）
  // 现在的做法：有 pywebview 桥就用它 —— 弹系统「另存为」、存到用户选的位置、
  // 再把结果如实说出来。浏览器里没有这个桥，照旧走浏览器的下载。
  function bridgeApi() {
    return (window.pywebview && window.pywebview.api) || null;
  }
  let savingNow = false;
  async function saveViaDesktop(url) {
    const api = bridgeApi();
    if (!api || typeof api.save_file !== "function") return false;
    // ⚠️ 防重入：手快点两下会**并发**发出多个保存请求，第 2 个抢不到系统对话框
    // → 立刻返回空 → 界面连着弹「已取消保存」，看起来就像"点了没用"（实测踩到：
    // 日志里 1 秒内三条「另存为：用户取消了」）。
    if (savingNow) {
      showToast("上一次保存还没结束，稍等一下再点", "warn");
      return true;
    }
    savingNow = true;
    try {
      const r = await api.save_file(url, "");
      if (r && r.ok) showToast("已保存：" + r.path, "ok");
      else if (r && r.cancelled) showToast("已取消保存（在「另存为」里选了取消）", "warn");
      else showToast("保存失败：" + ((r && r.error) || "未知原因"), "warn");
    } catch (e) {
      showToast("保存失败：" + String(e.message || e), "warn");
    } finally {
      savingNow = false;
    }
    return true;                       // true = 已经接管，别再走默认行为
  }
  // 事件委托：回答里的下载/定位按钮是 innerHTML 塞进去的，逐个绑定不现实
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    // ①「在文件库中打开」：跳到生成文库并选中该文件
    const op = t.closest(".dl-open");
    if (op) {
      e.preventDefault();
      openInLibrary(op.getAttribute("data-rel") || "");
      return;
    }
    // ② 下载按钮：桌面壳里走原生「另存为」（否则会被 WebView2 静默吞掉）
    const a = t.closest("a.dl-link");
    if (!a) return;
    const url = fixApiUrl(a.getAttribute("href") || a.href || "");
    if (url.indexOf("/api/") !== 0) return;
    const api = bridgeApi();
    if (!api || typeof api.save_file !== "function") return;   // 浏览器：默认下载
    e.preventDefault();
    saveViaDesktop(url);
  });
  window.__saveViaDesktop = saveViaDesktop;       // 文库面板等处复用

  function addMsg(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    el.innerHTML = `<div class="bubble"></div>`;
    // 助手的历史回答也走一遍链接渲染 —— 否则程序重启后恢复出来的对话里，
    // 之前给过的下载链接又变回不能点的纯文本了（实测踩到）。
    if (role === "assistant") renderAnswerLinks(el.querySelector(".bubble"), text);
    else el.querySelector(".bubble").textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, opts));
    const t = await r.text();
    let data;
    try { data = JSON.parse(t); } catch { data = { raw: t }; }
    // ⚠️ 必须检查状态码。不检查的话，后端返回 400 / 500 时会把
    // `{detail: "已存在同名文件：xxx"}` 当成正常结果交给调用方，
    // 界面就会出现「已复制为：undefined」这种**把失败当成功**的提示 ——
    // 用户以为成了，其实什么都没做（实测踩到，这是最坑的一类 bug）。
    if (!r.ok) {
      const m = (data && (data.detail || data.error || data.message)) || ("HTTP " + r.status);
      const err = new Error(typeof m === "string" ? m : JSON.stringify(m));
      err.status = r.status;
      throw err;
    }
    return data;
  }

  // ---------- 状态检测 ----------
  // 「模型版本 + 参数大小」**一律从后端现读**（后端再去问 Ollama 的 /api/tags），
  // 前端一个模型名都不写死 —— 用户 2026-09-26 明确要求：
  // 界面显示的模型版本与参数大小要跟着最新的走（换模型后自动变，不用改前端）。
  function fmtModel(info) {
    if (!info) return "";
    // 名 · 参数量 · 量化档 · 体积，缺哪个就跳哪个（别显示成 "· · ·"）
    return [info.name, info.parameter_size, info.quantization_level, info.size_text]
      .filter((x) => x && String(x).trim()).join(" · ");
  }
  // 家族名 → 好看的大写写法（deepseek → DeepSeek，而不是 Deepseek）
  const MODEL_BRANDS = {
    qwen: "Qwen", deepseek: "DeepSeek", llama: "Llama", gemma: "Gemma",
    glm: "GLM", mistral: "Mistral", phi: "Phi",
  };

  async function refreshHealth() {
    try {
      const d = await api("/api/health");
      const dot = $("#statusDot"), t = $("#statusTitle"), det = $("#statusDetail");
      if (d.online) {
        dot.className = "dot" + (d.model_ready ? " on" : " off");
        t.textContent = d.model_ready ? "模型就绪（离线）" : "Ollama 在线，模型未下载";
        det.textContent = d.model_ready ? "智能体已就绪" : "请下载模型";
      } else {
        dot.className = "dot off";
        t.textContent = "Ollama 未运行";
        det.textContent = "请先启动 ollama serve";
      }
      // ---- 模型信息那一行（用户能一眼确认自己到底在用哪个模型）----
      const mi = $("#modelInfo");
      if (mi) {
        // ⚠️ 区分两种"没有信息"：**连不上** vs **真的没下载**。
        //    连不上时写"（未下载）"是错的 —— 模型明明装着，只是没启动 Ollama。
        const online = d.online !== false;
        const bits = [];
        const main = fmtModel(d.model_info);
        if (main) bits.push("对话 " + main);
        else if (d.model) bits.push("对话 " + d.model + (online ? "（未下载）" : ""));
        if (d.code_model) {
          // 就绪标记以 code_model_ready 为准（它是后端明确的结论），
          // 有它就一定有 code_model_info
          if (d.code_model_ready) bits.push("代码 " + fmtModel(d.code_model_info));
          else bits.push("代码 " + d.code_model + (online ? "（未下载，暂用默认模型）" : ""));
        }
        mi.textContent = bits.join("　|　");
        mi.title = bits.join("\n");
      }
      // ---- 左上角那句"XX 智能体"也按实际模型推导（原来写死 Qwen，换模型就说错了）----
      const bs = $("#brandSub");
      if (bs) {
        const nm = String(d.model || "").toLowerCase();
        const fam = String((d.model_info && d.model_info.family) || "").toLowerCase();
        let brand = "";
        for (const k of Object.keys(MODEL_BRANDS)) {
          if (nm.includes(k) || fam.includes(k)) { brand = MODEL_BRANDS[k]; break; }
        }
        bs.textContent = (brand ? brand + " " : "本地") + "智能体 · 数据不出机";
      }
    } catch { /* 后端未就绪时忽略 */ }
  }

  // ---------- 顶部正上方的能力开关（极简，仅开关）----------
  async function loadToggles() {
    try {
      const d = await api("/api/config");
      const c = d.config;
      document.querySelectorAll(".pill[data-cfg]").forEach((p) => {
        const key = p.dataset.cfg;
        const on = key === "memory_enabled" ? c.memory_enabled !== false : !!c[key];
        p.classList.toggle("on", on);
      });
      // 询问模式：二选一，不是开关（见 index.html 里为什么不加 data-cfg）
      setAskMode(c.ask_mode === "deep" ? "deep" : "quick");
      // 若联网模式已开启，顺带校验当前网络（断网时给出提示）
      if (c.web_enabled) {
        const net = await checkNetwork();
        if (!net.online) showToast("电脑未连接网络，联网搜索将不可用", "warn");
      }
    } catch {}
  }
  // ---------- 询问方式：快速了解 / 深度询问 ----------
  // 用户 2026-09-22：
  //   · 两种模式的区别只在"问到什么程度就动手"，**两种模式都不限制问题个数**
  //     （后端 ask_mode 注入系统提示，见 main.py 的 _ask_mode_line）；
  //   · 前端原来并排两个按钮，要求**合并成一个「询问方式」按钮**，
  //     并把当前选择直接显示在按钮上。
  const ASK_MODES = {
    quick: {
      name: "快速",
      full: "快速了解",
      desc: "只问最关键的 1~3 个问题，拿到基本信息就开工；"
          + "剩下的按最合理的默认做，并在结果里说明替你假设了什么。",
    },
    deep: {
      name: "深度",
      full: "深度询问",
      desc: "问题个数不限、可以分多轮，把关键信息问透再动手。"
          + "想做的东西越复杂，越建议用这个。",
    },
  };
  let askMode = "quick";                       // 当前选择（唯一事实来源）

  function askModeLabel() {
    return "询问方式：" + (ASK_MODES[askMode] || ASK_MODES.quick).name;
  }
  function setAskMode(mode) {
    askMode = ASK_MODES[mode] ? mode : "quick";
    const b = document.getElementById("askModeBtn");
    if (b) {
      b.textContent = askModeLabel();
      // 深度模式给个视觉标记（开着 accent 色），一眼能看出不是默认档
      b.classList.toggle("on", askMode === "deep");
    }
  }
  async function saveAskMode(mode) {
    setAskMode(mode);
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify({ ask_mode: askMode }) });
      showToast(askMode === "deep"
        ? "已切到「深度询问」：模型会把关键信息问透再动手"
        : "已切到「快速了解」：模型只问最关键的几条", "ok");
    } catch (e) {
      showToast("切换失败：" + String(e.message || e), "warn");
    }
  }
  // 点按钮 → 弹一个小面板二选一（不新开页面、不占顶栏宽度）
  function showAskModePicker() {
    if (document.querySelector(".askmode-layer")) return;      // 防重复弹层
    const layer = document.createElement("div");
    layer.className = "confirm-layer askmode-layer";
    const opts = Object.keys(ASK_MODES).map((k) => {
      const m = ASK_MODES[k];
      const on = k === askMode;
      return '<button type="button" class="askmode-opt' + (on ? " on" : "") +
        '" data-mode="' + k + '">' +
        '<span class="askmode-mark">' + (on ? "●" : "○") + "</span>" +
        '<span class="askmode-text"><b>' + m.full + "</b><em>" + m.desc + "</em></span>" +
        "</button>";
    }).join("");
    layer.innerHTML =
      '<div class="confirm-box askmode-box">' +
      '<div class="confirm-title">💬 询问方式</div>' +
      '<div class="sample-sub">决定模型在<b>动手之前</b>会问你多少问题。' +
      '随时可以换，<b>下一次提问就生效</b>。</div>' +
      opts +
      '<div class="confirm-btns">' +
      '<button class="btn primary askmode-close" type="button">完成</button>' +
      "</div></div>";
    document.body.appendChild(layer);

    const close = () => layer.remove();
    layer.querySelector(".askmode-close").onclick = close;
    layer.addEventListener("click", (e) => {
      if (e.target === layer) close();          // 点遮罩关闭
    });
    layer.querySelectorAll(".askmode-opt").forEach((btn) => {
      btn.onclick = async () => {
        await saveAskMode(btn.dataset.mode);
        close();
      };
    });
  }
  document.getElementById("askModeBtn") &&
    (document.getElementById("askModeBtn").onclick = showAskModePicker);
  window.__showAskMode = showAskModePicker;
  async function saveToggles() {
    const body = {
      memory_enabled: $('.pill[data-cfg="memory_enabled"]').classList.contains("on"),
      rag_enabled: $('.pill[data-cfg="rag_enabled"]').classList.contains("on"),
      web_enabled: $('.pill[data-cfg="web_enabled"]').classList.contains("on"),
      auto_memorize: $('.pill[data-cfg="auto_memorize"]').classList.contains("on"),
      code_auto_route: $('.pill[data-cfg="code_auto_route"]').classList.contains("on"),
      code_exec_enabled: $('.pill[data-cfg="code_exec_enabled"]').classList.contains("on"),
      // ⚠️ 带上询问方式：否则用户改完模式、再点任意开关时，
      //    保存的 body 里没有 ask_mode —— 后端只 merge 传了的键，本身不会丢，
      //    但两处状态容易不同步（这里显式带上，前端为准）。
      //    合并成一个按钮后，模式的唯一事实来源是 `askMode` 变量（不再从 DOM 反推）。
      ask_mode: askMode,
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
  }
  // 检测本机是否已连接互联网（开启「联网」前的前置校验）
  async function checkNetwork() {
    try {
      const r = await fetch("/api/net/check");
      return await r.json();
    } catch (e) {
      return { online: false, error: String(e) };
    }
  }

  // ⚠️ 只给**真正的开关**（带 data-cfg 的）绑开关逻辑。
  // 顶栏的「高德 key」按钮借用了 .pill 的外观，如果这里选 ".pill" 会把它一起抓进来，
  // `p.onclick = ...` 会**覆盖掉它自己的点击处理**，表现就是"按钮点了完全没反应"。
  // 加 [data-cfg] 限定就好。顶栏以后再加按钮，记得同样别让它被这条规则吃掉。
  document.querySelectorAll(".pill[data-cfg]").forEach((p) => {
    p.onclick = async () => {
      const key = p.dataset.cfg;
      const turningOn = !p.classList.contains("on");

      // 开启「联网」前先校验电脑是否联网：未联网则提示，且开关保持原状不变亮
      if (key === "web_enabled" && turningOn) {
        p.classList.add("checking");
        const net = await checkNetwork();
        p.classList.remove("checking");
        if (!net.online) {
          showToast("电脑未连接网络", "warn");
          return;
        }
      }

      p.classList.toggle("on");
      await saveToggles();
      if (key === "web_enabled") {
        const on = p.classList.contains("on");
        showToast(on ? "已开启联网模式" : "已关闭联网模式", on ? "ok" : "");
        // ⚠️ 联网开关一变，高德**还能不能用**这件事就立刻变了 ——
        //    必须**马上**同步地图按钮，等 45 秒轮询是不行的：
        //    实测用户关了联网后按钮还亮着，非要点它一下才变暗，
        //    看着就像"这个按钮坏了"。
        //    关掉时不必重新验证（离线必然用不了）；打开时要 force 一次，
        //    真确认 key 还好使，没问题才重新亮起。
        //    （app.js 整个包在一个 IIFE 里，直接调即可。）
        await refreshAmapState(on);
      }
      // 开启"本地算代码"是**有风险的操作**，必须明确说清而不是默默打开
      if (key === "code_exec_enabled" && p.classList.contains("on")) {
        showToast("已开启：模型写的 Python 会在你电脑上真实执行（临时目录 + 时限，会拦截删除/联网类操作）", "warn");
      }
      // 开了代码模型但本机没装时，别让用户以为坏了
      if (key === "code_auto_route" && p.classList.contains("on")) {
        try {
          const r = await fetch("/api/models");
          const d = await r.json();
          const names = (d.models || []).map((m) => m.name || "");
          const cfg = (await (await fetch("/api/config")).json()).config || {};
          const want = cfg.code_model || "";
          if (want && !names.some((n) => n === want || n.split(":")[0] === want.split(":")[0])) {
            showToast(`本机还没下载 ${want}，现在仍用默认模型；下载后会自动生效`, "warn");
          }
        } catch (e) {}
      }
    };
  });

  // 模型**全自动选择** —— 一律走后端的按轮路由，前端不做手动指定。
  // （曾经加过手动下拉，用户反馈"手动太麻烦了"，已移除。
  //   后端的 ChatRequest.model 覆盖能力保留着，随时能再挂上来。）

  // ---------- 事件 ----------
  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("hidden");

  // ---------- 拖拽文件到聊天框 ----------
  // 拖进来的**任何非图片/视频文件**都按"文档"处理：抽取正文当作本轮资料。
  // 这里刻意**不做扩展名白名单** —— 白名单会把没列到的类型静默丢掉，
  // 用户看到的就是"拖了完全没反应"。能不能读交给后端判断，读不出会明确说明原因。

  // 字数显示：不足 1000 就照实写，别四舍五入成"1k"（一份 50 字的文件显示"1k 字"很误导）
  function fmtChars(n) {
    n = Number(n || 0);
    return n >= 1000 ? (n / 1000).toFixed(1) + "k 字" : n + " 字";
  }

  async function addDocAttachment(f) {
    const chip = { name: f.name, text: "", chars: 0, loading: true };
    docs.push(chip);
    renderAttachments();
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      const r = await fetch("/api/doc/extract", { method: "POST", body: fd });
      const d = await r.json();
      if (d.ok) {
        chip.text = d.text || "";
        chip.chars = d.chars || 0;
        chip.loading = false;
        chip.note = d.note || "";
      } else {
        // 抽不出文字（例如旧版 .doc/.wps）：如实告诉用户，别静默失败
        const i = docs.indexOf(chip);
        if (i >= 0) docs.splice(i, 1);
        showToast(`《${f.name}》读不出文字：${d.error || "格式不支持"}`, "warn");
      }
    } catch (e) {
      const i = docs.indexOf(chip);
      if (i >= 0) docs.splice(i, 1);
      showToast(`《${f.name}》上传失败：${e.message || e}`, "warn");
    }
    renderAttachments();
  }

  function onDropFiles(files) {
    let list = files ? [...files] : [];
    if (!list.length) {
      // 别静默失败：拖进来却什么都没发生，用户只会以为功能坏了
      showToast("没有读到文件。请从资源管理器把文件直接拖到窗口里再松开。", "warn");
      return;
    }
    // ⚠️ **同一批里先去重**（2026-09-22 用户报"拖一张图结果贴上两张"）：
    //    剪贴板/拖拽源有时会同时给出同一个文件的多个表示（PNG + JPG、带/不带元数据），
    //    WebView2 下尤其常见 —— 按 名字+大小+修改时间 判重就够了。
    const seen = new Set();
    list = list.filter((f) => {
      if (!f) return false;
      const key = [f.name || "", f.size || 0, f.lastModified || 0].join("|");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    list.forEach((f) => {
      if (!f) return;
      if (f.type && f.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = () => {
          const src = String(reader.result || "");
          // ⚠️ 再按**内容**兜一道：同一张图（字节完全相同）已经挂在那儿了就别重复挂。
          //    （跨批次也拦，因为"同一个图挂两遍"几乎总是误操作，用户不会这么干。）
          if (src && images.indexOf(src) >= 0) {
            showToast("这张图已经在附件里了，没有重复添加", "warn");
            return;
          }
          images.push(src);
          renderAttachments();
        };
        reader.readAsDataURL(f);
      } else if (/\.(mp4|avi|mkv|mov|webm|flv|wmv|m4v|ts)$/i.test(f.name || "")) {        // 视频：交给底部按钮处理逻辑保持一致（复用 videoInput）
        const dt = new DataTransfer(); dt.items.add(f);
        $("#videoInput").files = dt.files;
        $("#videoInput").dispatchEvent(new Event("change"));
      } else {
        // 其余一律当"文档"处理，**不再用扩展名白名单**
        // （以前白名单里没有的类型会被静默忽略，表现为"拖了没反应"）。
        // 能不能读由后端判定，读不出会明确告诉你原因。
        addDocAttachment(f);
      }
    });
  }
  // ---------- 拖拽导入：**整个窗口**都是拖放区 ----------
  // 为什么不绑在 #main 上（踩过）：只绑 #main 的话，拖到左侧边栏、顶部空白、
  // 底部提示这些**不在 #main 里的区域**就完全没人接事件，用户看到的
  // 就是"拖了半天一点反应都没有"。现在改成绑 document、并且用**捕获阶段** ——
  // 事件还没轮到别的元素处理就先被我们接住。
  const dropOverlay = $("#dropOverlay");
  let dragDepth = 0;            // dragenter/dragleave 会在子元素间反复触发，用计数收敛

  function dragHasFiles(e) {
    const dt = e.dataTransfer;
    if (!dt) return false;
    if (dt.types && [...dt.types].includes("Files")) return true;
    return !!(dt.items && [...dt.items].some((it) => it.kind === "file"));
  }
  function setDragging(on) {
    document.body.classList.toggle("dragging", !!on);
    if (dropOverlay) dropOverlay.classList.toggle("show", !!on);
  }
  document.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    setDragging(true);
  }, true);
  document.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();                    // 不 preventDefault 就永远等不到 drop
    e.dataTransfer.dropEffect = "copy";
  }, true);
  document.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) setDragging(false);
  }, true);
  document.addEventListener("drop", (e) => {
    dragDepth = 0;
    setDragging(false);
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    const dt = e.dataTransfer;
    let files = (dt && dt.files) ? [...dt.files] : [];
    if (!files.length && dt && dt.items) {
      // 少数环境下 files 是空的，得从 items 里捞（必须在任何 await 之前同步取）
      for (const it of dt.items) {
        if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
      }
    }
    // 拖到「开发台」里 → 当成"导入到项目"，不要塞进聊天附件
    // （在开发台里拖文件，用户的意图显然是"把这个文件加进项目"）
    const tgt = e.target;
    if (tgt && tgt.closest && tgt.closest(".studio") &&
        window.Studio && window.Studio.onDropFiles) {
      window.Studio.onDropFiles(files);
      return;
    }
    onDropFiles(files);
  }, true);

  // 再兜一层：拖到窗口任意位置都不要让浏览器"直接打开"这个文件 ——
  // 否则整个界面会被替换成文件内容，看起来像把应用弄坏了。
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  // ---------- 粘贴导入（万一拖放被宿主窗口吞掉，还有这条路）----------
  // 支持：复制文件后 Ctrl+V、截图后 Ctrl+V、复制图片后 Ctrl+V。
  document.addEventListener("paste", (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const files = [];
    for (const it of items) {
      if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
    }
    const text = (e.clipboardData && e.clipboardData.getData("text")) || "";
    if (files.length) {
      e.preventDefault();
      onDropFiles(files);
    } else if (text && /^file:\/\/\/.+\.\w{1,6}$/i.test(text.trim())) {
      // 从资源管理器"复制文件"拿到的是路径，浏览器读不了 → 如实告知，别装作成功
      e.preventDefault();
      showToast("读到的是文件路径，浏览器打不开。请用输入框左边的 📎 选择文件，或直接把文件拖进来。", "warn");
    }
  });

  // ---------- 图片库 ----------
  // 搜到的图 / 生成的图保存后集中在这里，可随时查看、放大、删除。
  const libGrid = $("#libGrid");
  const libHint = $("#libHint");

  async function loadLibrary() {
    if (!libGrid) return;
    let data;
    try {
      data = await api("/api/library/images");
    } catch (e) {
      return;
    }
    const items = (data && data.images) || [];
    if (libHint) {
      const st = (data && data.stats) || {};
      const mb = ((st.total_bytes || 0) / 1048576).toFixed(1);
      libHint.textContent = items.length
        ? `共 ${items.length} 张 · 占用 ${mb} MB · 点图可放大`
        : "还没有保存的图片。搜图或生成图片后，点「⭐ 保存到图库」即可留存。";
    }
    libGrid.innerHTML = "";
    items.forEach((m) => {
      const cell = document.createElement("div");
      cell.className = "lib-item";
      const tag = m.origin === "web" ? "🌐" : ((m.origin === "gen" || m.origin === "edit") ? "🎨" : "🖼️");
      cell.innerHTML = `
        <img src="/api/library/images/${m.id}/raw" alt="${m.name}" loading="lazy">
        <div class="lib-name" title="${m.name}">${tag} ${m.name}</div>
        <button class="lib-del" title="删除这张图">×</button>`;
      cell.querySelector("img").onclick = async () => {
        try {
          const d = await api("/api/library/images/" + m.id);
          if (d && d.b64) showLightbox("image/png", d.b64, tag + " " + m.name);
        } catch (e) { /* 忽略 */ }
      };
      cell.querySelector(".lib-del").onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`删除图片「${m.name}」？`)) return;
        await api("/api/library/images/" + m.id, { method: "DELETE" });
        loadLibrary();
        showToast("已删除", "ok");
      };
      libGrid.appendChild(cell);
    });
  }

  const libRefreshBtn = $("#libRefresh");
  if (libRefreshBtn) libRefreshBtn.onclick = loadLibrary;

  // 打开图片库所在的文件夹（就是图片实际保存的位置）
  const libOpenFolderBtn = $("#libOpenFolder");
  if (libOpenFolderBtn) {
    libOpenFolderBtn.onclick = () =>
      openFolderRequest("/api/library/open_folder", { method: "POST" });
  }

  // ---------- 知识库 ----------
  // 把 .txt/.md 丢进这个文件夹就会被检索到，**不需要导入操作** ——
  // 所以界面上最重要的一件事就是"让人知道该往哪放"，
  // 因此把目录路径直接显示出来，并提供一键打开。
  async function loadKb() {
    const list = $("#kbList"), pathEl = $("#kbPath"), hint = $("#kbHint");
    if (!list) return;
    try {
      const d = await api("/api/kb");
      if (pathEl) pathEl.textContent = d.dir || "";
      const docs = d.documents || [];
      if (hint) {
        hint.innerHTML = docs.length
          ? `已收录 <b>${docs.length}</b> 篇文档。把新的 .txt / .md 放进文件夹即自动生效。`
          : "还没有资料。把 .txt / .md 文件放进下面的文件夹即可，<b>不需要导入</b>。";
      }
      if (!docs.length) {
        list.innerHTML = '<p class="hint">（空）</p>';
        return;
      }
      list.innerHTML = "";
      // 知识库支持子文件夹（按课程 / 项目分类放）。
      // 有子文件夹就按文件夹分组，像资源管理器；只有根目录文件时照旧平铺 ——
      // 否则明明没分层却多出一行「📁 根目录」，反而更乱。
      const kbRow = (doc) => {
        const row = document.createElement("div");
        row.className = "kb-item" + (doc.indexed ? "" : " bad");
        const ext = (doc.ext || "").toLowerCase();
        const icon = ext === "pdf" ? "📕"
          : ["doc", "docx", "wps"].includes(ext) ? "📘"
          : ["xls", "xlsx", "et", "csv"].includes(ext) ? "📗"
          : ["ppt", "pptx", "dps"].includes(ext) ? "📙" : "📄";
        row.innerHTML = `<span class="kb-ico">${icon}</span>
          <span class="kb-body">
            <span class="kb-name"></span>
            <span class="kb-sub"></span>
          </span>
          <span class="kb-size"></span>
          <button class="btn sm ghost danger kb-del" title="从知识库移除">×</button>`;
        row.querySelector(".kb-name").textContent = doc.filename || doc.id;
        row.querySelector(".kb-sub").textContent = doc.indexed
          ? `${fmtChars(doc.chars)} · 可检索`
          : (doc.note || "读不出文字");      // 读不了要说清原因，不能让人以为没放进去
        row.querySelector(".kb-size").textContent = fmtBytes(doc.size || 0);
        const rel = doc.rel || doc.filename || doc.id;
        row.querySelector(".kb-del").onclick = async () => {
          if (!confirm(`从知识库移除《${rel}》？\n（磁盘上的文件也会一起删掉）`)) return;
          // ⚠️ 用 rel（含文件夹）而不是 filename：子目录里可能有同名文件，
          // 只给文件名后端无法确定删哪一个（会拒绝执行）
          await api("/api/kb/" + encodeURIComponent(rel), { method: "DELETE" });
          loadKb();
        };
        return row;
      };
      if (!docs.some((d) => (d.folder || "").trim())) {
        docs.forEach((doc) => list.appendChild(kbRow(doc)));
      } else {
        // 分组顺序**照后端给的来**（后端会把「第一章」排在「第二章」前面）。
        // 前端自己 sort 的话，中文会按拼音排成 二、九、六、七、三… 看不懂。
        const groups = {};
        const order = [];
        docs.forEach((d) => {
          const k = d.folder || "";
          if (!groups[k]) { groups[k] = []; order.push(k); }
          groups[k].push(d);
        });
        order.forEach((folder) => {
          const head = document.createElement("div");
          head.className = "folder-head";
          head.textContent = "📁 " + (folder || "根目录");
          list.appendChild(head);
          groups[folder].forEach((doc) => list.appendChild(kbRow(doc)));
        });
      }
    } catch (e) { /* 静默 */ }
  }

  (function bindKbPanel() {
    const open = $("#kbOpenFolder");
    if (open) open.onclick = () =>
      openFolderRequest("/api/kb/open_folder", { method: "POST" });
    const rf = $("#kbRefresh");
    if (rf) rf.onclick = loadKb;
  })();

  // ---------- 生成文库（模型产出的文件）----------
  // 刻意和知识库分开：知识库是用户的资料、只读；这里是模型的产出，可改可删可导出。
  let dlCurrent = "";

  async function loadDoclib(highlight) {
    const list = $("#dlList");
    if (!list) return;
    if (highlight) dlCurrent = highlight;
    let d;
    try {
      d = await api("/api/doclib/files");
    } catch (e) {
      list.innerHTML = '<div class="hint">读取失败：' + escapeHtml(String(e.message || e)) + "</div>";
      return;
    }
    const files = d.files || [];
    if (!files.length) {
      list.innerHTML = '<div class="hint">还没有文件。让模型「把这篇作文写成文档」' +
        '或者「给我一个 Python 模块」，它就会存到这里。</div>';
      return;
    }
    const dlRow = (f) => {
      // 注意反斜杠只有一个：`/\\.(docx)$/` 是"反斜杠+任意字符+docx"，
      // 永远匹配不上 .docx（早先过度转义留下的坑，这里修掉）
      const icon = /\.(docx)$/i.test(f.name) ? "📄"
        : /\.(md|markdown)$/i.test(f.name) ? "📝"
        : /\.(py|js|ts|java|c|cpp|go|rs|sh|bat|ps1)$/i.test(f.name) ? "💻"
        : /\.(json|csv|yml|yaml|ini)$/i.test(f.name) ? "🗂" : "📃";
      return `<div class="list-item dl-item${f.rel === dlCurrent ? " active" : ""}" data-rel="${escapeHtml(f.rel)}">` +
        `<span class="dl-icon">${icon}</span>` +
        `<span class="dl-name" title="${escapeHtml(f.rel)}">${escapeHtml(f.name)}</span>` +
        `<span class="dl-meta">${escapeHtml(f.modified)} · ${(f.size / 1024).toFixed(1)}KB</span></div>`;
    };
    // 同知识库：有子文件夹才分组显示
    if (!files.some((f) => (f.folder || "").trim())) {
      list.innerHTML = files.map(dlRow).join("");
    } else {
      // 同知识库：顺序按后端给的来，别在前端重排
      const groups = {};
      const order = [];
      files.forEach((f) => {
        const k = f.folder || "";
        if (!groups[k]) { groups[k] = []; order.push(k); }
        groups[k].push(f);
      });
      list.innerHTML = order
        .map((folder) =>
          `<div class="folder-head">📁 ${escapeHtml(folder || "根目录")}</div>` +
          groups[folder].map(dlRow).join(""))
        .join("");
    }
    list.querySelectorAll(".dl-item").forEach((el) => {
      el.onclick = () => openDoclibFile(el.dataset.rel);
    });
  }

  async function openDoclibFile(rel) {
    dlCurrent = rel;
    let d;
    try {
      d = await api("/api/doclib/file?rel=" + encodeURIComponent(rel));
    } catch (e) {
      // docx 这类二进制文件读不出文本是正常的，给一条能走的出路
      showToast("这个文件读不出正文（可能是 Word/表格这类）。可以直接下载或导出。", "warn");
      return;
    }
    $("#dlEditor").hidden = false;
    $("#dlEditorName").textContent = d.rel + "（" + (d.chars || 0) + " 字）";
    $("#dlText").value = d.text || "";
    document.querySelectorAll(".dl-item").forEach((el) =>
      el.classList.toggle("active", el.dataset.rel === rel));
  }

  (function bindDoclibPanel() {
    const wrap = $("#doclibPanel");
    if (!wrap) return;
    const open = $("#dlOpenFolder");
    if (open) open.onclick = () =>
      openFolderRequest("/api/doclib/open_folder", { method: "POST" });
    const rf = $("#dlRefresh");
    if (rf) rf.onclick = () => loadDoclib();
    const bk = $("#dlBackup");
    if (bk) bk.onclick = async () => {
      try {
        const r = await api("/api/doclib/backup", { method: "POST" });
        showToast(`已备份 ${r.count} 个文件到 ${r.path}`, "ok");
      } catch (e) { showToast("备份失败：" + String(e.message || e), "warn"); }
    };
    const close = $("#dlCloseBtn");
    if (close) close.onclick = () => { $("#dlEditor").hidden = true; dlCurrent = ""; };
    const save = $("#dlSaveBtn");
    if (save) save.onclick = async () => {
      if (!dlCurrent) return;
      try {
        await api("/api/doclib/file", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, text: $("#dlText").value }) });
        showToast("已保存（原内容自动备份）", "ok");
        loadDoclib();
      } catch (e) { showToast("保存失败：" + String(e.message || e), "warn"); }
    };
    const docx = $("#dlDocxBtn");
    if (docx) docx.onclick = async () => {
      if (!dlCurrent) return;
      docx.disabled = true;
      try {
        // 先把当前编辑框的内容存下来，再导出 —— 否则导出的还是旧版本
        await api("/api/doclib/file", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, text: $("#dlText").value }) });
        const r = await api("/api/doclib/export_docx", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent }) });
        showToast("已导出：" + r.rel + "（WPS / Word 都能打开）", "ok");
        // ⚠️ 这里**不能**传 r.rel：loadDoclib 会把 dlCurrent 指到那个新 .docx 上，
        // 下次点「保存」就会把文本正文写进 .docx 文件、把刚导出的 Word 冲掉。
        // 保持 dlCurrent 仍指向原文本文件，新文件靠列表按时间倒序自然置顶。
        loadDoclib();
      } catch (e) { showToast("导出失败：" + String(e.message || e), "warn"); }
      finally { docx.disabled = false; }
    };
    const dl = $("#dlDownload");
    if (dl) dl.onclick = async () => {
      if (!dlCurrent) return;
      const url = "/api/doclib/download?rel=" + encodeURIComponent(dlCurrent);
      // 桌面壳里 window.open 会被丢给外部浏览器（OpenExternalLinksInBrowser），
      // 先试原生另存为；没有桥再退回原来的行为。
      if (await window.__saveViaDesktop(url)) return;
      window.open(url, "_blank");
    };
    const cp = $("#dlCopyBtn");
    if (cp) cp.onclick = async () => {
      if (!dlCurrent) return;
      // 预填「原名-副本」：想改名就改，直接回车也能用
      const dot = dlCurrent.lastIndexOf(".");
      const guess = dot > 0
        ? dlCurrent.slice(0, dot) + "-副本" + dlCurrent.slice(dot)
        : dlCurrent + "-副本";
      const name = await showInputDialog({
        title: "复制为",
        tip: "会在原位置生成一份新文件，原文件不动。名字可以改，也可带子目录（如 草稿/第二版.md）。",
        value: guess,
        okText: "复制",
        placeholder: "新文件名",
      });
      if (!name || name === dlCurrent) return;
      cp.disabled = true;
      try {
        const r = await api("/api/doclib/copy", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, new_rel: name }) });
        showToast("已复制为：" + r.to, "ok");
        await loadDoclib();
        // 文本类直接打开新文件（复制完通常就是要接着改它）；
        // Word/表格这类读不出正文，只刷新列表，免得弹一句"读不出正文"扫兴。
        if (/\.(md|markdown|txt|json|csv|log|ini|cfg|ya?ml|py|js|ts|html?|css|sql|sh|bat)$/i.test(r.to)) {
          await openDoclibFile(r.to);
        }
      } catch (e) { showToast("复制失败：" + String(e.message || e), "warn"); }
      finally { cp.disabled = false; }
    };
    const del = $("#dlDelBtn");
    if (del) del.onclick = async () => {
      if (!dlCurrent) return;
      if (!confirm("删除《" + dlCurrent + "》？\n（会移进回收站，需要时能捞回来）")) return;
      try {
        await api("/api/doclib/delete", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent }) });
        showToast("已删除（在回收站里）", "ok");
        $("#dlEditor").hidden = true;
        dlCurrent = "";
        loadDoclib();
      } catch (e) { showToast("删除失败：" + String(e.message || e), "warn"); }
    };
  })();


  // ---------- 多会话管理 ----------
  // 每个会话互相独立（各自的消息与上下文），全部持久化在后端，
  // 程序重启后自动恢复上次使用的会话。
  const sessionsListEl = $("#sessionsList");

  // 界面渲染 + 送给后端的上下文条数。
  // ⚠️ 原来是 20（=10 轮），实测太短：聊到十几轮以后模型就开始"忘了前面说的话"。
  // 真正兜底的是后端的 token 预算（_trim_history_to_budget），
  // 它装不下会自动裁并压成摘要 —— 所以这里可以放心放宽。
  const RECENT_SHOW = 40;

  /** 把历史消息渲染到界面，并同步为上下文。返回实际渲染条数。 */
  function renderHistory(msgs) {
    messagesEl.innerHTML = "";
    history.length = 0;
    const shown = (msgs || []).slice(-RECENT_SHOW);
    const hidden = (msgs || []).length - shown.length;
    if (hidden > 0) {
      const tip = document.createElement("div");
      tip.className = "msg bot";
      tip.innerHTML = `<div class="bubble fold-tip">📁 更早的 ${hidden} 条记录已折叠（要点已存入长期记忆）</div>`;
      messagesEl.appendChild(tip);
    }
    shown.forEach((m) => {
      if ((m.role === "user" || m.role === "assistant") && m.content) {
        addMsg(m.role, m.content);
        history.push({ role: m.role, content: m.content });
      }
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return shown.length;
  }

  async function loadSessions() {
    let data;
    try {
      data = await api("/api/sessions");
    } catch (e) {
      return [];
    }
    const list = (data && data.sessions) || [];
    // ⚠️⚠️ 这里会**悄悄换掉当前会话**：localStorage 里没有 id、或那个会话已经不在
    // 列表里时，自动切到最新的一条。它和文件末尾的 loadMemory() 是**并发**跑的：
    //   ① loadMemory 先用「旧的（可能是空）sessionId」把请求发了出去
    //   ② loadSessions 回来，把 sessionId 改成另一条
    //   ③ 那个响应回来时 `d.session_id !== sessionId` → 被判成"用户已切走"**整条丢弃**：
    //      不填任何字段、不报错、也不会重试
    // → 界面就**一直停在灰色占位符**上，看着像"记忆一个字都没记住"。
    // 实测复现过：DOM 里 #memLongMeta / #memShortMeta 全空、#memSessName 还是初始的「—」。
    // 所以：只要真的换了会话，这里必须**补刷一次 loadMemory()**。
    if (!sessionId || !list.some((s) => s.id === sessionId)) {
      const next = list.length ? list[0].id : "";
      const changed = next !== sessionId;
      setSessionId(next);
      if (changed) loadMemory();
    }
    renderSessions(list);
    return list;
  }

  function renderSessions(list) {
    if (!sessionsListEl) return;
    sessionsListEl.innerHTML = "";
    if (!list.length) {
      sessionsListEl.innerHTML = '<p class="hint">暂无对话，点「＋ 新建」开始。</p>';
      return;
    }
    list.forEach((s) => {
      const row = document.createElement("div");
      row.className = "session-item" + (s.id === sessionId ? " active" : "");
      const title = document.createElement("span");
      title.className = "session-title";
      title.textContent = s.title || "新对话";
      title.title = `${s.title || "新对话"}（${s.count || 0} 条）`;
      const del = document.createElement("button");
      del.className = "session-del";
      del.textContent = "×";
      del.title = "删除这个对话";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除对话「${s.title || "新对话"}」？`)) return;
        await api("/api/sessions/" + s.id, { method: "DELETE" });
        const rest = await loadSessions();
        if (s.id === sessionId && rest.length) await switchSession(rest[0].id);
      };
      row.appendChild(title);
      row.appendChild(del);
      row.onclick = () => switchSession(s.id);
      sessionsListEl.appendChild(row);
    });
  }

  async function switchSession(sid) {
    if (!sid || sid === sessionId) return;
    if (streaming) { showToast("正在回答中，请稍候再切换", "warn"); return; }
    setSessionId(sid);
    let msgs = [];
    try {
      const d = await api("/api/sessions/" + sid);
      msgs = d.messages || [];
    } catch (e) { /* 读取失败则当作空会话 */ }
    if (renderHistory(msgs) === 0) {
      addMsg("bot", "这是一段新对话，直接说需求即可。");
    }
    await loadSessions();
    // 记忆按对话隔离：换了对话，记忆面板也必须跟着换
    await loadMemory();
    showToast(`已切换到「${await currentTitle(sid)}」`);
  }

  async function currentTitle(sid) {
    try {
      const d = await api("/api/sessions");
      const hit = (d.sessions || []).find((s) => s.id === sid);
      return (hit && hit.title) || "新对话";
    } catch (e) { return "新对话"; }
  }

  async function newSession() {
    if (streaming) { showToast("正在回答中，请稍候再新建", "warn"); return; }
    try {
      const d = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
      const sid = d.session && d.session.id;
      if (!sid) return;
      setSessionId(sid);
      history.length = 0;
      messagesEl.innerHTML = "";
      addMsg("bot", "新对话已开始，这段对话与之前互不影响。");
      await loadSessions();
      // 新对话有自己独立的记忆（从空白开始），面板要跟着切
      await loadMemory();
      showToast("已新建对话", "ok");
    } catch (e) {
      showToast("新建失败：" + e.message, "warn");
    }
  }

  async function persistSession() {
    if (!sessionId) return;
    try {
      const d = await api("/api/sessions/" + sessionId, {
        method: "PUT",
        body: JSON.stringify({ messages: history }),
      });
      if (d && d.session) renderSessions(await loadSessions_noSwitch());
    } catch (e) { /* 保存失败不打断对话 */ }
  }

  async function loadSessions_noSwitch() {
    try {
      const d = await api("/api/sessions");
      return (d && d.sessions) || [];
    } catch (e) { return []; }
  }

  const newSessionBtn = $("#newSessionBtn");
  if (newSessionBtn) newSessionBtn.onclick = newSession;

  // ---------- 记忆库 · 文段式 ----------
  // ---------- 记忆库：长期（跨对话共享）+ 短期（按对话独立）----------
  // 切对话时只有"短期记忆"跟着变；长期记忆是同一份，永远不变。
  // 长期记忆读取失败了几次（用于"自动重试 + 显示原因"）
  let _memLoadFails = 0;

  async function loadMemory() {
    const lng = $("#memLongText"), sht = $("#memShortText");
    if (!lng || !sht) return;
    try {
      const d = await api("/api/memory?session_id=" + encodeURIComponent(sessionId || ""));
      if ((d.session_id || "") !== (sessionId || "")) return;   // 用户已切走，别覆盖
      lng.value = d.long || "";
      sht.value = d.short || "";
      _memLoadFails = 0;
      const lm = $("#memLongMeta");
      if (lm) lm.textContent = `${(d.long || "").length} / ${d.long_cap || 2000} 字`;
      const sm = $("#memShortMeta");
      if (sm) sm.textContent = (d.short || "").length
        ? `${(d.short || "").length} / ${d.short_cap || 1000} 字`
        : "这个对话还没有短期记忆";
      const nm = $("#memSessName");
      if (nm) nm.textContent = await currentTitle(sessionId);
    } catch (e) {
      // ⚠️ **这里绝不能静默**。原来是一句 `catch (e) { /* 静默 */ }`，后果很坑：
      // 后端还没起来 / 正在重启时加载失败，输入框就只剩**灰色占位符**，
      // 用户看到的是"记忆是空的、字体发暗"，以为模型压根没记住 ——
      // 实测被这么误判过（我重启动应用时抓到的，截图里那个就是）。
      // 现在：① 在字数行上写明失败原因；② 自动退避重试几次。
      _memLoadFails += 1;
      const lm = $("#memLongMeta");
      if (lm) lm.textContent = `⚠️ 记忆读取失败（第 ${_memLoadFails} 次）：${e.message || e}`;
      if (_memLoadFails <= 4) setTimeout(loadMemory, 1500 * _memLoadFails);
    }
  }

  // ⚠️ 别在对话一结束就立刻 loadMemory()。
  // 后端提炼记忆是**异步 + 防抖**的：停手 6 秒才启动，
  // 而提炼本身要跑一次完整推理（思考型模型很慢）——
  // **实测在 12GB 显卡上从"回答结束"到"记忆落盘"要 ~155 秒**（2026-09-15 实测）。
  // 答完马上刷新只会读到"还没提炼"的旧内容，用户看到面板没变，
  // 会误判成"模型没记住"（这正是用户反馈的问题）。
  // 所以按实测节奏补刷几次，一直覆盖到 3 分钟。
  let _memTimers = [];
  function scheduleMemoryRefresh() {
    _memTimers.forEach(clearTimeout);
    _memTimers = [10000, 30000, 60000, 120000, 180000].map((ms) => setTimeout(() => {
      loadMemory();
      loadMemoryUsage();
    }, ms));
  }

  function bindMemoryPanel() {
    const ls = $("#memLongSave");
    if (ls) ls.onclick = async () => {
      const r = await api("/api/memory", {
        method: "POST",
        body: JSON.stringify({ scope: "long", content: $("#memLongText").value }),
      });
      showToast(r.ok ? "已保存长期记忆（所有对话通用）" : "保存失败", r.ok ? "ok" : "warn");
      loadMemory();
    };
    const ss = $("#memShortSave");
    if (ss) ss.onclick = async () => {
      if (!sessionId) { showToast("还没有对话", "warn"); return; }
      const r = await api("/api/memory", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, content: $("#memShortText").value }),
      });
      showToast(r.ok ? "已保存本次对话的短期记忆" : "保存失败", r.ok ? "ok" : "warn");
      loadMemory();
    };
    const sc = $("#memShortClear");
    if (sc) sc.onclick = async () => {
      if (!sessionId) return;
      if (!confirm("清空**这个对话**的短期记忆？\n\n其他对话的短期记忆、以及长期记忆都不受影响。")) return;
      await api("/api/memory?scope=session&session_id=" + encodeURIComponent(sessionId),
                { method: "DELETE" });
      showToast("已清空本对话短期记忆");
      loadMemory();
    };
  }
  bindMemoryPanel();

  // ---------- 内存占用：看清占在哪 + 一键释放 ----------
  // 设计原则：**释放只动对话记录与归档，长期记忆一律保留**。
  // 用户最怕的是"清理的时候把有用的记忆也清了"，所以界面上要反复讲清楚这一点。
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function fmtTokens(n) {
    n = Number(n || 0);
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }

  async function loadMemoryUsage() {
    const list = $("#muList"), totals = $("#muTotals"), total = $("#muTotal");
    if (!list) return;
    try {
      const d = await api("/api/memory/usage");
      if (!d.ok) return;
      const t = d.totals || {};
      total.textContent = fmtBytes(t.disk_bytes);
      totals.innerHTML =
        `<div class="mu-row"><span>对话记录</span><b>${fmtBytes(t.sessions_bytes)}</b>`
        + `<em>${t.session_count} 个对话 · ${t.message_count} 条消息</em></div>`
        + `<div class="mu-row"><span>归档底稿</span><b>${fmtBytes(t.transcript_bytes)}</b>`
        + `<em>${t.transcript_files} 个文件（释放前会先沉淀要点）</em></div>`
        + `<div class="mu-row keep"><span>🧩 长期记忆</span><b>${t.long_chars || 0} 字</b>`
        + `<em>所有对话共享 · 始终保留</em></div>`
        + `<div class="mu-row"><span>💬 短期记忆</span><b>${t.short_chars || 0} 字</b>`
        + `<em>${t.short_used || 0} 个对话有短期记忆 · 可释放</em></div>`
        + (t.rss_bytes
            ? `<div class="mu-row"><span>运行内存</span><b>${fmtBytes(t.rss_bytes)}</b><em>应用进程常驻内存</em></div>`
            : "");

      const items = d.sessions || [];
      if (!items.length) {
        list.innerHTML = '<p class="hint">暂无对话记录。</p>';
      } else {
        list.innerHTML = "";
        items.slice(0, 12).forEach((s) => {
          const row = document.createElement("div");
          row.className = "mu-sess";
          const full = s.messages >= (t.context_messages || 80);
          row.innerHTML = `<div class="mu-sess-head">
              <span class="mu-sess-title"></span>
              <span class="mu-sess-size">${fmtBytes(s.bytes)}</span>
            </div>
            <div class="mu-sess-meta">${s.messages} 条 · 约 ${fmtTokens(s.tokens)} token`
            + `${s.memory_chars ? ` · 记忆 ${s.memory_chars} 字` : ' · 无记忆'}`
            + `${full ? ' · <span class="mu-warn">已达上下文上限，早期内容靠摘要保留</span>' : ''}</div>
            <button class="btn sm ghost mu-free">释放此对话历史</button>`;
          row.querySelector(".mu-sess-title").textContent = s.title || s.id;
          row.querySelector(".mu-free").onclick = async (ev) => {
            ev.stopPropagation();
            if (!confirm(`释放「${s.title || s.id}」的对话记录？\n\n`
                       + "· 释放前会先把这段对话里值得记住的要点提炼进记忆\n"
                       + "· 记忆本身不受影响")) return;
            const r = await api("/api/memory/release", {
              method: "POST",
              body: JSON.stringify({ scope: "session", session_id: s.id, sweep: true }),
            });
            const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
            showToast(r.ok ? `已释放 ${fmtBytes(r.freed_bytes)}${extra}` : "释放失败",
                      r.ok ? "ok" : "warn");
            loadMemoryUsage();
            loadMemory();
          };
          list.appendChild(row);
        });
      }
    } catch (e) { /* 忽略：面板不可用不影响其它功能 */ }
  }

  (function bindMemUsage() {
    const head = $("#muToggle"), body = $("#muBody");
    if (!head || !body) return;
    head.onclick = () => {
      body.hidden = !body.hidden;
      head.querySelector(".mu-caret").textContent = body.hidden ? "▸" : "▾";
      head.classList.toggle("open", !body.hidden);
      if (!body.hidden) loadMemoryUsage();
    };
    const rf = $("#muRefresh");
    if (rf) rf.onclick = (e) => { e.stopPropagation(); loadMemoryUsage(); };
    // 释放短期记忆：它是"缓存"性质的，可以先清；
    // 清之前后端会先把归档里的要点沉淀一遍，避免把结论一起清掉。
    const fs = $("#muFreeShort");
    if (fs) fs.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("释放所有对话的**短期记忆**？\n\n"
                 + "· 会先做：把归档里还没记下的要点提炼进记忆\n"
                 + "· 会清掉：各对话的短期记忆（正在做的事、本次结论）\n"
                 + "· 不会动：长期记忆（身份/偏好/约定）")) return;
      fs.disabled = true;
      const old = fs.textContent;
      fs.textContent = "沉淀并释放中…";
      try {
        const r = await api("/api/memory/release",
                            { method: "POST",
                              body: JSON.stringify({ scope: "short", sweep: true }) });
        if (r.ok) {
          const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
          showToast(`已释放短期记忆 ${r.short_freed_chars || 0} 字${extra}；长期记忆已保留`, "ok");
        } else {
          showToast(r.reason || "释放失败", "warn");
        }
        loadMemoryUsage();
        loadMemory();
      } finally {
        fs.disabled = false;
        fs.textContent = old;
      }
    };
    const rl = $("#muRelease");
    if (rl) rl.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("释放缓存占用？\n\n"
                 + "· 会先做：把归档里还没记下的重要要点提炼进记忆\n"
                 + "· 再清理：归档底稿、几乎没用过的空会话、过长对话的早期记录\n"
                 + "· 不会动：各对话的记忆、全局偏好")) return;
      rl.disabled = true;
      const old = rl.textContent;
      rl.textContent = "沉淀并释放中…";
      try {
        const r = await api("/api/memory/release",
                            { method: "POST",
                              body: JSON.stringify({ scope: "auto", sweep: true }) });
        if (r.ok) {
          const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
          showToast(`已释放 ${fmtBytes(r.freed_bytes)}${extra}；记忆已保留`, "ok");
        } else {
          showToast(r.reason || "释放失败", "warn");
        }
        loadMemoryUsage();
        loadMemory();
      } finally {
        rl.disabled = false;
        rl.textContent = old;
      }
    };
  })();

  // ---------- 附件：图片 / 文档 / 视频 ----------
  $("#attachBtn").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
    onDropFiles(e.target.files);
    e.target.value = "";
  };

  // 文档走独立入口：**不设 accept 白名单**（设了就只能在对话框里选到那几种）。
  // 这条路的必要性：拖放依赖宿主窗口把事件交给网页，万一被吞掉，
  // 用户就彻底没办法导入文档了 —— 有这个按钮至少永远有条走得通的路。
  $("#docBtn").onclick = () => $("#docInput").click();
  $("#docInput").onchange = (e) => {
    onDropFiles(e.target.files);
    e.target.value = "";
  };

  $("#videoBtn").onclick = () => $("#videoInput").click();
  $("#videoInput").onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const tip = showThinking("正在读取视频并抽取关键帧…");
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const videoEl = document.createElement("video");
        videoEl.src = URL.createObjectURL(f);
        videoEl.crossOrigin = "anonymous";
        videoEl.muted = true;
        await new Promise((res) => { videoEl.onloadeddata = res; videoEl.load(); });
        const frames = [];
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 150));
          videoEl.currentTime = (videoEl.duration * i) / 6;
          await new Promise((res) => { videoEl.onseeked = res; });
          const c = document.createElement("canvas");
          c.width = videoEl.videoWidth; c.height = videoEl.videoHeight;
          const scale = Math.min(768 / Math.max(c.width, c.height), 1);
          c.width = Math.round(c.width * scale); c.height = Math.round(c.height * scale);
          c.getContext("2d").drawImage(videoEl, 0, 0, c.width, c.height);
          frames.push(c.toDataURL("image/jpeg", 0.8));
        }
        videoB64 = frames.map((s) => s.split(",")[1]);
        tip.querySelector(".bubble").textContent = `✔ 已抽取 ${frames.length} 帧视频画面，可发送给 AI 分析。`;
      } catch (err) {
        tip.querySelector(".bubble").textContent = "❌ 视频抽帧失败: " + err.message;
      }
      e.target.value = "";
    };
    reader.readAsArrayBuffer(f);
  };

  function renderAttachments() {
    const box = $("#attachments");
    box.innerHTML = "";
    images.forEach((src, i) => {
      const d = document.createElement("div");
      d.className = "attachment";
      d.innerHTML = `<img src="${src}"><span class="x">✕</span>`;
      d.querySelector(".x").onclick = () => { images.splice(i, 1); renderAttachments(); };
      box.appendChild(d);
    });
    // 文档附件：显示成一张小卡片（文件名 + 字数）
    docs.forEach((doc, i) => {
      const d = document.createElement("div");
      d.className = "attachment doc" + (doc.loading ? " loading" : "");
      const icon = /\.pdf$/i.test(doc.name) ? "📕"
                 : /\.(docx?|wps)$/i.test(doc.name) ? "📘"
                 : /\.(xlsx?|et)$/i.test(doc.name) ? "📗"
                 : /\.(pptx?|dps)$/i.test(doc.name) ? "📙" : "📄";
      d.innerHTML = `<span class="doc-ico">${icon}</span>
        <span class="doc-meta"><b></b><em></em></span><span class="x">✕</span>`;
      d.querySelector("b").textContent = doc.name;
      d.querySelector("em").textContent = doc.loading
        ? "解析中…" : fmtChars(doc.chars);
      d.querySelector(".x").onclick = () => { docs.splice(i, 1); renderAttachments(); };
      box.appendChild(d);
    });
    if (videoB64) {
      const d = document.createElement("div"); d.className = "attachment vid";
      d.innerHTML = `<span style="font-size:22px">🎬</span><span class="x">✕</span>`;
      d.querySelector(".x").onclick = () => { videoB64 = null; renderAttachments(); };
      box.appendChild(d);
    }
  }

  // ---------- 发送 / 流式接收 ----------
  // 检索来源列表：回答下方的可伸缩面板（默认折叠）
  // 为什么放在正文之外：链接列表塞进回答气泡会把答案本身淹掉，
  // 而且模型每次重写都可能漏抄或改动链接；由后端直接给出真实来源更可靠。
  function renderSources(afterEl, data) {
    const items = data.items || [];
    const box = document.createElement("div");
    box.className = "src-box";

    const hdr = document.createElement("button");
    hdr.className = "src-hdr";
    hdr.innerHTML = `<span class="src-caret">▸</span>
      <span class="src-title">📚 检索来源（${items.length}）</span>
      <span class="src-hint">${data.query ? escapeHtml(data.query).slice(0, 40) : ""}</span>`;

    const list = document.createElement("div");
    list.className = "src-list";
    list.hidden = true;

    items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "src-item";
      const site = it.site ? `<span class="src-site">${escapeHtml(it.site)}</span>` : "";
      const read = it.read ? `<span class="src-read" title="已抓取该页正文用于分析">已精读</span>` : "";
      row.innerHTML = `<span class="src-idx">[${it.i}]</span>
        <a class="src-link" href="${escapeHtml(it.url)}" target="_blank" rel="noopener"></a>
        ${site}${read}`;
      const a = row.querySelector(".src-link");
      a.textContent = it.title || it.url;
      a.title = it.url;
      list.appendChild(row);
    });

    const toggle = () => {
      list.hidden = !list.hidden;
      hdr.classList.toggle("open", !list.hidden);
      hdr.querySelector(".src-caret").textContent = list.hidden ? "▸" : "▾";
    };
    hdr.onclick = toggle;

    box.appendChild(hdr);
    box.appendChild(list);
    // 插到回答气泡之后
    if (afterEl && afterEl.parentNode) afterEl.parentNode.insertBefore(box, afterEl.nextSibling);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showLightbox(mime, b64, prompt) {
    const ov = document.createElement("div");
    ov.className = "lightbox";
    ov.innerHTML = `<div class="lb-box">
      <img src="data:${mime || "image/png"};base64,${b64}">
      <div class="lb-cap"></div>
      <div class="lb-tools">
        <button class="lb-save">💾 保存到本地</button>
        <button class="lb-open">📂 打开所在文件夹</button>
      </div>
      <button class="lb-close" title="关闭">✕</button>
    </div>`;
    ov.querySelector(".lb-cap").textContent = prompt || "";
    ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
    ov.querySelector(".lb-close").onclick = () => ov.remove();

    const cap = ov.querySelector(".lb-cap");
    let lastPath = null;
    ov.querySelector(".lb-save").onclick = async (e) => {
      e.stopPropagation();
      const fn = "generated_" + Date.now() + (mime === "image/jpeg" ? ".jpg" : ".png");
      const body = { b64: b64, filename: fn, mime: mime || "image/png" };
      try {
        const r = await fetch("/api/save_image", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({ ok: false, detail: `服务端返回 ${r.status}（非 JSON）` }));
        if (d.ok) { lastPath = d.path; cap.textContent = "✅ 已保存：" + d.path; }
        else cap.textContent = "❌ 保存失败：" + (d.detail || "");
      } catch (err) {
        cap.textContent = "❌ 保存失败：" + err;
      }
    };
    ov.querySelector(".lb-open").onclick = (e) => {
      e.stopPropagation();
      openFolderRequest("/api/open_folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: lastPath }),
      }, cap);
    };
    document.body.appendChild(ov);
  }

  async function send() {
    const text = inputEl.value.trim();
    const imgB64 = images.map((s) => s.split(",")[1]);
    if ((!text && imgB64.length === 0 && !videoB64) || streaming) return;
    const promptText = text || (videoB64 ? "分析这段视频的内容" : "请详细描述这张图片的内容");
    addMsg("user", promptText);
    if (videoB64) {
      const el = document.createElement("div"); el.className = "msg user";
      el.innerHTML = `<div class="bubble">🎬 已附加视频关键帧 (${videoB64.length} 帧)</div>`;
      messagesEl.appendChild(el);
    } else {
      images.forEach((src) => {
        const el = document.createElement("div"); el.className = "msg user";
        el.innerHTML = `<div class="bubble" style="padding:6px"><img src="${src}" style="max-width:220px;border-radius:8px"></div>`;
        messagesEl.appendChild(el);
      });
    }
    const media = videoB64 ? [...videoB64] : imgB64;
    // 文档附件：把抽取出的正文带上（解析中的/失败的不要发）
    const docPayload = docs.filter((d) => d.text && d.text.trim())
                           .map((d) => ({ name: d.name, text: d.text }));
    if (docPayload.length) {
      const el = document.createElement("div");
      el.className = "msg user";
      el.innerHTML = `<div class="bubble doc-sent"></div>`;
      el.querySelector(".doc-sent").textContent =
        "📎 " + docPayload.map((d) => d.name).join("、");
      messagesEl.appendChild(el);
    }
    images = []; videoB64 = null; docs = []; renderAttachments();
    inputEl.value = ""; autoGrow();
    await doSend(promptText, media.length ? media : null, docPayload);
    scheduleMemoryRefresh();   // 记忆提炼是后端异步防抖的，延迟补刷面板
  }

  // ---------- 涂抹选区编辑器 ----------
  const maskChip = $("#maskChip");
  const refreshMaskChip = () => { if (maskChip) maskChip.hidden = !selMask; };
  const clearSelMask = () => { selMask = null; selMaskOn = null; refreshMaskChip(); };

  const openMaskEditor = (imgDataUrl) => {
    const layer = document.createElement("div");
    layer.className = "mask-editor";
    layer.innerHTML =
      '<div class="me-box">' +
        '<div class="me-head">✂️ 涂抹要改的地方' +
          '<span class="me-tip">被涂到的区域才会重画，其余部分原样保留</span></div>' +
        '<div class="me-stage"><div class="me-wrap">' +
          '<img src="' + imgDataUrl + '" alt="">' +
          '<canvas></canvas>' +
        '</div></div>' +
        '<div class="me-bar">' +
          '<div class="seg">' +
            '<button type="button" data-mode="brush" class="on">🖌 涂抹</button>' +
            '<button type="button" data-mode="rect">▭ 矩形</button>' +
            '<button type="button" data-mode="erase">🧽 擦除</button>' +
          '</div>' +
          '<label>粗细 <input type="range" min="8" max="140" value="40"></label>' +
          '<span class="me-count">已涂 0%</span>' +
          '<span class="sp"></span>' +
          '<button type="button" class="btn ghost" data-act="clear">清空</button>' +
          '<button type="button" class="btn ghost" data-act="cancel">取消</button>' +
          '<button type="button" class="btn primary" data-act="ok">就用这块区域</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(layer);

    const img = layer.querySelector("img");
    const cv = layer.querySelector("canvas");
    const ctx = cv.getContext("2d");
    const countEl = layer.querySelector(".me-count");
    let mode = "brush", size = 40, ops = [], drawing = false, cur = null;

    // 记录"画了哪些笔"（而不是只存像素）：这样擦除、清空、导出蒙版都好做
    const init = () => {
      const w = img.naturalWidth || 1024, h = img.naturalHeight || 1024;
      cv.width = w; cv.height = h;
      paint();
    };
    const toCv = (ev) => {
      const r = cv.getBoundingClientRect();
      return [ (ev.clientX - r.left) * (cv.width / (r.width || 1)),
               (ev.clientY - r.top) * (cv.height / (r.height || 1)) ];
    };
    const paint = () => {
      const w = cv.width, h = cv.height;
      ctx.clearRect(0, 0, w, h);
      const base = Math.max(2, size * (w / (cv.getBoundingClientRect().width || w)));
      for (const op of ops) {
        const isErase = op.kind === "erase";
        ctx.globalCompositeOperation = "source-over";
        ctx.strokeStyle = ctx.fillStyle = isErase ? "rgba(0,0,0,.75)" : "rgba(255,64,64,.55)";
        ctx.lineWidth = Math.max(2, op.size || base);
        ctx.lineCap = "round"; ctx.lineJoin = "round";
        if (op.kind === "rect") {
          ctx.beginPath();
          ctx.rect(Math.min(op.x0, op.x1), Math.min(op.y0, op.y1),
                   Math.abs(op.x1 - op.x0), Math.abs(op.y1 - op.y0));
          ctx.fill();
        } else {
          ctx.beginPath();
          op.pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
          if (op.pts.length === 1) ctx.lineTo(op.pts[0][0] + .1, op.pts[0][1] + .1);
          ctx.stroke();
        }
      }
    };
    // 导出真正的蒙版：黑底 + 白笔（擦除画黑）
    const buildMask = () => {
      const w = cv.width, h = cv.height;
      const out = document.createElement("canvas");
      out.width = w; out.height = h;
      const o = out.getContext("2d");
      o.fillStyle = "#000"; o.fillRect(0, 0, w, h);
      const base = Math.max(2, size * (w / (cv.getBoundingClientRect().width || w)));
      for (const op of ops) {
        o.strokeStyle = o.fillStyle = op.kind === "erase" ? "#000" : "#fff";
        o.lineWidth = Math.max(2, op.size || base);
        o.lineCap = "round"; o.lineJoin = "round";
        if (op.kind === "rect") {
          o.beginPath();
          o.rect(Math.min(op.x0, op.x1), Math.min(op.y0, op.y1),
                 Math.abs(op.x1 - op.x0), Math.abs(op.y1 - op.y0));
          o.fill();
        } else {
          o.beginPath();
          op.pts.forEach((p, i) => (i ? o.lineTo(p[0], p[1]) : o.moveTo(p[0], p[1])));
          if (op.pts.length === 1) o.lineTo(op.pts[0][0] + .1, op.pts[0][1] + .1);
          o.stroke();
        }
      }
      // 顺带算一下覆盖率，让用户知道"涂够没涂够"
      let on = 0;
      const px = o.getImageData(0, 0, w, h).data;
      for (let i = 0; i < px.length; i += 4 * 7) { if (px[i] > 127) on++; }
      return { url: out.toDataURL("image/png"),
               ratio: on / Math.max(1, (px.length / (4 * 7))) };
    };

    const updateCount = () => {
      const m = buildMask();
      countEl.textContent = "已涂 " + Math.round(m.ratio * 100) + "%";
      if (m.ratio < 0.002 && ops.length) countEl.textContent += "（太少了，再涂一点）";
    };
    const stopDrag = () => {
      if (!drawing) return;
      drawing = false;
      if (cur && cur.kind === "brush" && cur.pts.length === 1) { ops.push(cur); }
      cur = null; paint(); updateCount();
    };
    cv.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      cv.setPointerCapture && cv.setPointerCapture(ev.pointerId);
      const [x, y] = toCv(ev);
      drawing = true;
      if (mode === "rect") cur = { kind: ops.length && ev.shiftKey ? "erase" : "rect", x0: x, y0: y, x1: x, y1: y, size: 4 };
      else cur = { kind: mode === "erase" ? "erase" : "brush", size: size * (cv.width / (cv.getBoundingClientRect().width || cv.width)), pts: [[x, y]] };
      paint();
    });
    cv.addEventListener("pointermove", (ev) => {
      if (!drawing || !cur) return;
      const [x, y] = toCv(ev);
      if (cur.kind === "rect") { cur.x1 = x; cur.y1 = y; }
      else cur.pts.push([x, y]);
      paint();
    });
    cv.addEventListener("pointerup", (ev) => {
      if (cur && cur.kind === "brush") ops.push(cur);
      else if (cur && cur.kind === "erase" && cur.pts) ops.push(cur);
      if (cur && cur.kind === "rect") ops.push(cur);
      cur = null; drawing = false; paint(); updateCount();
    });
    cv.addEventListener("pointerleave", stopDrag);

    layer.querySelectorAll(".seg button").forEach((b) => {
      b.onclick = () => {
        mode = b.dataset.mode;
        layer.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
      };
    });
    layer.querySelector('input[type=range]').oninput = (e) => { size = +e.target.value; };
    const close = () => { layer.remove(); };
    layer.querySelector('[data-act="cancel"]').onclick = close;
    layer.querySelector('[data-act="clear"]').onclick = () => { ops = []; paint(); updateCount(); };
    layer.querySelector('[data-act="ok"]').onclick = () => {
      const m = buildMask();
      if (m.ratio < 0.002) { showToast("还没涂到东西 —— 在图上涂一块再确认", "warn"); return; }
      selMask = m.url.split(",")[1];
      selMaskOn = imgDataUrl;
      refreshMaskChip();
      close();
      showToast("已选好区域，接着说要改成什么（例如「换成深蓝色」）", "ok");
    };
    layer.onclick = (e) => { if (e.target === layer) close(); };
    if (img.complete) init(); else img.onload = init;
  };

  if (maskChip) {
    const editBtn = $("#maskEditBtn"), clearBtn = $("#maskClearBtn");
    if (editBtn) editBtn.onclick = () => {
      if (selMaskOn) openMaskEditor(selMaskOn);
      else showToast("先在图片上点「✂️ 涂抹选区」再来重涂", "warn");
    };
    if (clearBtn) clearBtn.onclick = () => { clearSelMask(); showToast("已清除选区", ""); };
  }
  refreshMaskChip();

  async function doSend(promptText, mediaB64, docPayload) {
    history.push({ role: "user", content: promptText });

    // ---------- 思考过程：默认折叠，但实时刷新状态；展开可见实时全文 ----------
    const thinkWrap = document.createElement("div");
    thinkWrap.className = "msg bot";
    const thinkHdr = document.createElement("div");
    thinkHdr.className = "bubble think-hdr";
    const thinkStatus = document.createElement("span");
    thinkStatus.textContent = "🧠 思考中…";
    const thinkArrow = document.createElement("span");
    thinkArrow.className = "arrow";
    thinkArrow.textContent = "▸";
    thinkHdr.appendChild(thinkStatus);
    thinkHdr.appendChild(thinkArrow);
    const thinkBody = document.createElement("div");
    thinkBody.className = "think-body";     // 默认 display:none（折叠）
    thinkBody.textContent = "本地模型思考中…";
    thinkWrap.appendChild(thinkHdr);
    thinkWrap.appendChild(thinkBody);
    messagesEl.appendChild(thinkWrap);
    const updateThinkStatus = () => {
      thinkStatus.textContent = thinking
        ? "🧠 思考中…（" + thinking.length + " 字）"
        : "🧠 思考中…";
    };
    thinkHdr.onclick = () => {
      const open = thinkBody.style.display !== "none";
      thinkBody.style.display = open ? "none" : "block";
      thinkArrow.textContent = open ? "▸" : "▾";
      if (!open) thinkBody.textContent = thinking + "▌";
    };

    // ---------- 工具调用过程（横向小条）----------
    const toolWrap = document.createElement("div");
    toolWrap.className = "tool-strip";
    messagesEl.appendChild(toolWrap);

    // ---------- 生成/读取的媒体 ----------
    const mediaWrap = document.createElement("div");
    mediaWrap.className = "media-strip";
    messagesEl.appendChild(mediaWrap);

    // ---------- 最终回答气泡 ----------
    const answerWrap = document.createElement("div");
    answerWrap.className = "msg bot";
    const answerBubble = document.createElement("div");
    answerBubble.className = "bubble";
    answerBubble.textContent = "…";
    answerWrap.appendChild(answerBubble);
    messagesEl.appendChild(answerWrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    // 后端可能发来 note（例如"输出被长度上限截断，正在重试"），
    // 收起来备用：万一最终没有正文，就把它显示出来，而不是留一个空气泡
    const notes = [];
    // 联网检索的来源清单：由后端在 ui 事件里单独发来，
    // 在回答下方渲染成可伸缩列表（不再让模型把链接写进正文）
    let sourcesData = null;

    let thinking = "", answer = "";
    // ---------- 思考过程平滑逐字播放 ----------
    // Ollama 对 qwen3-vl 等推理模型常把 thinking 一次性整体返回（非逐 token），
    // 为避免"和回答一起弹出来"，这里把到达的增量拆成小块逐步渲染，
    // 使思考内容在答案输出之前就能持续、实时地逐字增长。
    let thinkPending = "";      // 待逐字播放的增量缓冲
    let thinkTimer = null;
    const pushThinking = (delta) => {
      thinkPending += delta;
      if (thinkTimer === null) {
        thinkTimer = setInterval(() => {
          // ⚠️ 每帧字数**不能写死**。原来固定 5 字/26ms ≈ 190 字/秒，
          // 模型思考一旦比这快，待播队列就越积越长 —— 表现是"思考半天不动、
          // 结束前突然刷一大段"，正是用户说的"不是逐字"。
          // 改成跟着积压量自适应：积压越多每帧吐越多，既保持平滑又**永远不落后**。
          const STEP = Math.max(5, Math.ceil(thinkPending.length / 10));
          if (thinkPending) {
            const take = Math.min(STEP, thinkPending.length);
            thinking += thinkPending.slice(0, take);
            thinkPending = thinkPending.slice(take);
            updateThinkStatus();
            if (thinkBody.style.display !== "none") thinkBody.textContent = thinking + "▌";
          }
        }, 26);                                  // 帧间隔 ≈ 每帧约 26ms
      }
    };
    const stopThinking = () => {
      if (thinkTimer !== null) { clearInterval(thinkTimer); thinkTimer = null; }
      if (thinkPending) { thinking += thinkPending; thinkPending = ""; }
    };
    const addToolChip = (name) => {
      const chip = document.createElement("span");
      chip.className = "tool-chip";
      chip.textContent = TOOL_LABELS[name] || ("🔧 " + name);
      toolWrap.appendChild(chip);
      chip.scrollIntoView({ block: "nearest" });
      if (name === "generate_image") thinkStatus.textContent = "🎨 正在生成图片…";
      else if (name === "edit_image") thinkStatus.textContent = "🖼 正在微改图片…";
      else if (name === "remember" || name === "search_memory") thinkStatus.textContent = "🧠 正在读写记忆…";
      else if (name === "web_search") thinkStatus.textContent = "🌐 正在联网检索…";
      else thinkStatus.textContent = "🔧 正在调用工具：" + (TOOL_LABELS[name] || name);
    };

    // ---------- 代码卡片：模型跑了什么代码、结果怎样，都能直接看、直接改、直接重跑 ----------
    // 模型在回答里贴的代码块也会用它渲染（见 renderAnswerWithCode）。
    const makeCodeCard = (ui, title) => {
      const card = document.createElement("div");
      card.className = "code-card";
      const risk = (ui.risky || []).length
        ? `<span class="code-risk" title="这些操作已经过你批准">⚠ ${escapeHtml((ui.risky || []).join("、"))}</span>`
        : "";
      const meta = [];
      if (ui.seconds) meta.push("⏱ " + ui.seconds + "s");
      if (ui.rc === 0) meta.push("退出码 0");
      else if (ui.rc !== null && ui.rc !== undefined) meta.push("退出码 " + ui.rc);
      card.innerHTML =
        `<div class="code-head"><span class="code-title">${escapeHtml(title || "🐍 本地执行的代码")}</span>${risk}` +
        `<span class="code-meta">${escapeHtml(meta.join(" · "))}</span>` +
        `<span class="code-btns"><button class="btn sm ghost code-ws" title="直接写进开发台当前项目，并立刻在编辑器里打开 —— 不用手动复制粘贴">📝 写进开发台</button>` +
        `<button class="btn sm ghost code-edit">✏ 编辑</button>` +
        `<button class="btn sm ghost code-run">▶ 运行</button>` +
        `<button class="btn sm ghost code-save">💾 存到文库</button>` +
        `<button class="btn sm ghost code-copy">📋 复制</button></span></div>` +
        `<pre class="code-body"><code></code></pre><div class="code-out"></div>` +
        // 运行中才显示：给程序喂标准输入（程序里有 input() 时全靠它）
        `<div class="code-stdin" hidden><input class="code-stdin-in" spellcheck="false" ` +
        `placeholder="程序在等输入的话，在这里打字后回车（一行一次）">` +
        `<button class="btn sm ghost code-stdin-send">发送</button></div>`;
      const codeEl = card.querySelector(".code-body code");
      const wsBtn = card.querySelector(".code-ws");
      const editBtn = card.querySelector(".code-edit");
      const runBtn = card.querySelector(".code-run");
      const saveBtn = card.querySelector(".code-save");
      const copyBtn = card.querySelector(".code-copy");
      const outEl = card.querySelector(".code-out");
      const stdinRow = card.querySelector(".code-stdin");
      const stdinIn = card.querySelector(".code-stdin-in");
      const stdinSend = card.querySelector(".code-stdin-send");
      let current = ui.code || "";
      codeEl.textContent = current;

      const paintOut = (r) => {
        const bits = [];
        if (r.out) bits.push(`<div class="code-part"><b>输出</b><pre>${escapeHtml(r.out)}</pre></div>`);
        if (r.err) bits.push(`<div class="code-part err"><b>${r.rc ? "报错" : "提示"}</b><pre>${escapeHtml(r.err)}</pre></div>`);
        if (!r.out && !r.err) bits.push('<div class="code-part empty">（没有输出 —— 代码里要用 print() 打印结果）</div>');
        if (r.seconds !== undefined && r.seconds !== null) bits.push(`<div class="code-time">耗时 ${r.seconds}s</div>`);
        outEl.innerHTML = bits.join("");
      };
      if (ui.out || ui.err) paintOut(ui);

      // 编辑：把 <pre> 换成 textarea（所见即所得，改完直接跑）
      let editing = false;
      let area = null;
      editBtn.onclick = () => {
        // ⚠️ 替换的父节点要用 card，**不能用 pre 自己**：
        // `<pre>` 本身就是 .code-body，在它自己身上 querySelector(".code-body")
        // 只会搜子元素、返回 null，replaceChild(x, null) 会抛异常，
        // 结果就是"点了编辑按钮毫无反应"（踩过）。
        const pre = card.querySelector(".code-body");
        if (!editing) {
          area = document.createElement("textarea");
          area.className = "code-edit-area";
          area.spellcheck = false;
          area.value = current;
          card.replaceChild(area, pre);
          editing = true;
          editBtn.textContent = "✔ 完成";
          area.focus();
        } else {
          current = area.value;
          const np = document.createElement("pre");
          np.className = "code-body";
          const c = document.createElement("code");
          c.textContent = current;
          np.appendChild(c);
          card.replaceChild(np, area);
          editing = false;
          editBtn.textContent = "✏ 编辑";
        }
      };
      const grab = () => (editing && area ? area.value : current);

      // ---- ▶ 运行：**流式**跑，不设时限 ----
      // 为什么不用 /api/code/run：那个是同步的（跑完才出字）而且有硬性时限 ——
      // 计时器、服务器、番茄钟这类**本来就要一直跑**的程序，点运行只会等来
      // 一句"执行超过 25 秒，已被强制中止"，看起来像程序坏了（实测用户就是这么被卡住的）。
      // 现在：边跑边出字、不设时限、随时能停。
      //
      // 进度条/倒计时用的是 `print(x, end='\r')` —— 一个换行都没有。
      // 所以显示前要把同一行里的 \r 折叠掉，否则一屏都是 "25:00 24:59 24:58…"。
      const foldCR = (s) => s.split("\n").map((ln) => {
        if (ln.indexOf("\r") === -1) return ln;
        const parts = ln.split("\r").filter((x) => x !== "");
        return parts.length ? parts[parts.length - 1] : "";
      }).join("\n");
      let runningId = null;
      // 往正在跑的程序里送一行标准输入（程序里有 input() 时靠它）
      const sendStdin = async () => {
        if (!runningId) { showToast("现在没有正在跑的程序", "warn"); return; }
        const v = stdinIn.value;
        stdinIn.value = "";
        const rid = runningId;
        try {
          const r = await api("/api/ws/run_input",
            { method: "POST", body: JSON.stringify({ id: rid, data: v }) });
          if (r && r.ok === false) showToast(r.error || "输入送不进去", "warn");
        } catch (e) {
          showToast("输入送不进去：" + String(e.message || e), "warn");
        }
      };
      stdinSend.onclick = sendStdin;
      stdinIn.onkeydown = (e) => {
        // 回车就发；程序里一次 input() 读一行，所以这里不要自动补内容
        if (e.key === "Enter") { e.preventDefault(); sendStdin(); }
      };
      runBtn.onclick = async () => {
        if (runningId) {                 // 再点一次 = 停止
          const rid = runningId;
          runningId = null;
          runBtn.textContent = "停止中…";
          runBtn.disabled = true;
          try { await api("/api/ws/run_stop", { method: "POST", body: JSON.stringify({ id: rid }) }); }
          catch (e) {}
          runBtn.disabled = false;
          runBtn.textContent = "▶ 运行";
          runBtn.title = "";
          stdinRow.hidden = true;
          return;
        }
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        // 兜底：万一漏进来的是「内部工具调用」那坨 JSON（不是代码），别拿去跑 ——
        // 那只会得到 `SyntaxError: '{' was never closed`，把人吓一跳。
        if (looksLikeToolCall(ui.lang || "", code)) {
          showToast("这段不是代码，是一次内部工具调用（已折叠），不用运行它", "warn");
          return;
        }
        current = code;
        const rid = "card-" + Date.now();
        runningId = rid;
        runBtn.textContent = "■ 停止";
        runBtn.title = "点它就能随时停掉正在跑的程序";
        stdinRow.hidden = false;         // 运行中显示输入框：带 input() 的程序靠它
        let raw = "";
        const paintLive = () => {
          outEl.innerHTML = `<div class="code-part"><b>运行中…</b><pre>${escapeHtml(foldCR(raw))}</pre></div>`;
          outEl.scrollTop = outEl.scrollHeight;
        };
        outEl.innerHTML = '<div class="code-part">正在启动…</div>';
        try {
          const resp = await fetch("/api/code/run_stream", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code, id: rid }) });
          if (!resp.ok) throw new Error(resp.statusText || ("HTTP " + resp.status));
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n"); buf = lines.pop();
            for (const line of lines) {
              if (!line.trim()) continue;
              let o; try { o = JSON.parse(line); } catch { continue; }
              if (o.t === "out") { raw += o.data; paintLive(); }
              else if (o.t === "end") {
                if (!o.ok) throw new Error(o.error || "启动失败");
                paintOut({ out: foldCR(raw), rc: o.rc, seconds: o.seconds });
              }
            }
          }
          if (!raw) outEl.innerHTML = '<div class="code-part empty">（没有输出 —— 代码里要用 print() 打印结果）</div>';
        } catch (e) {
          outEl.innerHTML =
            `<div class="code-part err">运行失败：${escapeHtml(String(e.message || e))}</div>`;
        } finally {
          if (runningId === rid) runningId = null;
          runBtn.textContent = "▶ 运行";
          runBtn.title = "";
          stdinRow.hidden = true;
        }
      };
      // 存到生成文库：代码模型那一轮没有工具，保存只能靠这个按钮。
      // 后端会按语言挑后缀、优先用用户点名的文件名（"就叫 stats.py"）。
      saveBtn.onclick = async () => {
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        current = code;
        saveBtn.disabled = true;
        try {
          const r = await api("/api/code/save", {
            method: "POST",
            body: JSON.stringify({ code, language: ui.lang || "", user_text: ui.userText || "" }),
          });
          showToast("已存入生成文库：" + (r.rel || ""), "ok");
          // 面板可能正开着：刷新一下列表（不传参，别改当前选中项）
          try { if (typeof loadDoclib === "function") loadDoclib(); } catch (e) {}
        } catch (e) {
          showToast("保存失败：" + String(e.message || e), "warn");
        } finally {
          saveBtn.disabled = false;
        }
      };
      copyBtn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(grab());
          showToast("代码已复制", "ok");
        } catch (e) {
          showToast("复制失败，请手动选中", "warn");
        }
      };
      // 写进开发台：一步落盘到**当前工作区项目**并立刻在编辑器里打开。
      // 为什么要有它：开发台的 workspace_write 只有模型自己能调，
      // 模型在聊天里贴的代码块是纯文本 —— 原来只能「📋 复制」再手动粘进编辑器，
      // 等于把 AI 的输出又搬一遍。文件名优先用用户点名的那个（"就叫 stats.py"）。
      wsBtn.onclick = async () => {
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        current = code;
        if (!window.Studio || !window.Studio.reveal) {
          showToast("开发台还没就绪，稍后再试", "warn"); return;
        }
        wsBtn.disabled = true;
        const oldText = wsBtn.textContent;
        wsBtn.textContent = "写入中…";
        try {
          const r = await api("/api/ws/code", {
            method: "POST",
            body: JSON.stringify({ code, language: ui.lang || "",
                                   user_text: ui.userText || "" }),
          });
          showToast((r.overwrote ? "已覆盖并写进开发台：" : "已写进开发台：") + r.rel, "ok");
          await window.Studio.reveal(r.rel);
        } catch (e) {
          showToast("写入失败：" + String(e.message || e), "warn");
        } finally {
          wsBtn.disabled = false;
          wsBtn.textContent = oldText;
        }
      };
      return card;
    };

    /* AI 真的把文件写到磁盘上、而开发台没开着时给的提示卡片。
       为什么不直接弹开发台：用户可能只是在聊天里问问题，AI 顺手存了个文件，
       整页盖上去会很打扰。但也不能什么都不说 —— 那样用户根本不知道文件在哪，
       只能去聊天里复制粘贴。所以给一张卡片，点一下就到。 */
    const makeWsCard = (ui) => {
      const card = document.createElement("div");
      card.className = "ws-card";
      card.innerHTML =
        `<span class="ws-ico">📄</span>` +
        `<span class="ws-txt">AI 写入了 <b>${escapeHtml(ui.rel)}</b>` +
        (ui.chars ? `（${ui.chars} 字）` : "") + `</span>` +
        `<button class="btn sm ghost ws-open">在开发台打开</button>`;
      const go = async () => {
        if (window.Studio && window.Studio.reveal) {
          await window.Studio.reveal(ui.rel);
          card.classList.add("done");
        } else showToast("开发台还没就绪，稍后再试", "warn");
      };
      card.querySelector(".ws-open").onclick = (e) => { e.stopPropagation(); go(); };
      card.onclick = go;
      return card;
    };

    // 回答里的**站内下载链接**渲染成可点按钮（实现在文件上方 addMsg 附近，
    // 那里是顶层函数声明，历史恢复和实时回答两条路径都能用）。
    // ⚠️ 回答里的"代码块"未必都是代码。
    // 代码模型那一轮用的是**文本协议**：它会把工具调用写成一个 ```tool 块塞在正文里
    // （`{"name": "workspace_write", "arguments": {...}}`），后端会把它们摘掉；
    // 万一漏了一个（实测：模型把 JSON 的最后一个 `}` 漏了 → 后端解析失败 → 块留在正文里），
    // 这里就是最后一道闸 —— 否则那段 JSON 会变成一张"可编辑可运行"的代码卡片，
    // 用户点「▶ 运行」得到 `SyntaxError: '{' was never closed`，
    // 而且会看到好几张一模一样的卡片，完全不知道发生了什么。
    // ⚠️ 用**函数声明**而不是 const 箭头函数：后者在初始化之前被调用会抛
    // TDZ ReferenceError（而历史会话回放可能发生在初始化之前）。函数声明会提升。
    function looksLikeToolCall(lang, body) {
      if (/^(tool|tool_call|tool-call)$/i.test(String(lang || "").trim())) return true;
      const s = String(body || "").trim();
      return /^\{\s*"(name|tool|function)"\s*:/.test(s)
        && /"(arguments|parameters|args|input)"\s*:/.test(s);
    }

    const renderAnswerWithCode = (bubble, text, userText) => {
      text = stripToolLeak(text);          // 先摘掉泄漏的工具调用（见 stripToolLeak）
      const parts = [];
      const re = /```([a-zA-Z0-9_+#.-]*)[ \t]*\n([\s\S]*?)```/g;
      let last = 0, m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) parts.push({ t: "text", v: text.slice(last, m.index) });
        if (looksLikeToolCall(m[1], m[2])) {
          parts.push({ t: "raw" });          // 内部工具调用：不渲染成代码卡片
        } else {
          parts.push({ t: "code", lang: m[1] || "", v: m[2].replace(/\n$/, "") });
        }
        last = m.index + m[0].length;
      }
      if (last < text.length) parts.push({ t: "text", v: text.slice(last) });
      if (!parts.some((p) => p.t === "code")) { bubble.textContent = text; return; }
      bubble.textContent = "";
      parts.forEach((p) => {
        if (p.t === "text") {
          if (!p.v.trim()) return;
          const d = document.createElement("div");
          d.className = "md-text";
          d.textContent = p.v;
          bubble.appendChild(d);
        } else if (p.t === "raw") {
          const d = document.createElement("div");
          d.className = "md-text muted";
          d.textContent = "（这里原本是一次内部工具调用，已折叠）";
          bubble.appendChild(d);
        } else {
          bubble.appendChild(makeCodeCard(
            { code: p.v, lang: p.lang, userText: userText || "" },
            "📄 代码（可编辑后直接运行）"));
        }
      });
    };

    // 危险操作确认：模型要删文件/起进程/联网时，**先问一句**而不是直接拒绝
    const showConfirm = (ui) => {
      if (document.querySelector(".confirm-layer")) return;   // 同一时刻只弹一个
      const layer = document.createElement("div");
      layer.className = "confirm-layer";
      layer.innerHTML =
        '<div class="confirm-box">' +
        '<div class="confirm-title">⚠️ 这次操作需要你确认</div>' +
        `<div class="confirm-reason">${escapeHtml(ui.reason || "模型请求执行一段有风险的代码")}</div>` +
        '<pre class="confirm-code"><code></code></pre>' +
        '<div class="confirm-tip">批准后会在本机真实执行（临时目录、有运行时限）。不确定就别点允许。</div>' +
        '<div class="confirm-btns"><button class="btn ghost" data-act="deny">拒绝</button>' +
        '<button class="btn primary" data-act="allow">允许执行</button></div></div>';
      layer.querySelector("code").textContent = ui.code || "";
      document.body.appendChild(layer);
      const answer = async (allow) => {
        layer.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
          await api("/api/tool/confirm", {
            method: "POST", body: JSON.stringify({ id: ui.id, allow }) });
          showToast(allow ? "已允许执行" : "已拒绝", allow ? "ok" : "");
        } catch (e) {
          showToast("回复失败：" + String(e.message || e), "warn");
        } finally {
          layer.remove();
        }
      };
      layer.querySelector('[data-act="deny"]').onclick = () => answer(false);
      layer.querySelector('[data-act="allow"]').onclick = () => answer(true);
    };

    // 输入框弹层在文件顶部（最外层作用域）—— 文库面板要用它，
    // 而那段代码在独立的 IIFE 里，放到这里它访问不到（踩过）。

    // 问答框：材料不足时模型可以在这里问细节。用户填完它接着做，
    // 不用把需求重新描述一遍 —— 这是"写得像你要的"和"瞎猜一篇"的区别。
    const showAskDialog = (ui) => {
      if (document.querySelector(".ask-layer")) return;
      const qs = ui.questions || [];
      if (!qs.length) return;
      // title / hint 可以由后端指定 —— 比如"请填高德 key"那种场景，
      // 用默认那句"想先跟你确认几个细节"就对不上了。
      const title = ui.title || "💬 想先跟你确认几个细节";
      const hint = ui.hint || "补充下面的信息，写出来才贴你的要求。不想答的直接留空跳过。";
      const layer = document.createElement("div");
      layer.className = "confirm-layer ask-layer" + (ui.freeInput ? " ask-free-mode" : "");
      layer.innerHTML =
        '<div class="confirm-box">' +
        `<div class="confirm-title">${escapeHtml(title)}</div>` +
        `<div class="confirm-reason">${escapeHtml(hint).replace(/\n/g, "<br>")}</div>` +
        '<div class="ask-list"></div>' +
        '<div class="confirm-btns">' +
        `<button class="btn ghost" data-act="skip">${ui.freeInput ? "不填，跳过" : "跳过，按你的理解写"}</button>` +
        `<button class="btn primary" data-act="send">${ui.freeInput ? "保存并连接" : "提交，继续"}</button></div></div>`;
      const list = layer.querySelector(".ask-list");
      const items = [];
      qs.forEach((q, i) => {
        const box = document.createElement("div");
        box.className = "ask-item";
        const type = q.multi ? "checkbox" : "radio";
        const opts = (q.options || []).map((o) =>
          `<label class="ask-opt"><input type="${type}" name="ask${i}" value="${escapeHtml(o)}">` +
          `<span>${escapeHtml(o)}</span></label>`).join("");
        box.innerHTML = `<div class="ask-q">${ui.freeInput ? "" : (i + 1) + ". "}${escapeHtml(q.question)}</div>` +
          (opts ? `<div class="ask-opts">${opts}</div>` : "") +
          `<input class="ask-free" type="text" placeholder="${opts ? "也可以自己写…" : "在这里回答"}" />`;
        list.appendChild(box);
        items.push(box);
      });
      document.body.appendChild(layer);

      const finish = async (skip) => {
        layer.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        const answers = [];
        items.forEach((box, i) => {
          const checked = [...box.querySelectorAll("input[type=radio]:checked, input[type=checkbox]:checked")]
            .map((el) => el.value);
          const free = (box.querySelector(".ask-free").value || "").trim();
          const merged = [checked.join("、"), free].filter(Boolean).join("；");
          answers.push({ question: qs[i].question, answer: skip ? "" : merged });
        });
        try {
          await api("/api/tool/answer", {
            method: "POST", body: JSON.stringify({ id: ui.id, answers }) });
          showToast(skip ? "已跳过，让它自己决定" : "已提交，它继续写", skip ? "" : "ok");
        } catch (e) {
          showToast("提交失败：" + String(e.message || e), "warn");
        } finally {
          layer.remove();
        }
      };
      layer.querySelector('[data-act="skip"]').onclick = () => finish(true);
      layer.querySelector('[data-act="send"]').onclick = () => finish(false);
      layer.querySelector(".ask-free") && layer.querySelector(".ask-free").focus();
    };
    const addMedia = (ui) => {
      const isWeb = ui.origin === "web";
      const card = document.createElement("div");
      card.className = "media-card";
      const mime = ui.mime || "image/png";
      const b64 = ui.b64 || "";
      // 明确区分「网上搜到的」「AI 生成的」「AI 微改的」，避免混淆
      const badge = isWeb ? "🌐 网上搜到的"
        : (ui.origin === "gen" ? "🎨 AI 生成"
        : (ui.origin === "edit" ? "🎨 AI 微改" : "🖼️ 图片"));
      const caption = (ui.prompt || "").slice(0, 60);
      card.innerHTML = `
        <div class="media-cap"><span class="badge ${isWeb ? "web" : "gen"}">${badge}</span>${
          caption ? " " + caption : ""}</div>
        <img src="data:${mime};base64,${b64}">
        <div class="media-meta">${
          ui.source ? `<a href="${ui.source}" target="_blank" rel="noopener">来源页</a> · ` : ""}${
          ui.device ? "🖥 " + ui.device + " " : ""}${ui.cost_s ? "⏱ " + ui.cost_s + "s" : ""}${
          ui.size ? " · " + ui.size : ""}</div>
        <div class="media-actions"><button class="btn sm ghost lib-save">⭐ 保存到图库</button>
          <button class="btn sm ghost mask-pick" title="在这张图上涂一下，之后只改涂过的地方（比整图微改准得多）">✂️ 涂抹选区</button></div>`;
      card.querySelector("img").onclick = (e) => {
        e.stopPropagation();
        showLightbox(mime, b64, badge + (caption ? "：" + caption : ""));
      };
      const pick = card.querySelector(".mask-pick");
      if (pick) pick.onclick = (e) => {
        e.stopPropagation();
        openMaskEditor("data:" + mime + ";base64," + b64);
      };
      const btn = card.querySelector(".lib-save");
      btn.onclick = async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        btn.textContent = "保存中…";
        try {
          const r = await api("/api/library/images", {
            method: "POST",
            body: JSON.stringify({
              b64, name: caption.slice(0, 30),
              source: ui.source || ui.url || "",
              origin: ui.origin || "web",
            }),
          });
          if (r && r.ok) {
            btn.textContent = "✅ 已保存";
            loadLibrary();
            showToast("已保存到图片库", "ok");
          } else {
            btn.textContent = "⭐ 保存到图库";
            btn.disabled = false;
            showToast("保存失败：" + ((r && r.error) || "未知错误"), "warn");
          }
        } catch (err) {
          btn.textContent = "⭐ 保存到图库";
          btn.disabled = false;
          showToast("保存失败：" + err.message, "warn");
        }
      };
      mediaWrap.appendChild(card);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    };
    const finishThinking = () => {
      stopThinking();                            // 结束播放，一次性补齐剩余思考
      thinkStatus.textContent = thinking
        ? "🧠 思考过程（" + thinking.length + " 字）"
        : "🧠 未输出思考";
      if (thinking) thinkBody.textContent = thinking;
      else thinkBody.textContent = "（无思考输出）";
      thinkBody.style.display = "none";   // 完成后保持折叠
      thinkArrow.textContent = "▸";
    };

    streaming = true;
    typingHintShown = false;
    aborted = false;
    $("#sendBtn").disabled = true;
    setStopVisible(true);
    abortCtl = new AbortController();
    // ⏳ 卡顿看门狗：**没有任何数据进来时，界面不能一声不吭**。
    //
    // 为什么需要（2026-09-22 用户拿着截图问"思考才憋出两个字然后卡着不动，正常吗"）：
    //   第 0.5 秒到第一个 token 之间是**纯等待**（Ollama 要先预填充整份提示词，
    //   启动后第一条约 2 万 token，实测 10~16 秒；并发时还可能被前一条占住几十秒）。
    //   这段时间界面只有一个不动的「🧠 思考中…」，用户完全无法分辨
    //   "在算" 还是 "死了"。这里每收到一块就重置计时，超过 4 秒就报出已等待时长。
    // ⚠️ 有数据进来后自然恢复成「思考中…（N 字）」——因为 updateThinkStatus 会覆盖它。
    let _stallT0 = 0;
    let _stallTimer = null;
    // 后端在"模型还没加载"时会先发一条 status（30B 冷启动实测约 20 秒）。
    // 存下来，让下面的等待提示带上**具体原因**，而不是只说"在准备"。
    let _backendStatus = "";
    const _clearStall = () => {
      if (_stallTimer) { clearInterval(_stallTimer); _stallTimer = null; }
    };
    const _armStall = () => {
      _clearStall();
      _stallT0 = Date.now();
      _stallTimer = setInterval(() => {
        const sec = Math.round((Date.now() - _stallT0) / 1000);
        if (sec >= 4) {
          thinkStatus.textContent = _backendStatus
            ? (_backendStatus + "　已等待 " + sec + " 秒")
            : ("⏳ 模型正在准备…已等待 " + sec
               + " 秒（大提示词要先读一遍；若前面还有任务在跑，会等它让出来）");
        }
      }, 1000);
    };
    _armStall();
    try {
      const resp = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        signal: abortCtl.signal,
        body: JSON.stringify({ messages: history, images_b64: mediaB64,
                               // ★ 涂抹选区（若有）：后端据此走"局部重绘"，只改涂过的地方
                               mask_b64: selMask ? [selMask] : null,
                               docs: (docPayload && docPayload.length) ? docPayload : null,
                               // 在开发台里说话 = 就是在写代码 → 后端**直接接入编程模型**，
                               // 不再靠关键词猜意图（用户："自动接入编程的模型"）
                               studio: !!(window.Studio && window.Studio.opened),
                               stream: true, session_id: sessionId }) });
      if (!resp.ok) {
        const e = await resp.json();
        throw new Error(e.detail || resp.statusText);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        _armStall();                 // 收到数据 → 重新计时（不再是"卡住"状态）
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let obj;
          try { obj = JSON.parse(line); } catch { continue; }
          if (obj.error) {
            // ⚠️ `explained` = 后端已经把原因翻成人话、也给了下一步怎么办。
            // 这类错误**不要**再跟一句"（可能内存/模型未就绪，请查看状态）"——
            // 实测用户看到那句会去查显存，而真正的原因（模型把工具调用的 JSON
            // 写坏了）跟硬件毫无关系。见下面 catch 里的用法。
            const _e = new Error(obj.error);
            if (obj.explained) _e.explained = true;
            throw _e;
          }
          if (obj.message) {
            if (obj.message.thinking_reset) {
              // 后端因"思考吃满了输出空间"而加长上限重试：上一轮的思考已经作废。
              // 必须把面板和缓冲都清空 —— 否则新一轮的思考会直接接在旧的后面，
              // 界面上看起来就是"同一个思路说了两遍"（2026-09-19 用户反馈）。
              thinking = ""; thinkPending = "";
              thinkBody.textContent = "";
              updateThinkStatus();
            }
            if (obj.message.thinking) {
              pushThinking(obj.message.thinking);  // 进入平滑播放器，逐字实时渲染
            }
            if (obj.message.content) {
              answer += obj.message.content;
              // 逐字流式时也过一遍闸：后端会先把"可能是工具调用壳子"的尾巴扣住，
              // 万一有变体漏过来，这里显示时也不会闪出一坨裸 JSON（`answer` 存的仍是原文）。
              answerBubble.textContent = stripToolLeak(answer) + "▌";
            }
          }
          if (obj.tool_start) addToolChip(obj.tool_start.name);
          // 一轮里同时发起多个工具时给个提示 —— 让用户知道这是并行执行、在省时间
          if (obj.tool_parallel) {
            addToolChip(`⚡ 并行执行 ${obj.tool_parallel} 个工具`);
          }
          if (obj.ui) {
            if (obj.ui.type === "sources") sourcesData = obj.ui;   // 稍后在回答下方渲染
            else if (obj.ui.type === "confirm") showConfirm(obj.ui);
            else if (obj.ui.type === "ask") showAskDialog(obj.ui);
            else if (obj.ui.type === "library") {
              // 模型动了生成文库 → 面板跟着刷新，让用户马上看到结果
              if (typeof loadDoclib === "function") loadDoclib(obj.ui.rel);
            }
            else if (obj.ui.type === "map") {
              // 地图卡片：地点/路线已经算好，这里把它画出来
              makeMapCard(obj.ui);
            }
            else if (obj.ui.type === "code") {
              answerWrap.appendChild(makeCodeCard(obj.ui));
              messagesEl.scrollTop = messagesEl.scrollHeight;
            }
            else if (obj.ui.type === "typing") {
              // AI 正在把代码**逐字**写进项目文件（后端是边生成边落盘的）。
              // 编辑器（内置 VS Code）自己盯着磁盘，会看到内容一点点长出来；
              // 这里只负责把状态说清楚，免得用户以为"它又在憋大招"。
              if (window.Studio && window.Studio.notify) window.Studio.notify(obj.ui);
              if (!window.Studio || !window.Studio.opened) {
                if (!typingHintShown) {
                  typingHintShown = true;
                  showToast("AI 正在写代码到项目文件（可在开发台看到它逐字敲）", "ok");
                }
              }
            }
            else if (obj.ui.type === "workspace") {
              // 模型改了工作区文件：
              // · 开发台开着 → notify 直接把那个文件切到编辑器里，用户不用复制
              // · 开发台没开 → **不强行弹出来打断阅读**，给一张卡片，点一下才打开
              if (window.Studio && window.Studio.notify) window.Studio.notify(obj.ui);
              if (obj.ui.act === "write" && window.Studio && !window.Studio.opened) {
                answerWrap.appendChild(makeWsCard(obj.ui));
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }
            } else addMedia(obj.ui);
          }
          if (obj.note) { notes.push(obj.note); showToast(obj.note, "warn"); }
          // ⏳ 后端在模型**还没加载**时先发一条状态（30B 冷启动实测约 20 秒）。
          //    这段时间 Ollama 一个字都不吐，不提示的话界面上就是"完全没反应"。
          //    只改状态栏文案，不动回答气泡 —— 真正的思考/正文到了会覆盖它。
          if (obj.status) { _backendStatus = obj.status; thinkStatus.textContent = obj.status; }
          if (obj.done) break;
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      finishThinking();
      // 回答里带代码块的话渲染成可编辑卡片；否则维持原来的纯文本（不改变原有观感）
      if (answer && answer.indexOf("```") >= 0) renderAnswerWithCode(answerBubble, answer, promptText);
      else renderAnswerLinks(answerBubble, answer);
      if (!answer) {
        // 别把空白气泡藏起来让用户一脸茫然——明确说明发生了什么
        answerBubble.textContent = notes.length
          ? "⚠️ " + notes[notes.length - 1]
          : "⚠️ 模型这次没有输出内容，请再试一次或换个问法。";
        answerBubble.classList.add("empty-answer");
        answer = "";
      }
      if (answer) history.push({ role: "assistant", content: answer });
      // 检索来源：渲染成回答下方的可伸缩列表（默认折叠，点标题展开）
      if (sourcesData && sourcesData.items && sourcesData.items.length) {
        renderSources(answerWrap, sourcesData);
      }
      persistSession();   // 落盘，保证程序重启后能恢复这段对话
    } catch (err) {
      finishThinking();
      if (aborted || (err && err.name === "AbortError")) {
        // 用户点了「■ 终止」：把已经生成出来的部分留着，别报成错误
        answerBubble.classList.remove("empty-answer");
        // ⚠️⚠️ **终止也必须往历史里写一条助手消息**（哪怕一个字都没生成）。
        //
        // 实测（2026-09-22 用户反馈）：点了终止之后，历史里只留下一条"没人回答的用户消息"，
        // 下一轮模型会把它当成**未完成的需求接着做** —— 用户问「红腹锦鸡长什么样」，
        // 图库里却混进一张上一条要的狐狸图。⚠️ 已复现：把这种历史直接发给后端，
        // 模型当场又调了一次 generate_image，而且**完全没回答这一轮真正的问题**。
        //
        // 文案要**对模型说话**（它会被写进提示词），用户看了也不突兀：
        // 明确"这条需求作废、别在新的一轮补做"。显示与落盘用**同一段文本**，
        // 免得刷新后两处不一致。
        const stopMark = "〔已终止〕用户主动中断了这次生成 —— 上面那条需求就此作废，"
                       + "不要在新的一轮里接着做，除非用户再次明确要求。";
        answerBubble.textContent = answer ? answer + "\n\n" + stopMark : stopMark;
        history.push({ role: "assistant",
                       content: (answer ? answer + "\n\n" : "") + stopMark });
        showToast("已终止本次生成");
        try { persistSession(); } catch (e) { /* 落盘失败不影响使用 */ }
      } else {
        // 后端已经解释过原因的（explained），正文里就是完整的人话，别再补尾巴。
        answerBubble.textContent = "❌ " + err.message
          + (err.explained ? "" : "（可能内存/模型未就绪，请查看状态）");
        answerBubble.classList.remove("empty-answer");
      }
    } finally {
      streaming = false;
      abortCtl = null;
      _clearStall();                 // 别忘了停掉卡顿看门狗
      $("#sendBtn").disabled = false;
      setStopVisible(false);
    }
  }

  // ---------- 终止当前任务 ----------
  function setStopVisible(on) {
    const b = $("#stopBtn");
    if (!b) return;
    b.hidden = !on;
    b.disabled = !on;
  }

  function stopStreaming() {
    if (!streaming) return;
    aborted = true;
    // ① 断掉前端的流（连接一断，服务端就会掐掉到 Ollama 的连接）
    try { abortCtl && abortCtl.abort(); } catch (e) { /* 忽略 */ }
    // ② 再补一刀：显式通知后端停止，防止连接没断干净导致模型继续跑
    try {
      fetch("/api/chat/stop", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch (e) { /* 忽略 */ }
    // ③ 立刻恢复界面，不用等 finally
    streaming = false;
    $("#sendBtn").disabled = false;
    setStopVisible(false);
  }

  const stopBtnEl = $("#stopBtn");
  if (stopBtnEl) stopBtnEl.onclick = stopStreaming;
  setStopVisible(false);

  $("#sendBtn").onclick = send;
  inputEl.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = inputEl.scrollHeight + "px"; }
  inputEl.addEventListener("input", autoGrow);

  // ---------- 语音输入（唤醒词「小千小千」+ 静音自动发送）----------
  const voiceHintEl = $("#voiceHint");
  const voiceTextEl = $("#voiceText");
  const voiceBtn = $("#voiceBtn");
  const voiceLevelEl = $("#voiceLevel");
  const voiceDevEl = $("#voiceDev");
  let voiceWs = null;
  let voiceOn = false;
  // 唤醒那一刻输入框里已有的内容（用户可能先打了草稿再说话）——
  // 语音文本**追加**在它后面，而不是把它覆盖掉。
  let voiceDraft = "";

  // 把"草稿 + 语音识别文本"写进输入框（草稿为空时就等于纯语音）。
  function voiceFill(text) {
    const t = String(text == null ? "" : text).trim();
    inputEl.value = voiceDraft ? (voiceDraft + " " + t).trim() : t;
    autoGrow();
  }
  let voiceDevLoaded = false;

  // 电平条：说话时跟着跳。**一直不动就等于"麦克风没采到声音"** ——
  // 以前"喊不动"只能靠猜（选错设备？系统静音了？），现在一眼能看出来。
  function voicePaintLevel(rms, th) {
    if (!voiceLevelEl) return;
    const bar = voiceLevelEl.querySelector("i");
    if (!bar) return;
    const ref = Math.max(0.02, (th || 0.01) * 2);      // 满格 = 说话阈值的 2 倍
    const pct = Math.max(3, Math.min(100, Math.round((rms || 0) / ref * 100)));
    bar.style.width = pct + "%";
    voiceLevelEl.classList.toggle("hot", (rms || 0) >= (th || 0.01));
  }

  // 麦克风下拉：让用户能换掉"选错的那个"（本机默认就是摄像头上的麦）
  async function voiceLoadDevices(keepCurrent) {
    if (!voiceDevEl) return;
    try {
      const r = await api("/api/voice/devices");
      if (!r || !r.devices) return;
      const cur = r.current || "";
      const opts = ['<option value="">（系统默认）</option>'];
      r.devices.forEach((d) => {
        const label = d.name + (d.hostapi ? " · " + d.hostapi : "");
        const sel = (!keepCurrent && d.name === cur) ? " selected" : "";
        opts.push(`<option value="${escapeHtml(d.name)}"${sel}>${escapeHtml(label)}</option>`);
      });
      voiceDevEl.innerHTML = opts.join("");
      if (cur && !Array.from(voiceDevEl.options).some((o) => o.value === cur)) {
        // 当前用的设备不在列表里（名字变了）→ 加一条并选中，别让下拉框显示成"系统默认"
        const o = document.createElement("option");
        o.value = cur; o.textContent = cur + "（当前）"; o.selected = true;
        voiceDevEl.appendChild(o);
      }
      voiceDevLoaded = true;
    } catch (e) { /* 拿不到就保持空下拉 */ }
  }
  if (voiceDevEl) {
    voiceDevEl.onchange = async () => {
      const dev = voiceDevEl.value;
      try {
        const r = await api("/api/voice/device", {
          method: "POST", body: JSON.stringify({ device: dev }) });
        if (r && r.ok === false) showToast(r.error || "换麦克风失败", "warn");
        else {
          showToast(dev ? ("已切换到麦克风：" + dev) : "已切回系统默认麦克风", "ok");
          voiceOn = true;
          voiceUI("listening");
        }
      } catch (e) {
        showToast("换麦克风失败：" + String(e.message || e), "warn");
      }
    };
  }

  function voiceUI(state, text) {
    if (!voiceHintEl || !voiceTextEl) return;
    voiceHintEl.classList.toggle("on", state === "listening");
    voiceHintEl.classList.toggle("awake", state === "awake");
    if (voiceBtn) voiceBtn.classList.toggle("active", state !== "idle");
    if (text) {
      voiceTextEl.textContent = text;
    } else if (state === "awake") {
      voiceTextEl.textContent = "🎙 我在听 · 请说内容（说完停顿 2 秒自动发送）";
    } else if (state === "listening") {
      voiceTextEl.textContent = "👂 监听中 · 说「小千小千」唤醒";
    } else {
      voiceTextEl.textContent = "语音未开启 · 点 🎤 后说「小千小千」唤醒";
    }
  }

  function voiceConnect() {
    if (voiceWs && voiceWs.readyState <= 1) return voiceWs;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    voiceWs = new WebSocket(proto + location.host + "/ws/voice");
    voiceWs.onopen = () => voiceUI("idle", "语音就绪 · 点 🎤 开始监听");
    voiceWs.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "state") {
        voiceUI(msg.state);
        // note 用来解释"这一轮为什么结束了"（比如"没听到内容"）——
        // 不然用户只看到提示条不再发绿，不知道发生了什么。
        if (msg.note && voiceTextEl) voiceTextEl.textContent = "ℹ " + msg.note;
        // 提示条一露出来就把麦克风下拉填上（让用户能一眼看到"现在用的是哪只麦"）
        if (!voiceDevLoaded) voiceLoadDevices();
      } else if (msg.type === "wake") {
        voiceUI("awake", msg.note ? "🎙 " + msg.note : undefined);
        // ⚠️ 唤醒后**把光标放进输入框**：用户接着说的话会实时出现在这里，
        //    他不用再去点一下（"唤醒了但不知道往哪说"就是这么来的）。
        //    顺便把视口滚到输入区 —— 用户喊唤醒词时多半没看着窗口。
        try { inputEl.focus(); } catch (e) {}
        // 记下此刻输入框里的内容：用户可能先打了草稿才说话，别被语音覆盖掉
        voiceDraft = inputEl.value.trim();
      } else if (msg.type === "partial") {
        voiceUI("awake", "🎙 " + (msg.text || "…"));
        voiceFill(msg.text);
      } else if (msg.type === "final") {
        voiceFill(msg.text);
        voiceDraft = "";
        voiceUI("listening", "✅ 已识别，自动发送…");
        if (msg.auto && inputEl.value.trim()) setTimeout(() => send(), 120);
      } else if (msg.type === "level") {
        // 实时电平（后端每 0.5 秒推一次）
        voicePaintLevel(msg.rms, msg.speech_th);
      } else if (msg.type === "status") {
        voiceUI(msg.state || "idle");
        if (msg.model_ready === false) voiceTextEl.textContent = "⚠ 未找到语音模型（asr_model 目录）";
        if (msg.device) voiceLoadDevices();
      } else if (msg.type === "error") {
        voiceUI("idle", "⚠ " + (msg.message || "语音出错"));
        voiceOn = false;
      } else if (msg.type === "ack") {
        const r = msg.result || {};
        if (r.ok === false && r.error) {
          voiceUI("idle", "⚠ " + r.error);
          voiceOn = false;
        } else if (r.already) {
          voiceOn = true;
        }
      }
    };
    voiceWs.onclose = () => { voiceOn = false; voiceUI("idle", "语音连接已断开"); };
    voiceWs.onerror = () => { };
    return voiceWs;
  }

  if (voiceBtn) {
    // ⚠️ 点击时**先问后端**再决定是开还是关，而不是信本地的 voiceOn 变量。
    //    本地状态一旦和后端不同步（页面重载、WS 断线重连、后端自己停了），
    //    按钮就会**方向反了**：明明没在听，点一下却发的是「停止」——
    //    用户怎么点都喊不出来，界面还显示"监听中"。查一次 status 就彻底消除这种分叉。
    voiceBtn.onclick = async () => {
      voiceBtn.disabled = true;
      try {
        const st = await api("/api/voice/status");
        const running = !!(st && st.running);
        if (running) {
          await api("/api/voice/stop", { method: "POST" });
          voiceOn = false;
          voiceUI("idle");
        } else {
          voiceConnect();                       // 开之前先把事件通道连上
          const r = await api("/api/voice/start", { method: "POST" });
          if (r && r.ok === false) {
            voiceUI("idle", "⚠ " + (r.error || "启动失败"));
          } else {
            voiceOn = true;
            voiceUI("listening");
            voiceLoadDevices();
          }
        }
      } catch (e) {
        voiceUI("idle", "⚠ " + String(e.message || e));
      } finally {
        voiceBtn.disabled = false;
      }
    };
    voiceUI("idle");
  }

  // 页面一打开就连上语音通道。
  // **必须做**：后端起播后唤醒事件、识别文本都是通过这条 WebSocket 推过来的，
  // 不连的话"喊醒了窗口却收不到话" —— 只有 🎤 按钮被点过才会连就太晚了。
  (async function attachVoice() {
    try {
      const st = await api("/api/voice/status");
      if (!st || st.running !== true) return;      // 后端没在听，保持"未开启"
      const ws = voiceConnect();
      const onOpen = () => {
        voiceOn = true;
        voiceUI(st.state === "awake" ? "awake" : "listening");
        voiceLoadDevices();
      };
      if (ws.readyState === 1) onOpen();
      else ws.addEventListener("open", onOpen, { once: true });
    } catch (e) { /* 语音不可用时保持原样 */ }
  })();

  // 定期和后端**对一次账**：后端可能已经不在监听了（换设备失败、麦克风被别的程序
  // 独占、别处调了 /api/voice/stop），而界面还写着"监听中" —— 这时候用户怎么喊
  // 都不会有反应，却完全看不出问题（实测踩过：status 里 running=false、error=null）。
  // 只修正**界面显示**，不擅自替用户重新打开（开关的主动权在用户手里）。
  setInterval(async () => {
    if (!voiceTextEl) return;
    try {
      const st = await api("/api/voice/status");
      const running = !!(st && st.running);
      if (!running && voiceOn) {
        voiceOn = false;
        voiceUI("idle", "⚠ 语音已停止 · 点 🎤 可重新开始");
      } else if (running && !voiceOn) {
        voiceOn = true;
        voiceUI(st.state === "awake" ? "awake" : "listening");
        if (!voiceDevLoaded) voiceLoadDevices();
      }
    } catch (e) { /* 忽略：下次再对账 */ }
  }, 20000);

  // ---------- 初始化 ----------
  // 先恢复会话（含上次的历史消息），再跑其它轮询
  (async () => {
    const list = await loadSessions();
    if (sessionId) {
      try {
        const d = await api("/api/sessions/" + sessionId);
        const msgs = d.messages || [];
        if (msgs.length && renderHistory(msgs) > 0) {
          showToast("已恢复上次的对话记录", "ok");
        }
      } catch (e) { /* 恢复失败则用空白会话 */ }
    }
    renderSessions(list);
  })();

  // ---------- 界面自更新：改了前端不用用户手动刷新 ----------
  // 这个应用是 WebView2 窗口，**没有地址栏、也没有刷新按钮** ——
  // 原来每次改完界面都要让用户"自己刷新一下才看得到"，等于把开发成本转嫁给用户。
  // 现在后台比对前端的版本号（mtime+size），变了就自己重载。
  //
  // ⚠️ 三条**必须满足**才敢重载，否则宁可不更新：
  //   ① 没有正在生成 —— 中途重载会把这一轮回答打断，用户会以为"又崩了"；
  //   ② 输入框是空的 —— 否则用户打了一半的字会被冲掉；
  //   ③ 开发台没有未保存的改动 —— 否则改了一半的文件会丢。
  // 不满足就把 pendingReload 挂起，等下一轮轮询条件够了再重载（回答结束后会自动恢复聊天记录）。
  let feVersion = "";
  let pendingReload = false;
  let reloading = false;

  const canReload = () => {
    if (streaming) return false;
    const box = document.getElementById("input");
    if (box && box.value.trim()) return false;
    if (window.Studio && window.Studio.hasUnsaved && window.Studio.hasUnsaved()) return false;
    return true;
  };

  async function checkFrontendVersion() {
    if (reloading) return;
    let v = "";
    try {
      const d = await api("/api/frontend/version");
      v = d.version || "";
    } catch (e) { return; }          // 探测失败不影响使用
    if (!feVersion) { feVersion = v; return; }
    if (v === feVersion) return;
    feVersion = v;
    if (!canReload()) {
      if (!pendingReload) {
        pendingReload = true;
        showToast("界面有新版本，等这一轮结束会自动更新", "ok");
      }
      return;
    }
    reloading = true;
    showToast("界面已更新，正在重新加载…", "ok");
    // 给 toast 一点时间被看见，再重载（重载后会恢复聊天记录）
    setTimeout(() => location.reload(), 900);
  }

  setInterval(checkFrontendVersion, 3000);
  // 挂起中时，每轮生成结束后立刻再试一次（不用干等下一个 3 秒）
  setInterval(() => { if (pendingReload) checkFrontendVersion(); }, 1200);

  // ---------- 绘图计算设备：徽标 + 切换菜单 ----------
  // 文生图与图片微改共用同一个 torch 环境，设备一致。
  // 切换是"有后果"的操作：后端会在新设备上跑一次真实运算、并把绘图引擎
  // 实际加载上去，两步都过了才算成功。所以这里必须把结果讲清楚，
  // 失败时尤其要说明**当前是什么模式**和**为什么失败**。
  let deviceSwitching = false;

  function renderDeviceBadge(kind, gpu) {
    const text = $("#deviceBadgeText");
    if (!text) return;
    text.textContent = kind === "gpu"
      ? ("⚡ GPU 加速" + (gpu ? "（" + gpu + "）" : ""))
      : "🖥 CPU 模式";
    const badge = $("#deviceBadge");
    badge.classList.toggle("gpu", kind === "gpu");
  }

  // 标记哪一项是"当前选择"：forced 是用户的选择（auto/cpu/gpu），
  // kind 是实际生效的设备（cpu/gpu）。两者要分开显示，
  // 否则"自动 → 实际 GPU"会被误标成"手动选了 GPU"。
  const MODE_LABEL = { auto: "自动（推荐）", gpu: "GPU 加速", cpu: "CPU 模式" };

  function markDeviceOptions(forced, kind) {
    ["auto", "gpu", "cpu"].forEach((m) => {
      const el = $("#devState" + m.charAt(0).toUpperCase() + m.slice(1));
      if (el) {
        // 「自动」选中时补一句实际用的是什么，避免用户猜
        el.textContent = (m === forced)
          ? (m === "auto" ? "当前·" + (kind === "gpu" ? "GPU" : "CPU") : "当前")
          : "";
      }
      const btn = document.querySelector(`.dev-opt[data-mode="${m}"]`);
      if (btn) btn.classList.toggle("active", m === forced);
    });
  }

  function setDeviceStatus(html, cls) {
    const el = $("#deviceStatus");
    if (!el) return;
    el.innerHTML = html || "";
    el.className = "dev-status" + (cls ? " " + cls : "");
    el.hidden = !html;
  }

  async function loadDevice() {
    const badge = $("#deviceBadge");
    if (!badge) return;
    try {
      const d = await api("/api/t2i/capability");
      renderDeviceBadge(d.kind, d.gpu);
      markDeviceOptions(d.forced || "auto", d.kind);
      badge.title = (d.kind === "gpu"
        ? `绘图使用显卡加速：${d.gpu || "未知型号"}`
        : `绘图使用 CPU，单张约 20 秒`)
        + `\ntorch ${d.torch || "?"}`
        + `\n选择：${MODE_LABEL[d.forced] || d.forced}`
        + (d.reason ? `\n${d.reason}` : "")
        + "\n点击可切换设备";
      badge.classList.remove("loading");
    } catch (e) {
      const text = $("#deviceBadgeText");
      if (text) text.textContent = "设备未知";
    }
  }

  async function switchDevice(mode) {
    if (deviceSwitching) return;
    deviceSwitching = true;
    const badge = $("#deviceBadge");
    badge.classList.add("loading");
    const label = MODE_LABEL[mode] || mode;
    setDeviceStatus(`⏳ 正在切换到「${label}」，并校验设备可用性…`, "busy");
    try {
      const r = await api("/api/t2i/device", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      if (r.ok) {
        renderDeviceBadge(r.mode, r.gpu);
        markDeviceOptions(r.forced || mode, r.mode);
        const extra = r.note ? `<div class="dev-sub">${r.note}</div>` : "";
        setDeviceStatus(
          `<div class="dev-ok">✅ 已切换到「${label}」</div>`
          + `<div class="dev-sub">绘图实际使用：<b>`
          + `${r.mode === "gpu" ? "GPU 加速" : "CPU 模式"}`
          + `${r.gpu ? "（" + r.gpu + "）" : ""}</b>　已通过运行校验</div>` + extra,
          "ok");
        showToast(`设备已切换：${label}`, "ok");
      } else {
        const cur = r.mode === "gpu" ? "GPU 加速" : "CPU 模式";
        setDeviceStatus(
          `<div class="dev-fail">❌ 切换到「${label}」失败</div>`
          + `<div class="dev-sub">当前仍是：<b>${cur}</b></div>`
          + `<div class="dev-sub">失败原因：${r.reason || "未知"}</div>`, "fail");
        markDeviceOptions(r.forced || r.mode, r.mode);
        renderDeviceBadge(r.mode, r.gpu);
        showToast(`切换到「${label}」失败，当前为 ${cur}`, "warn");
      }
    } catch (e) {
      setDeviceStatus(`<div class="dev-fail">❌ 切换失败：${e.message || e}</div>`, "fail");
    } finally {
      deviceSwitching = false;
      badge.classList.remove("loading");
    }
  }

  (function bindDeviceMenu() {
    const badge = $("#deviceBadge");
    const menu = $("#deviceMenu");
    if (!badge || !menu) return;
    badge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      menu.hidden = !menu.hidden;
      if (!menu.hidden) {
        // 打开时刷新一下当前状态，并清掉上一次的结果（避免误读成这次的）
        setDeviceStatus("");
        loadDevice();
      }
    });
    menu.querySelectorAll(".dev-opt").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        switchDevice(btn.dataset.mode);
      });
    });
    // 点别处收起；切换进行中不收起（要看结果）
    document.addEventListener("click", () => {
      if (!deviceSwitching) menu.hidden = true;
    });
    menu.addEventListener("click", (ev) => ev.stopPropagation());
  })();

  refreshHealth();
  loadToggles();
  loadMemory();
  loadKb();
  loadDoclib();
  loadLibrary();
  loadDevice();
  setInterval(refreshHealth, 5000);
})();