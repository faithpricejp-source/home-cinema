"use strict";
/* F08 · esc() 明明覆盖了引号，但若干「拼进 HTML 属性/URL」的位置直接跳过了它
   位置（project/homecinema/web/app.js）：
     :68  cardHTML          '<a class="card" href="' + item.href + '">'
     :103 continueCardHTML   '<a class="cw-card…" href="' + item.href + '">'
     :311 recCardHTML        '" data-id="' + item.tmdb_id + '" data-url="' + esc(item.url) + '">'
     :220 epRowHTML         '<div class="ep-row" data-ep="' + ep.id + '">'
     :62  posterHTML        '<img class="' + cls + '" … src="' + src + '">'   (src 走 imgUrl->encodeURIComponent，安全)
     :181/:182/:184/:238/:241 renderMovie/renderShow 把 m.year、m.runtime_minutes、s.episode_count 直接推进 metaBits
   正确行为：任何来自数据（后端/TMDB/文件名）的值在写进 HTML/属性前都要过 esc()（数字用 Number 校验）。
   当前后端这些字段都是服务端用整型 id 拼出来的，所以还没到「可直接打」的程度 —— 但这是把转义责任
   默默交给上游的写法：只要将来有一处 href/id 变成可写来源（例如 tmdb_id 允许字符串、href 改由前端拼），
   立刻变成属性注入。本脚本用带引号的载荷证明这些位置确实不转义。
   断言正确行为 —— 在当前代码上应当失败。 */

const fs = require("fs");
const { assert, makeApp, EP } = require("./lib/harness.js");

const app = makeApp({ hash: "#/" });        // 只用它把 app.js 的函数取出来
const D = app.__diag;

/* ---------- 1) 静态：确认这些位置在源码里就没过 esc() ---------- */
const src = fs.readFileSync(require("path").resolve(__dirname, "../../homecinema/web/app.js"), "utf8");
const lines = src.split("\n");
const SITES = [
  ["cardHTML href", /class="card" href="'\s*\+\s*item\.href/],
  ["continueCardHTML href", /cw-card'[\s\S]{0,60}?href="'\s*\+\s*item\.href/],
  ["recCardHTML data-id", /data-id="'\s*\+\s*item\.tmdb_id/],
  ["epRowHTML data-ep", /data-ep="'\s*\+\s*ep\.id/],
];
console.log("静态扫描（属性位置上的裸拼接）：");
const rawSites = [];
const seen = new Set();
for (const [name, re] of SITES) {
  lines.forEach((line, idx) => {
    if (!re.test(line) || seen.has(idx)) return;
    seen.add(idx);
    const wrapped = new RegExp(re.source.replace("'\\s*\\+\\s*", "'\\s*\\+\\s*esc\\(")).test(line);
    console.log("  app.js:" + (idx + 1) + " " + name + " -> " + (wrapped ? "已 esc" : "裸拼接") +
      "   |   " + line.trim().slice(0, 92));
    if (!wrapped) rawSites.push(name + "@" + (idx + 1));
  });
}
/* 对照组：同一行里 title 是走了 esc() 的 */
console.log("  对照 app.js:69 card-t 位置:", /'\s*\+\s*esc\(item\.title\)/.test(src) ? "已 esc" : "裸拼接");

/* ---------- 2) 动态：带引号的载荷 ---------- */
const PAY = { q: '" onmouseover="alert(1)', marker: "onmouseover=\"alert(1)" };
const cases = [
  ["cardHTML href", () => D.cardHTML({ title: "T", href: "#/movie/1" + PAY.q }, "sub")],
  ["continueCardHTML href", () => D.continueCardHTML({ type: "movie", title: "T", href: "#/movie/1" + PAY.q, position_sec: 10, progress: 0.2 })],
  ["recCardHTML data-id", () => D.recCardHTML({ kind: "movie", tmdb_id: "1" + PAY.q, title: "T", poster: null, year: 2000, rating: null, because: [], url: "https://tmdb/1" })],
  ["epRowHTML data-ep", () => D.epRowHTML(Object.assign(EP(1, 1), { id: "9" + PAY.q }))],
];

let broken = [];
for (const [name, fn] of cases) {
  const html = fn();
  const inject = /onmouseover="alert\(1\)/.test(html);
  console.log("\n" + name + " 输出:\n  " + html.slice(0, 150));
  console.log("  出现可执行属性注入:", inject);
  if (inject) broken.push(name);
}

/* 对照组：走 esc() 的标题确实安全；esc() 本身覆盖引号 */
const safe = D.cardHTML({ title: '<img src=x onerror="alert(1)">', href: "#/movie/1" }, "s");
console.log("\n对照 cardHTML(title) 输出:", safe.slice(0, 130));
const titleSafe = !/<img src=x/.test(safe);
assert(titleSafe, "（对照）经 esc() 的 title 不会逃逸");
console.log("对照 esc() 覆盖:", JSON.stringify(D.esc(`&<>"'`)));
assert(D.esc(`"'`) === "&quot;&#39;", "（对照）esc() 覆盖双引号和单引号");

const okStatic = assert(rawSites.length === 0,
  "静态：href/data-id/data-ep 不应裸拼接（实际 " + rawSites.length + " 处：" + rawSites.join(", ") + "）");
const okDyn = assert(broken.length === 0, "动态：注入载荷不应产生第二个属性（实际逃逸 " + broken.length + " 处）");

const bad = !(okStatic && okDyn);
console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 4 处属性位置裸拼接，载荷可逃逸"
                : "RESULT: 未发现失败（此复现不成立）");
process.exit(bad ? 1 : 0);
