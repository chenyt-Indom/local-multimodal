/* 本地多模态助手 —— 前端逻辑 */
(() => {
  const $ = (s) => document.querySelector(s);
  let images = [];          // 待发送附件 base64（含 data: 前缀）
  let streaming = false;
  const history = [];       // 会话消息（用于多轮上下文）

  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const temperature = $("#temperature");
  const numCtx = $("#numCtx");
  const maxTokens = $("#maxTokens");

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

  // ---------- 状态检测 ----------
  async function refreshHealth() {
    try {
      const r = await fetch("/api/health");
      const d = await r.json();
      const dot = $("#statusDot"), t = $("#statusTitle"), det = $("#statusDetail");
      if (d.online) {
        dot.className = "dot" + (d.model_ready ? " on" : " off");
        t.textContent = d.model_ready ? "模型就绪（离线）" : "Ollama 在线，模型未下载";
        det.textContent = d.model_ready ? `默认模型：${d.model}` : `请下载模型 ${d.model}`;
      } else {
        dot.className = "dot off";
        t.textContent = "Ollama 未运行";
        det.textContent = "请先启动 ollama serve";
      }
      loadModels();
    } catch { /* 后端未就绪时忽略 */ }
  }

  async function loadModels() {
    try {
      const r = await fetch("/api/models");
      const d = await r.json();
      if (d.ok) {
        const sel = $("#modelSelect");
        sel.innerHTML = "";
        d.models.forEach((m) => {
          const o = document.createElement("option");
          o.value = m.name; o.textContent = m.name;
          sel.appendChild(o);
        });
        $("#refreshModels").disabled = false;
      }
    } catch { $("#refreshModels").disabled = true; }
  }

  async function loadConfig() {
    try {
      const r = await fetch("/api/config");
      const d = await r.json();
      const c = d.config;
      temperature.value = c.temperature; $("#tempVal").textContent = c.temperature;
      numCtx.value = c.num_ctx; maxTokens.value = c.max_tokens;
    } catch {}
  }

  // ---------- 事件 ----------
  document.getElementById("refreshModels").onclick = loadModels;

  document.getElementById("pullModel").onclick = async (e) => {
    const btn = e.target; btn.disabled = true;
    const tip = showThinking("正在下载模型 qwen3-vl:8b，约 8GB，请耐心等待…");
    try {
      const r = await fetch("/api/pull", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}) });
      const d = await r.json();
      tip.querySelector(".bubble").textContent = d.ok ? "✅ " + d.message : "❌ " + (d.detail || "下载失败");
    } catch (err) {
      tip.querySelector(".bubble").textContent = "❌ " + err.message;
    }
    btn.disabled = false;
    refreshHealth();
  };

  document.getElementById("saveConfig").onclick = async () => {
    const body = { temperature: parseFloat(temperature.value), num_ctx: parseInt(numCtx.value, 10),
      max_tokens: parseInt(maxTokens.value, 10) };
    await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const t = $("#saveTip"); t.style.display = "block";
    setTimeout(() => t.style.display = "none", 1500);
  };

  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("hidden");

  // 上传图片
  $("#attachBtn").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
    [...e.target.files].forEach((f) => {
      const reader = new FileReader();
      reader.onload = () => {
        images.push(reader.result);
        renderAttachments();
      };
      reader.readAsDataURL(f);
    });
    e.target.value = "";
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
  }

  temperature.oninput = () => $("#tempVal").textContent = temperature.value;

  // ---------- 发送 / 流式接收 ----------
  async function send() {
    const text = inputEl.value.trim();
    const imgB64 = images.map((s) => s.split(",")[1]);
    if ((!text && imgB64.length === 0) || streaming) return;
    const promptText = text || "请详细描述这张图片的内容"; // 只发图片时的默认指令

    // 界面追加
    addMsg("user", promptText);
    images.forEach((src) => {
      const el = document.createElement("div"); el.className = "msg user";
      el.innerHTML = `<div class="bubble" style="padding:6px"><img src="${src}" style="max-width:220px;border-radius:8px"></div>`;
      messagesEl.appendChild(el);
    });
    images = []; renderAttachments();
    inputEl.value = ""; autoGrow();

    // 更新上下文
    history.push({ role: "user", content: promptText });

    const think = showThinking("本地模型思考中…");
    streaming = true;
    $("#sendBtn").disabled = true;

    try {
      const resp = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, images_b64: imgB64.length ? imgB64 : null, stream: true }) });
      if (!resp.ok) {
        const e = await resp.json();
        throw new Error(e.detail || resp.statusText);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "", answer = "";
      const botBubble = think.querySelector(".bubble");
      botBubble.classList.remove("thinking");
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const obj = JSON.parse(line);
            if (obj.message && obj.message.content) {
              answer += obj.message.content;
              botBubble.textContent = answer + "▌";
            }
            if (obj.done) break;
          } catch {}
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      botBubble.textContent = answer;
      if (answer) history.push({ role: "assistant", content: answer });
    } catch (err) {
      think.querySelector(".bubble").textContent = "❌ " + err.message + "（可能内存/模型未就绪，请查看状态）";
    } finally {
      streaming = false; $("#sendBtn").disabled = false;
    }
  }

  $("#sendBtn").onclick = send;
  inputEl.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = inputEl.scrollHeight + "px"; }
  inputEl.addEventListener("input", autoGrow);

  // ---------- 初始化 ----------
  refreshHealth();
  loadConfig();
  setInterval(refreshHealth, 5000);
})();