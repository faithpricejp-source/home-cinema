"use strict";
/* F07 · select.sort 把焦点轮廓抹掉后又没给任何替代焦点样式（WCAG 2.4.7 Focus Visible）
   位置：project/homecinema/web/app.css:155-163  select.sort { … outline: none; }
         对比 app.css:79-91 #search { outline: none } 有配套的 #search:focus{…}
   正确行为：每个 outline:none 的选择器都要有对应的 :focus / :focus-visible 可见样式。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert } = require("./lib/harness.js");
const { rules, readCss } = require("./lib/css.js");

const rs = rules(readCss());
const killers = rs.filter((r) => /outline:\s*(none|0)/.test(r.decl));
console.log("抹掉焦点轮廓的规则:");
for (const r of killers) console.log("  " + r.media + r.sel + " { …" + r.decl.match(/outline:[^;]+/)[0] + " }");

const offenders = [];
for (const r of killers) {
  const base = r.sel.replace(/:focus(-visible)?/g, "").trim();
  const hasFocusRule = rs.some((f) => /:focus\b|:focus-visible/.test(f.sel) && f.sel.includes(base));
  console.log("  " + base + " -> 配套 :focus 规则: " + (hasFocusRule
    ? "有 (" + rs.filter((f) => f.sel.includes(base) && /:focus/.test(f.sel)).map((f) => f.sel).join(", ") + ")"
    : "无"));
  if (!hasFocusRule) offenders.push(base);
}

console.log("\n没有替代焦点样式的选择器:", JSON.stringify(offenders));
const ok = assert(offenders.length === 0,
  '所有 outline:none 的选择器都应有可见的 :focus 样式（当前 select.sort 没有）');

console.log(ok ? "RESULT: 未发现失败（此复现不成立）"
              : "RESULT: BUG REPRODUCED (exit 1) — 键盘用户看不到排序下拉的焦点位置");
process.exit(ok ? 0 : 1);
