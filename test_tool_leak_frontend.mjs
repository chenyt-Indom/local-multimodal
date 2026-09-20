// 「工具调用泄漏进正文」前端兜底闸门的单元测试（不需要浏览器，直接跑 node）
//
// 跑法：
//   "C:\Users\19853\.workbuddy\binaries\node\versions\22.22.2\node.exe" test_tool_leak_frontend.mjs
//
// ### 为什么要有它
// 2026-09-21 用户反馈：模型偶尔把工具调用包成
//     <function-call>{ "name": "search_knowledge", "arguments": {…} }</function-call>
// 写进**正文**，界面上就渲染成一坨裸露的 JSON，而工具根本没执行。
// 后端已经会接住并真去执行；前端这一层防的是**历史会话回放** + 后端没认出来的变体。
//
// ### 两种验法
// ① 把 app.js 里的 stripToolLeak **原样抠出来**跑用例（测的是真代码，不是副本）；
// ② 一条**静态守卫**：stripToolLeak 必须定义在**外层作用域**（2 空格缩进、且在
//    `async function doSend` **之前**）—— renderAnswerLinks 在 doSend 之外，
//    定义在内层的话它调用时会直接 ReferenceError（我第一版就是这么写错的）。
import fs from "fs";
import path from "path";
import url from "url";

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(HERE, "frontend", "app.js"), "utf8");

let PASS = 0, FAIL = 0;
const check = (name, ok, detail = "") => {
  if (ok) { PASS++; console.log(`  [OK] ${name}${detail ? "  " + detail : ""}`); }
  else { FAIL++; console.log(`  [!!] ${name}${detail ? "  " + detail : ""}`); }
};

// ---------- ① 把真代码抠出来 ----------
function grab(re) {
  const m = SRC.match(re);
  if (!m) throw new Error("抠不到：" + re);
  return m[0];
}
const REGEX_SRC = [
  grab(/^ {2}const TOOL_LEAK_XML_RE = .*$/m),
  grab(/^ {2}const TOOL_LEAK_TAG_RE = .*$/m),
  grab(/^ {2}const TOOL_LEAK_FENCE_RE = .*$/m),
  grab(/^ {2}const TOOL_LEAK_STRAY_RE = .*$/m),
  grab(/^ {2}const TOOL_LEAK_HEAD_RE = .*$/m),
].join("\n");
const FUNC_SRC = grab(/^ {2}function stripToolLeak\(text\) \{[\s\S]*?\n {2}\}/m);
// eslint-disable-next-line no-new-func
const stripToolLeak = new Function(
  REGEX_SRC + "\n" + FUNC_SRC + "\nreturn stripToolLeak;")();

console.log("=".repeat(64));
console.log("工具调用泄漏 —— 前端兜底闸门");
console.log("=".repeat(64));

// ---------- ② 截图那一轮的原样文本 ----------
const LEAK = [
  "我帮你在知识库里查一下。",
  "",
  "<function-call>",
  "{",
  '  "name": "search_knowledge",',
  '  "arguments": {',
  '    "query": "名侦探柯南 中柯哀 vs 新兰 角色塑造 社会价值观 分析",',
  '    "list_all": false',
  "  }",
  "}",
  "</function-call>",
].join("\n");

console.log("\n① 截图那一轮的原样文本");
const c1 = stripToolLeak(LEAK);
check("壳子被摘掉", !/function[-_ ]?call/i.test(c1), JSON.stringify(c1.slice(0, 60)));
check("裸 JSON 不残留", !c1.includes('"arguments"') && !c1.includes('"query"'));
check("它自己的说明文字保留", c1.includes("我帮你在知识库里查一下"), JSON.stringify(c1));
check("不会留下多余空行", !/\n{3,}/.test(c1));
check("再跑一次结果一样（幂等）", stripToolLeak(c1) === c1);

console.log("\n② 各种写法");
for (const [name, txt] of [
  ["<tool_call>", '前文\n<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>\n后文'],
  ["大写 <FUNCTION-CALL>", '前文\n<FUNCTION-CALL>\n{"name": "x", "arguments": {}}\n</FUNCTION-CALL>'],
  ["下划线 <function_call>", '前文\n<function_call>\n{"name": "x", "arguments": {}}\n</function_call>'],
  ["带空格 <function call>", '前文\n<function call>\n{"name": "x", "arguments": {}}\n</function call>'],
  ["只剩开标签（被截断）", '前文\n<function-call>\n{"name": "x", "arguments": {}}'],
  ["空壳标签", "前文\n<function-call></function-call>\n后文"],
  ["```tool 围栏", "前文\n```tool\n{\"name\": \"x\", \"arguments\": {}}\n```\n后文"],
]) {
  const out = stripToolLeak(txt);
  const clean = !/function[-_ ]?call|tool[-_ ]?call|```tool/i.test(out)
    && !out.includes('"arguments"');
  check(`${name} → 清干净`, clean, JSON.stringify(out.slice(0, 50)));
}

console.log("\n②b 「半个标签」——模型写到一半放弃留下的碎片（实机复现过）");
for (const [name, txt, want] of [
  ["单独占一行", "<function\n以下是键值对的示例：\n```json\n{}\n```", "以下是键值对的示例"],
  ["正文最开头", "<function以下是键值对的示例", "以下是键值对的示例"],
  ["<tool_call 半截独占一行", "<tool_call\n正文开始", "正文开始"],
  ["半截 + 前后都有正文", "前面的话\n<function\n后面的话", "前面的话"],
]) {
  const out = stripToolLeak(txt);
  check(`${name} → 碎片被清掉`, !/<\s*\/?\s*(?:function|tool)/i.test(out), JSON.stringify(out.slice(0, 46)));
}
check("正常的正文不受影响（没有 < 时原样返回）",
  stripToolLeak("这是一段普通的回答，没有任何标签。") === "这是一段普通的回答，没有任何标签。");
const explain = "<function> 是 JS 里定义函数的关键字，写法是 function foo() {}";
check("⚠️ 讲标签用法的句子不被整段吃掉",
  stripToolLeak(explain).includes("是 JS 里定义函数的关键字"), JSON.stringify(stripToolLeak(explain)));

console.log("\n③ ⚠️ 不能误伤正常内容（宁可少删，不可多删）");
const mdJson = '这是接口返回：\n```json\n{"name": "张三", "arguments": {"age": 20}}\n```\n请参考。';
check("```json 代码块原样保留", stripToolLeak(mdJson) === mdJson);
const plainLt = "当 1 < 2 成立时输出 yes，用 a<b 判断。";
check("正文里的 '<' 不受影响", stripToolLeak(plainLt) === plainLt, JSON.stringify(stripToolLeak(plainLt)));
const htmlSnippet = "示例 HTML：\n```html\n<div class=\"tool-call-box\">hi</div>\n```";
check("HTML 里的 tool-call 字样不受影响", stripToolLeak(htmlSnippet) === htmlSnippet);
check("空输入安全", stripToolLeak("") === "" && stripToolLeak(null) === "" && stripToolLeak(undefined) === "");

console.log("\n④ 静态守卫：定义必须在**外层作用域**（否则 renderAnswerLinks 调不到）");
const lines = SRC.split("\n");
const defLine = lines.findIndex((l) => l.includes("function stripToolLeak"));
const sendLine = lines.findIndex((l) => l.includes("async function doSend("));
check("找得到 stripToolLeak 的定义", defLine >= 0, "第 " + (defLine + 1) + " 行");
check("定义在 doSend **之前**（外层）", defLine >= 0 && sendLine > 0 && defLine < sendLine,
  `def=L${defLine + 1} doSend=L${sendLine + 1}`);
check("定义缩进是 2 空格（外层作用域）", /^ {2}function stripToolLeak\(text\) \{/.test(lines[defLine]),
  JSON.stringify(lines[defLine]));
check("恰好只定义一次", (SRC.match(/function stripToolLeak/g) || []).length === 1);
check("三处调用点都在", (SRC.match(/stripToolLeak\(/g) || []).length === 4);

console.log("\n" + "=".repeat(64));
console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
console.log("=".repeat(64));
process.exit(FAIL ? 1 : 0);
