/*
 * 助手桥接：把外部程序（本地多模态助手）想执行的命令，送进 VS Code 的**集成终端**里跑。
 *
 * 为什么需要它：VS Code 不接受"从外部命令它开终端并执行"（官方 CLI 没这个能力，
 * 实测 `code-server <文件>` 还会另起一个服务进程）。而终端是我们最想要的运行方式 ——
 * 它天然支持 input()、能交互、能跑任意命令。
 *
 * 做法：**用一个文件当信箱**。外部程序把 {ts, cmd, cwd} 写进去，这里每 0.7 秒读一次，
 * 发现 ts 变大就在终端里执行。双方都跑在同一台机器上，纯本地、不联网、不开端口。
 *
 * ⚠️ 信箱放在**系统临时目录**，不用 `~` ——
 * 实测扩展宿主进程的 `os.homedir()` 并不是用户主目录，写 ~/.xxx 会静默失败
 * （扩展显示"已激活"，但外部程序什么都等不到，最难查）。
 * 而 `os.tmpdir()` 和 Python 的 `tempfile.gettempdir()` 拿到的是同一个路径。
 */
const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");

const MAILBOX = path.join(os.tmpdir(), "mm_bridge_cmd.json");
const ALIVE = path.join(os.tmpdir(), "mm_bridge_alive.txt");
const DIAG = path.join(os.tmpdir(), "mm_bridge_diag.txt");
const TERM_NAME = "助手";
const POLL_MS = 700;

let lastTs = 0;

function readCmd() {
  try {
    return JSON.parse(fs.readFileSync(MAILBOX, "utf8"));
  } catch (e) {
    return null;
  }
}

function writeJson(file, obj) {
  try {
    fs.writeFileSync(file, JSON.stringify(obj), "utf8");
    return true;
  } catch (e) {
    return false;
  }
}

/** 拿到（或新建）那个专用终端。被用户关掉后 exitStatus 会有值，这时要重建。 */
function ensureTerminal(cwd) {
  let t = (vscode.window.terminals || []).find(
    (x) => x.name === TERM_NAME && x.exitStatus === undefined
  );
  if (t) return t;
  const opts = { name: TERM_NAME };
  if (cwd) {
    try {
      opts.cwd = vscode.Uri.file(String(cwd));
    } catch (e) {}
  }
  return vscode.window.createTerminal(opts);
}

function handle(o) {
  if (!o) return;
  const ts = Number(o.ts || 0);
  if (!ts || ts <= lastTs) return;
  lastTs = ts;
  const cmd = String(o.cmd || "").trim();
  if (!cmd) return;
  try {
    const t = ensureTerminal(o.cwd);
    t.show(true); // 让用户看得到
    t.sendText(cmd, true);
    // 回执：外部程序可以靠它确认"命令真的进终端了"，而不是只写了个文件
    writeJson(ALIVE, {
      event: "ran", last_cmd: cmd, last_ts: ts, ran_at: Date.now(),
    });
  } catch (e) {
    writeJson(ALIVE, { event: "error", detail: String(e), at: Date.now() });
  }
}

function activate(context) {
  // 先把信箱里现有的 ts 吃掉 —— 否则扩展一激活就把**上一条**命令重放一遍
  const cur = readCmd();
  if (cur) lastTs = Number(cur.ts || 0);

  // 诊断信息：排查时能确认扩展到底有没有被加载、拿到的路径是什么
  writeJson(DIAG, {
    event: "activated",
    at: Date.now(),
    pid: process.pid,
    mailbox: MAILBOX,
    alive: ALIVE,
    tmpdir: os.tmpdir(),
    homedir: os.homedir(),
    userprofile: process.env.USERPROFILE || "",
    tmp_env: process.env.TMP || process.env.TEMP || "",
    terminal_names: (vscode.window.terminals || []).map((x) => x.name),
  });

  context.subscriptions.push(
    vscode.commands.registerCommand("mmBridge.runLast", () => {
      const c = readCmd();
      if (c && c.cmd) {
        const t = ensureTerminal(c.cwd);
        t.show(true);
        t.sendText(String(c.cmd), true);
      }
    })
  );

  const timer = setInterval(() => handle(readCmd()), POLL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function deactivate() {}

module.exports = { activate, deactivate };
