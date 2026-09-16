/* 开发台（Dev Studio）—— 人机协同开发
 *
 * 和聊天**并列**（不是全屏浮层）：打开时聊天区仍然完整可用，
 * 可以一边改代码一边跟模型对话；收起就回到原来的纯聊天界面。
 *
 * 组成：
 *   · 左：项目切换 + 文件树 + 「AI 改动」列表（可点开 diff、可撤销）
 *   · 中：离线 Monaco 编辑器（VS Code 内核）+ 多标签 + 运行输出
 *   · 上：导入 / 保存 / 运行 / 预览 / 部署 / 打包 / 交给 AI
 *
 * Monaco 从 /static/vendor/monaco 本地加载 —— **不连任何 CDN**，断网可用。
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var files = [];           // [{rel,size,ext}]
  var tabs = [];            // 已打开的 rel
  var cur = "";             // 当前 rel
  var dirty = {};           // rel -> true
  var models = {};          // rel -> monaco model
  var editor = null;
  var diffEditor = null;
  var diffModels = null;
  var monacoRef = null;
  var loading = null;
  var opened = false;
  var project = "";
  var changes = [];

  // ---------- 极简 API（app.js 的 api() 在闭包内，拿不到） ----------
  function jget(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function jpost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error((d && d.detail) || ("HTTP " + r.status));
        return d;
      });
    });
  }
  function toast(msg, kind) {
    if (window.__toast) { window.__toast(msg, kind); }
    else { console.log("[studio]", msg); }
  }

  /* 自建输入框 —— ⚠️ WebView2 里**没有 window.prompt()**，调用会静默失败。 */
  function askText(title, placeholder, def) {
    return new Promise(function (resolve) {
      var wrap = document.createElement("div");
      wrap.className = "st-dialog-mask";
      wrap.innerHTML =
        '<div class="st-dialog"><div class="st-dialog-title"></div>' +
        '<input class="st-dialog-input" type="text" />' +
        '<div class="st-dialog-acts">' +
        '<button class="btn sm ghost st-dialog-cancel">取消</button>' +
        '<button class="btn sm st-dialog-ok">确定</button></div></div>';
      wrap.querySelector(".st-dialog-title").textContent = title;
      var inp = wrap.querySelector(".st-dialog-input");
      inp.placeholder = placeholder || "";
      inp.value = def || "";
      function done(v) {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        resolve((v || "").trim());
      }
      wrap.querySelector(".st-dialog-ok").onclick = function () { done(inp.value); };
      wrap.querySelector(".st-dialog-cancel").onclick = function () { done(""); };
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); done(inp.value); }
        if (e.key === "Escape") { e.preventDefault(); done(""); }
      });
      wrap.addEventListener("mousedown", function (e) {
        if (e.target === wrap) done("");
      });
      document.body.appendChild(wrap);
      inp.focus();
    });
  }

  // ---------- 编辑器：**内置的真 VS Code**（code-server），直接嵌在这个界面里 ----------
  // 演变过程：Monaco → 本机 PyCharm（独立窗口）→ **回到内置 VS Code**。
  //
  // 结论：**能"嵌进网页"的专业 IDE 本来就极少**，VS Code 系（code-server）
  // 是其中最成熟的 —— 有终端 / 断点调试 / 变量监视 / Git 面板，且全程离线。
  // PyCharm 是桌面程序，结构上就嵌不进来（JetBrains 的 Projector 已停更、
  // Code With Me 也在 2026.1 日落），只能开独立窗口，用起来要来回切。
  //
  // 而"AI 写的代码怎么自动呈现"这件事，内置 VS Code 反而更好做：
  // 后端会 `code-server <文件>`（走 socket 转发给运行中的实例）去**主动打开**该文件，
  // 它就嵌在当前窗口里，不用切。
  var vsReady = false;

  function vsMsg(text) {
    var m = $("stVsMsg"), c = $("stVsCover");
    if (m) m.textContent = text;
    if (c) c.hidden = !text;
  }

  function vsFrame(url) {
    var f = $("stVs");
    if (f && url && f.getAttribute("src") !== url) f.setAttribute("src", url);
    vsReady = true;
    vsMsg("");
  }

  /* 确保内置 VS Code 已就绪、且开的就是当前项目。
     ⚠️ code-server 是**启动时绑定目录**的，切项目不会自己跟着换 ——
     不比对项目的话，用户切了项目看到的还是上一个项目的文件，会以为文件丢了。 */
  async function ensureVs(force) {
    var st = {};
    try { st = await jget("/api/ide/status"); } catch (e) { st = {}; }
    if (!st.installed) {
      vsMsg("没有找到内置 VS Code。\n\n获取方式见 vendor/ 说明，或使用 Docker 版（已内置）。");
      return;
    }
    if (!force && st.running && st.project === project) { vsFrame(st.url); return; }
    if (st.running) { vsMsg("正在切换到项目 " + project + " …"); await jpost("/api/ide/stop", {}); }
    else vsMsg("正在启动内置 VS Code…（第一次要几秒）");
    var f = $("stVs");
    if (f) f.setAttribute("src", "about:blank");
    try {
      var d = await jpost("/api/ide/start", {});
      if (!d.ok) { vsMsg("启动失败：" + (d.error || "未知错误")); return; }
      vsFrame(d.url);
    } catch (e) { vsMsg("启动失败：" + String(e.message || e)); }
  }

  // --- 备用：本机 PyCharm（嵌不进来，只能开独立窗口）---
  var ideInfo = { name: "", path: "" };

  /* 问后端：本机装了什么 IDE、项目在哪个目录。**只探测，不启动** ——
     用户没点之前不该擅自弹出个 PyCharm 窗口。 */
  async function probeIde() {
    try {
      var d = await jget("/api/ws/ide/status");
      ideInfo = { name: d.name || "", path: d.path || "" };
    } catch (e) { ideInfo = { name: "", path: "" }; }
    renderIdePanel();
  }

  function renderIdePanel() {
    var t = $("stIdeDesc"), p = $("stIdePath"), b = $("stIdeOpen"), btn = $("stPyCharm");
    var nm = ideInfo.name || "PyCharm";
    if (t) {
      t.textContent = ideInfo.name
        ? "已检测到本机 " + ideInfo.name + "。项目就在本机磁盘上，"
          + "那边改的就是同一批文件（不是副本）。AI 写的文件它会自己同步。"
        : "本机没检测到 PyCharm / VS Code。可以先用「📂 资源管理器」打开项目目录，"
          + "或在设置里装一个。";
    }
    if (p) p.textContent = ideInfo.path || "";
    if (b) {
      b.textContent = "🧠 用 " + nm + " 打开";
      b.disabled = !ideInfo.name;
    }
    if (btn) btn.textContent = "🧠 " + nm;
  }

  /* 一键把当前项目交给本机 PyCharm。**不复制文件** —— 就是打开那个目录本身。 */
  async function openInIde() {
    try {
      var d = await jpost("/api/ws/open_ide", {});
      if (!d.ok) { toast(d.error || "没找到 PyCharm / VS Code"); return; }
      toast("已用 " + (d.ide || d.name || "PyCharm") + " 打开项目：" + (d.project || ""));
      if (d.path) showOut("已在本机 IDE 中打开",
        "项目目录：\n" + d.path + "\n\n" +
        "· 打开的就是**磁盘上这个目录**，和开发台改的是同一批文件（不是副本）；\n" +
        "· AI 写的文件它会自己同步过来（点一下 IDE 窗口让它获得焦点即可刷新）；\n" +
        "· 断点调试、变量监视、重构、代码导航都在 PyCharm 里用。");
    } catch (e) { toast("打开失败：" + String(e.message || e)); }
  }

  function themeName() {
    try {
      var bg = getComputedStyle(document.body).backgroundColor || "";
      var m = bg.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
      if (m) {
        var lum = (+m[1] * 0.299 + +m[2] * 0.587 + +m[3] * 0.114);
        return lum < 128 ? "vs-dark" : "vs";
      }
    } catch (e) { /* 忽略 */ }
    return "vs-dark";
  }

  var LANG = {
    py: "python", js: "javascript", ts: "typescript", json: "json",
    html: "html", htm: "html", css: "css", md: "markdown", txt: "plaintext",
    sh: "shell", bat: "bat", yml: "yaml", yaml: "yaml", xml: "xml",
    c: "c", h: "c", cpp: "cpp", java: "java", go: "go", rs: "rust",
    sql: "sql", toml: "ini", ini: "ini", csv: "plaintext", log: "plaintext"
  };
  function langOf(rel) {
    var ext = (rel.split(".").pop() || "").toLowerCase();
    return LANG[ext] || "plaintext";
  }

  // ---------- 项目 ----------
  async function loadProjects() {
    try {
      var d = await jget("/api/ws/projects");
      project = d.active || "";
      var sel = $("stProj");
      sel.innerHTML = "";
      (d.projects || []).forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.name;
        o.textContent = p.name + (p.files ? "  (" + p.files + ")" : "  (空)");
        sel.appendChild(o);
      });
      if (!sel.options.length) {
        var o2 = document.createElement("option");
        o2.value = project || "default";
        o2.textContent = o2.value;
        sel.appendChild(o2);
      }
      sel.value = project;
    } catch (e) { toast("读取项目列表失败：" + e.message); }
  }

  async function useProject(name) {
    try {
      var d = await jpost("/api/ws/projects/use", { name: name });
      if (!d.ok) { toast(d.error || "切换失败"); return; }
      project = d.active;
      // 切换项目 = 换一整套文件，打开的标签全部作废
      tabs.forEach(function (r) { if (models[r]) models[r].dispose(); });
      tabs = []; models = {}; dirty = {}; cur = "";
      if (editor) editor.setModel(null);
      renderTabs();
      await refresh();
      await loadChanges();
      toast("已切到项目：" + project);
      // ⚠️ **PyCharm 不会跟着换项目**（它是独立桌面程序，我们只能"为某个目录打开它"，
      // 换不了它当前开着的项目）。这里的当前项目一换，AI 的读写就都在新项目里，
      // 而用户眼前的 PyCharm 还停在上一个 —— 会以为"AI 把文件弄丢了 / 怎么报文件不存在"。
      // 实测就踩过：模型读到"文件不存在"，于是干脆不改了。
      // 所以换完项目必须提醒一句，把"两边对不上"这件事说破。
      toast("别忘了点「🧠 " + (ideInfo.name || "PyCharm") + "」让 " +
            (ideInfo.name || "PyCharm") + " 也切到这个项目", "warn");
      setTimeout(function () {
        toast("提示：AI 读写的是本项目（" + project + "），" +
              (ideInfo.name || "PyCharm") + " 需要你点一下按钮才会跟着切", "warn");
      }, 1600);
    } catch (e) { toast("切换失败：" + e.message); }
  }

  async function newProject() {
    var n = await askText("新建项目", "项目名，例如 my-site", "");
    if (!n) return;
    var d = await jpost("/api/ws/projects/new", { name: n });
    if (!d.ok) { toast(d.error || "新建失败"); return; }
    await loadProjects();
    await useProject(n);
  }

  async function delProject() {
    if (!confirm("删除项目「" + project + "」?\n\n会移进回收站（可以捞回来），但界面里就不显示了。")) return;
    var d = await jpost("/api/ws/projects/delete", { name: project });
    if (!d.ok) { toast(d.error || "删除失败"); return; }
    await loadProjects();
    await useProject(($("stProj") || {}).value || "");
  }

  // ---------- 文件树 ----------
  function iconOf(ext) {
    var e = (ext || "").toLowerCase();
    if (e === "py") return "🐍";
    if (e === "js" || e === "ts") return "🟨";
    if (e === "html" || e === "htm") return "🌐";
    if (e === "css") return "🎨";
    if (e === "json" || e === "yml" || e === "yaml" || e === "toml") return "⚙️";
    if (e === "md" || e === "txt") return "📄";
    if (e === "png" || e === "jpg" || e === "jpeg" || e === "gif" || e === "webp") return "🖼";
    return "📃";
  }

  function renderTree() {
    var box = $("stTree");
    box.innerHTML = "";
    if (!files.length) {
      var empty = document.createElement("div");
      empty.className = "st-empty";
      empty.textContent = "项目还是空的。点「📥 导入」把本地文件拖进来，" +
        "或直接在聊天里让 AI 写。";
      box.appendChild(empty);
      return;
    }
    var root = { children: {}, files: [] };
    files.forEach(function (f) {
      var segs = f.rel.split("/");
      var node = root;
      for (var i = 0; i < segs.length - 1; i++) {
        if (!node.children[segs[i]]) node.children[segs[i]] = { children: {}, files: [] };
        node = node.children[segs[i]];
      }
      node.files.push(f);
    });
    function build(node, depth) {
      Object.keys(node.children).sort().forEach(function (k) {
        var d = document.createElement("div");
        d.className = "st-dir";
        d.style.paddingLeft = (8 + depth * 12) + "px";
        d.textContent = "▸ " + k + "/";
        box.appendChild(d);
        build(node.children[k], depth + 1);
      });
      node.files.sort(function (a, b) { return a.rel < b.rel ? -1 : 1; }).forEach(function (f) {
        var d = document.createElement("div");
        d.className = "st-file" + (f.rel === cur ? " on" : "");
        d.style.paddingLeft = (8 + depth * 12) + "px";
        d.title = f.rel + "（" + f.size + " 字节）  ·  右键可重命名/删除";
        d.setAttribute("data-rel", f.rel);
        d.innerHTML = '<span class="st-ico">' + iconOf(f.ext) + '</span>' +
                      '<span class="st-name"></span>' +
                      (dirty[f.rel] ? '<span class="st-dot">●</span>' : "");
        d.querySelector(".st-name").textContent = f.rel.split("/").pop();
        d.onclick = function () { openFile(f.rel); };
        d.oncontextmenu = function (e) {
          e.preventDefault();
          fileMenu(f.rel, e.clientX, e.clientY);
        };
        box.appendChild(d);
      });
    }
    build(root, 0);
    var rec = document.createElement("div");
    rec.className = "st-rec";
    rec.textContent = "右键文件可「重命名 / 删除」；右键这块空白可新建/导入。" +
      "覆盖或删除的东西都进 _回收站/，不会真丢。";
    box.appendChild(rec);
  }

  /* ---------- 右键菜单 ----------
     之前只有"右键"这一个入口，而且文件那个还要你**手打 rename** —— 等于没有。
     现在做成正经的浮动菜单：项目（重命名/删除）和文件（打开/重命名/删除）都能点。 */
  function closeMenu() {
    var old = document.getElementById("stMenu");
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }

  function showMenu(x, y, items) {
    closeMenu();
    var m = document.createElement("div");
    m.className = "st-menu";
    m.id = "stMenu";
    items.forEach(function (it) {
      if (it.sep) {
        var s = document.createElement("div");
        s.className = "st-menu-sep";
        m.appendChild(s);
        return;
      }
      if (it.tip) {
        var tp = document.createElement("div");
        tp.className = "st-tip";
        tp.textContent = it.tip;
        m.appendChild(tp);
        return;
      }
      var b = document.createElement("div");
      b.className = "st-menu-item" + (it.danger ? " danger" : "");
      b.textContent = it.label;
      b.onmousedown = function (ev) { ev.stopPropagation(); };
      b.onclick = function (ev) {
        ev.stopPropagation();
        closeMenu();
        try { it.run && it.run(); } catch (e) { toast(String(e.message || e)); }
      };
      m.appendChild(b);
    });
    m.style.left = "-9999px";
    m.style.top = "-9999px";
    document.body.appendChild(m);
    var r = m.getBoundingClientRect();
    m.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    m.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  }

  function fileMenu(rel, x, y) {
    showMenu(x, y, [
      { label: "📄 打开", run: function () { openFile(rel); } },
      {
        label: "✏️ 重命名 / 移动", run: async function () {
          var to = await askText("重命名「" + rel + "」", "新路径（可以带目录，如 sub/x.py）", rel);
          if (!to || to === rel) return;
          var d = await jpost("/api/ws/rename", { rel: rel, to: to });
          if (!d.ok) { toast(d.error || "重命名失败"); return; }
          if (models[rel]) { models[rel].dispose(); delete models[rel]; }
          tabs = tabs.filter(function (r) { return r !== rel; });
          if (cur === rel) cur = "";
          await refresh(); await loadChanges();
          toast("已重命名为 " + d.rel);
        }
      },
      { sep: true },
      {
        label: "🗑 删除", danger: true, run: async function () {
          if (!confirm("删除「" + rel + "」?\n\n（会进 _回收站/，可以捞回来）")) return;
          var r = await jpost("/api/ws/delete", { rel: rel });
          if (!r.ok) { toast(r.error || "删除失败"); return; }
          if (models[rel]) { models[rel].dispose(); delete models[rel]; }
          tabs = tabs.filter(function (x2) { return x2 !== rel; });
          if (cur === rel) { cur = tabs.length ? tabs[tabs.length - 1] : ""; }
          renderTabs();
          await refresh(); await loadChanges();
          toast("已删除 " + rel + "（在回收站里）");
        }
      }
    ]);
  }

  function treeMenu(x, y) {
    showMenu(x, y, [
      { tip: "项目：" + project },
      { label: "＋ 新建文件", run: function () { newEntry("file"); } },
      { label: "＋ 新建文件夹", run: function () { newEntry("dir"); } },
      { sep: true },
      { label: "📥 导入本地文件", run: function () { $("stFileInput").click(); } },
      { label: "⟳ 刷新", run: function () { refresh(); loadChanges(); } }
    ]);
  }

  function projMenu(x, y) {
    showMenu(x, y, [
      { tip: "当前项目：" + project },
      {
        label: "✏️ 重命名项目", run: async function () {
          var to = await askText("重命名项目「" + project + "」", "新项目名", project);
          if (!to || to === project) return;
          var d = await jpost("/api/ws/projects/rename", { old: project, new: to });
          if (!d.ok) { toast(d.error || "重命名失败"); return; }
          tabs.forEach(function (r) { if (models[r]) models[r].dispose(); });
          tabs = []; models = {}; dirty = {}; cur = "";
          if (editor) editor.setModel(null);
          renderTabs();
          await loadProjects(); await refresh(); await loadChanges();
          toast("项目已重命名为 " + d.name);
        }
      },
      {
        label: "🗑 删除项目", danger: true, run: async function () {
          if (!confirm("删除项目「" + project + "」?\n\n" +
                       "整个项目会移进 _回收站/（可以捞回来），但界面上就不显示了。")) return;
          var d = await jpost("/api/ws/projects/delete", { name: project });
          if (!d.ok) { toast(d.error || "删除失败"); return; }
          tabs.forEach(function (r) { if (models[r]) models[r].dispose(); });
          tabs = []; models = {}; dirty = {}; cur = "";
          if (editor) editor.setModel(null);
          renderTabs();
          await loadProjects();
          await useProject(($("stProj") || {}).value || "");
          toast("项目已删除（在回收站里）");
        }
      },
      { sep: true },
      { label: "🧠 用 IDE 打开", run: openIde },
      { label: "📂 在资源管理器打开", run: openFolder }
    ]);
  }

  // ---------- 标签 ----------
  function renderTabs() {
    var box = $("stTabs");
    // 保留「交给 AI 改」按钮
    Array.prototype.slice.call(box.querySelectorAll(".st-tab")).forEach(function (n) { n.remove(); });
    var ask = $("stAskAi");
    tabs.forEach(function (rel) {
      var t = document.createElement("div");
      t.className = "st-tab" + (rel === cur ? " on" : "");
      var n = document.createElement("span");
      n.className = "st-tab-name";
      n.textContent = (dirty[rel] ? "● " : "") + rel.split("/").pop();
      n.title = rel;
      var x = document.createElement("span");
      x.className = "st-tab-x";
      x.textContent = "✕";
      x.onclick = function (ev) { ev.stopPropagation(); closeTab(rel); };
      t.appendChild(n); t.appendChild(x);
      t.onclick = function () { openFile(rel); };
      box.insertBefore(t, ask);
    });
  }

  function closeTab(rel) {
    if (dirty[rel] && !confirm("「" + rel + "」还有未保存的修改，关闭会丢掉。确定关掉？")) return;
    tabs = tabs.filter(function (r) { return r !== rel; });
    delete dirty[rel];
    if (models[rel]) { models[rel].dispose(); delete models[rel]; }
    if (cur === rel) {
      cur = tabs.length ? tabs[tabs.length - 1] : "";
      if (cur) showTab(cur); else if (editor) editor.setModel(null);
    }
    renderTabs(); renderTree();
  }

  function showTab(rel) {
    if (!editor || !models[rel]) return;
    editor.setModel(models[rel]);
    editor.setPosition({ lineNumber: 1, column: 1 });
  }

  /* 选中一个文件。**不再往 Monaco 里塞内容** —— 编辑发生在右侧的 VS Code 里。
     这里只记住"当前文件"（▶ 运行 / 🤖 交给 AI 改 都要用它）并高亮文件树。 */
  async function openFile(rel) {
    await initEditor();
    if (/\.(png|jpe?g|gif|webp|ico|pdf|zip|woff2?|ttf)$/i.test(rel)) {
      toast("这是二进制文件，已选中；用「📦 打包」可整体下载");
    }
    if (tabs.indexOf(rel) < 0) tabs.push(rel);
    cur = rel;
    renderTabs(); renderTree();
  }

  // ---------- 编辑器 ----------
  /* 保留这个函数名（很多地方在 await initEditor()），但**不建任何内嵌编辑器** ——
     编辑器是本机的 PyCharm，这里只负责探一下"装的是哪个 IDE、项目在哪"，
     好把面板上的按钮文案和路径显示对。 */
  async function initEditor() {
    // 编辑器 = 内置 VS Code（嵌在界面里）。顺手探一下本机有没有 PyCharm，
    // 好把那个备用按钮的文案写对。
    await ensureVs();
    probeIde();
  }

  // ---------- 动作 ----------
  async function refresh() {
    try {
      var d = await jget("/api/ws/tree");
      files = d.files || [];
      project = d.project || project;
      renderTree();          // 树已隐藏，但 AI 改动列表等地方还依赖它渲染的数据
      fillRunList();
    } catch (e) { toast("读取项目文件失败：" + e.message); }
  }

  /* 填充"运行哪个文件"的下拉。
     为什么需要它：我们自己的文件树已经隐藏（和 VS Code 的 EXPLORER 重复），
     可「▶ 运行」还得知道跑哪个 —— 让用户在下拉里选，比"回树里点一下再回来"直接。 */
  function fillRunList() {
    var sel = $("stRunFile");
    if (!sel) return;
    var py = (files || []).filter(function (f) { return /\.py$/i.test(f.rel || ""); });
    var keep = sel.value || cur || "";
    sel.innerHTML = "";
    if (!py.length) {
      var o0 = document.createElement("option");
      o0.value = "";
      o0.textContent = "（项目里没有 .py）";
      sel.appendChild(o0);
      return;
    }
    py.forEach(function (f) {
      var o = document.createElement("option");
      o.value = f.rel;
      o.textContent = f.rel;
      sel.appendChild(o);
    });
    var hit = py.some(function (f) { return f.rel === keep; });
    sel.value = hit ? keep : py[0].rel;
  }

  async function saveCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    var text = models[cur] ? models[cur].getValue() : "";
    var d = await jpost("/api/ws/file", { rel: cur, text: text });
    if (!d.ok) { toast(d.error || "保存失败"); return; }
    delete dirty[cur];
    renderTabs(); renderTree();
    toast("已保存 " + cur);
    lintCur();          // 存完立刻做语法检查 → 编辑器里当场标红
  }

  function showOut(title, body) {
    $("stOut").hidden = false;
    $("stOutTitle").textContent = title;
    $("stOutBody").textContent = body;
  }

  var runId = "";
  var stoppedByUser = false;

  function setRunning(on) {
    var s = $("stRunStop");
    if (s) s.hidden = !on;
    var b = $("stRun");
    if (b) { b.disabled = !!on; b.textContent = on ? "▶ 运行中…" : "▶ 运行"; }
  }

  /* 运行 = **流式**（像终端一样边跑边出字），不是跑完才给结果。
     原来用 /api/ws/run 是同步的：计时器/服务器这类长任务就是"一直正在执行"，
     而且超时被强杀时那段时间打印的内容全丢（实测番茄钟跑满 25 秒 →
     界面显示"（没有输出）"）。见后端 ws_run_stream 的注释。 */
  async function runCur() {
    if (runId) { toast("已经有一个在跑了，先点「■ 停止」"); return; }
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.py$/i.test(cur)) { toast("目前只有 .py 能直接运行；前端项目请点「🚀 部署」"); return; }
    if (dirty[cur]) await saveCur();

    var out = $("stOutBody");
    $("stOut").hidden = false;
    $("stOutTitle").textContent = "运行中 · " + cur + "（实时输出）";
    out.textContent = "";
    runId = "r" + Date.now();
    stoppedByUser = false;
    setRunning(true);
    var t0 = Date.now();
    var head = "";
    try {
      var resp = await fetch("/api/ws/run_stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: cur, id: runId })
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var reader = resp.body.getReader();
      var dec = new TextDecoder();
      var tail = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        tail += dec.decode(chunk.value, { stream: true });
        var lines = tail.split("\n");
        tail = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var s = lines[i];
          if (!s.trim()) continue;
          var o;
          try { o = JSON.parse(s); } catch (e) { continue; }
          if (o.t === "out") {
            out.textContent += o.data;
            out.scrollTop = out.scrollHeight;      // 自动滚到底，像终端
          } else if (o.t === "start") {
            if (o.risky && o.risky.length) {
              out.textContent += "[注意] 这段代码含：" + o.risky.join("、") + "\n";
            }
          } else if (o.t === "end") {
            var secs = ((Date.now() - t0) / 1000).toFixed(2);
            if (o.ok === false) {
              head = "启动失败";
              out.textContent += "\n" + (o.error || "未知错误");
            } else {
              head = "退出码 " + o.rc + "   用时 " + secs + " 秒"
                + (stoppedByUser ? "   （你手动停止了）" : "");
            }
          }
        }
      }
    } catch (e) {
      head = "运行出错";
      out.textContent += "\n" + (e.message || e);
    } finally {
      runId = "";
      setRunning(false);
      var body = out.textContent;
      // 没有输出时必须**说清楚是"文件本身没输出"，而不是"运行坏了"** ——
      // 实测用户看到一行轻飘飘的"（没有输出）"会直接当成 bug 报上来。
      if (!body.trim()) {
        var sz = 0;
        try {
          var fr = files.filter(function (f) { return f.rel === cur; })[0];
          sz = fr ? Number(fr.size || 0) : 0;
        } catch (e) { sz = 0; }
        if (!sz) {
          body = "这个文件是【空的】（0 字节），所以没有输出 —— 运行本身是正常的。\n"
               + "让它有东西可跑：在里面写一行 print(\"hello\") 再点运行即可。";
        } else {
          body = "这个文件跑完了，但没有打印任何东西（没有 print）。\n"
               + "运行本身是正常的 —— 想看到输出，在代码里加 print(...) 即可。";
        }
      }
      out.textContent = head + "\n" + "─".repeat(34) + "\n" + body;
      $("stOutTitle").textContent = "运行结果 · " + cur;
      stoppedByUser = false;
    }
  }

  async function stopRun() {
    stoppedByUser = true;
    try { await jpost("/api/ws/run_stop", { id: runId }); }
    catch (e) { toast("停止失败：" + e.message); }
  }

  /* 用本机已装的专业 IDE 打开这个项目。
     断点调试、变量监视、重构、代码导航这些，成熟 IDE 打磨了十几年 ——
     与其自研一个半成品，不如把专业 IDE 直接接进来（**就是同一个项目目录**）。 */
  async function openIde() {
    try {
      var d = await jpost("/api/ws/open_ide", {});
      if (!d.ok) {
        showOut("打开失败", (d.error || "") + "\n\n项目路径：\n" + (d.path || ""));
        return;
      }
      showOut("已用 " + d.ide + " 打开", "项目目录：\n" + d.path +
        "\n\n在 " + d.ide + " 里直接改这个目录即可 —— 改完回开发台点「⟳」刷新。");
      toast("已用 " + d.ide + " 打开项目");
    } catch (e) { showOut("打开失败", String(e.message || e)); }
  }

  async function openFolder() {
    try {
      var d = await jpost("/api/ws/open_folder", {});
      if (!d.ok) {
        showOut("打开失败", (d.error || "") + "\n\n" + (d.path || ""));
        return;
      }
      toast("已在资源管理器打开");
    } catch (e) { showOut("打开失败", String(e.message || e)); }
  }

  function previewCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.(html?|svg)$/i.test(cur)) { toast("预览只对 .html 有效；多文件项目请用「🚀 部署」"); return; }
    var w = window.open("/api/ws/raw?rel=" + encodeURIComponent(cur), "_blank");
    if (!w) showOut("预览地址", location.origin + "/api/ws/raw?rel=" + encodeURIComponent(cur));
  }

  async function deploy() {
    try {
      var st = await jget("/api/ws/serve/status");
      if (st.running) {
        if (!confirm("本地服务正在运行：\n" + st.url + "\n\n要停掉它吗？")) {
          window.open(st.url, "_blank");
          return;
        }
        await jpost("/api/ws/serve/stop", {});
        showOut("已停止", "本地服务已停止。");
        return;
      }
      var d = await jpost("/api/ws/serve", {});
      if (!d.ok) { showOut("部署失败", d.error || "未知错误"); return; }
      showOut("本地部署成功 · 项目 " + d.project,
        "地址：" + d.url + "\n\n" +
        "· 这是**在本机起的静态服务**，完全不联网；\n" +
        "· 项目里的 css/js/图片用相对路径引用都能正确加载；\n" +
        "· 再点一次「🚀 部署」可以停掉。");
      var w = window.open(d.url, "_blank");
      if (!w) toast("地址已显示在下方： " + d.url);
    } catch (e) { showOut("部署失败", String(e.message || e)); }
  }

  async function newEntry(kind) {
    var rel = await askText(kind === "dir" ? "新建文件夹" : "新建文件",
      kind === "dir" ? "相对路径，如 static" : "文件名，如 main.py",
      kind === "dir" ? "static" : "main.py");
    if (!rel) return;
    var d = await jpost("/api/ws/new", { rel: rel, kind: kind });
    if (!d.ok) { toast(d.error || "创建失败"); return; }
    await refresh();
    if (kind !== "dir") openFile(rel);
  }

  // ---------- 导入（按钮 + 拖拽） ----------
  async function uploadFiles(list) {
    if (!list || !list.length) return;
    var fd = new FormData();
    var n = 0;
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      // 拖整个文件夹时浏览器给的相对路径，带上去保留目录结构
      var name = f.rel || f.name;
      fd.append("files", f, name);
      n++;
    }
    showOut("导入中…", "正在导入 " + n + " 个文件到项目「" + project + "」…");
    try {
      var r = await fetch("/api/ws/upload", { method: "POST", body: fd });
      var d = await r.json();
      if (!r.ok) throw new Error((d && d.detail) || ("HTTP " + r.status));
      await refresh();
      var lines = ["成功 " + (d.saved || []).length + " 个："];
      (d.saved || []).slice(0, 50).forEach(function (s) { lines.push("  ✓ " + s); });
      if ((d.failed || []).length) {
        lines.push("", "失败 " + d.failed.length + " 个：");
        d.failed.forEach(function (f2) { lines.push("  ✗ " + f2.rel + " —— " + f2.error); });
      }
      showOut("导入完成 · 项目 " + d.project, lines.join("\n"));
      toast("已导入 " + (d.saved || []).length + " 个文件");
    } catch (e) { showOut("导入失败", String(e.message || e)); }
  }

  /* 拖进来一个完整的项目文件夹时，最外层那层壳（"myproj/"）通常不需要 ——
     把"内容"直接铺在当前项目根下更符合直觉。
     只有当**所有**带路径的文件共用同一个顶层目录时才剥，避免误伤。 */
  function stripCommonRoot(list) {
    var tops = {}, withSlash = 0;
    list.forEach(function (f) {
      var r = String(f.rel || f.name);
      var i = r.indexOf("/");
      if (i > 0) { tops[r.slice(0, i)] = 1; withSlash++; }
    });
    var keys = Object.keys(tops);
    if (!withSlash || keys.length !== 1) return list;
    var rootPrefix = keys[0] + "/";
    list.forEach(function (f) {
      var r = String(f.rel || f.name);
      if (r.indexOf(rootPrefix) === 0) {
        var nv = r.slice(rootPrefix.length);
        try { Object.defineProperty(f, "rel", { value: nv, configurable: true }); }
        catch (e) { f.rel = nv; }
      }
    });
    return list;
  }

  /* 拖进来的可能是**文件夹**（"上传代码"最常见就是拖一个项目文件夹），
     所以走 webkitGetAsEntry 递归读，保留目录结构。 */
  function onDropFiles(fileList, dt) {
    var out = [];
    var entries = [];
    try {
      if (dt && dt.items) {
        for (var i = 0; i < dt.items.length; i++) {
          var it = dt.items[i];
          if (it.kind === "file" && it.webkitGetAsEntry) {
            var en = it.webkitGetAsEntry();
            if (en) entries.push(en);
          }
        }
      }
    } catch (e) { entries = []; }
    if (!entries.length) { uploadFiles(fileList); return; }

    var pending = 0, done = 0;
    function finish() {
      done++;
      if (done >= pending) uploadFiles(stripCommonRoot(out));
    }
    function walk(entry, prefix) {
      if (!entry) return;
      if (entry.isFile) {
        pending++;
        entry.file(function (f) {
          try {
            Object.defineProperty(f, "rel",
              { value: prefix + f.name, configurable: true });
          } catch (e) { f.rel = prefix + f.name; }
          out.push(f);
          finish();
        }, finish);
      } else if (entry.isDirectory) {
        if (prefix.split("/").length > 6) return;      // 别陷太深
        if (entry.name === "node_modules" || entry.name === ".git" ||
            entry.name === "__pycache__") return;      // 这些没必要导
        var rd = entry.createReader();
        rd.readEntries(function (ents) {
          ents.forEach(function (e2) { walk(e2, prefix + entry.name + "/"); });
        }, function () { /* 忽略 */ });
      }
    }
    entries.forEach(function (en) { walk(en, ""); });
    if (!pending) uploadFiles(fileList);
  }

  // ---------- AI 改动：审阅 + 撤销 ----------
  async function loadChanges() {
    try {
      var d = await jget("/api/ws/changes?limit=40");
      changes = (d.changes || []).filter(function (c) { return c.by === "ai"; });
      renderChanges();
    } catch (e) { /* 忽略 */ }
  }

  function renderChanges() {
    var box = $("stChangeList");
    if (!box) return;
    $("stChangeCount").textContent = String(changes.length);
    box.innerHTML = "";
    if (!changes.length) {
      var e = document.createElement("div");
      e.className = "st-change-empty";
      e.textContent = "还没有 AI 改动。让 AI 写点东西，这里会列出它动过的每个文件，" +
        "点开能看 diff、能一键撤销。";
      box.appendChild(e);
      return;
    }
    changes.forEach(function (c) {
      var d = document.createElement("div");
      d.className = "st-change";
      var rel = document.createElement("span");
      rel.className = "st-c-rel";
      rel.textContent = c.rel;
      var meta = document.createElement("span");
      meta.className = "st-c-meta";
      var when = new Date((c.ts || 0) * 1000);
      var hh = ("0" + when.getHours()).slice(-2);
      var mm = ("0" + when.getMinutes()).slice(-2);
      meta.textContent = (c.created ? "新建" : "修改") + " · " + hh + ":" + mm +
        " · " + c.before_chars + "→" + c.after_chars + " 字";
      if (c.created) meta.className += " st-c-new";
      d.appendChild(rel); d.appendChild(meta);
      d.onclick = function () { openDiff(c.id); };
      box.appendChild(d);
    });
  }

  /* 原来这里用 Monaco 的 diff 编辑器做"AI 改动对比"。
     Monaco 删掉之后不自己再造一套对比界面 —— **VS Code 自带**：
     每个项目本身就是个 git 仓库，左侧「源代码管理」面板（分支图标）
     会列出 AI 动过的文件，点开就是左右对比，右键就能撤销。
     所以这里只负责把话说清楚 + 帮用户定位到那儿。 */
  async function openDiff(cid) {
    var d = await jget("/api/ws/change?id=" + encodeURIComponent(cid));
    if (!d.ok) { toast(d.error || "读取改动失败"); return; }
    toast("在右侧 VS Code 的「源代码管理」里看这个文件的改动（点左侧分支图标）");
    showOut((d.created ? "新建文件：" : "改动：") + d.rel,
      "这份改动的对比与撤销都在 VS Code 里（它自带 Git）：\n\n" +
      "  1) 看右侧 VS Code → 左侧栏最上面那个「分支」图标（源代码管理）\n" +
      "  2) 里面会列出 AI 改过的文件，点开就是左右对比\n" +
      "  3) 想退回：在文件上右键 → Discard Changes\n\n" +
      "（另外旧版内容仍会自动备份到 _回收站/，需要时也能捞回来）");
  }

  async function revertChange() {
    var cid = $("stDiffRevert").getAttribute("data-id");
    if (!cid) return;
    var d = await jpost("/api/ws/changes/revert", { id: cid });
    if (!d.ok) { toast(d.error || "撤销失败"); return; }
    closeDiff();
    await refresh();
    await loadChanges();
    // 撤销后把打开的那个文件重新读一遍，否则编辑器里还是旧内容
    if (models[d.rel]) {
      var f = await jget("/api/ws/file?rel=" + encodeURIComponent(d.rel));
      if (f.ok) {
        if (f.ok && f.text === null) { /* 文件被删掉的情况 */ }
        models[d.rel].setValue(f.text || "");
        delete dirty[d.rel];
      }
    } else if (tabs.indexOf(d.rel) >= 0) {
      await openFile(d.rel);
    }
    toast("已撤销：" + d.rel);
  }

  function closeDiff() {
    $("stDiffMask").hidden = true;
    $("stDiffRevert").setAttribute("data-id", "");
  }

  // ---------- 交给 AI ----------
  async function askAi() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (dirty[cur]) await saveCur();
    handToAi("项目「" + project + "」里的 " + cur +
      "，请你先用 workspace_read 读它的最新内容（磁盘上的为准，我可能刚在开发台里手改过），" +
      "然后：\n（在这里写你要改什么，写完按回车）");
  }

  // ---------- 开关（整页全屏；聊天区搬进来，不是分屏小窗） ----------
  /* 把 #main（整个聊天区，含消息、输入框、附件、终止按钮）**整块搬进**
     开发台右栏 —— 同一个 DOM 节点搬家，所有已有事件监听、拖拽、多模态
     上传、流式渲染统统照旧，不用重写一套聊天。关闭时再搬回原来的位置。 */
  var chatHomeMark = null;
  // 对话区默认**收起**（只留一条输入框），这样开发台是真正铺满整屏的。
  var chatCollapsed = true;

  function setChatCollapsed(v) {
    chatCollapsed = !!v;
    var box = $("stChatSlot");
    if (box) box.classList.toggle("collapsed", chatCollapsed);
    var b = $("stChatToggle");
    if (b) b.textContent = chatCollapsed ? "💬 展开对话" : "💬 收起对话";
  }

  function expandChat() { if (chatCollapsed) setChatCollapsed(false); }

  /* 在开发台里一发消息就自动展开对话记录 —— 否则消息发出去了却看不见回复。
     用**捕获阶段**挂，抢在原来的发送处理之前跑。 */
  function hookComposer() {
    var send = $("sendBtn");
    if (send && !send.__stHooked) {
      send.__stHooked = true;
      send.addEventListener("click", function () { setChatCollapsed(false); }, true);
    }
    var inp = $("input");
    if (inp && !inp.__stHooked) {
      inp.__stHooked = true;
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) setChatCollapsed(false);
      }, true);
    }
  }

  function mountChat() {
    var main = $("main");
    var slot = $("stChatSlot");
    if (!main || !slot || main.parentNode === slot) return;
    if (!chatHomeMark) {
      chatHomeMark = document.createComment("chat-home");
      main.parentNode.insertBefore(chatHomeMark, main);
    }
    slot.appendChild(main);
    hookComposer();
  }

  function unmountChat() {
    var main = $("main");
    if (!main || !chatHomeMark || !chatHomeMark.parentNode) return;
    chatHomeMark.parentNode.insertBefore(main, chatHomeMark);
    chatHomeMark.parentNode.removeChild(chatHomeMark);
    chatHomeMark = null;
  }

  async function open() {
    if (opened) return;
    var el = $("studio");
    mountChat();
    setChatCollapsed(chatCollapsed);      // 保持上次的收/展状态
    el.hidden = false;
    opened = true;
    var btn = $("openStudioBtn");
    if (btn) btn.textContent = "💬 回到聊天";
    try { await initEditor(); } catch (e) { toast("编辑器加载失败：" + e.message); }
    // 后台预热模型：不做的话第一次写代码要现加载，界面上会"卡"十几秒
    jpost("/api/ws/warm", {}).catch(function () { /* 预热失败不影响使用 */ });
    await loadProjects();
    await refresh();
    await loadChanges();
    if (!tabs.length && files.length) {
      var first = files.filter(function (f) {
        return /\.(py|js|html|css|json|md)$/i.test(f.rel);
      })[0] || files[0];
      openFile(first.rel);
    }
  }

  /* 把 AI 正在动/刚动完的文件**摆到用户眼前**：开发台没开就打开，然后确保
     VS Code 正开着当前项目。
     为什么不用"把内容塞进编辑器"了：编辑器已经是 VS Code 本体，
     它自己会盯着磁盘（文件监视器），我们**只要保证文件真的在磁盘上**，
     编辑器里就会自己出现、自己刷新 —— 包括"AI 正在逐字写"的过程。 */
  async function revealFile(rel, opts) {
    if (!rel) return;
    if (!opened) await open();
    await refresh();                       // 让文件树认到这个（可能是刚建出来的）文件
    if (tabs.indexOf(rel) < 0) tabs.push(rel);
    cur = rel;
    renderTabs(); renderTree();
  }

  /* AI 正在往某个文件里逐字写 —— 给一条看得见的进度。
     真正的"逐字"发生在 VS Code 里（文件在长），这里只是把状态说清楚，
     免得用户以为"它又在憋大招"。 */
  function showTyping(ev) {
    if (!ev || !ev.rel) return;
    var line = $("stVsTyping");
    if (!line) {
      line = document.createElement("div");
      line.id = "stVsTyping";
      line.className = "st-vs-typing";
      var bar = document.querySelector(".studio-bar");
      if (bar) bar.appendChild(line);
    }
    line.textContent = "✍ AI 正在写 " + ev.rel + " · " + (ev.chars || 0) + " 字";
    line.hidden = false;
    clearTimeout(showTyping._t);
    showTyping._t = setTimeout(function () { line.hidden = true; }, 4000);
  }

  function close() {
    var el = $("studio");
    unmountChat();
    el.hidden = true;
    opened = false;
    var btn = $("openStudioBtn");
    if (btn) btn.textContent = "🛠 开发台";
  }

  function toggle() { if (opened) close(); else open(); }

  function notify(ev) {
    if (!ev) return;
    // ① AI 正在往文件里逐字写（生成没结束）—— 只更新进度提示。
    //    内容在我们这边是**边生成边落盘**的，VS Code 看到文件在变就会自己刷出来。
    if (ev.type === "typing") { showTyping(ev); return; }
    if (ev.type !== "workspace") return;
    // AI 自己建了项目 / 换了项目 → 项目下拉框和文件树都得跟着换，
    // 否则它明明在写另一个项目，界面上还停在上一个（看起来就像"没生效"）。
    if (ev.act === "project") {
      project = ev.project || project;
      loadProjects().then(function () { refresh(); loadChanges(); });
      toast("AI 切到了项目：" + (ev.project || ""));
      return;
    }
    if (ev.act === "write") {
      // 写完了 → 把那个文件切到用户眼前（开发台开着的话），并刷新"AI 改动"列表。
      if (opened) {
        revealFile(ev.rel).then(function () { loadChanges(); });
      }
      toast("AI 更新了项目文件：" + ev.rel);
    }
  }

  // ---------- 调试：语法标记 + 报错直接交给 AI ----------
  async function lintCur() {
    if (!cur || !/\.py$/i.test(cur) || !editor || !monacoRef || !models[cur]) return;
    try {
      var d = await jget("/api/ws/check?rel=" + encodeURIComponent(cur));
      var ds = (d.diagnostics || []).map(function (x) {
        return {
          startLineNumber: x.line || 1, startColumn: x.col || 1,
          endLineNumber: x.line || 1, endColumn: (x.col || 1) + 1,
          message: x.message || "语法错误",
          severity: monacoRef.MarkerSeverity.Error
        };
      });
      monacoRef.editor.setModelMarkers(models[cur], "pycheck", ds);
    } catch (e) { /* 检查失败不打扰用户 */ }
  }

  function handToAi(text) {
    var box = $("input");
    if (!box) { toast("找不到聊天输入框"); return; }
    box.value = text;
    box.focus();
    try { box.setSelectionRange(box.value.length, box.value.length); } catch (e) { /* ignore */ }
  }

  /* 调试：跑一遍 → 有报错就把**完整输出**交给 AI 去修。
     ⚠️ 直接复用 runCur 的**流式**通道 —— 原来自成一套、走同步接口，
     于是"▶ 运行"已经改成实时输出了，「🔧 调试」还是 25 秒封顶的老路。 */
  async function debugCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.py$/i.test(cur)) {
      toast("调试针对 .py；前端项目请在「🧩 VS Code」里用浏览器调试");
      return;
    }
    if (dirty[cur]) await saveCur();
    await lintCur();
    await runCur();                    // 流式跑完（没有 25 秒上限）
    var body = ($("stOutBody").textContent || "").trim();
    var okRun = /^退出码 0\b/m.test(body);
    if (okRun) { toast("调试通过：退出码 0，没有报错"); return; }
    handToAi("运行项目里的 " + cur + " 报错了 / 没正常结束，完整输出如下：\n\n```\n" +
      body.slice(-3000) + "\n```\n\n" +
      "请先用 workspace_read 读它的最新内容，定位原因并**直接改好**" +
      "（用 workspace_write 写回整份文件），然后确认能跑通。");
    toast("输出已填进聊天框 —— 按回车让 AI 修");
  }

  // ---------- 上传到代码托管平台 ----------
  async function uploadProject() {
    var repo = await askText("上传到代码托管平台（GitHub / Gitee / 自建 Git）",
      "仓库地址，如 git@github.com:你的账号/仓库.git（留空＝沿用已有 origin）", "");
    var msg = await askText("提交说明", "这次改了什么", "更新 " + new Date().toLocaleString());
    if (!msg) msg = "更新 " + new Date().toLocaleString();
    showOut("上传中…", "正在提交并推送到远端，请稍候…\n（认证用的是本机 git 凭据，应用不保存任何 token）");
    try {
      var d = await jpost("/api/ws/git/push", { repo: repo, message: msg, branch: "main" });
      showOut(d.ok ? ("上传成功 · 分支 " + (d.branch || "main")) : "上传失败",
        (d.error ? (d.error + "\n\n") : "") + (d.logs || []).join("\n"));
      if (d.ok) toast("已上传到远端仓库");
    } catch (e) { showOut("上传失败", String(e.message || e)); }
  }

  /* 内置的**真 VS Code**（code-server）：绿色包，跑在 127.0.0.1，全程离线。
     它现在是开发台**唯一的编辑器**（见 .studio-editor 里的 #stVs），
     所以原来那套独立的全屏覆盖层（showVs / closeVs / openVsCode / stopVsCode）
     已经删掉，只留 ensureVs() 负责"起进程 + 指到当前项目"。
     停进程用后端「■ 停掉」也可以：POST /api/ide/stop。 */

  // ---------- 事件绑定 ----------
  function bind() {
    var btn = $("openStudioBtn");
    if (btn) btn.onclick = toggle;
    if (!$("studio")) return;
    $("stClose").onclick = close;
    $("stChatToggle").onclick = function () { setChatCollapsed(!chatCollapsed); };
    $("stRefresh").onclick = function () { refresh(); loadChanges(); };
    $("stSave").onclick = saveCur;
    // 「▶ 运行」的目标取自下拉（文件树已隐藏，不再有"当前文件"这个来源）
    $("stRun").onclick = function () {
      var sel = $("stRunFile");
      if (sel && sel.value) cur = sel.value;
      runCur();
    };
    if ($("stRunFile")) $("stRunFile").onchange = function () { if (this.value) cur = this.value; };
    $("stPreview").onclick = previewCur;
    $("stDeploy").onclick = deploy;
    $("stUpload").onclick = uploadProject;
    // 🧠 PyCharm：把当前项目交给本机 IDE（不复制文件，就是打开那个目录）
    // ⟳ VS Code：重启内置编辑器并重新加载（切了项目、或界面卡住时用）
    if ($("stVsReload")) $("stVsReload").onclick = function () { ensureVs(true); };
    // 备用：本机 PyCharm（独立窗口）。嵌不进来，但留着当"重调试"的出口。
    if ($("stPyCharm")) $("stPyCharm").onclick = openInIde;
    if ($("stIdeOpen")) $("stIdeOpen").onclick = openInIde;
    if ($("stIdeFolder")) $("stIdeFolder").onclick = openFolder;
    $("stFolder").onclick = openFolder;
    $("stRunStop").onclick = stopRun;
    $("stDebug").onclick = debugCur;
    $("stOutFix").onclick = function () {
      var body = ($("stOutBody").textContent || "").trim();
      if (!body) { toast("还没有输出可发"); return; }
      handToAi("这是我在开发台运行 " + (cur || "代码") + " 得到的输出，请帮我定位并修好：\n\n```\n" +
        body.slice(-3000) + "\n```");
      toast("已填进聊天框 —— 按回车发给 AI");
    };
    $("stAskAi").onclick = askAi;
    $("stProj").onchange = function () { useProject(this.value); };
    $("stProjNew").onclick = newProject;
    $("stProjMenu").onclick = function (e) {
      var r = this.getBoundingClientRect();
      projMenu(r.left, r.bottom + 4);
    };
    $("stProj").oncontextmenu = function (e) { e.preventDefault(); projMenu(e.clientX, e.clientY); };
    $("stTree").oncontextmenu = function (e) {
      // 点在文件行上时交给文件菜单处理（那个已经 preventDefault 了）
      if (e.target.closest && e.target.closest(".st-file")) return;
      e.preventDefault();
      treeMenu(e.clientX, e.clientY);
    };
    // 点别处 / 按 Esc 就收起菜单
    document.addEventListener("click", closeMenu);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeMenu();
    });
    $("stNewFile").onclick = function () { newEntry("file"); };
    $("stNewDir").onclick = function () { newEntry("dir"); };
    $("stImport").onclick = function () { $("stFileInput").click(); };
    $("stFileInput").onchange = function () {
      if (this.files && this.files.length) uploadFiles(Array.prototype.slice.call(this.files));
      this.value = "";
    };
    $("stZip").onclick = function () {
      window.open("/api/ws/zip", "_blank");
    };
    $("stOutClose").onclick = function () { $("stOut").hidden = true; };
    $("stOutClear").onclick = function () { $("stOutBody").textContent = ""; };
    $("stChangeRefresh").onclick = loadChanges;
    $("stDiffClose").onclick = closeDiff;
    $("stDiffKeep").onclick = closeDiff;
    $("stDiffRevert").onclick = revertChange;
    $("stDiffMask").addEventListener("mousedown", function (e) {
      if (e.target === $("stDiffMask")) closeDiff();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("stDiffMask").hidden) { closeDiff(); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else { bind(); }

  window.Studio = {
    open: open, close: close, toggle: toggle, refresh: refresh, notify: notify,
    onDropFiles: onDropFiles, loadChanges: loadChanges,
    // 打开开发台并把某个文件摆到编辑器里（聊天区的"写进开发台"按钮用它）
    reveal: revealFile, openFile: openFile,
    // 有没有还没保存的改动 —— 界面自更新要重载页面前必须先问，
    // 否则会把用户改了一半的文件冲掉。
    hasUnsaved: function () {
      try { return Object.keys(dirty).some(function (k) { return dirty[k]; }); }
      catch (e) { return false; }
    },
    get current() { return cur; },
    get project() { return project; },
    get opened() { return opened; }
  };
})();
