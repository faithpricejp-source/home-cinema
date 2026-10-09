"use strict";
/* versions · 同片多版本合并的前端行为：
   1) 海报墙卡片：version_count>1 显示「N 个版本」角标，=1 不显示；
   2) 详情页列出全部版本：版本名、分辨率（空则不显示）、容器、大小、各自进度；
   3) 主播放按钮走 default_version_id；点某一版本按该版本 id 请求播放。
   成功 exit 0，任一断言失败 exit 1。只用 Node 内置模块 + 本目录假 DOM。 */

const { assert, makeApp } = require("./lib/harness.js");

const HOME = {
  continue_watching: [],
  recently_added: [],
  movies: [
    { type: "movie", id: 1, href: "#/movie/1", title: "Watchmen", year: 2009,
      status: "matched", poster: null, backdrop: null, added_at: "x", watched: false,
      version_count: 2 },
    { type: "movie", id: 3, href: "#/movie/3", title: "Solo Film", year: 2001,
      status: "unmatched", poster: null, backdrop: null, added_at: "x", watched: false,
      version_count: 1 },
  ],
  shows: [],
};

function VER(id, extra) {
  return Object.assign({
    id: id, name: "Ultimate Cut", video_height: 1080, ext: "mp4", size: 2147483648,
    watched: false, position_sec: 600, duration_sec: 7200, progress: 0.083,
    played_at: "2026-10-01T00:00:00+00:00", is_default: false,
  }, extra || {});
}

const DETAIL = {
  type: "movie", id: 1, href: "#/movie/1", title: "Watchmen", year: 2009,
  status: "matched", poster: "p.jpg", backdrop: null, added_at: "x", watched: false,
  overview: "简介", genres: ["动作"], rating: 7.5, runtime_minutes: 164,
  position_sec: null, duration_sec: null, progress: null,
  version_count: 2, default_version_id: 2,
  versions: [
    VER(1),
    VER(2, { name: "Extended Cut", video_height: null, ext: "mkv", size: 700000000,
             watched: true, position_sec: 0, duration_sec: null, progress: null,
             is_default: true }),
  ],
};

(async () => {
  let ok = true;

  /* ---------- 1) 海报墙角标 ---------- */
  const app = makeApp({ hash: "#/" });
  await app.tick();
  app.lastRequest().respond(200, HOME);
  await app.flush();
  const homeHtml = app.view.innerHTML;
  ok = assert((homeHtml.match(/ver-badge/g) || []).length === 1,
    "两张卡里只有多版本的那张带角标") && ok;
  ok = assert(homeHtml.includes("2 个版本"), "角标文案为「2 个版本」") && ok;
  ok = assert(homeHtml.includes("Watchmen") && homeHtml.includes("Solo Film"),
    "两个条目各渲染一张卡") && ok;

  /* ---------- 2) 详情页版本列表 ---------- */
  app.setHash("#/movie/1");
  await app.tick();
  app.lastRequest().respond(200, DETAIL);
  await app.flush();
  const dHtml = app.view.innerHTML;
  ok = assert(dHtml.includes("Ultimate Cut"), "版本行显示版本名") && ok;
  ok = assert(dHtml.includes("1080p"), "显示分辨率") && ok;
  ok = assert(dHtml.includes("MP4") && dHtml.includes("MKV"), "显示容器格式（大写扩展名）") && ok;
  ok = assert(dHtml.includes("2.0 GB") && dHtml.includes("668 MB"), "显示文件大小") && ok;
  ok = assert(dHtml.includes("看到 10:00"), "未看完的版本显示自己的观看进度") && ok;
  ok = assert(dHtml.includes("已看"), "已看的版本显示已看状态") && ok;
  ok = assert((dHtml.match(/ver-tag/g) || []).length === 1 && dHtml.includes("默认"),
    "恰好一个版本带「默认」标记") && ok;
  ok = assert((dHtml.match(/data-ver="/g) || []).length === 2, "两个版本各一行") && ok;

  /* ---------- 3) 播放目标 ---------- */
  app.el("play").fire("click");
  await app.tick();
  const plays = app.reqs().filter((r) => r.url === "/api/play");
  ok = assert(plays.length >= 1, "主播放按钮发出了 /api/play 请求") && ok;
  if (plays.length) {
    const body = JSON.parse(plays[plays.length - 1].body);
    ok = assert(body.id === 2 && body.type === "movie",
      "主按钮播放默认版本（default_version_id=2）", body) && ok;
    plays[plays.length - 1].respond(200, { path: "/x.mkv", start_at: 0, title: "Watchmen" });
  }
  await app.flush();

  const verList = app.el("ver-list");
  ok = assert(!!verList && verList.handlerCount("click") >= 1, "版本列表绑定了点击播放") && ok;
  if (verList) {
    verList.fire("click", {
      target: { closest: (sel) => (String(sel) === ".ep-row" ? { dataset: { ver: "1" } } : null) },
    });
    await app.tick();
    const p2 = app.reqs().filter((r) => r.url === "/api/play");
    ok = assert(p2.length >= 2, "点击版本行发出了新的 /api/play 请求") && ok;
    if (p2.length >= 2) {
      const b2 = JSON.parse(p2[p2.length - 1].body);
      ok = assert(b2.id === 1, "按被点击版本的 id 播放", b2) && ok;
      p2[p2.length - 1].respond(200, { path: "/y.mp4", start_at: 0, title: "Watchmen" });
    }
  }
  await app.flush();

  console.log(ok ? "RESULT: OK" : "RESULT: FAILED (exit 1)");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
