/* HomeCinema 前端：原生 JS，hash 路由，无构建、无外部依赖。
   网络层统一走 XMLHttpRequest（同源相对路径；id 一律 parseInt 校验后才拼接）。 */

"use strict";

const $view = document.getElementById("view");
const $search = document.getElementById("search");
const $nav = document.getElementById("nav");
const $rescan = document.getElementById("rescan");
const $toast = document.getElementById("toast");

const state = { q: "", sort: "added", season: {} };

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
  return '<a class="card" href="' + esc(item.href) + '">' + posterHTML(item) +
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

async function renderList() {
  const kind = window.location.hash.startsWith("#/shows") ? "shows" : "movies";
  const label = kind === "movies" ? "电影" : "剧集";
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("sort", state.sort);
  const items = await fetchView(`/api/${kind}?${params}`);
  if (!items) return;
  const cards = items.map((it) => {
    const sub = kind === "shows"
      ? (it.episode_count ? "已看 " + it.watched_count + " / " + it.episode_count : "")
      : [it.year, it.status === "unmatched" ? "未匹配" : ""].filter(Boolean).join(" · ");
    return cardHTML(it, sub);
  }).join("");
  const sortSel = '<select class="sort" id="sort"><option value="added">最近添加</option>' +
    '<option value="title">按片名</option><option value="year">按年份</option></select>';
  $view.innerHTML = sectionHTML(
    "<h2>" + label + "</h2>" +
    (state.q ? '<span class="count">“' + esc(state.q) + '” 共 ' + items.length + " 条</span>"
             : '<span class="count">' + items.length + " 条</span>") +
    '<span class="spacer"></span>' + sortSel,
    cards ? '<div class="grid">' + cards + "</div>" : '<div class="empty">没有匹配的条目</div>');
  const sel = document.getElementById("sort");
  sel.value = state.sort;
  sel.addEventListener("change", () => { state.sort = sel.value; renderList(); });
}

/* ---------- 电影详情 ---------- */

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

  $view.innerHTML =
    '<div class="hero' + (bg ? "" : " ph-hero") + '">' +
    (bg ? '<img src="' + bg + '" alt="">' : "") + '<div class="hero-fade"></div></div>' +
    '<div class="detail-body"><div class="detail-poster">' + posterHTML(m) + "</div>" +
    '<div class="detail-info"><h1>' + esc(m.title) + "</h1>" +
    '<div class="meta-line">' + metaBits.join('<span class="dot">·</span>') + "</div>" +
    (m.overview ? '<p class="overview">' + esc(m.overview) + "</p>" : "") +
    '<div class="actions"><button class="btn primary" id="play">▶ ' + esc(playLabel) + "</button>" +
    '<button class="btn ghost" id="watched">' + watchedLabel + "</button></div>" +
    "</div></div>";

  document.getElementById("play").addEventListener("click", () =>
    playItem("movie", id, "已交给 IINA 播放"));
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
    '</div><div class="ep-side">' + side + "</div></div>";
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
    (s.overview ? '<p class="overview">' + esc(s.overview) + "</p>" : "") +
    '<div class="actions">' + continueBtn + "</div>" +
    "</div></div>" +
    '<section class="section"><div class="section-head"><h2>剧集列表</h2></div>' +
    '<div class="tabs" id="tabs"></div><div class="ep-list" id="ep-list"></div></section>';

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
    const row = ev.target.closest(".ep-row");
    if (row) playRow(row);
  });
  epList.addEventListener("keydown", (ev) => {  // 键盘 Tab 到某一集后回车/空格播放
    const row = ev.target.closest(".ep-row");
    if (row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); playRow(row); }
  });
  const nextBtn = document.getElementById("play-next");
  if (nextBtn && s.next_episode) {
    nextBtn.addEventListener("click", () => playItem("episode", s.next_episode.id, "已交给 IINA 播放"));
  }
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
        skip: r.skip,
      });
      return;
    }
    const r = await apiCall("POST", "/api/play", { type: type, id: id });
    toast(doneMsg + "，从 " + fmt(r.start_at) + " 开始");
  } catch (err) { toast("播放失败：" + err.message); }
}

/* 播放结束后 Swift 外壳调用：重新执行当前路由，刷新「继续观看」和进度条。 */
window.homecinemaRefresh = function () { route(); };

/* ---------- 路由 ---------- */

function setActiveNav(name) {
  for (const a of $nav.querySelectorAll("a")) {
    a.classList.toggle("active", a.dataset.nav === name);
  }
}

async function route() {
  try {
    await renderRoute();
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
    } catch (err) { clearInterval(scanTimer); $rescan.disabled = false; }
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

window.addEventListener("hashchange", route);
route();
