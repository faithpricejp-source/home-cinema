"use strict";
/* F03 · 推荐生成的轮询定时器不随路由清理：完成时无条件 renderRecs()，覆盖用户当前页面；
   离开推荐页再回来又点一次，会出现两个 interval 同时轮询。
   位置：project/homecinema/web/app.js:368-401 startRecRefresh()
   （timer 只在「本次轮询看到完成/出错」时 clearInterval，路由切走不清）
   正确行为：（a）离开推荐页后不得用推荐内容覆盖别的页面；（b）同一时刻最多一个轮询定时器。
   断言正确行为 —— 在当前代码上应当失败。 */

const { assert, makeApp, RECS } = require("./lib/harness.js");

const getRecReqs = (app) => app.reqs().filter((r) => r.method === "GET" && r.url === "/api/recommendations");

(async () => {
  /* ---------- 场景 A：生成完成时用户已经在电影详情页 ---------- */
  const app = makeApp({ hash: "#/recs" });
  await app.tick(0);
  app.lastRequest().respond(200, RECS);
  await app.flush();
  console.log("初始页面        :", app.location.hash, "| 推荐网格:", /rec-grid/.test(app.view.innerHTML));

  const btn = app.el("rec-refresh");
  console.log("找到「生成推荐」按钮:", !!btn);
  btn.fire("click");
  await app.flush();
  const refreshReq = app.lastRequest();
  console.log("请求:", refreshReq.method, refreshReq.url, "body=", refreshReq.body);
  refreshReq.respond(200, { ok: true, already_running: false });
  await app.flush();

  await app.tick(1000);
  let st = app.requestsFor("/api/recommendations/status").pop();
  console.log("轮询 1          :", st.url);
  st.respond(200, { running: true, phase: "running", done: 1, total: 2, movies: 0, shows: 0, error: null });
  await app.flush();

  app.setHash("#/movie/3");
  await app.tick(0);
  const movieReq = app.lastRequest();
  console.log("用户去看电影    :", movieReq.method, movieReq.url);

  await app.tick(1000);
  st = app.requestsFor("/api/recommendations/status").pop();
  st.respond(200, { running: false, phase: "idle", done: 2, total: 2, movies: 1, shows: 0, error: null });
  await app.flush();
  const recReq = getRecReqs(app).filter((r) => !r._done).pop();
  console.log("定时器又发起了  :", recReq && recReq.method + " " + recReq.url);
  if (recReq) recReq.respond(200, RECS);
  await app.flush();

  console.log("当前 hash        :", app.location.hash);
  console.log("视图里的标题      :", (app.view.innerHTML.match(/<h[12]>(.*?)<\/h[12]>/g) || []).join(" "));
  console.log("视图含推荐网格    :", /class="grid rec-grid"/.test(app.view.innerHTML));
  console.log("视图含详情页 h1   :", /<h1>/.test(app.view.innerHTML));
  const okA = assert(!/class="grid rec-grid"/.test(app.view.innerHTML),
    "停在 #/movie/3 时不得被推荐页内容覆盖");

  /* ---------- 场景 B：生成未完成时离开再回来，再点一次 ---------- */
  const app2 = makeApp({ hash: "#/recs" });
  await app2.tick(0);
  app2.lastRequest().respond(200, RECS);
  await app2.flush();
  app2.el("rec-refresh").fire("click");
  await app2.flush();
  app2.lastRequest().respond(200, { ok: true, already_running: false });
  await app2.flush();
  const before = app2.requestsFor("/api/recommendations/status").length;

  for (let i = 0; i < 3; i++) {              // 定时器 A 一直报「还在跑」
    await app2.tick(1000);
    const p = app2.requestsFor("/api/recommendations/status").filter((r) => !r._done).pop();
    if (p) p.respond(200, { running: true, phase: "running", done: 1, total: 30, movies: 0, shows: 0, error: null });
    await app2.flush();
  }
  app2.setHash("#/movies");
  await app2.tick(0);
  const ml = app2.lastRequest();
  if (ml.url.startsWith("/api/movies")) ml.respond(200, []);
  await app2.flush();
  app2.setHash("#/recs");
  await app2.tick(0);
  app2.lastRequest().respond(200, RECS);
  await app2.flush();

  app2.el("rec-refresh").fire("click");       // 第二次点击 —— 定时器 B
  await app2.flush();
  app2.lastRequest().respond(200, { ok: true, already_running: true });
  await app2.flush();
  const mid = app2.requestsFor("/api/recommendations/status").length;
  await app2.tick(2000);                      // 期望：只有 1 个定时器 -> 2 次轮询
  const polls = app2.requestsFor("/api/recommendations/status").length - mid;
  console.log("2 秒内 status 轮询次数:", polls, "（正确实现应为 2）");
  const okB = assert(polls <= 2, "重进推荐页再点一次不应叠加多个轮询定时器");

  const bad = !(okA && okB);
  console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 轮询跨页面覆盖视图，且定时器叠加"
                  : "RESULT: 未发现失败（此复现不成立）");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
