"use strict";
/* F10 · index.html：搜索框只有 placeholder 没有可访问名称；#toast 没有 role/aria-live
   位置：project/homecinema/web/index.html:20（<input id="search" …>）、index.html:29（<div id="toast">）
         相关行为：app.js:79-84 toast() 只改 textContent —— 播放成功、扫描完成、操作失败全靠它播报。
   正确行为：控件要有 <label for> 或 aria-label；动态提示区要 role="status"/aria-live="polite"。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert, makeApp } = require("./lib/harness.js");
const { readHtml } = require("./lib/css.js");

const html = readHtml();
const searchTag = (html.match(/<input[^>]*id="search"[^>]*>/i) || ["(找不到)"])[0];
const toastTag = (html.match(/<(div|output)[^>]*id="toast"[^>]*>/i) || ["(找不到)"])[0];
console.log("搜索框标签 :", searchTag);
console.log("提示区标签 :", toastTag);

/* app.js 确实用这两处做全部用户反馈 */
const app = makeApp({ hash: "#/" });
app.toastEl.textContent = "已开始后台扫描";
console.log("toast 写入方式: textContent（无 aria-live，屏幕阅读器不会朗读）");

const named = /aria-label=|aria-labelledby=|<label[^>]*for="search"/i.test(html);
const live = /role="(status|alert|log)"|aria-live=/i.test(toastTag);

const ok1 = assert(named, "搜索框必须有可访问名称（aria-label 或 <label for=\"search\">）");
const ok2 = assert(live, "#toast 必须是 live region（role=\"status\" 或 aria-live）");

const bad = !(ok1 && ok2);
console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 读屏用户不知道搜索框是什么，也听不到任何操作结果"
                : "RESULT: 未发现失败（此复现不成立）");
process.exit(bad ? 1 : 0);
