/* 本地多模态助手 —— 前端逻辑 */
(() => {
  const $ = (s) => document.querySelector(s);
  let images = [];          // 待发送附件 base64（含 data: 前缀）
  let docs = [];            // 待发送文档：[{name, text, chars}]（拖进来时抽取正文）
  let videoB64 = null;      // 待发送视频帧 base64 列表
  let streaming = false;
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
  function showToast(msg, kind = "") {
    let wrap = document.getElementById("toastWrap");
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
  function addMsg(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    el.innerHTML = `<div class="bubble"></div>`;
    el.querySelector(".bubble").textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, opts));
    const t = await r.text();
    try { return JSON.parse(t); } catch { return { raw: t }; }
  }

  // ---------- 状态检测 ----------
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
      // 若联网模式已开启，顺带校验当前网络（断网时给出提示）
      if (c.web_enabled) {
        const net = await checkNetwork();
        if (!net.online) showToast("电脑未连接网络，联网搜索将不可用", "warn");
      }
    } catch {}
  }
  async function saveToggles() {
    const body = {
      memory_enabled: $('.pill[data-cfg="memory_enabled"]').classList.contains("on"),
      rag_enabled: $('.pill[data-cfg="rag_enabled"]').classList.contains("on"),
      web_enabled: $('.pill[data-cfg="web_enabled"]').classList.contains("on"),
      auto_memorize: $('.pill[data-cfg="auto_memorize"]').classList.contains("on"),
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

  document.querySelectorAll(".pill").forEach((p) => {
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
        showToast(p.classList.contains("on") ? "已开启联网模式" : "已关闭联网模式",
                  p.classList.contains("on") ? "ok" : "");
      }
    };
  });

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
    const list = files ? [...files] : [];
    if (!list.length) {
      // 别静默失败：拖进来却什么都没发生，用户只会以为功能坏了
      showToast("没有读到文件。请从资源管理器把文件直接拖到窗口里再松开。", "warn");
      return;
    }
    list.forEach((f) => {
      if (!f) return;
      if (f.type && f.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = () => { images.push(reader.result); renderAttachments(); };
        reader.readAsDataURL(f);
      } else if (/\.(mp4|avi|mkv|mov|webm|flv|wmv|m4v|ts)$/i.test(f.name || "")) {
        // 视频：交给底部按钮处理逻辑保持一致（复用 videoInput）
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
  const dropHint = $("#dropHint");
  ["dragover", "dragenter"].forEach((ev) => mainEl.addEventListener(ev, (e) => {
    e.preventDefault();
    if (dropHint) dropHint.classList.add("show");
  }));
  ["dragleave", "drop"].forEach((ev) => mainEl.addEventListener(ev, (e) => {
    e.preventDefault();
    if (dropHint) dropHint.classList.remove("show");
  }));
  mainEl.addEventListener("drop", (e) => {
    const dt = e.dataTransfer;
    let files = (dt && dt.files) ? [...dt.files] : [];
    if (!files.length && dt && dt.items) {
      // 少数环境下 files 是空的，得从 items 里捞（必须在任何 await 之前同步取）
      for (const it of dt.items) {
        if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
      }
    }
    onDropFiles(files);
  });

  // 拖到窗口任意位置都不要让浏览器"直接打开"这个文件 ——
  // 否则整个界面会被替换成文件内容，看起来像把应用弄坏了。
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

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
      const tag = m.origin === "web" ? "🌐" : (m.origin === "gen" ? "🎨" : "🖼️");
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
      docs.forEach((doc) => {
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
        row.querySelector(".kb-del").onclick = async () => {
          if (!confirm(`从知识库移除《${doc.filename}》？\n（磁盘上的文件也会一起删掉）`)) return;
          await api("/api/kb/" + encodeURIComponent(doc.filename), { method: "DELETE" });
          loadKb();
        };
        list.appendChild(row);
      });
    } catch (e) { /* 静默 */ }
  }

  (function bindKbPanel() {
    const open = $("#kbOpenFolder");
    if (open) open.onclick = () =>
      openFolderRequest("/api/kb/open_folder", { method: "POST" });
    const rf = $("#kbRefresh");
    if (rf) rf.onclick = loadKb;
  })();

  // ---------- 多会话管理 ----------
  // 每个会话互相独立（各自的消息与上下文），全部持久化在后端，
  // 程序重启后自动恢复上次使用的会话。
  const sessionsListEl = $("#sessionsList");

  // 界面只渲染最近这么多条消息，更早的折叠（要点由长期记忆承载）；
  // 上下文同样只带最近这些，避免越聊越慢。
  const RECENT_SHOW = 20;

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
    if (!sessionId || !list.some((s) => s.id === sessionId)) {
      setSessionId(list.length ? list[0].id : "");
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
  async function loadMemory() {
    const lng = $("#memLongText"), sht = $("#memShortText");
    if (!lng || !sht) return;
    try {
      const d = await api("/api/memory?session_id=" + encodeURIComponent(sessionId || ""));
      if ((d.session_id || "") !== (sessionId || "")) return;   // 用户已切走，别覆盖
      lng.value = d.long || "";
      sht.value = d.short || "";
      const lm = $("#memLongMeta");
      if (lm) lm.textContent = `${(d.long || "").length} / ${d.long_cap || 2000} 字`;
      const sm = $("#memShortMeta");
      if (sm) sm.textContent = (d.short || "").length
        ? `${(d.short || "").length} / ${d.short_cap || 1000} 字`
        : "这个对话还没有短期记忆";
      const nm = $("#memSessName");
      if (nm) nm.textContent = await currentTitle(sessionId);
    } catch (e) { /* 静默：面板不可用不影响聊天 */ }
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

  // ---------- 附件：图片 / 视频 ----------
  $("#attachBtn").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
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
    loadMemory();   // 对话后同步 AI 写入的记忆文段
  }

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
          const STEP = 5;                       // 每帧播放字速
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
    const addMedia = (ui) => {
      const isWeb = ui.origin === "web";
      const card = document.createElement("div");
      card.className = "media-card";
      const mime = ui.mime || "image/png";
      const b64 = ui.b64 || "";
      // 明确区分「网上搜到的」与「AI 生成的」，避免混淆
      const badge = isWeb ? "🌐 网上搜到的" : (ui.origin === "gen" ? "🎨 AI 生成" : "🖼️ 图片");
      const caption = (ui.prompt || "").slice(0, 60);
      card.innerHTML = `
        <div class="media-cap"><span class="badge ${isWeb ? "web" : "gen"}">${badge}</span>${
          caption ? " " + caption : ""}</div>
        <img src="data:${mime};base64,${b64}">
        <div class="media-meta">${
          ui.source ? `<a href="${ui.source}" target="_blank" rel="noopener">来源页</a> · ` : ""}${
          ui.device ? "🖥 " + ui.device + " " : ""}${ui.cost_s ? "⏱ " + ui.cost_s + "s" : ""}${
          ui.size ? " · " + ui.size : ""}</div>
        <div class="media-actions"><button class="btn sm ghost lib-save">⭐ 保存到图库</button></div>`;
      card.querySelector("img").onclick = (e) => {
        e.stopPropagation();
        showLightbox(mime, b64, badge + (caption ? "：" + caption : ""));
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
    aborted = false;
    $("#sendBtn").disabled = true;
    setStopVisible(true);
    abortCtl = new AbortController();
    try {
      const resp = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        signal: abortCtl.signal,
        body: JSON.stringify({ messages: history, images_b64: mediaB64,
                               docs: (docPayload && docPayload.length) ? docPayload : null,
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
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let obj;
          try { obj = JSON.parse(line); } catch { continue; }
          if (obj.error) throw new Error(obj.error);
          if (obj.message) {
            if (obj.message.thinking) {
              pushThinking(obj.message.thinking);  // 进入平滑播放器，逐字实时渲染
            }
            if (obj.message.content) { answer += obj.message.content; answerBubble.textContent = answer + "▌"; }
          }
          if (obj.tool_start) addToolChip(obj.tool_start.name);
          // 一轮里同时发起多个工具时给个提示 —— 让用户知道这是并行执行、在省时间
          if (obj.tool_parallel) {
            addToolChip(`⚡ 并行执行 ${obj.tool_parallel} 个工具`);
          }
          if (obj.ui) {
            if (obj.ui.type === "sources") sourcesData = obj.ui;   // 稍后在回答下方渲染
            else addMedia(obj.ui);
          }
          if (obj.note) { notes.push(obj.note); showToast(obj.note, "warn"); }
          if (obj.done) break;
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      finishThinking();
      answerBubble.textContent = answer;
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
        answerBubble.textContent = answer ? answer + "\n\n〔已终止〕" : "〔已终止〕";
        if (answer) history.push({ role: "assistant", content: answer });
        showToast("已终止本次生成");
        try { persistSession(); } catch (e) { /* 落盘失败不影响使用 */ }
      } else {
        answerBubble.textContent = "❌ " + err.message + "（可能内存/模型未就绪，请查看状态）";
      }
    } finally {
      streaming = false;
      abortCtl = null;
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
  let voiceWs = null;
  let voiceOn = false;

  function voiceUI(state, text) {
    if (!voiceHintEl || !voiceTextEl) return;
    voiceHintEl.classList.toggle("on", state === "listening");
    voiceHintEl.classList.toggle("awake", state === "awake");
    if (voiceBtn) voiceBtn.classList.toggle("active", state !== "idle");
    if (text) {
      voiceTextEl.textContent = text;
    } else if (state === "awake") {
      voiceTextEl.textContent = "🎙 已唤醒 · 请说内容（停顿 2 秒自动发送）";
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
      } else if (msg.type === "wake") {
        voiceUI("awake");
      } else if (msg.type === "partial") {
        voiceUI("awake", "🎙 " + (msg.text || "…"));
        inputEl.value = msg.text || "";
        autoGrow();
      } else if (msg.type === "final") {
        inputEl.value = msg.text || "";
        autoGrow();
        voiceUI("listening", "✅ 已识别，自动发送…");
        if (msg.auto && inputEl.value.trim()) setTimeout(() => send(), 120);
      } else if (msg.type === "status") {
        voiceUI(msg.state || "idle");
        if (msg.model_ready === false) voiceTextEl.textContent = "⚠ 未找到语音模型（asr_model 目录）";
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
    voiceBtn.onclick = () => {
      const ws = voiceConnect();
      const toggle = () => {
        if (!voiceOn) {
          ws.send(JSON.stringify({ action: "start" }));
          voiceOn = true;
          voiceUI("listening");
        } else {
          ws.send(JSON.stringify({ action: "stop" }));
          voiceOn = false;
          voiceUI("idle");
        }
      };
      if (ws.readyState === 1) toggle();
      else ws.addEventListener("open", toggle, { once: true });
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
      };
      if (ws.readyState === 1) onOpen();
      else ws.addEventListener("open", onOpen, { once: true });
    } catch (e) { /* 语音不可用时保持原样 */ }
  })();

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
  loadLibrary();
  loadDevice();
  setInterval(refreshHealth, 5000);
})();