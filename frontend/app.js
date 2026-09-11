/* 本地多模态助手 —— 前端逻辑 */
(() => {
  const $ = (s) => document.querySelector(s);
  let images = [];          // 待发送附件 base64（含 data: 前缀）
  let videoB64 = null;      // 待发送视频帧 base64 列表
  let streaming = false;
  const history = [];       // 会话消息（用于多轮上下文）
  // 会话标识（后端据此把历史会话落盘，供记忆检索）
  let sessionId = localStorage.getItem("ai_session_id") || (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("ai_session_id", sessionId);

  // 工具名 → 中文展示
  const TOOL_LABELS = {
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
  };

  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const mainEl = $("#main");

  // ---------- 基础工具 ----------
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
  document.querySelectorAll(".pill").forEach((p) => {
    p.onclick = async () => {
      p.classList.toggle("on");
      await saveToggles();
    };
  });

  // ---------- 事件 ----------
  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("hidden");

  // ---------- 拖拽图片到聊天框 ----------
  function onDropFiles(files) {
    if (!files) return;
    [...files].forEach((f) => {
      if (f && f.type && f.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = () => { images.push(reader.result); renderAttachments(); };
        reader.readAsDataURL(f);
      } else if (f && /\.(mp4|avi|mkv|mov|webm|flv|wmv|m4v|ts)$/i.test(f.name || "")) {
        // 视频：交给底部按钮处理逻辑保持一致（复用 videoInput）
        const dt = new DataTransfer(); dt.items.add(f);
        $("#videoInput").files = dt.files;
        $("#videoInput").dispatchEvent(new Event("change"));
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
    onDropFiles(e.dataTransfer && e.dataTransfer.files);
  });

  // ---------- 记忆库 · 文段式 ----------
  async function loadMemory() {
    try {
      const d = await api("/api/memory");
      const box = $("#memoryList");
      box.innerHTML = "";
      if (!d.ok || !d.sections || !d.sections.length) {
        box.innerHTML = '<p class="hint">暂无记忆文段。AI 会在对话中按分区文段自动写入，也可手动新增。</p>';
        return;
      }
      d.sections.forEach((s) => {
        const item = document.createElement("div");
        item.className = "sec-item";
        item.innerHTML = `
          <input class="sec-title" value="${esc(s.title || "")}" placeholder="分区标题" />
          <textarea class="sec-content" rows="5" placeholder="该分区的文段内容…">${esc(s.content || "")}</textarea>
          <div class="sec-meta">
            <span class="sec-time"></span>
            <button class="sec-save btn sm">💾 保存</button>
            <button class="sec-del btn sm ghost danger">删除</button>
          </div>`;
        const tEl = item.querySelector(".sec-title");
        const cEl = item.querySelector(".sec-content");
        const timeEl = item.querySelector(".sec-time");
        try {
          timeEl.textContent = "更新 " + new Date((s.updated_at || 0) * 1000).toLocaleString();
        } catch {}
        item.querySelector(".sec-save").onclick = async () => {
          await api("/api/memory/" + s.id, { method: "PUT", body: JSON.stringify({ title: tEl.value, content: cEl.value }) });
          loadMemory();
        };
        item.querySelector(".sec-del").onclick = async () => {
          if (confirm("删除该分区文段？")) { await api("/api/memory/" + s.id, { method: "DELETE" }); loadMemory(); }
        };
        box.appendChild(item);
      });
    } catch {}
  }
  $("#secAdd").onclick = async () => {
    const t = $("#secTitleInput").value.trim();
    if (!t) { alert("请填写分区标题"); return; }
    await api("/api/memory", { method: "POST", body: JSON.stringify({ title: t, content: "" }) });
    $("#secTitleInput").value = "";
    loadMemory();
  };
  $("#memRefresh").onclick = loadMemory;
  $("#memClearAll").onclick = async () => {
    if (confirm("确认清空全部记忆文段？")) { await api("/api/memory", { method: "DELETE" }); loadMemory(); }
  };

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
    if (videoB64) {
      const d = document.createElement("div"); d.className = "attachment vid";
      d.innerHTML = `<span style="font-size:22px">🎬</span><span class="x">✕</span>`;
      d.querySelector(".x").onclick = () => { videoB64 = null; renderAttachments(); };
      box.appendChild(d);
    }
  }

  // ---------- 发送 / 流式接收 ----------
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
        const d = await r.json();
        if (d.ok) { lastPath = d.path; cap.textContent = "✅ 已保存：" + d.path; }
        else cap.textContent = "❌ 保存失败：" + (d.detail || "");
      } catch (err) {
        cap.textContent = "❌ 保存失败：" + err;
      }
    };
    ov.querySelector(".lb-open").onclick = async (e) => {
      e.stopPropagation();
      try {
        const r = await fetch("/api/open_folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: lastPath }),
        });
        const d = await r.json();
        cap.textContent = d.ok ? "📂 已打开：" + d.path : ("❌ " + (d.detail || ""));
      } catch (err) {
        cap.textContent = "❌ " + err;
      }
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
    images = []; videoB64 = null; renderAttachments();
    inputEl.value = ""; autoGrow();
    await doSend(promptText, media.length ? media : null);
    loadMemory();   // 对话后同步 AI 写入的记忆文段
  }

  async function doSend(promptText, mediaB64) {
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
      else thinkStatus.textContent = "🔧 正在调用工具：" + (TOOL_LABELS[name] || name);
    };
    const addMedia = (ui) => {
      const card = document.createElement("div");
      card.className = "media-card" + (ui.prompt ? " gen" : "");
      const mime = ui.mime || "image/png";
      const caption = ui.prompt ? ("🎨 " + ui.prompt) : "🖼 已读取";
      card.innerHTML = `<div class="media-cap gen-label">${caption}</div>
        <img src="data:${mime};base64,${ui.b64}"><div class="media-meta">${
          ui.device ? "🖥 " + ui.device : ""} ${ui.cost_s ? "⏱ " + ui.cost_s + "s" : ""} · 点击可放大</div>`;
      card.querySelector("img").onclick = (e) => {
        e.stopPropagation();
        showLightbox(mime, ui.b64, caption);
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
    $("#sendBtn").disabled = true;
    try {
      const resp = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, images_b64: mediaB64, stream: true, session_id: sessionId }) });
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
          if (obj.ui) addMedia(obj.ui);
          if (obj.done) break;
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      finishThinking();
      answerBubble.textContent = answer;
      if (!answer) { answerWrap.style.display = "none"; answer = "（模型未给出文字回答）"; }
      if (answer) history.push({ role: "assistant", content: answer });
    } catch (err) {
      finishThinking();
      answerBubble.textContent = "❌ " + err.message + "（可能内存/模型未就绪，请查看状态）";
    } finally {
      streaming = false; $("#sendBtn").disabled = false;
    }
  }

  $("#sendBtn").onclick = send;
  inputEl.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = inputEl.scrollHeight + "px"; }
  inputEl.addEventListener("input", autoGrow);

  // ---------- 语音输入（唤醒词「西派西派」+ 静音自动发送）----------
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
      voiceTextEl.textContent = "👂 监听中 · 说「西派西派」唤醒";
    } else {
      voiceTextEl.textContent = "语音未开启 · 点 🎤 后说「西派西派」唤醒";
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

  // ---------- 初始化 ----------
  refreshHealth();
  loadToggles();
  loadMemory();
  setInterval(refreshHealth, 5000);
})();