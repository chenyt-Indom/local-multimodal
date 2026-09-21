// 「🎛 采样」面板的浏览器端到端验证（CDP 真点，不靠看代码）
//
// 跑法（三步）：
//   1) 先确保应用在跑：http://127.0.0.1:8000
//   2) 起一个带调试端口的浏览器：
//      "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new //        --remote-debugging-port=9222 --user-data-dir=%TEMP%/mm_cdp http://127.0.0.1:8000
//   3) node test_sampling_ui.mjs
//
// ⚠️ 为什么要这么测：这个面板**全是动态生成的**（弹层、滑条、事件绑定都在 JS 里），
//    光看代码或 grep 字符串证明不了"点了真的有反应"。
//    脚本会验证：按钮 → 弹层 → 两条滑条（范围/步长）→ 拖动后**后端真的存了** →
//    建议值跳回 → 关闭后还能再打开，最后截一张图留证。
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
function check(name, ok, detail = "") {
  if (ok) { PASS++; console.log("  [OK] " + name + (detail ? "  " + detail : "")); }
  else { FAIL++; console.log("  [!!] " + name + (detail ? "  " + detail : "")); }
}
async function cfgOf() {
  const r = await fetch(APP + "/api/config");
  return (await r.json()).config || {};
}
async function setCfg(body) {
  await fetch(APP + "/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

console.log("=".repeat(62));
console.log("「🎛 采样」面板 · 浏览器端到端验证");
console.log("=".repeat(62));

await send("Runtime.enable").catch(() => {});
await sleep(1200);                       // 等页面脚本挂载完

// ---------- ① 按钮在不在 ----------
console.log("\n① 顶栏按钮");
const btn = await ev(`(() => {
  const b = document.getElementById("sampleBtn");
  return b ? { text: b.textContent.trim(), title: b.title } : null;
})()`);
check("顶栏有「🎛 采样」按钮", !!btn, btn ? btn.text : "没找到 #sampleBtn");
check("按钮有点击处理（onclick 指向 window.__showSampling）",
      await ev(`typeof window.__showSampling === "function"`));
check("按钮提示里写清了「拖完立刻生效」",
      !!(btn && btn.title && btn.title.includes("立刻生效")), btn ? btn.title.slice(0, 40) : "");

// ---------- ② 点开弹层 ----------
console.log("\n② 点开后出现两条滑条");
await ev(`document.getElementById("sampleBtn").click()`);
await sleep(700);
const box = await ev(`(() => {
  const l = document.querySelector(".sample-layer");
  if (!l) return null;
  const rs = [...l.querySelectorAll(".sample-range")].map(r => ({
    key: r.dataset.key, min: +r.min, max: +r.max, step: +r.step, value: +r.value,
  }));
  const bests = [...l.querySelectorAll(".sample-best")].map(b => b.textContent.trim());
  return { title: (l.querySelector(".confirm-title") || {}).textContent,
           ranges: rs, bests, hints: l.querySelectorAll(".sample-hint").length,
           sub: (l.querySelector(".sample-sub") || {}).textContent || "" };
})()`);
check("弹层打开了", !!box);
check("标题正确", !!(box && box.title && box.title.includes("采样参数")), box ? box.title : "");
check("有两条滑条", !!(box && box.ranges.length === 2), box ? JSON.stringify(box.ranges.map(r => r.key)) : "");
const temp = box && box.ranges.find(r => r.key === "temperature");
const topp = box && box.ranges.find(r => r.key === "top_p");
check("温度滑条：0~2 连续（步长 0.05）",
      !!temp && temp.min === 0 && temp.max === 2 && temp.step === 0.05, JSON.stringify(temp));
check("top-p 滑条：0.1~1 连续（步长 0.01）",
      !!topp && topp.min === 0.1 && topp.max === 1 && topp.step === 0.01, JSON.stringify(topp));
check("两条滑条旁边都标着建议值",
      !!(box && box.bests.length === 2 && box.bests.join(" ").includes("0.6")
         && box.bests.join(" ").includes("0.95")), box ? box.bests.join(" / ") : "");
check("每条滑条都有说明文字", !!(box && box.hints === 2), box ? "hints=" + box.hints : "");
check("面板上写明了「立刻生效，不用重启」",
      !!(box && box.sub.includes("立刻生效")), box ? box.sub.slice(0, 46) : "");
check("初始值＝当前配置（0.6 / 0.95）",
      !!temp && Math.abs(temp.value - 0.6) < 1e-6 && Math.abs(topp.value - 0.95) < 1e-6,
      box ? box.ranges.map(r => r.key + "=" + r.value).join(" ") : "");

// ---------- ③ 拖动 → 显示 + 保存 ----------
console.log("\n③ 拖动温度滑条 → 数值跟着变、后端也存了");
await ev(`(() => {
  const r = document.querySelector('.sample-range[data-key="temperature"]');
  r.value = 1.35;
  r.dispatchEvent(new Event("input", { bubbles: true }));
})()`);
await sleep(300);
const shown = await ev(`(() => {
  const v = document.querySelector('.sample-val[data-key="temperature"]');
  return { text: v.textContent.trim(), off: v.classList.contains("off") };
})()`);
check("滑条旁的数字实时变成 1.35", shown && shown.text === "1.35", JSON.stringify(shown));
check("偏离建议值时数字标黄（给了视觉反馈）", !!(shown && shown.off));
await sleep(900);                       // 等防抖 350ms + 请求
const c1 = await cfgOf();
check("后端配置里的温度真的变成 1.35（拖动即保存）",
      Math.abs(Number(c1.temperature) - 1.35) < 1e-6, "temperature=" + c1.temperature);
check("提示区显示「已保存…立即生效」",
      (await ev(`(document.querySelector(".sample-msg")||{}).textContent||""`)).includes("立即生效"),
      await ev(`(document.querySelector(".sample-msg")||{}).textContent||""`));

// ---------- ④ top-p 也能拖 ----------
console.log("\n④ 拖动 top-p 滑条");
await ev(`(() => {
  const r = document.querySelector('.sample-range[data-key="top_p"]');
  r.value = 0.62;
  r.dispatchEvent(new Event("input", { bubbles: true }));
})()`);
await sleep(900);
const c2 = await cfgOf();
check("top-p 也存进去了", Math.abs(Number(c2.top_p) - 0.62) < 1e-6, "top_p=" + c2.top_p);

// ---------- ⑤ 点「建议 0.6」跳回去 ----------
console.log("\n⑤ 点建议值 / 恢复建议值");
await ev(`document.querySelector('.sample-best[data-key="temperature"]').click()`);
await sleep(900);
const c3 = await cfgOf();
check("点「建议 0.6」→ 值回到 0.6（并已保存）",
      Math.abs(Number(c3.temperature) - 0.6) < 1e-6, "temperature=" + c3.temperature);

await ev(`document.querySelector(".sample-reset").click()`);
await sleep(900);
const c4 = await cfgOf();
check("「恢复建议值」→ 温度 0.6、top-p 0.95",
      Math.abs(Number(c4.temperature) - 0.6) < 1e-6 && Math.abs(Number(c4.top_p) - 0.95) < 1e-6,
      "temp=" + c4.temperature + " top_p=" + c4.top_p);

// ---------- ⑥ 关闭 ----------
console.log("\n⑥ 关闭面板");
await ev(`document.querySelector(".sample-close").click()`);
await sleep(500);
check("点「完成」后面板关闭",
      await ev(`!document.querySelector(".sample-layer")`));
check("再点按钮还能打开（不会因为关闭一次就失灵）", await ev(`(async () => {
  document.getElementById("sampleBtn").click();
  await new Promise(r => setTimeout(r, 500));
  return !!document.querySelector(".sample-layer");
})()`));
await ev(`document.querySelector(".sample-close").click()`);

// ---------- ⑦ 界面截图（留证） ----------
await ev(`document.getElementById("sampleBtn").click()`);
await sleep(600);
const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot.result && shot.result.data) {
  const fs = await import("node:fs");
  fs.writeFileSync("C:/Users/19853/AppData/Local/Temp/mm_sample_panel.png",
                   Buffer.from(shot.result.data, "base64"));
  check("面板截图已保存", true, "mm_sample_panel.png");
}
await ev(`document.querySelector(".sample-close").click()`);

console.log("\n" + "=".repeat(62));
console.log("通过 " + PASS + " 项，失败 " + FAIL + " 项");
console.log("=".repeat(62));
ws.close();
process.exit(FAIL ? 1 : 0);
