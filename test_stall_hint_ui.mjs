// 验证「等待时界面会说话」：发一条消息，看思考状态行会不会报出已等待时长。
//
// 跑法：先起带调试端口的浏览器（--remote-debugging-port=9223），再 node test_stall_hint_ui.mjs
//
// 为什么要真点：这段是纯前端行为（定时器 + DOM 文案），grep 源码证明不了"界面上真的出现了"。
// 用启动后的**第一条**消息最好观察 —— 那一条必然要预填充整份提示词（十几秒）。
// ⚠️ 调试端口可配：无头浏览器换了端口就不用改脚本（MM_CDP=http://127.0.0.1:9223 node ...）
const BASE = process.env.MM_CDP || "http://127.0.0.1:9222";
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
const send = (method, params = {}) => { const id = ++seq;
  return new Promise(res => { waiting.set(id, res); ws.send(JSON.stringify({ id, method, params })); }); };
const ev = async (e) => {
  const m = await send("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true });
  if (m.result && m.result.exceptionDetails) throw new Error("页面报错：" + JSON.stringify(m.result.exceptionDetails));
  return m.result.result.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

let PASS = 0, FAIL = 0;
function check(name, ok, detail) {
  if (ok) { PASS++; console.log("  [OK] " + name); }
  else { FAIL++; console.log("  [!!] " + name + (detail ? "   → " + detail : "")); }
}

await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.reload");
await sleep(2600);

console.log("=".repeat(62));
console.log("① 发一条消息，盯着「思考状态行」的文案变化");
await ev(`(() => { const i = document.getElementById("input");
  i.value = "你好"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
await ev(`document.getElementById("sendBtn").click()`);

const seen = [];
for (let i = 0; i < 26; i++) {
  await sleep(1000);
  const t = await ev(`(document.querySelector(".think-hdr span") || {}).textContent || ""`);
  if (t) seen.push(t);
  if (t && t.includes("思考中…（") && !t.includes("已等待")) break;   // 已经开始流式思考
}
const stallLine = seen.find(t => t.includes("已等待"));
check("等待期间出现了「模型正在准备…已等待 N 秒」",
      !!stallLine, seen.slice(0, 6).join(" | "));
check("提示里带具体秒数", !!stallLine && /\d+ 秒/.test(stallLine), stallLine);
check("提示解释了原因（大提示词 / 前面还有任务）",
      !!stallLine && stallLine.includes("模型正在准备"), stallLine);

console.log("\n② 数据开始进来之后，回到正常的「思考中…（N 字）」");
for (let i = 0; i < 30; i++) {
  await sleep(1000);
  const t = await ev(`(document.querySelector(".think-hdr span") || {}).textContent || ""`);
  if (t.includes("思考中…（")) {
    check("恢复正常计数文案", true, t);
    check("⚠️ 不再显示「已等待」（没有被定时器一直覆盖）", !t.includes("已等待"), t);
    break;
  }
}

console.log("\n③ 用户能看出它在动（有内容在增长）");
const body = await ev(`(document.querySelector(".think-body") || {}).textContent || ""`);
check("思考正文里有内容", body.length > 10, body.slice(0, 40));

console.log("\n" + "=".repeat(62));
console.log("通过 " + PASS + " 项，失败 " + FAIL + " 项");
console.log("=".repeat(62));
ws.close();
process.exit(FAIL ? 1 : 0);
