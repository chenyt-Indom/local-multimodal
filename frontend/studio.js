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

  // ---------- Monaco（离线本地加载） ----------
  function loadMonaco() {
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      if (window.monaco) return resolve(window.monaco);
      var s = document.createElement("script");
      s.src = "/static/vendor/monaco/vs/loader.js";
      s.onload = function () {
        try {
          window.require.config({ paths: { vs: "/static/vendor/monaco/vs" } });
          window.require(["vs/editor/editor.main"], function () {
            resolve(window.monaco);
          }, reject);
        } catch (e) { reject(e); }
      };
      s.onerror = function () { reject(new Error("编辑器资源加载失败")); };
      document.head.appendChild(s);
    });
    return loading;
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

  async function openFile(rel) {
    try { await initEditor(); }
    catch (e) { toast("编辑器加载失败：" + e.message); return; }
    if (!models[rel]) {
      var d = await jget("/api/ws/file?rel=" + encodeURIComponent(rel));
      if (!d.ok) { toast(d.error || "打开失败"); return; }
      if (/\.(png|jpe?g|gif|webp|ico|pdf|zip|woff2?|ttf)$/i.test(rel)) {
        showOut("这是二进制文件", rel + "（" + (d.chars || 0) + " 字符）不适合在编辑器里改。\n" +
          "可以在文件树里右键删除，或用「📦 打包」整体下载。");
        return;
      }
      models[rel] = monacoRef.editor.createModel(d.text || "", langOf(rel));
      models[rel].onDidChangeContent(function () {
        if (!dirty[rel]) { dirty[rel] = true; renderTabs(); renderTree(); }
      });
    }
    if (tabs.indexOf(rel) < 0) tabs.push(rel);
    cur = rel;
    showTab(rel);
    renderTabs(); renderTree();
    lintCur();
  }

  // ---------- 编辑器 ----------
  async function initEditor() {
    if (editor) return;
    monacoRef = await loadMonaco();
    editor = monacoRef.editor.create($("stEditor"), {
      value: "", language: "python", theme: themeName(),
      automaticLayout: true, fontSize: 13, tabSize: 4,
      minimap: { enabled: false }, scrollBeyondLastLine: false,
      renderWhitespace: "selection",
      fontFamily: "Consolas, 'Cascadia Mono', 'Microsoft YaHei Mono', monospace"
    });
    editor.addCommand(monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.KeyS,
      function () { saveCur(); });
    editor.addCommand(monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.Enter,
      function () { runCur(); });
  }

  // ---------- 动作 ----------
  async function refresh() {
    try {
      var d = await jget("/api/ws/tree");
      files = d.files || [];
      project = d.project || project;
      renderTree();
    } catch (e) { toast("读取项目文件失败：" + e.message); }
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
      if (!body.trim()) body = "（没有输出 —— 代码里记得加 print()）";
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

  async function openDiff(cid) {
    try { await initEditor(); } catch (e) { toast("编辑器加载失败"); return; }
    var d = await jget("/api/ws/change?id=" + encodeURIComponent(cid));
    if (!d.ok) { toast(d.error || "读取改动失败"); return; }
    var mask = $("stDiffMask");
    mask.hidden = false;
    $("stDiffName").textContent = (d.created ? "新建文件：" : "改动：") + d.rel;
    $("stDiffRevert").setAttribute("data-id", cid);
    if (!diffEditor) {
      diffEditor = monacoRef.editor.createDiffEditor($("stDiffBody"), {
        readOnly: true, automaticLayout: true, theme: themeName(),
        renderSideBySide: true, originalEditable: false,
        fontSize: 13, minimap: { enabled: false }
      });
    }
    if (diffModels) { diffModels.o.dispose(); diffModels.m.dispose(); }
    diffModels = {
      o: monacoRef.editor.createModel(d.before || "", langOf(d.rel)),
      m: monacoRef.editor.createModel(d.after || "", langOf(d.rel))
    };
    diffEditor.setModel({ original: diffModels.o, modified: diffModels.m });
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
    if (!ev || ev.type !== "workspace") return;
    if (ev.act === "write") {
      if (ev.rel === cur && models[cur]) {
        jget("/api/ws/file?rel=" + encodeURIComponent(ev.rel)).then(function (d) {
          if (d.ok && d.text !== models[cur].getValue()) {
            var pos = editor ? editor.getPosition() : null;
            models[cur].setValue(d.text);
            if (pos && editor) editor.setPosition(pos);
          }
          delete dirty[ev.rel];
          renderTabs();
        });
      }
      if (opened) { refresh(); loadChanges(); }
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

  /* 调试：跑一遍 → 有报错就把**完整 traceback** 交给 AI 去修。
     这是"debug 能力"里最实的一环：报错不用你自己抄给模型。 */
  async function debugCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.py$/i.test(cur)) { toast("调试针对 .py；前端项目请点「🚀 部署」看效果"); return; }
    if (dirty[cur]) await saveCur();
    await lintCur();
    showOut("调试中…", "正在执行 " + cur + " …");
    var d = await jpost("/api/ws/run", { rel: cur });
    if (d.needs_confirm) {
      var risk = (d.risky || []).join("、");
      if (confirm("这段代码里有需要确认的操作：" + risk + "\n\n确定要运行吗？")) {
        d = await jpost("/api/ws/confirm_run", { rel: cur });
      } else { showOut("已取消", "你拒绝了这次执行。"); return; }
    }
    if (!d.ok) { showOut("调试失败", d.error || "未知错误"); return; }
    var out = ["退出码 " + d.rc + "   用时 " + d.seconds + " 秒", "",
               "── 标准输出 ──", d.out || "（无输出）"];
    if (d.err) { out.push("", "── 错误 ──", d.err); }
    showOut("调试 · " + cur, out.join("\n"));
    if (d.rc !== 0 || (d.err || "").trim()) {
      handToAi("运行项目里的 " + cur + " 报错了，完整输出如下：\n\n```\n" +
        (d.err || d.out || "").slice(-3000) + "\n```\n\n" +
        "请先用 workspace_read 读它的最新内容，定位原因并**直接改好**" +
        "（用 workspace_write 写回整份文件），再用 workspace_run 跑一次确认通过。");
      toast("报错已填进聊天框 —— 按回车让 AI 修");
    } else {
      toast("调试通过：退出码 0，没有报错");
    }
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
     断点调试 / 变量监视 / 终端 / Git 面板 / 扩展市场都在里面 ——
     这些是自研编辑器做不出来的，所以直接把它接进来。 */
  async function openVsCode() {
    var st = {};
    try { st = await jget("/api/ide/status"); } catch (e) { st = {}; }
    if (!st.installed) {
      showOut("没有内置 VS Code", "没在 vendor/ 下找到 code-server。\n\n" +
        "获取方式（一次性）：到 github.com/coder/code-server/releases 下载\n" +
        "  code-server-<版本>-windows-amd64.tar.gz\n" +
        "解压到项目的 vendor/ 目录即可（目录名保持 code-server-<版本>-windows-amd64）。");
      return;
    }
    if (st.running) {
      if (confirm("内置 VS Code 正在运行：\n" + st.url + "\n\n要停掉它吗？")) {
        await jpost("/api/ide/stop", {});
        showOut("已停止", "内置 VS Code 已停止。");
        toast("已停止内置 VS Code");
        return;
      }
      window.open(st.url, "_blank");
      return;
    }
    showOut("正在启动内置 VS Code…", "第一次启动要几秒，请稍候…");
    try {
      var d = await jpost("/api/ide/start", {});
      if (!d.ok) { showOut("启动失败", d.error || "未知错误"); return; }
      showOut("内置 VS Code 已就绪 · 项目 " + d.project,
        "地址：" + d.url + "\n\n" +
        "· 这是**真正的 VS Code**（code-server 4.137 / Code 1.137），全程离线；\n" +
        "· 打开的就是当前项目目录 —— 和开发台改的是**同一批文件**；\n" +
        "· 断点调试、变量监视、终端、Git 面板、扩展市场都在里面；\n" +
        "· 窗口没自动弹出的话，把上面的地址复制到浏览器打开；\n" +
        "· 再点一次「🧩 VS Code」可以停掉它。");
      var w = window.open(d.url, "_blank");
      if (!w) toast("地址已显示在下方，复制到浏览器打开");
    } catch (e) { showOut("启动失败", String(e.message || e)); }
  }

  // ---------- 事件绑定 ----------
  function bind() {
    var btn = $("openStudioBtn");
    if (btn) btn.onclick = toggle;
    if (!$("studio")) return;
    $("stClose").onclick = close;
    $("stChatToggle").onclick = function () { setChatCollapsed(!chatCollapsed); };
    $("stRefresh").onclick = function () { refresh(); loadChanges(); };
    $("stSave").onclick = saveCur;
    $("stRun").onclick = runCur;
    $("stPreview").onclick = previewCur;
    $("stDeploy").onclick = deploy;
    $("stUpload").onclick = uploadProject;
    $("stIde").onclick = openIde;
    $("stVsCode").onclick = openVsCode;
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
    get current() { return cur; },
    get project() { return project; }
  };
})();
