"use strict";
/* F04 · 剧集行只用 div + click 委托，键盘完全够不着：无法用键盘播放某一集
   位置：project/homecinema/web/app.js:216-228 epRowHTML()（'<div class="ep-row" data-ep=…>'）
         project/homecinema/web/app.js:285-290 只在 ep-list 上绑了 "click"
         project/homecinema/web/app.css:381-394 .ep-row { cursor: pointer }（视觉上是可点元素，实际不可聚焦）
   正确行为：整行应是可聚焦的控件（<button>/<a>，或 tabindex="0" + role + keydown Enter/Space），
             否则只用键盘（或 VoiceOver/Switch Control）的用户看不到、也点不了任何一集。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert, makeApp, SHOW } = require("./lib/harness.js");

(async () => {
  const app = makeApp({ hash: "#/show/2" });
  await app.tick(0);
  app.lastRequest().respond(200, SHOW);
  await app.flush();

  const epList = app.el("ep-list");
  const html = epList.innerHTML;
  console.log("ep-list 片段:\n" + html.slice(0, 320) + "\n");
  console.log("行数                :", (html.match(/class="ep-row"/g) || []).length);
  console.log("行里有 tabindex     :", /tabindex/.test(html));
  console.log("行里有 role=        :", /role="/.test(html));
  console.log("行是 <button>/<a>   :", /<(button|a)\b/i.test(html));
  console.log("ep-list 绑定的事件   :", Object.keys(epList._handlers));
  console.log("整页可聚焦元素序列    :", (app.view.innerHTML.match(/<(button|a)\b[^>]*>/g) || []).map((s) => s.slice(0, 46)));

  const focusable = /<div class="ep-row"[^>]*\btabindex="0"/.test(html) || /<(button|a)\b[^>]*class="ep-row/.test(html);
  const ok1 = assert(focusable, '每一行 .ep-row 必须可聚焦（tabindex="0" 或用 <button>/<a>）');
  const ok2 = assert((epList._handlers.keydown || []).length > 0
    || /onkey/i.test(html), "需要 Enter/Space 键盘激活（keydown 处理）");
  const ok3 = assert(/role="(button|link)"/.test(html), '需要 role="button"/"link" 让辅助技术识别为可操作');

  const bad = !(ok1 && ok2 && ok3);
  console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 键盘用户无法播放任何一集"
                  : "RESULT: 未发现失败（此复现不成立）");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
