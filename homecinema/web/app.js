/* HomeCinema 前端：原生 JS，hash 路由，无构建、无外部依赖。
   网络层统一走 XMLHttpRequest（同源相对路径；id 一律 parseInt 校验后才拼接）。 */

"use strict";

const $view = document.getElementById("view");
const $search = document.getElementById("search");
const $nav = document.getElementById("nav");
const $rescan = document.getElementById("rescan");
const $toast = document.getElementById("toast");

const state = {
  q: "", sort: "added", season: {},
  // 列表筛选（电影/剧集各一份）；genres 记下未筛选时见过的类型，筛选后下拉框选项不缩水
  filters: { movies: {}, shows: {} }, genres: { movies: [], shows: [] }, allPeople: false,
};

/* ---------- 工具 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* 同源 JSON 请求的唯一出口（XMLHttpRequest）。 */
function apiCall(method, url, bodyObj) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    if (bodyObj !== undefined) {
      xhr.setRequestHeader("Content-Type", "application/json");
    }
    xhr.onerror = () => reject(new Error("网络请求失败"));
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (err) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data);
      } else {
        reject(new Error((data && data.detail) || xhr.statusText || ("HTTP " + xhr.status)));
      }
    };
    xhr.send(bodyObj === undefined ? null : JSON.stringify(bodyObj));
  });
}

function imgUrl(name) {
  return name ? "/img/" + encodeURIComponent(String(name)) : null;
}

function pad(n) { return String(n).padStart(2, "0"); }

function fmt(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
}

function posterHTML(item, cls) {
  cls = cls || "poster";
  const src = imgUrl(item.poster);
  if (src) return '<img class="' + cls + '" loading="lazy" src="' + src + '" alt="">';
  const ch = esc((item.title || "?").trim().charAt(0).toUpperCase() || "?");
  return '<div class="' + cls + ' ph"><span>' + ch + "</span></div>";
}

function cardHTML(item, sub) {
  const badge = item.version_count > 1
    ? '<span class="ver-badge">' + esc(item.version_count) + " 个版本</span>" : "";
  return '<a class="card" href="' + esc(item.href) + '">' + posterHTML(item) + badge +
    '<div class="card-t">' + esc(item.title) + "</div>" +
    (sub ? '<div class="card-s">' + esc(sub) + "</div>" : "") + "</a>";
}

function barHTML(progress) {
  const pct = progress != null ? Math.round(progress * 100) : 0;
  return '<div class="bar"><i style="width:' + pct + '%"></i></div>';
}

let toastTimer = null;
function toast(msg) {
  $toast.textContent = msg;
  $toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.remove("show"), 3200);
}

function loadingHTML() { return '<div class="loading">加载中…</div>'; }

function errorHTML(err) { return '<div class="error">出错了：' + esc(err.message) + "</div>"; }

/* 每次渲染领一个代次：请求回来时代次已变，说明用户已经切走，旧结果直接丢弃，
   免得慢的旧请求盖掉新页面。请求失败显示错误，不停在「加载中…」。 */
let viewGen = 0;
async function fetchView(url) {
  const g = ++viewGen;
  $view.innerHTML = loadingHTML();
  let d;
  try {
    d = await apiCall("GET", url);
  } catch (err) {
    if (g === viewGen) $view.innerHTML = errorHTML(err);
    return null;
  }
  if (g !== viewGen) return null;
  if (d == null) { $view.innerHTML = errorHTML(new Error("服务器返回了空响应")); return null; }
  return d;
}

/* ---------- 首页 ---------- */

function continueCardHTML(item) {
  const bg = imgUrl(item.backdrop) || imgUrl(item.still) || imgUrl(item.poster);
  let title, sub;
  if (item.type === "movie") {
    title = esc(item.title);
    sub = "继续播放 · 从 " + (fmt(item.position_sec) || "0:00");
  } else {
    title = esc(item.show_title) + ' <span class="dim">S' + pad(item.season_number) +
      "E" + pad(item.episode_number) + "</span>";
    sub = esc(item.title);
  }
  return '<a class="cw-card' + (bg ? "" : " ph-bg") + '" href="' + esc(item.href) + '">' +
    (bg ? '<img class="cw-bg" src="' + bg + '" alt="">' : "") +
    '<div class="cw-shade"></div>' +
    '<div class="cw-meta"><div class="cw-title">' + title + '</div>' +
    '<div class="cw-sub">' + sub + "</div>" + barHTML(item.progress) + "</div></a>";
}

function sectionHTML(head, inner) {
  return '<section class="section"><div class="section-head">' + head +
    '<span class="spacer"></span></div>' + inner + "</section>";
}

async function renderHome() {
  const d = await fetchView("/api/home");
  if (!d) return;
  const parts = [];

  const cw = d.continue_watching.map(continueCardHTML).join("");
  parts.push(sectionHTML("<h2>继续观看</h2>",
    cw ? '<div class="row">' + cw + "</div>" : '<div class="empty">暂无正在看的片子</div>'));

  const ra = d.recently_added.map((it) => {
    const sub = it.type === "show" ? "剧集" : (it.year || "");
    return cardHTML(it, sub);
  }).join("");
  parts.push(sectionHTML("<h2>最近添加</h2>",
    ra ? '<div class="row">' + ra + "</div>" : '<div class="empty">还没有入库的内容</div>'));

  const movies = d.movies.map((m) => cardHTML(m, [m.year, m.status === "unmatched" ? "未匹配" : ""].filter(Boolean).join(" · "))).join("");
  parts.push(sectionHTML('<h2>电影</h2><span class="count">' + d.movies.length + " 部</span>",
    movies ? '<div class="grid">' + movies + "</div>" : '<div class="empty">电影库是空的</div>'));

  const shows = d.shows.map((s) => cardHTML(s, s.episode_count ? "已看 " + s.watched_count + " / " + s.episode_count : "")).join("");
  parts.push(sectionHTML('<h2>剧集</h2><span class="count">' + d.shows.length + " 部</span>",
    shows ? '<div class="grid">' + shows + "</div>" : '<div class="empty">剧集库是空的</div>'));

  $view.innerHTML = parts.join("");
}

/* ---------- 列表页 ---------- */

const WATCHED_OPTS = [["", "全部状态"], ["unwatched", "未看"], ["progress", "在看"], ["watched", "已看"]];
const RATING_OPTS = [["", "不限评分"], ["6", "6 分以上"], ["7", "7 分以上"], ["8", "8 分以上"]];
const RES_OPTS = [["", "不限清晰度"], ["4k", "4K"], ["1080", "1080p"], ["720", "720p"], ["sd", "标清"]];

function selectHTML(id, opts, value, label) {
  return '<select class="sort" id="' + id + '" aria-label="' + esc(label) + '">' +
    opts.map(([v, l]) => '<option value="' + esc(v) + '"' + (String(value || "") === v ? " selected" : "") +
      ">" + esc(l) + "</option>").join("") + "</select>";
}

async function renderList() {
  const kind = window.location.hash.startsWith("#/shows") ? "shows" : "movies";
  const label = kind === "movies" ? "电影" : "剧集";
  const f = state.filters[kind];
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("sort", state.sort);
  for (const k of ["genre", "min_rating", "watched", "res"]) if (f[k]) params.set(k, f[k]);
  if (f.fav) params.set("fav", "true");
  const items = await fetchView(`/api/${kind}?${params}`);
  if (!items) return;
  const filtered = !!(f.genre || f.min_rating || f.watched || f.res || f.fav);
  const seen = new Set(state.genres[kind]);
  for (const it of items) for (const g of it.genres || []) seen.add(g);
  state.genres[kind] = Array.from(seen).sort((a, b) => a.localeCompare(b, "zh"));
  const cards = items.map((it) => {
    const sub = kind === "shows"
      ? (it.episode_count ? "已看 " + it.watched_count + " / " + it.episode_count : "")
      : [it.year, it.status === "unmatched" ? "未匹配" : ""].filter(Boolean).join(" · ");
    return cardHTML(it, (it.favorite ? "♥ " : "") + sub);
  }).join("");
  const controls =
    selectHTML("f-genre", [["", "全部类型"]].concat(state.genres[kind].map((g) => [g, g])), f.genre, "类型") +
    selectHTML("f-rating", RATING_OPTS, f.min_rating, "评分") +
    selectHTML("f-watched", WATCHED_OPTS, f.watched, "观看状态") +
    (kind === "movies" ? selectHTML("f-res", RES_OPTS, f.res, "清晰度") : "") +
    '<button type="button" class="btn ghost chip' + (f.fav ? " on" : "") + '" id="f-fav" aria-pressed="' +
    (f.fav ? "true" : "false") + '">♥ 收藏</button>' +
    selectHTML("sort", [["added", "最近添加"], ["title", "按片名"], ["year", "按年份"], ["rating", "按评分"]],
      state.sort, "排序");
  $view.innerHTML = sectionHTML(
    "<h2>" + label + "</h2>" +
    '<span class="count">' + (state.q ? "“" + esc(state.q) + "” " : "") + items.length + " 条</span>" +
    '<span class="spacer"></span><div class="filters">' + controls + "</div>",
    cards ? '<div class="grid">' + cards + "</div>"
          : '<div class="empty">' + (filtered ? "没有符合筛选条件的条目" : "没有匹配的条目") + "</div>");
  const bind = (id, key) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => { f[key] = el.value; renderList(); });
  };
  bind("f-genre", "genre"); bind("f-rating", "min_rating"); bind("f-watched", "watched"); bind("f-res", "res");
  document.getElementById("f-fav").addEventListener("click", () => { f.fav = !f.fav; renderList(); });
  const sel = document.getElementById("sort");
  sel.addEventListener("change", () => { state.sort = sel.value; renderList(); });
}

/* ---------- 电影详情 ---------- */

function personChipHTML(p, withCharacter) {
  const src = imgUrl(p.profile);
  return '<a class="person" href="#/person/' + esc(p.id) + '">' +
    (src ? '<img class="avatar" loading="lazy" src="' + src + '" alt="">'
         : '<div class="avatar ph"><span>' + esc((p.name || "?").charAt(0)) + "</span></div>") +
    '<div class="person-name">' + esc(p.name) + "</div>" +
    (withCharacter && p.character ? '<div class="person-role">' + esc(p.character) + "</div>" : "") + "</a>";
}

function creditsHTML(leadLabel, leads, cast) {
  let html = "";
  if (leads && leads.length) {
    html += '<div class="credit-line"><span class="dim">' + leadLabel + "</span> " +
      leads.map((p) => '<a class="link" href="#/person/' + esc(p.id) + '">' + esc(p.name) + "</a>").join("、") + "</div>";
  }
  if (cast && cast.length) {
    html += '<section class="section"><div class="section-head"><h2>演员</h2></div>' +
      '<div class="people-row">' + cast.map((p) => personChipHTML(p, true)).join("") + "</div></section>";
  }
  return html;
}

function favButtonHTML(on) {
  return '<button class="btn ghost" id="fav" aria-pressed="' + (on ? "true" : "false") + '">' +
    (on ? "♥ 已收藏" : "♡ 收藏") + "</button>";
}

function bindFav(type, id, on, rerender) {
  const btn = document.getElementById("fav");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    try {
      await apiCall("POST", "/api/favorite", { type: type, id: id, favorite: !on });
      rerender();
    } catch (err) { toast("操作失败：" + err.message); }
  });
}

function fmtSize(bytes) {
  if (!bytes) return "";
  const gb = bytes / 1073741824;
  return gb >= 1 ? gb.toFixed(1) + " GB" : Math.max(1, Math.round(bytes / 1048576)) + " MB";
}

/* 详情页的一个版本行：版本名 + 分辨率/容器/大小 + 该版本自己的观看进度 */
function versionRowHTML(v) {
  const bits = [];
  if (v.video_height) bits.push(v.video_height + "p");
  if (v.ext) bits.push(String(v.ext).toUpperCase());
  if (v.size) bits.push(fmtSize(v.size));
  let state = "";
  if (v.watched) state = "已看";
  else if (v.progress != null && (v.position_sec || 0) >= 30) state = "看到 " + fmt(v.position_sec);
  return '<div class="ep-row' + (v.is_default ? " ver-default" : "") + '" role="button" tabindex="0" data-ver="' + esc(v.id) + '">' +
    '<div><div class="ep-name">' + esc(v.name) +
    (v.is_default ? ' <span class="ver-tag">默认</span>' : "") + "</div>" +
    '<div class="ep-sub">' + esc(bits.join(" · ")) + (state ? ' <span class="dot">·</span> ' + esc(state) : "") + "</div>" +
    (!v.watched && v.progress != null && v.progress > 0 ? barHTML(v.progress) : "") +
    '</div><div class="ep-side"><button type="button" class="btn ghost chip ver-play">▶ 播放</button></div></div>';
}

async function renderMovie() {
  const raw = (window.location.hash.match(/^#\/movie\/([0-9]{1,10})$/) || [])[1];
  const id = parseInt(raw, 10);
  if (!Number.isInteger(id) || id <= 0) { window.location.hash = "#/"; return; }
  const m = await fetchView("/api/movie/" + id);
  if (!m) return;
  const bg = imgUrl(m.backdrop) || imgUrl(m.poster);
  const metaBits = [];
  if (m.year) metaBits.push(esc(m.year));
  if (m.runtime_minutes) metaBits.push(esc(m.runtime_minutes) + " 分钟");
  if (m.genres && m.genres.length) metaBits.push(esc(m.genres.join(" / ")));
  if (m.rating != null && m.rating > 0) metaBits.push('<span class="star">★</span> ' + m.rating.toFixed(1));
  if (m.status === "unmatched") metaBits.push("未匹配到元数据");

  let playLabel = "播放";
  if (!m.watched && m.position_sec >= 30 && m.progress != null && m.progress < 1) {
    playLabel = "继续播放 · 从 " + fmt(m.position_sec);
  }
  const watchedLabel = m.watched ? "标为未看" : "标为已看";
  const playId = m.default_version_id || id;

  const versionsHTML = (m.versions && m.versions.length > 1)
    ? '<section class="section"><div class="section-head"><h2>版本</h2>' +
      '<span class="count">' + esc(m.versions.length) + " 个</span></div>" +
      '<div class="ep-list" id="ver-list">' + m.versions.map(versionRowHTML).join("") + "</div></section>"
    : "";

  $view.innerHTML =
    '<div class="hero' + (bg ? "" : " ph-hero") + '">' +
    (bg ? '<img src="' + bg + '" alt="">' : "") + '<div class="hero-fade"></div></div>' +
    '<div class="detail-body"><div class="detail-poster">' + posterHTML(m) + "</div>" +
    '<div class="detail-info"><h1>' + esc(m.title) + "</h1>" +
    '<div class="meta-line">' + metaBits.join('<span class="dot">·</span>') + "</div>" +
    creditsHTML("导演", m.directors, null) +
    (m.collection ? '<div class="credit-line"><span class="dim">合集</span> <a class="link" href="#/collection/' +
      esc(m.collection.id) + '">' + esc(m.collection.name) + "</a>" +
      (m.collection.owned > 1 ? ' <span class="dim">（库里 ' + esc(m.collection.owned) + " 部）</span>" : "") + "</div>" : "") +
    (m.overview ? '<p class="overview">' + esc(m.overview) + "</p>" : "") +
    '<div class="actions"><button class="btn primary" id="play">▶ ' + esc(playLabel) + "</button>" +
    '<button class="btn ghost" id="watched">' + watchedLabel + "</button>" + favButtonHTML(m.favorite) + "</div>" +
    '<div id="sub-box" class="sub-box"></div>' +
    "</div></div>" + versionsHTML + creditsHTML("", null, m.cast);
  renderSubtitlePanel("movie", id, document.getElementById("sub-box"));
  bindFav("movie", id, m.favorite, renderMovie);

  const verList = document.getElementById("ver-list");
  if (verList) {
    const playVer = (row) => {
      const verId = parseInt(String(row.dataset.ver || ""), 10);
      if (Number.isInteger(verId) && verId > 0) playItem("movie", verId, "已交给 IINA 播放");
    };
    verList.addEventListener("click", (ev) => {
      const row = ev.target.closest(".ep-row");
      if (row) playVer(row);
    });
    verList.addEventListener("keydown", (ev) => {
      const row = ev.target.closest(".ep-row");
      if (row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); playVer(row); }
    });
  }

  document.getElementById("play").addEventListener("click", () =>
    playItem("movie", playId, "已交给 IINA 播放"));
  document.getElementById("watched").addEventListener("click", async () => {
    try {
      await apiCall("POST", "/api/watched", { type: "movie", id: id, watched: !m.watched });
      renderMovie();
    } catch (err) { toast("操作失败：" + err.message); }
  });
}

/* ---------- 剧集详情 ---------- */

function epRowHTML(ep) {
  const thumb = imgUrl(ep.still);
  const label = "S" + pad(ep.season_number) + "E" + pad(ep.episode_number);
  const side = ep.watched ? '<span class="check">✓</span>' : (ep.runtime_minutes ? ep.runtime_minutes + " 分钟" : "");
  return '<div class="ep-row" role="button" tabindex="0" data-ep="' + esc(ep.id) + '">' +
    (thumb ? '<img class="ep-thumb" loading="lazy" src="' + thumb + '" alt="">'
           : '<div class="ep-thumb ph"><span>' + pad(ep.episode_number) + "</span></div>") +
    '<div><div class="ep-name">' + esc(ep.title) + "</div>" +
    '<div class="ep-sub">' + label +
    (ep.missing ? " · 文件缺失" : "") + "</div>" +
    (!ep.watched && ep.progress != null && ep.progress > 0 ? barHTML(ep.progress) : "") +
    '<div class="sub-box ep-sub-box" hidden></div>' +
    '</div><div class="ep-side">' + side +
    (ep.missing ? "" : ' <button type="button" class="ep-subs" title="字幕" aria-label="字幕">字</button>') +
    "</div></div>";
}

async function renderShow() {
  const raw = (window.location.hash.match(/^#\/show\/([0-9]{1,10})$/) || [])[1];
  const id = parseInt(raw, 10);
  if (!Number.isInteger(id) || id <= 0) { window.location.hash = "#/"; return; }
  const s = await fetchView("/api/show/" + id);
  if (!s) return;
  const bg = imgUrl(s.backdrop) || imgUrl(s.poster);
  const metaBits = [];
  if (s.year) metaBits.push(esc(s.year));
  if (s.genres && s.genres.length) metaBits.push(esc(s.genres.join(" / ")));
  if (s.rating != null && s.rating > 0) metaBits.push('<span class="star">★</span> ' + s.rating.toFixed(1));
  metaBits.push("共 " + esc(s.episode_count) + " 集");

  let continueBtn = "";
  if (s.next_episode) {
    const ep = s.next_episode;
    const label = "S" + pad(ep.season_number) + "E" + pad(ep.episode_number);
    continueBtn = '<button class="btn primary" id="play-next">▶ 继续：' + label + "</button>";
  }

  $view.innerHTML =
    '<div class="hero' + (bg ? "" : " ph-hero") + '">' +
    (bg ? '<img src="' + bg + '" alt="">' : "") + '<div class="hero-fade"></div></div>' +
    '<div class="detail-body"><div class="detail-poster">' + posterHTML(s) + "</div>" +
    '<div class="detail-info"><h1>' + esc(s.title) + "</h1>" +
    '<div class="meta-line">' + metaBits.join('<span class="dot">·</span>') + "</div>" +
    creditsHTML("主创", s.creators, null) +
    (s.next_air ? '<div class="credit-line"><span class="dim">下一集</span> S' + pad(s.next_air.season) + "E" +
      pad(s.next_air.episode) + " · " + esc(s.next_air.air_date) + "</div>" : "") +
    ((s.missing_episodes || []).length ? '<div class="credit-line"><span class="dim">缺集</span> ' +
      s.missing_episodes.map((g) => "第 " + esc(g.season) + " 季 " + g.episodes.map((n) => "E" + pad(n)).join(" ")).join("；") +
      "</div>" : "") +
    (s.overview ? '<p class="overview">' + esc(s.overview) + "</p>" : "") +
    '<div class="actions">' + continueBtn + favButtonHTML(s.favorite) + "</div>" +
    "</div></div>" + creditsHTML("", null, s.cast) +
    '<section class="section"><div class="section-head"><h2>剧集列表</h2></div>' +
    '<div class="tabs" id="tabs"></div><div class="ep-list" id="ep-list"></div></section>';

  bindFav("show", id, s.favorite, renderShow);
  const tabs = document.getElementById("tabs");
  const epList = document.getElementById("ep-list");
  if (!s.seasons.length) {
    epList.innerHTML = '<div class="empty">暂无剧集文件</div>';
  } else {
    const wanted = state.season[id] != null
      ? s.seasons.find((x) => x.season_number === state.season[id])
      : (s.next_episode ? s.seasons.find((x) => x.season_number === s.next_episode.season_number) : s.seasons[0]);
    const current = wanted || s.seasons[0];
    for (const season of s.seasons) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab" + (season === current ? " active" : "");
      btn.textContent = season.name || "第 " + season.season_number + " 季";
      btn.addEventListener("click", () => {
        state.season[id] = season.season_number;
        renderShow();
      });
      tabs.appendChild(btn);
    }
    epList.innerHTML = current.episodes.map(epRowHTML).join("") || '<div class="empty">这一季没有在架的文件</div>';
  }

  const playRow = (row) => {
    const epId = parseInt(String(row.dataset.ep || ""), 10);
    if (Number.isInteger(epId) && epId > 0) playItem("episode", epId, "已交给 IINA 播放");
  };
  epList.addEventListener("click", (ev) => {
    if (ev.target.closest(".sub-box")) return;  // 字幕面板里的点击不触发播放
    const row = ev.target.closest(".ep-row");
    if (!row) return;
    const subBtn = ev.target.closest(".ep-subs");
    if (subBtn) {
      const box = row.querySelector(".ep-sub-box");
      const epId = parseInt(String(row.dataset.ep || ""), 10);
      box.hidden = !box.hidden;
      if (!box.hidden && Number.isInteger(epId)) renderSubtitlePanel("episode", epId, box);
      return;
    }
    playRow(row);
  });
  epList.addEventListener("keydown", (ev) => {  // 键盘 Tab 到某一集后回车/空格播放
    if (ev.target.closest(".ep-subs, .sub-box")) return;  // 字幕按钮/面板自己处理按键
    const row = ev.target.closest(".ep-row");
    if (row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); playRow(row); }
  });
  const nextBtn = document.getElementById("play-next");
  if (nextBtn && s.next_episode) {
    nextBtn.addEventListener("click", () => playItem("episode", s.next_episode.id, "已交给 IINA 播放"));
  }
}

/* ---------- 中文字幕 ---------- */

/* 在 box 里渲染某部片/某一集的字幕状态；没有中文时可搜射手网并下载。 */
async function renderSubtitlePanel(type, id, box) {
  box.innerHTML = '<div class="dim">字幕：检查中…</div>';
  let st;
  try { st = await apiCall("GET", "/api/subtitles?type=" + type + "&id=" + id); }
  catch (err) { box.innerHTML = '<div class="dim">字幕：' + esc(err.message) + "</div>"; return; }
  const parts = [];
  if (st.embedded.length) parts.push("内封 " + st.embedded.map(esc).join(" / "));
  if (st.external.length) parts.push("外挂 " + st.external.length + " 个");
  const own = st.downloaded.map((n) => '<span class="sub-file">✓ ' + esc(n) +
    ' <button type="button" class="link sub-remove" data-name="' + esc(n) + '">移除</button></span>').join("");
  box.innerHTML = '<div class="credit-line"><span class="dim">字幕</span> ' +
    (st.has_zh ? "有中文" : '<span class="warn">没有内封或外挂的中文字幕</span>') +
    (parts.length ? ' <span class="dim">（' + parts.join("；") + "）</span>" : "") +
    (st.search_enabled ? ' <button type="button" class="btn ghost chip" data-act="sub-search">搜索中文字幕</button>' : "") +
    "</div>" + (own ? '<div class="sub-own">' + own + "</div>" : "") + '<div class="sub-results"></div>';
  for (const b of box.querySelectorAll(".sub-remove")) {
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try { await apiCall("POST", "/api/subtitles/remove", { type: type, id: id, name: b.dataset.name }); }
      catch (err) { toast("移除失败：" + err.message); }
      renderSubtitlePanel(type, id, box);
    });
  }
  const btn = box.querySelector('[data-act="sub-search"]');
  if (btn) btn.addEventListener("click", (ev) => { ev.stopPropagation(); searchSubtitles(type, id, box, btn); });
}

async function searchSubtitles(type, id, box, btn) {
  const out = box.querySelector(".sub-results");
  btn.disabled = true;
  out.innerHTML = '<div class="dim">搜索中…（字幕服务由 assrt.net 提供）</div>';
  let r;
  try { r = await apiCall("GET", "/api/subtitles/search?type=" + type + "&id=" + id); }
  catch (err) { out.innerHTML = '<div class="error">' + esc(err.message) + "</div>"; btn.disabled = false; return; }
  btn.disabled = false;
  if (!r.results.length) { out.innerHTML = '<div class="dim">射手网上没找到中文字幕</div>'; return; }
  out.innerHTML = r.results.map((s) =>
    '<div class="sub-cand"><div><div class="ep-name">' + esc(s.name) + '</div><div class="ep-sub">' +
    esc([s.lang, s.subtype, s.site, s.videoname, (s.uploaded || "").slice(0, 10)].filter(Boolean).join(" · ")) +
    '</div></div><button type="button" class="btn ghost chip" data-sub="' + esc(s.id) + '">下载</button></div>').join("") +
    '<div class="dim small">字幕服务由 <a class="link" href="https://assrt.net" target="_blank" rel="noopener">assrt.net</a> 提供</div>';
  for (const b of out.querySelectorAll("[data-sub]")) {
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const subId = parseInt(b.dataset.sub, 10);
      if (!Number.isInteger(subId)) return;
      b.disabled = true; b.textContent = "下载中…";
      try {
        const d = await apiCall("POST", "/api/subtitles/download", { type: type, id: id, sub_id: subId });
        toast("已下载：" + d.name + "，下次播放自动加载");
        renderSubtitlePanel(type, id, box);
      } catch (err) { b.disabled = false; b.textContent = "下载"; toast("下载失败：" + err.message); }
    });
  }
}

/* ---------- 人物 / 导演 ---------- */

function workCardHTML(it) {
  const sub = [it.year, it.character].filter(Boolean).join(" · ");
  return cardHTML(it, sub);
}

async function renderPerson() {
  const raw = (window.location.hash.match(/^#\/person\/([0-9]{1,10})$/) || [])[1];
  const id = parseInt(raw, 10);
  if (!Number.isInteger(id) || id <= 0) { window.location.hash = "#/"; return; }
  const p = await fetchView("/api/person/" + id);
  if (!p) return;
  const src = imgUrl(p.profile);
  $view.innerHTML =
    '<div class="person-head">' +
    (src ? '<img class="avatar big" src="' + src + '" alt="">' : '<div class="avatar big ph"><span>' + esc(p.name.charAt(0)) + "</span></div>") +
    "<div><h1>" + esc(p.name) + '</h1><div class="meta-line">库里 ' + (p.made.length + p.acted.length) + " 部" +
    '<span class="dot">·</span><a class="link" href="' + esc(p.url) + '" target="_blank" rel="noopener">TMDB</a></div></div></div>' +
    (p.made.length ? sectionHTML("<h2>导演 / 主创</h2>", '<div class="grid">' + p.made.map(workCardHTML).join("") + "</div>") : "") +
    (p.acted.length ? sectionHTML("<h2>出演</h2>", '<div class="grid">' + p.acted.map(workCardHTML).join("") + "</div>") : "");
}

async function renderPeople() {
  const list = await fetchView("/api/people?min_count=" + (state.allPeople ? 1 : 2));
  if (!list) return;
  const cards = list.map((p) => {
    const src = imgUrl(p.profile);
    return '<a class="card person-card" href="#/person/' + esc(p.id) + '">' +
      (src ? '<img class="poster" loading="lazy" src="' + src + '" alt="">'
           : '<div class="poster ph"><span>' + esc(p.name.charAt(0)) + "</span></div>") +
      '<div class="card-t">' + esc(p.name) + '</div><div class="card-s">' + esc(p.count) + " 部</div></a>";
  }).join("");
  $view.innerHTML = sectionHTML(
    '<h2>导演与主创</h2><span class="count">' + list.length + " 位</span><span class=\"spacer\"></span>" +
    '<button type="button" class="btn ghost chip' + (state.allPeople ? " on" : "") + '" id="all-people">' +
    (state.allPeople ? "只看 2 部以上" : "显示全部") + "</button>",
    cards ? '<div class="grid">' + cards + "</div>" : '<div class="empty">还没有演职员数据（运行 fetch-extras 或重新扫描）</div>');
  document.getElementById("all-people").addEventListener("click", () => { state.allPeople = !state.allPeople; renderPeople(); });
}

/* ---------- 合集 ---------- */

async function renderCollections() {
  const list = await fetchView("/api/collections");
  if (!list) return;
  const cards = list.map((c) => cardHTML({ href: "#/collection/" + c.id, poster: c.poster_cached, title: c.name },
    "库里 " + c.owned + " 部")).join("");
  $view.innerHTML = sectionHTML('<h2>合集</h2><span class="count">' + list.length + " 个</span>",
    cards ? '<div class="grid">' + cards + "</div>" : '<div class="empty">还没有合集数据</div>');
}

async function renderCollection() {
  const raw = (window.location.hash.match(/^#\/collection\/([0-9]{1,10})$/) || [])[1];
  const id = parseInt(raw, 10);
  if (!Number.isInteger(id) || id <= 0) { window.location.hash = "#/collections"; return; }
  const c = await fetchView("/api/collection/" + id);
  if (!c) return;
  const owned = c.movies.map((m) => cardHTML(m, m.year || "")).join("");
  const missing = (c.missing || []).map((m) =>
    '<a class="card missing-card" href="' + esc(m.url) + '" target="_blank" rel="noopener">' +
    posterHTML({ title: m.title }) + '<div class="card-t">' + esc(m.title) + '</div><div class="card-s">' +
    esc(m.year || "") + " · 库里没有</div></a>").join("");
  $view.innerHTML = sectionHTML('<h2>' + esc(c.name) + '</h2><span class="count">库里 ' + c.movies.length + " 部" +
    (c.missing && c.missing.length ? "，缺 " + c.missing.length + " 部" : "") + "</span>",
    '<div class="grid">' + owned + missing + "</div>");
}

/* ---------- 追剧日历 ---------- */

function daysUntil(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((d - today) / 86400000);
}

function calRowHTML(it, side) {
  return '<a class="cal-row" href="' + esc(it.href) + '">' + posterHTML(it, "cal-poster") +
    '<div><div class="ep-name">' + esc(it.title) + '</div><div class="ep-sub">S' + pad(it.season) + "E" + pad(it.episode) +
    (it.name ? " · " + esc(it.name) : "") + '</div></div><div class="ep-side">' + side + "</div></a>";
}

async function renderCalendar(autoRefreshed) {
  const d = await fetchView("/api/calendar");
  if (!d) return;
  const stale = !d.refreshed_at || (Date.now() - new Date(d.refreshed_at).getTime()) > 12 * 3600 * 1000;
  const up = d.upcoming.map((it) => {
    const n = daysUntil(it.air_date);
    return calRowHTML(it, esc(it.air_date) + "<br>" + (n === 0 ? "今天" : n === 1 ? "明天" : n > 0 ? n + " 天后" : "已播"));
  }).join("");
  const aired = d.new_aired.map((it) => calRowHTML(it, esc(it.air_date) + "<br>库里到 S" + pad(it.local_season) + "E" + pad(it.local_episode))).join("");
  const gaps = d.gaps.map((g) => '<a class="cal-row" href="' + esc(g.href) + '">' + posterHTML(g, "cal-poster") +
    '<div><div class="ep-name">' + esc(g.title) + '</div><div class="ep-sub">' +
    g.missing.map((s) => "第 " + esc(s.season) + " 季缺 " + s.episodes.map((n) => "E" + pad(n)).join(" ")).join("；") +
    "</div></div></a>").join("");
  $view.innerHTML =
    '<div class="section-head rec-head"><h2>追剧日历</h2><span class="count">' +
    esc(d.refreshed_at ? "更新于 " + fmtTime(d.refreshed_at) : "还没有拉过播出信息") + '</span><span class="spacer"></span>' +
    '<button class="btn ghost" id="cal-refresh" type="button">刷新</button></div>' +
    sectionHTML("<h2>即将播出</h2>", up ? '<div class="ep-list">' + up + "</div>" : '<div class="empty">没有已排期的新集</div>') +
    sectionHTML("<h2>已播出、库里还没有</h2>", aired ? '<div class="ep-list">' + aired + "</div>" : '<div class="empty">都跟上了</div>') +
    sectionHTML("<h2>季内缺集</h2>", gaps ? '<div class="ep-list">' + gaps + "</div>" : '<div class="empty">没有缺集</div>');
  const btn = document.getElementById("cal-refresh");
  btn.addEventListener("click", () => startCalRefresh(btn));
  if (stale && !autoRefreshed) startCalRefresh(btn, true);
}

let calTimer = null;
async function startCalRefresh(btn, auto) {
  btn.disabled = true;
  btn.textContent = "刷新中…";
  try {
    await apiCall("POST", "/api/calendar/refresh", {});
  } catch (err) {
    btn.disabled = false; btn.textContent = "刷新";
    if (!auto) toast("刷新失败：" + err.message);
    return;
  }
  clearInterval(calTimer);
  const timer = calTimer = setInterval(async () => {
    try {
      const st = await apiCall("GET", "/api/calendar/status");
      if (st.running) { btn.textContent = st.total ? "刷新中 " + st.done + "/" + st.total : "刷新中…"; return; }
      clearInterval(timer);
      if (st.error) { btn.disabled = false; btn.textContent = "刷新"; toast("刷新出错：" + st.error); return; }
      if ((window.location.hash || "").startsWith("#/calendar")) renderCalendar(true);
    } catch (err) { clearInterval(timer); btn.disabled = false; btn.textContent = "刷新"; }
  }, 1000);
}

/* ---------- 推荐 ---------- */

function fmtTime(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d.getTime())) return iso || "";
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function recCardHTML(item) {
  const sub = [item.year, item.rating != null ? "★ " + item.rating.toFixed(1) : ""]
    .filter(Boolean).join(" · ");
  const because = (item.because || []).join("、");
  return '<div class="card rec-card" data-kind="' + esc(item.kind) +
    '" data-id="' + esc(item.tmdb_id) + '" data-url="' + esc(item.url) + '">' +
    posterHTML({ poster: item.poster, title: item.title }) +
    '<button class="rec-dismiss" type="button" title="不感兴趣">×</button>' +
    '<div class="card-t">' + esc(item.title) + "</div>" +
    (sub ? '<div class="card-s">' + esc(sub) + "</div>" : "") +
    (because ? '<div class="rec-because">因为你有：' + esc(because) + "</div>" : "") +
    "</div>";
}

function recGridHTML(label, items) {
  if (!items.length) return "";
  return sectionHTML('<h2>' + label + '</h2><span class="count">' + items.length + " 部</span>",
    '<div class="grid rec-grid">' + items.map(recCardHTML).join("") + "</div>");
}

async function renderRecs() {
  const d = await fetchView("/api/recommendations");
  if (!d) return;
  const genLine = d.generated_at ? "生成于 " + fmtTime(d.generated_at) : "还没有生成过推荐";
  let body;
  if (!d.movies.length && !d.shows.length) {
    body = '<div class="empty">还没有推荐数据，点「生成推荐」从你的片库和观看记录算一份。</div>';
  } else {
    body = recGridHTML("电影", d.movies) + recGridHTML("剧集", d.shows);
  }
  $view.innerHTML =
    '<div class="section-head rec-head"><h2>为你推荐</h2>' +
    '<span class="count">' + esc(genLine) + "</span>" +
    '<span class="spacer"></span>' +
    '<button class="btn ghost" id="rec-refresh" type="button">' +
    (d.movies.length || d.shows.length ? "刷新推荐" : "生成推荐") + "</button></div>" + body;
  bindRecEvents();
}

function bindRecEvents() {
  const btn = document.getElementById("rec-refresh");
  if (btn) btn.addEventListener("click", () => startRecRefresh(btn));
  for (const grid of $view.querySelectorAll(".rec-grid")) {
    grid.addEventListener("click", async (ev) => {
      const card = ev.target.closest(".rec-card");
      if (!card) return;
      if (ev.target.closest(".rec-dismiss")) {
        const kind = card.dataset.kind;
        const id = parseInt(card.dataset.id, 10);
        if (!Number.isInteger(id)) return;
        try {
          await apiCall("POST", "/api/recommendations/dismiss", { kind: kind, tmdb_id: id });
          card.remove();
          toast("已隐藏，不再推荐");
        } catch (err) { toast("操作失败：" + err.message); }
        return;
      }
      const url = card.dataset.url || "";
      if (/^https?:\/\//i.test(url)) window.open(url, "_blank");
    });
  }
}

let recTimer = null;
async function startRecRefresh(btn) {
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    await apiCall("POST", "/api/recommendations/refresh", {});
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "刷新推荐";
    toast("生成失败：" + err.message);
    return;
  }
  clearInterval(recTimer);
  const timer = recTimer = setInterval(async () => {
    try {
      const st = await apiCall("GET", "/api/recommendations/status");
      if (st.running) {
        btn.textContent = st.total ? "生成中 " + st.done + "/" + st.total : "生成中…";
        return;
      }
      clearInterval(timer);
      if (st.error) {
        btn.disabled = false;
        btn.textContent = "刷新推荐";
        toast("生成出错：" + st.error);
      } else {
        toast("推荐已更新：电影 " + st.movies + "、剧集 " + st.shows);
        if ((window.location.hash || "").startsWith("#/recs")) renderRecs();
      }
    } catch (err) {
      clearInterval(timer);
      btn.disabled = false;
      btn.textContent = "刷新推荐";
    }
  }, 1000);
}

/* ---------- 播放 / 标记（URL 固定，id 走 JSON body） ---------- */

/* 在 Home Cinema.app 里（Swift 外壳注册了 player message handler）走内嵌 libmpv 播放；
   普通浏览器保持 v1 行为：起 IINA。 */
function inEmbeddedApp() {
  return typeof window !== "undefined" &&
    window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.player;
}

async function playItem(type, id, doneMsg) {
  try {
    if (inEmbeddedApp()) {
      const r = await apiCall("POST", "/api/play", { type: type, id: id, embedded: true });
      window.webkit.messageHandlers.player.postMessage({
        type: type, id: id, path: r.path, start_at: r.start_at, title: r.title,
        skip: r.skip, subs: r.subs || [],
      });
      return;
    }
    const r = await apiCall("POST", "/api/play", { type: type, id: id });
    toast(doneMsg + "，从 " + fmt(r.start_at) + " 开始");
  } catch (err) { toast("播放失败：" + err.message); }
}

/* 播放结束后 Swift 外壳调用：重新执行当前路由，刷新「继续观看」和进度条。 */
window.homecinemaRefresh = function () { route(); };
window.homecinemaToast = function (msg) { toast(String(msg || "")); };  // App 外壳的提示（如下一集无法播放）

/* ---------- 路由 ---------- */

function setActiveNav(name) {
  for (const a of $nav.querySelectorAll("a")) {
    a.classList.toggle("active", a.dataset.nav === name);
  }
}

async function route() {
  try {
    await renderRoute();
    applyNavFocus();
  } catch (err) {  // 渲染本身出错（数据形状不对等）也要给出提示
    $view.innerHTML = errorHTML(err);
  }
}

async function renderRoute() {
  const hash = window.location.hash || "#/";
  if (hash === "#/" || hash === "#" || hash === "") {
    setActiveNav("home");
    await renderHome();
  } else if (hash.startsWith("#/movies")) {
    setActiveNav("movies");
    await renderList();
  } else if (hash.startsWith("#/shows")) {
    setActiveNav("shows");
    await renderList();
  } else if (hash.startsWith("#/movie/")) {
    setActiveNav("movies");
    await renderMovie();
  } else if (hash.startsWith("#/show/")) {
    setActiveNav("shows");
    await renderShow();
  } else if (hash.startsWith("#/recs")) {
    setActiveNav("recs");
    await renderRecs();
  } else if (hash.startsWith("#/person/")) {
    setActiveNav("people");
    await renderPerson();
  } else if (hash.startsWith("#/people")) {
    setActiveNav("people");
    await renderPeople();
  } else if (hash.startsWith("#/collection/")) {
    setActiveNav("collections");
    await renderCollection();
  } else if (hash.startsWith("#/collections")) {
    setActiveNav("collections");
    await renderCollections();
  } else if (hash.startsWith("#/calendar")) {
    setActiveNav("calendar");
    await renderCalendar();
  } else {
    window.location.hash = "#/";
  }
}

/* ---------- 搜索 / 重新扫描 ---------- */

let searchTimer = null;
$search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = $search.value.trim();
    const hash = window.location.hash || "#/";
    if (hash.startsWith("#/shows")) {
      route();
    } else if (!hash.startsWith("#/movies")) {
      window.location.hash = "#/movies";
    } else {
      route();
    }
  }, 250);
});

let scanTimer = null;
function pollScan() {
  clearInterval(scanTimer);
  scanTimer = setInterval(async () => {
    try {
      const st = await apiCall("GET", "/api/scan/status");
      if (!st.running) {
        clearInterval(scanTimer);
        $rescan.disabled = false;
        $rescan.textContent = "重新扫描";
        if (st.error) {
          toast("扫描出错：" + st.error);
        } else {
          toast("扫描完成：电影 " + st.movies + "、剧集 " + st.episodes +
            "，元数据 " + (st.metadata_total ? st.metadata_done + "/" + st.metadata_total : "未配置 key，跳过"));
        }
        route();
      } else {
        $rescan.textContent = st.phase === "metadata"
          ? "拉元数据 " + st.metadata_done + "/" + st.metadata_total
          : "扫描中…";
      }
    } catch (err) {
      /* Kimi-F-4：轮询请求本身失败（服务刚重启、浏览器唤醒）不能只停轮询——
         文字要复位、要说一句「后台扫描仍在进行」，否则用户以为扫描结束或已被取消。 */
      clearInterval(scanTimer);
      $rescan.disabled = false;
      $rescan.textContent = "重新扫描";
      toast("扫描状态获取失败：" + err.message + "（后台扫描仍在进行）");
    }
  }, 1000);
}

$rescan.addEventListener("click", async () => {
  try {
    await apiCall("POST", "/api/scan", {});
    $rescan.disabled = true;
    $rescan.textContent = "扫描中…";
    toast("已开始后台扫描");
    pollScan();
  } catch (err) { toast("触发扫描失败：" + err.message); }
});

/* ---------- ESC 退一层 ---------- */

/* 记下 App 内走过的页面：ESC 优先回到上一页（从首页点进剧集，ESC 回首页）；
   直接打开的页面没有上一页，就退到上一级。播放中的 ESC 由 App 外壳处理，到不了这里。 */
const navStack = [window.location.hash || "#/"];
window.addEventListener("hashchange", () => {
  const h = window.location.hash || "#/";
  if (navStack.length > 1 && navStack[navStack.length - 2] === h) navStack.pop();
  else if (navStack[navStack.length - 1] !== h) navStack.push(h);
});

function parentRoute(hash) {
  const rules = [[/^#\/show\//, "#/shows"], [/^#\/movie\//, "#/movies"], [/^#\/person\//, "#/people"],
                 [/^#\/collection\//, "#/collections"]];
  for (const [re, to] of rules) if (re.test(hash)) return to;
  return hash === "#/" || hash === "" || hash === "#" ? null : "#/";
}

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape" || ev.defaultPrevented) return;
  const el = document.activeElement;
  if (el && el.matches && el.matches("input, select, textarea")) {
    if (el === $search && $search.value) { $search.value = ""; $search.dispatchEvent(new Event("input")); }
    el.blur();
    return;
  }
  const open = $view.querySelector(".ep-sub-box:not([hidden])");
  if (open) { open.hidden = true; return; }  // 先收起展开的字幕面板
  if (navStack.length > 1) { history.back(); return; }
  const up = parentRoute(window.location.hash || "#/");
  if (up) window.location.hash = up;
});

/* ---------- 方向键焦点导航（10-foot：遥控器方向键 + 确认 + 返回） ---------- */

/* 参与焦点导航的元素：海报墙/横排里的卡片、继续观看大卡、人物 chip、剧集行与版本行、日历行。 */
const NAV_ITEM_SELS = [".card", ".cw-card", ".person", ".ep-row", ".cal-row"];

let navFocused = null;     // 当前带焦点样式的元素
let navReturnHref = null;  // 从详情页返回时，焦点要回到的那张卡片的 href

function navIsItem(el) {
  if (!el || !el.classList) return false;
  return NAV_ITEM_SELS.some((sel) => el.classList.contains(sel.slice(1)));
}

function navAttr(el, name) {
  return el && typeof el.getAttribute === "function" ? el.getAttribute(name) : null;
}

/* 元素的视觉盒子；隐藏元素（或拿不到布局信息时）返回 null，不参与导航。 */
function navBox(el) {
  if (!el || typeof el.getBoundingClientRect !== "function") return null;
  const r = el.getBoundingClientRect();
  return r && r.width > 0 && r.height > 0 ? r : null;
}

/* 视口里可见的导航元素，按文档顺序去重。 */
function navItems() {
  const out = [];
  for (const sel of NAV_ITEM_SELS) {
    for (const el of $view.querySelectorAll(sel)) {
      if (out.indexOf(el) < 0 && navBox(el)) out.push(el);
    }
  }
  return out;
}

/* 事件目标（可能是卡片里的 img、按钮等子元素）所属的导航元素。 */
function navItemFrom(node) {
  for (let el = node; el && el !== $view; el = el.parentNode) {
    if (navIsItem(el)) return el;
  }
  return null;
}

/* 按视觉位置分行：top 相同（或高度差的一半以内）的算同一行，行内按 left 从左到右。
   首页各横排、列表页网格都用同一套，跨横排的上下移动也就自然落在相邻行上。 */
function navRows(items) {
  const boxes = [];
  for (const el of items) boxes.push({ el: el, r: navBox(el) });
  boxes.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
  const rows = [];
  for (const b of boxes) {
    const row = rows.find((rw) => Math.abs(rw.top - b.r.top) <= Math.max(8, Math.min(rw.h, b.r.height) / 2));
    if (row) {
      row.items.push(b);
      row.top = Math.min(row.top, b.r.top);
      row.h = Math.max(row.h, b.r.height);
    } else {
      rows.push({ top: b.r.top, h: b.r.height, items: [b] });
    }
  }
  for (const row of rows) row.items.sort((x, y) => x.r.left - y.r.left);
  return rows;
}

function navRowOf(rows, el) {
  for (let i = 0; i < rows.length; i++) {
    const col = rows[i].items.findIndex((b) => b.el === el);
    if (col >= 0) return { row: i, col: col };
  }
  return null;
}

/* dir: left/right/up/down。返回该获得焦点的元素；这一方向没有可去的邻居时返回 null。
   上下换行时按水平位置取最近的那张，所以网格里是同列的上下邻居、横排之间也是位置对齐的那张。
   当前焦点不在导航元素上（刚进页面、或焦点在「播放」这类按钮上）时，
   从视口左上角/右下角开始，符合「按了向下/向右就进内容区」的直觉。 */
function navStep(from, dir) {
  const items = navItems();
  if (!items.length) return null;
  const rows = navRows(items);
  const at = from ? navRowOf(rows, from) : null;
  if (!at) {
    const back = dir === "up" || dir === "left";
    const pick = back ? items[items.length - 1] : items[0];
    let best = pick;
    for (const el of items) {
      const r = navBox(el);
      const b = navBox(best);
      if (back ? (r.top > b.top || (r.top === b.top && r.left > b.left))
               : (r.top < b.top || (r.top === b.top && r.left < b.left))) best = el;
    }
    return best;
  }
  if (dir === "left" || dir === "right") {
    const col = at.col + (dir === "right" ? 1 : -1);
    return col >= 0 && col < rows[at.row].items.length ? rows[at.row].items[col].el : null;
  }
  const next = rows[at.row + (dir === "down" ? 1 : -1)];
  if (!next) return null;
  const box = navBox(from);
  const cx = box.left + box.width / 2;
  let best = null, dist = Infinity;
  for (const b of next.items) {
    const d = Math.abs(b.r.left + b.r.width / 2 - cx);
    if (d < dist) { dist = d; best = b; }
  }
  return best ? best.el : null;
}

function setNavFocus(el) {
  if (!el) return;
  if (navFocused && navFocused !== el && navFocused.classList) navFocused.classList.remove("nav-focus");
  navFocused = el;
  if (el.classList) el.classList.add("nav-focus");
  navRemember(el);
  if (typeof el.focus === "function") el.focus();
  if (typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest", inline: "nearest" });
}

/* 只记住通往详情页的卡片：从哪张片子进详情，ESC 回来就回到那张卡片。 */
function navRemember(el) {
  const href = navAttr(el, "href");
  if (href && /^#\/(movie|show)\//.test(href)) navReturnHref = href;
}

/* 焦点在卡片内部的按钮/面板上时不代劳，那属于它自己的按键语义。 */
function navIsPlainTarget(target, item) {
  if (item !== target) return false;                     // 焦点在子控件（如「不感兴趣」「字」）上
  if (item.classList.contains("ep-row")) return false;    // 剧集行/版本行已自带 keydown 处理
  const tag = target.tagName;
  return tag !== "BUTTON" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA";
}

function navIsEditable(el) {
  return !!(el && el.matches && el.matches("input, textarea, select, [contenteditable]"));
}

function onNavKey(ev) {
  if (ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const target = ev.target;
  /* 搜索框等文本输入里方向键归文本编辑，不抢；ESC 仍走上面的处理器先让输入框失焦。 */
  if (navIsEditable(target) || navIsEditable(document.activeElement)) return;
  if (target && target.closest && target.closest(".sub-box")) return;

  if (ev.key === "Enter" || ev.key === " ") {
    const item = navItemFrom(target);
    if (!item || !navIsPlainTarget(target, item)) return;
    /* 卡片是 <a>：自己派发点击并 preventDefault，避免和浏览器原生的 Enter 跟随链接叠加。
       空格在链接/div 上浏览器本来什么都不做（只会滚页），同样走这里。 */
    ev.preventDefault();
    navRemember(item);
    if (typeof item.click === "function") item.click();
    return;
  }

  const dir = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" }[ev.key];
  if (!dir) return;
  const next = navStep(navItemFrom(target), dir);
  if (!next) return;
  ev.preventDefault();
  setNavFocus(next);
}

document.addEventListener("keydown", onNavKey);

/* 换页后的落点：详情页落在「播放」，其余页面回到进入前那张卡片（页面上没有就不抢焦点）。 */
function applyNavFocus() {
  const act = document.activeElement;
  if (act && act !== document.body && navIsEditable(act)) return;
  const hash = window.location.hash || "#/";
  if (/^#\/(movie|show)\//.test(hash)) {
    const play = document.getElementById("play") || document.getElementById("play-next");
    if (play) { setNavFocus(play); return; }
    const first = $view.querySelector(".ep-row");
    if (first) { setNavFocus(first); return; }
  }
  if (!navReturnHref) return;
  for (const sel of NAV_ITEM_SELS) {
    for (const el of $view.querySelectorAll(sel)) {
      if (navAttr(el, "href") === navReturnHref) { setNavFocus(el); navReturnHref = null; return; }
    }
  }
}

window.addEventListener("hashchange", route);
route();
