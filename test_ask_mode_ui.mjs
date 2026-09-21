// 「询问方式」按钮的浏览器端到端验证（CDP 真点，不靠看代码）
//
// 跑法（三步）：
//   1) 先确保应用在跑：http://127.0.0.1:8000
//   2) 起一个带调试端口的浏览器：
//      "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new
//        --remote-debugging-port=9222 --user-data-dir=%TEMP%/mm_cdp http://127.0.0.1:8000
//   3) node test_ask_mode_ui.mjs
//
// ⚠️ 为什么要这么测：用户 2026-09-22 要求把原来并排的「快速询问 / 深度询问」两个按钮
//    合并成**一个**「询问方式」按钮，并在按钮上显示当前选择。这段全是动态生成的
//    （弹层、事件绑定都在 app.js 里），光 grep 字符串证明不了"点了真的有反应"。
//    脚本验证：按钮存在且写着当前模式 → 点开有面板 → 选另一个 → 按钮文字变了、
//    **后端真的存了** ask_mode → 面板关闭 → 还能再打开 → 最后截图留证。
const BASE = "http://127.0.0.1:9222";
const APP = "http://127.0.0.1:8000";

const list = await (await fetch(BASE + "/json")).json();
const page = list.find(t => t.type === "page" && (t.url || "").includes("127.0.0.1:8000"));
if (!page) { console.error("找不到应用页面"); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
let seq = 0; const waiting = new Map();
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
});
await new Promise(r => ws.addEventListener("open", r));
function send(method, params = {}) {
  const id = ++seq;
  return new Promise(res => { waiting.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
}
async function ev(expr) {
  const m = await send("Runtime.evaluate",
    { expression: expr, awaitPromise: true, returnByValue: true });
  if (m.result && m.result.exceptionDetails) {
    throw new Error("页面里报错：" + JSON.stringify(m.result.exceptionDetails.exception || {}));
  }
  return m.result && m.result.result ? m.result.result.value : undefined;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

let PASS = 0, FAIL = 0;
function check(name, ok, detail) {
  if (ok) { PASS++; console.log("  [OK] " + name); }
  else { FAIL++; console.log("  [!!] " + name + (detail ? "   → " + detail : "")); }
}

const cfgOf = async () => {
  const r = await fetch(APP + "/api/config");
  return (await r.json()).config || {};
};
const setCfg = async (body) => {
  await fetch(APP + "/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
};

// 先把模式复位成默认值，让断言有确定的起点
await setCfg({ ask_mode: "quick" });
await send("Page.reload");
await sleep(2500);

console.log("=".repeat(62));
console.log("① 顶栏上只剩一个按钮（不再是并排两个）");
check("存在 #askModeBtn", await ev(`!!document.getElementById("askModeBtn")`));
check("⚠️ 已经没有任何 data-askmode 的旧控件",
      await ev(`document.querySelectorAll("[data-askmode]").length === 0`));
check("按钮文本里有「询问方式」",
      String(await ev(`document.getElementById("askModeBtn").textContent`)).includes("询问方式"));
check("按钮上没有 data-cfg（不会被当成布尔开关）",
      !(await ev(`document.getElementById("askModeBtn").hasAttribute("data-cfg")`)));

console.log("\n② 按钮显示当前选择");
check("默认显示「询问方式：快速」",
      String(await ev(`document.getElementById("askModeBtn").textContent`)).includes("快速"),
      await ev(`document.getElementById("askModeBtn").textContent`));

console.log("\n③ 点开 → 面板二选一 → 选中 → 按钮文字变了、后端也存了");
await ev(`document.getElementById("askModeBtn").click()`);
await sleep(600);
check("点一下弹出选择面板", await ev(`!!document.querySelector(".askmode-layer")`));
check("面板里有两个选项", (await ev(`document.querySelectorAll(".askmode-opt").length`)) === 2);
check("当前选中的那个有 on 标记",
      (await ev(`document.querySelectorAll(".askmode-opt.on").length`)) === 1);

await ev(`document.querySelector('.askmode-opt[data-mode="deep"]').click()`);
await sleep(1200);
check("选「深度询问」后面板自动关闭", await ev(`!document.querySelector(".askmode-layer")`));
check("按钮文字变成「深度」（当前选择直接可见）",
      String(await ev(`document.getElementById("askModeBtn").textContent`)).includes("深度"),
      await ev(`document.getElementById("askModeBtn").textContent`));
check("⚠️ 后端真的存下了 ask_mode=deep", (await cfgOf()).ask_mode === "deep",
      "实得 " + (await cfgOf()).ask_mode);

console.log("\n④ 再打开一次，选回「快速了解」");
await ev(`document.getElementById("askModeBtn").click()`);
await sleep(600);
check("能再次打开（不会因为关过一次就失灵）",
      await ev(`!!document.querySelector(".askmode-layer")`));
check("打开时默认高亮当前模式（深度）",
      await ev(`(document.querySelector(".askmode-opt.on") || {}).dataset`
              + ` ? document.querySelector(".askmode-opt.on").dataset.mode === "deep" : false`));
await ev(`document.querySelector('.askmode-opt[data-mode="quick"]').click()`);
await sleep(1200);
check("选回「快速了解」：按钮与后端都跟着回退",
      String(await ev(`document.getElementById("askModeBtn").textContent`)).includes("快速")
      && (await cfgOf()).ask_mode === "quick",
      "按钮=" + await ev(`document.getElementById("askModeBtn").textContent`)
      + " 后端=" + (await cfgOf()).ask_mode);

console.log("\n⑤ 面板说明文字（告诉用户两种模式的区别）");
await ev(`document.getElementById("askModeBtn").click()`);
await sleep(500);
const panelText = String(await ev(`(document.querySelector(".askmode-layer") || {}).textContent || ""`));
check("面板里写了两者的区别", panelText.includes("快速了解") && panelText.includes("深度询问"));
check("面板里说明「下一次提问就生效」", panelText.includes("下一次提问"));

const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot.result && shot.result.data) {
  const fs = await import("node:fs");
  fs.writeFileSync("C:/Users/19853/AppData/Local/Temp/mm_askmode_panel.png",
                   Buffer.from(shot.result.data, "base64"));
  check("面板截图已保存", true, "mm_askmode_panel.png");
}
await ev(`document.querySelector(".askmode-close").click()`);

console.log("\n" + "=".repeat(62));
console.log("通过 " + PASS + " 项，失败 " + FAIL + " 项");
console.log("=".repeat(62));
ws.close();
process.exit(FAIL ? 1 : 0);
