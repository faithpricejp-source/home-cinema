"use strict";
/* F01 · route() 没有请求代次守卫：慢的旧请求会覆盖新页面的渲染结果
   位置：project/homecinema/web/app.js:438-461 route()、144-169 renderList()、115-140 renderHome()
   正确行为：视图与导航高亮始终反映「最后一次路由」的数据；被丢弃的旧响应不得写入 $view。
   本脚本断言正确行为 —— 在当前代码上应当失败（退出码 1 = 发现成立）。 */

const { assert, makeApp, HOME, list } = require("./lib/harness.js");

(async () => {
  const app = makeApp({ hash: "#/" });
  await app.tick();
  app.lastRequest().respond(200, HOME);
  await app.flush();

  /* 1) 用户先点「电影」 */
  app.setHash("#/movies");
  await app.tick();
  const rMovies = app.lastRequest();
  console.log("请求 A:", rMovies.method, rMovies.url);

  /* 2) 紧接着点「剧集」（/api/movies 数据量大，还没回来） */
  app.setHash("#/shows");
  await app.tick();
  const rShows = app.lastRequest();
  console.log("请求 B:", rShows.method, rShows.url);

  /* 3) 剧集先回来（正常），渲染剧集页 */
  rShows.respond(200, list("shows", "【剧集页】"));
  await app.flush();
  console.log("剧集响应后视图含「【剧集页】」:", app.view.innerHTML.includes("【剧集页】"));

  /* 4) 电影列表慢一步回来 —— 旧响应覆盖当前页面 */
  rMovies.respond(200, list("movies", "【过期电影页】"));
  await app.flush();

  const html = app.view.innerHTML;
  const navActive = app.nav.children.filter((a) => a.classList.contains("active")).map((a) => a.dataset.nav);
  console.log("当前 hash        :", app.location.hash);
  console.log("导航高亮         :", JSON.stringify(navActive));
  console.log("视图标题片段     :", (html.match(/<h2>(.*?)<\/h2>/) || [])[0]);
  console.log("视图里的卡片标题 :", (html.match(/class="card-t">([^<]*)</g) || []).join(" | "));

  let ok = true;
  ok = assert(!html.includes("【过期电影页】"),
    "剧集页 (#/shows) 不应被更早发出的 /api/movies 响应覆盖") && ok;
  ok = assert(html.includes("【剧集页】"),
    "视图应显示最后一次路由（剧集页）的数据") && ok;

  /* 5) 同一守卫缺失的第二个触发点：快速连点排序 */
  app.setHash("#/movies");
  await app.tick();
  app.lastRequest().respond(200, list("movies", "电影"));
  await app.flush();
  const sel = app.el("sort");
  sel.value = "title";
  sel.fire("change");                       // 请求 C：sort=title
  await app.tick();
  const rTitle = app.lastRequest();
  sel.value = "year";
  sel.fire("change");                       // 请求 D：sort=year（用户最终选择）
  await app.tick();
  const rYear = app.lastRequest();
  console.log("排序请求 C/D     :", rTitle.url, "->", rYear.url);
  rYear.respond(200, list("movies", "按年份结果"));
  await app.flush();
  rTitle.respond(200, list("movies", "按片名结果(过期)"));
  await app.flush();
  console.log("排序竞态后视图   :", (app.view.innerHTML.match(/class="card-t">([^<]*)</) || [])[1]);
  ok = assert(!app.view.innerHTML.includes("按片名结果(过期)"),
    "选中「按年份」后不应被更早的「按片名」响应覆盖") && ok;

  console.log(ok ? "RESULT: 未发现失败（此复现不成立）"
                 : "RESULT: BUG REPRODUCED (exit 1) — 旧请求覆盖了新页面");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
