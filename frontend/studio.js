/* 开发台（Dev Studio）—— 人机协同开发
 *
 * 定位：把"模型写完 → 你复制到别处改 → 再贴回来"变成
 *       "同一个工作区里，你改你的、它改它的，都在界面上看得见、跑得动"。
 *
 * 组成：
 *   · 左：工作区文件树（多级目录）
 *   · 中：离线 Monaco 编辑器（就是 VS Code 的内核）+ 多标签
 *   · 底：运行输出（.py 真跑，cwd = 文件所在目录）
 *   · 上：「让 AI 改这个文件」把当前文件交给模型
 *
 * Monaco 从 /static/vendor/monaco 本地加载 —— **不连任何 CDN**，断网可用。
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var files = [];          // [{rel,size,ext}] 工作区文件
  var tabs = [];           // 已打开的 rel 列表
  var cur = "";            // 当前 rel
  var dirty = {};          // rel -> true
  var models = {};         // rel -> monaco model
  var editor = null;
  var monacoRef = null;
  var loading = null;
  var opened = false;

  // ---------- 极简 API 封装（app.js 里的 api() 在闭包内，拿不到） ----------
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
    // 复用 app.js 的全局提示（有就用，没有就退化成 console）
    if (window.__toast) { window.__toast(msg, kind); }
    else { console.log("[studio]", msg); }
  }

  /* 自建输入框 —— ⚠️ WebView2 里 **没有 window.prompt()**（本项目踩过：
     调用会静默失败/报错）。所以这里自己弹一个。 */
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
        resolve(v);
      }
      wrap.querySelector(".st-dialog-ok").onclick = function () { done(inp.value.trim()); };
      wrap.querySelector(".st-dialog-cancel").onclick = function () { done(""); };
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); done(inp.value.trim()); }
        if (e.key === "Escape") { e.preventDefault(); done(""); }
      });
      wrap.addEventListener("mousedown", function (e) {
        if (e.target === wrap) done("");
      });
      document.body.appendChild(wrap);
      inp.focus();
    });
  }

  // ---------- Monaco 懒加载（离线本地文件） ----------
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
    // 跟着界面主题走：暗底用 vs-dark，亮底用 vs
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

  // ---------- 文件树 ----------
  function renderTree() {
    var box = $("stTree");
    if (!box) return;
    box.innerHTML = "";
    if (!files.length) {
      var empty = document.createElement("div");
      empty.className = "st-empty";
      empty.textContent = "工作区还是空的。点「＋ 文件」建一个，或让 AI 直接写。";
      box.appendChild(empty);
      return;
    }
    // flat -> 树
    var root = { name: "", children: {}, files: [] };
    files.forEach(function (f) {
      var segs = f.rel.split("/");
      var node = root;
      for (var i = 0; i < segs.length - 1; i++) {
        if (!node.children[segs[i]]) node.children[segs[i]] = { name: segs[i], children: {}, files: [] };
        node = node.children[segs[i]];
      }
      node.files.push(f);
    });

    function build(node, depth, prefix) {
      Object.keys(node.children).sort().forEach(function (k) {
        var d = document.createElement("div");
        d.className = "st-dir";
        d.style.paddingLeft = (8 + depth * 12) + "px";
        d.textContent = "▸ " + k + "/";
        box.appendChild(d);
        build(node.children[k], depth + 1, prefix + k + "/");
      });
      node.files.sort(function (a, b) { return a.rel < b.rel ? -1 : 1; }).forEach(function (f) {
        var name = f.rel.split("/").pop();
        var d = document.createElement("div");
        d.className = "st-file" + (f.rel === cur ? " on" : "");
        d.style.paddingLeft = (8 + depth * 12) + "px";
        d.title = f.rel + "（" + f.size + " 字节）";
        d.setAttribute("data-rel", f.rel);
        d.innerHTML = '<span class="st-ico">' + iconOf(f.ext) + '</span>' +
                      '<span class="st-name"></span>' +
                      (dirty[f.rel] ? '<span class="st-dot">●</span>' : "");
        d.querySelector(".st-name").textContent = name;
        d.onclick = function () { openFile(f.rel); };
        box.appendChild(d);
      });
    }
    build(root, 0, "");

    // 回收站提示
    var rec = document.createElement("div");
    rec.className = "st-rec";
    rec.textContent = "覆盖/删除的文件会进 _回收站/，不会真丢";
    box.appendChild(rec);
  }

  function iconOf(ext) {
    var e = (ext || "").toLowerCase();
    if (e === "py") return "🐍";
    if (e === "js" || e === "ts") return "🟨";
    if (e === "html" || e === "htm") return "🌐";
    if (e === "css") return "🎨";
    if (e === "json" || e === "yml" || e === "yaml" || e === "toml") return "⚙️";
    if (e === "md" || e === "txt") return "📄";
    return "📃";
  }

  // ---------- 标签 ----------
  function renderTabs() {
    var box = $("stTabs");
    box.innerHTML = "";
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
      x.onclick = function (ev) {
        ev.stopPropagation();
        closeTab(rel);
      };
      t.appendChild(n); t.appendChild(x);
      t.onclick = function () { openFile(rel); };
      box.appendChild(t);
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
    $("stPath").textContent = "  ·  " + rel;
  }

  async function openFile(rel) {
    try {
      await initEditor();
    } catch (e) {
      toast("编辑器加载失败：" + e.message);
      return;
    }
    if (!models[rel]) {
      var d = await jget("/api/ws/file?rel=" + encodeURIComponent(rel));
      if (!d.ok) { toast(d.error || "打开失败"); return; }
      models[rel] = monacoRef.editor.createModel(d.text || "", langOf(rel));
      models[rel].onDidChangeContent(function () {
        if (!dirty[rel]) { dirty[rel] = true; renderTabs(); renderTree(); }
      });
    }
    if (tabs.indexOf(rel) < 0) tabs.push(rel);
    cur = rel;
    showTab(rel);
    renderTabs(); renderTree();
    // 定位到文件树里的高亮
    var node = $("stTree").querySelector('[data-rel="' + CSS.escape(rel) + '"]');
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  // ---------- 编辑器 ----------
  async function initEditor() {
    if (editor) return;
    monacoRef = await loadMonaco();
    editor = monacoRef.editor.create($("stEditor"), {
      value: "",
      language: "python",
      theme: themeName(),
      automaticLayout: true,
      fontSize: 13,
      tabSize: 4,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      renderWhitespace: "selection",
      fontFamily: "Consolas, 'Cascadia Mono', 'Microsoft YaHei Mono', monospace"
    });
    editor.addCommand(monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.KeyS, function () { saveCur(); });
    editor.addCommand(monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.Enter, function () { runCur(); });
  }

  // ---------- 动作 ----------
  async function refresh() {
    try {
      var d = await jget("/api/ws/tree");
      files = d.files || [];
      $("stPath").textContent = files.length ? ("  ·  " + files.length + " 个文件") : "  ·  空工作区";
      renderTree();
    } catch (e) { toast("读取工作区失败：" + e.message); }
  }

  async function saveCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    var text = models[cur] ? models[cur].getValue() : "";
    try {
      var d = await jpost("/api/ws/file", { rel: cur, text: text });
      if (!d.ok) { toast(d.error || "保存失败"); return; }
      delete dirty[cur];
      renderTabs(); renderTree();
      toast("已保存 " + cur + (d.backup ? "（旧版进回收站）" : ""));
    } catch (e) { toast("保存失败：" + e.message); }
  }

  function showOut(title, body) {
    $("stOut").hidden = false;
    $("stOutTitle").textContent = title;
    $("stOutBody").textContent = body;
  }

  async function runCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.py$/i.test(cur)) { toast("目前只有 .py 能直接运行；HTML 请点「🌐 预览」"); return; }
    if (dirty[cur]) await saveCur();      // 先存再跑，否则跑的是旧代码
    showOut("运行中…", "正在执行 " + cur + " …");
    try {
      var d = await jpost("/api/ws/run", { rel: cur });
      if (d.needs_confirm) {
        var risk = (d.risky || []).join("、");
        if (confirm("这段代码里有需要确认的操作：" + risk + "\n\n确定要运行吗？")) {
          d = await jpost("/api/ws/confirm_run", { rel: cur });
        } else {
          showOut("已取消", "你拒绝了这次执行。");
          return;
        }
      }
      if (!d.ok) { showOut("运行失败", d.error || "未知错误"); return; }
      var out = [];
      out.push("退出码 " + d.rc + "   用时 " + d.seconds + " 秒");
      out.push("");
      out.push("── 标准输出 ──");
      out.push(d.out || "（没有输出 —— 代码里记得加 print()）");
      if (d.err) { out.push(""); out.push("── 错误 ──"); out.push(d.err); }
      showOut("运行结果 · " + cur, out.join("\n"));
    } catch (e) { showOut("运行失败", String(e.message || e)); }
  }

  function previewCur() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (!/\.(html?|svg)$/i.test(cur)) { toast("预览只对 .html 有效"); return; }
    var w = window.open("/api/ws/raw?rel=" + encodeURIComponent(cur), "_blank");
    if (!w) showOut("预览", "浏览器窗口被拦截了。用系统浏览器打开：\n" +
      location.origin + "/api/ws/raw?rel=" + encodeURIComponent(cur));
  }

  async function newEntry(kind) {
    var rel = await askText(
      kind === "dir" ? "新建文件夹" : "新建文件",
      kind === "dir" ? "相对路径，如 src" : "文件名，如 main.py",
      kind === "dir" ? "src" : "main.py");
    if (!rel) return;
    try {
      var d = await jpost("/api/ws/new", { rel: rel, kind: kind });
      if (!d.ok) { toast(d.error || "创建失败"); return; }
      await refresh();
      if (kind !== "dir") openFile(rel);
    } catch (e) { toast("创建失败：" + e.message); }
  }

  /* 「让 AI 改这个文件」：先把当前内容存盘，再去聊天框里放好一句话。
     不自动发送 —— 用户往往想补充"改成什么样"，让他自己按回车。 */
  async function askAi() {
    if (!cur) { toast("先打开一个文件"); return; }
    if (dirty[cur]) await saveCur();
    var box = $("input");
    if (!box) { toast("找不到聊天输入框"); return; }
    close();
    box.value = "请用 workspace_read 读一下工作区里的 " + cur +
      "（它的最新内容以磁盘上的为准，我可能刚在开发台里手改过），" +
      "然后在此基础上：\n（在这里写你要改什么，写完按回车）";
    box.focus();
    try { box.setSelectionRange(box.value.length, box.value.length); } catch (e) { /* ignore */ }
  }

  // ---------- 开关 ----------
  async function open() {
    $("studio").hidden = false;
    opened = true;
    try { await initEditor(); } catch (e) { toast("编辑器加载失败：" + e.message); }
    await refresh();
    if (!tabs.length && files.length) {
      var first = files.filter(function (f) { return /\.(py|js|html|css|json|md)$/i.test(f.rel); })[0] || files[0];
      openFile(first.rel);
    }
  }

  function close() { $("studio").hidden = true; opened = false; }

  function notify(ev) {
    // 模型写了工作区文件 → 刷新树；当前打开的就是它 → 重新载入内容
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
      if (opened) refresh();
      toast("AI 更新了工作区文件：" + ev.rel);
    }
  }

  // ---------- 事件绑定 ----------
  function bind() {
    var btn = $("openStudioBtn");
    if (btn) btn.onclick = open;
    if (!$("studio")) return;
    $("stClose").onclick = close;
    $("stRefresh").onclick = refresh;
    $("stSave").onclick = saveCur;
    $("stRun").onclick = runCur;
    $("stPreview").onclick = previewCur;
    $("stAskAi").onclick = askAi;
    $("stNewFile").onclick = function () { newEntry("file"); };
    $("stNewDir").onclick = function () { newEntry("dir"); };
    $("stOutClose").onclick = function () { $("stOut").hidden = true; };
    $("stOutClear").onclick = function () { $("stOutBody").textContent = ""; };
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && opened && !$("studio").hidden) {
        // 只关开发台，不影响聊天
        if (document.activeElement && document.activeElement.closest &&
            document.activeElement.closest(".studio")) { close(); }
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else { bind(); }

  window.Studio = {
    open: open, close: close, refresh: refresh, notify: notify,
    get current() { return cur; }
  };
})();
