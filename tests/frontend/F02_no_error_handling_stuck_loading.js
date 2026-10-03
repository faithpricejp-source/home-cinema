"use strict";
/* F02 · 所有 render* 都没有错误处理：接口 4xx/5xx 或返回空/非数组时页面永久停在「加载中…」
   位置：project/homecinema/web/app.js:115-140 renderHome / 144-169 renderList / 173-212 renderMovie
         / 230-295 renderShow / 326-343 renderRecs；错误 HTML 生成器 errorHTML()（app.js:88）全文件未被调用。
   正确行为：请求失败时视图显示可读错误（errorHTML），并给出返回入口；不得遗留 loading，
             也不得把 rejection 抛成 window 上的 unhandledrejection。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert, makeApp, HOME } = require("./lib/harness.js");

const unhandled = [];
process.on("unhandledRejection", (r) => unhandled.push(String(r && r.message || r)));

(async () => {
  const app = makeApp({ hash: "#/" });
  await app.tick();
  app.lastRequest().respond(200, HOME);
  await app.flush();

  /* 场景 1：详情页 id 不存在 —— server.py:376-381 返回 404 {"detail":"电影不存在"} */
  app.setHash("#/movie/9999");
  await app.tick();
  const r1 = app.lastRequest();
  console.log("请求:", r1.method, r1.url);
  r1.respond(404, { detail: "电影不存在" });
  await app.flush();
  await app.flush();
  console.log("视图 HTML        :", JSON.stringify(app.view.innerHTML));
  console.log("未处理 rejection :", unhandled.map((e) => e.slice(0, 60)));

  assert(/class="error"/.test(app.view.innerHTML), "404 时视图应渲染 errorHTML（class=\"error\"）");
  const shown1 = app.view.innerHTML.includes("电影不存在");
  console.log("视图包含后端错误文案:", shown1);
  const ok1 = assert(shown1, "用户应看到「电影不存在」这类可读原因，而不是一直转圈");
  const loadingGone1 = assert(!app.view.innerHTML.includes("加载中"),
    "请求失败后不应停留在 loadingHTML");
  const noReject1 = assert(unhandled.length === 0,
    "route() 的失败不得成为 unhandledrejection（app.js:517 直接 addEventListener(route)）");

  /* 场景 2：5xx 且响应体不是 JSON（Starlette 的 PlainText「Internal Server Error」） */
  app.setHash("#/");
  await app.tick();
  const r2 = app.lastRequest();
  r2.respond(200, HOME);           // 先让首页回来
  await app.flush();
  app.setHash("#/movies");
  await app.tick();
  const r3 = app.lastRequest();
  r3.respond(500, "Internal Server Error");
  await app.flush();
  await app.flush();
  console.log("500 之后视图 HTML :", JSON.stringify(app.view.innerHTML));
  const ok2 = assert(/class="error"/.test(app.view.innerHTML),
    "500 时视图应显示错误而不是 loadingHTML");

  /* 场景 3：200 + null（反代/空响应） —— apiCall 把 null 当成功返回，renderHome 立刻 TypeError */
  app.setHash("#/recs");
  await app.tick();
  app.lastRequest().respond(200, null);
  await app.flush();
  await app.flush();
  console.log("null 响应后视图   :", JSON.stringify(app.view.innerHTML));
  console.log("累计未处理 rejection:", unhandled.length,
    unhandled.map((e) => e.slice(0, 50)));
  const ok3 = assert(/class="error"/.test(app.view.innerHTML),
    "空/异常响应也应显示错误态而不是永久 loading");

  const bad = !(ok1 && loadingGone1 && noReject1 && ok2 && ok3);
  console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 错误路径完全没有处理"
                  : "RESULT: 未发现失败（此复现不成立）");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
