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
        d.oncontextmenu = function (e) { e.preventDefault(); fileMenu(f.rel); };
        box.appendChild(d);
      });
    }
    build(root, 0);
    var rec = document.createElement("div");
    rec.className = "st-rec";
    rec.textContent = "覆盖/删除的文件会进 _回收站/，不会真丢";
    box.appendChild(rec);
  }

  async function fileMenu(rel) {
    var act = await askText("「" + rel + "」—— 输入 rename / delete / 留空取消",
      "rename 或 delete", "");
    if (act === "rename") {
      var to = await askText("重命名为（相对路径）", "新路径", rel);
      if (!to) return;
      var d = await jpost("/api/ws/rename", { rel: rel, to: to });
      if (!d.ok) { toast(d.error || "重命名失败"); return; }
      if (models[rel]) { models[rel].dispose(); delete models[rel]; }
      tabs = tabs.filter(function (r) { return r !== rel; });
      if (cur === rel) cur = "";
      await refresh();
    } else if (act === "delete") {
      if (!confirm("删除「" + rel + "」?（会进回收站）")) return;
      var r = await jpost("/api/ws/delete", { rel: rel });
      if (!r.ok) { toast(r.error || "删除失败"); return; }
      if (models[rel]) { models[rel].dispose(); delete models[rel]; }
      tabs = tabs.filter(function (x) { return x !== rel; });
      if (cur === rel) { cur = tabs.length ? tabs[tabs.length - 1] : ""; }
      renderTabs();
      await refresh();
    }
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

  async function runCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.py$/i.test(cur)) { toast("目前只有 .py 能直接运行；前端项目请点「🚀 部署」"); return; }
    if (dirty[cur]) await saveCur();
    showOut("运行中…", "正在执行 " + cur + " …");
    var d = await jpost("/api/ws/run", { rel: cur });
    if (d.needs_confirm) {
      var risk = (d.risky || []).join("、");
      if (confirm("这段代码里有需要确认的操作：" + risk + "\n\n确定要运行吗？")) {
        d = await jpost("/api/ws/confirm_run", { rel: cur });
      } else { showOut("已取消", "你拒绝了这次执行。"); return; }
    }
    if (!d.ok) { showOut("运行失败", d.error || "未知错误"); return; }
    var out = ["退出码 " + d.rc + "   用时 " + d.seconds + " 秒", "", "── 标准输出 ──",
      d.out || "（没有输出 —— 代码里记得加 print()）"];
    if (d.err) { out.push("", "── 错误 ──", d.err); }
    showOut("运行结果 · " + cur, out.join("\n"));
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

  function mountChat() {
    var main = $("main");
    var slot = $("stChatSlot");
    if (!main || !slot || main.parentNode === slot) return;
    if (!chatHomeMark) {
      chatHomeMark = document.createComment("chat-home");
      main.parentNode.insertBefore(chatHomeMark, main);
    }
    slot.appendChild(main);
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
    el.hidden = false;
    opened = true;
    var btn = $("openStudioBtn");
    if (btn) btn.textContent = "💬 回到聊天";
    try { await initEditor(); } catch (e) { toast("编辑器加载失败：" + e.message); }
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

  // ---------- 事件绑定 ----------
  function bind() {
    var btn = $("openStudioBtn");
    if (btn) btn.onclick = toggle;
    if (!$("studio")) return;
    $("stClose").onclick = close;
    $("stRefresh").onclick = function () { refresh(); loadChanges(); };
    $("stSave").onclick = saveCur;
    $("stRun").onclick = runCur;
    $("stPreview").onclick = previewCur;
    $("stDeploy").onclick = deploy;
    $("stUpload").onclick = uploadProject;
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
    $("stProj").oncontextmenu = function (e) { e.preventDefault(); delProject(); };
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
