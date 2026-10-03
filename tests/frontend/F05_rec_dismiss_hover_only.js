"use strict";
/* F05 · 「不感兴趣」按钮只在 :hover 时出现：键盘用户 Tab 到它时它仍然 opacity:0 + pointer-events:none
   位置：project/homecinema/web/app.css:486-508
         .rec-dismiss{ …opacity:0; pointer-events:none }
         .rec-card:hover .rec-dismiss{ opacity:1; pointer-events:auto }
         （app.css:510-512 的 @media (max-width:900px) 只在窄屏兜底，宽窗/键盘场景没有）
         对应 markup：project/homecinema/web/app.js:313
   正确行为：除 :hover 外还要有 :focus / :focus-visible / .rec-card:focus-within 规则把按钮显示出来。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert, makeApp, RECS } = require("./lib/harness.js");
const { rules, readCss } = require("./lib/css.js");

(async () => {
  const app = makeApp({ hash: "#/recs" });
  await app.tick(0);
  app.lastRequest().respond(200, RECS);
  await app.flush();
  const markup = app.view.innerHTML;
  console.log("推荐卡里的按钮   :", (markup.match(/<button class="rec-dismiss"[^>]*>/) || ["(无)"])[0]);
  console.log("卡片本身的标签   :", (markup.match(/<div class="card rec-card"/) || ["(无)"])[0]);
  console.log("卡片是否可聚焦   :", /class="card rec-card"[^>]*tabindex/.test(markup) ? "是" : "否（div，无 tabindex）");

  const all = rules(readCss());
  const rel = all.filter((r) => /rec-dismiss|rec-card/.test(r.sel));
  console.log("\n相关 CSS 规则（原文，含所在媒体条件）:");
  for (const r of rel) console.log("  " + r.media + r.sel + " { " + r.decl.replace(/\s+/g, " ") + " }");

  const desktop = rel.filter((r) => !r.media);           // 无媒体条件 = 默认（宽窗）生效
  const baseHidden = desktop.some((r) => /\.rec-dismiss$/.test(r.sel)
    && /opacity:\s*0/.test(r.decl) && /pointer-events:\s*none/.test(r.decl));
  const focusReveal = desktop.filter((r) => /:focus\b|:focus-visible|:focus-within/.test(r.sel));
  const hoverReveal = desktop.filter((r) => /:hover/.test(r.sel));
  console.log("\n宽窗下基态隐藏          :", baseHidden);
  console.log("宽窗下 focus 显示按钮的规则:", focusReveal.length ? focusReveal.map((r) => r.sel) : "无");
  console.log("宽窗下 hover 显示按钮的规则:", hoverReveal.map((r) => r.sel));

  const ok1 = assert(baseHidden, "（前提）默认状态确实是 opacity:0 + pointer-events:none");
  const ok2 = assert(focusReveal.length > 0,
    "宽窗下也必须有 :focus/:focus-within 规则让按钮在键盘聚焦时可见、可点");

  const bad = ok1 && !ok2;
  console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 键盘聚焦时「不感兴趣」不可见也不可点"
                  : "RESULT: 未发现失败（此复现不成立）");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
