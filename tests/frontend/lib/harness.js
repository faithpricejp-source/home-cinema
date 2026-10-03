/* 最小假 DOM + 假 XMLHttpRequest + 可控时钟，用来在 Node 里直接跑 project/homecinema/web/app.js。
   只使用 Node 内置模块（fs/path/vm）。审计用，不修改 project/ 下任何文件。

   用法：
     const { makeApp, HOME } = require("./lib/harness.js");
     const app = makeApp();                 // 加载 app.js，触发初始 route()
     await app.tick();                      // 让 hashchange -> route() 跑起来
     app.lastRequest().respond(200, {...}); // 手动控制响应和顺序
     await app.flush();                     // 排空 Promise 续作
     console.log(app.view.innerHTML);       // 当前视图渲染出来的 HTML
*/
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const REAL_SET_TIMEOUT = global.setTimeout;
const APP_JS = path.resolve(__dirname, "../../../homecinema/web/app.js");

function makeApp(opts) {
  opts = opts || {};
  const initialHash = opts.hash === undefined ? "#/" : opts.hash;
  const appJsPath = opts.appJsPath || APP_JS;

  /* ---------- 元素 ---------- */
  class El {
    constructor(tag, id) {
      this.tagName = String(tag || "div").toUpperCase();
      this.id = id || "";
      this.dataset = {};
      this.style = {};
      this.children = [];
      this.disabled = false;
      this.value = "";
      this._html = "";
      this._text = "";
      this._handlers = Object.create(null);
      this._qa = Object.create(null);
      this._generated = [];
      this._classes = new Set();
      this._owner = null;
      this.focused = false;
      const self = this;
      this.classList = {
        add(...cs) { cs.forEach((c) => self._classes.add(c)); },
        remove(...cs) { cs.forEach((c) => self._classes.delete(c)); },
        contains(c) { return self._classes.has(c); },
        toggle(c, force) {
          const want = force === undefined ? !self._classes.has(c) : !!force;
          if (want) self._classes.add(c); else self._classes.delete(c);
          return want;
        },
      };
    }
    get className() { return Array.from(this._classes).join(" "); }
    set className(v) {
      this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    }
    get innerHTML() { return this._html; }
    set innerHTML(v) {
      /* 重建内容 = 旧子树被销毁：丢掉上一次扫描出来的桩元素（含其子节点、含焦点） */
      for (const el of this._generated) disposeStub(el, this);
      this._generated = [];
      this._qa = Object.create(null);
      this._html = String(v);
      this._indexIds();
    }
    get textContent() { return this._text; }
    set textContent(v) { this._text = String(v); }
    appendChild(c) {
      this.children.push(c);
      c.parentNode = this;
      const q = this._qa;
      (q[c.tagName.toLowerCase()] = q[c.tagName.toLowerCase()] || []).push(c);
      (q.button = q.button || []).push(c);
      return c;
    }
    remove() {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    }
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); }
    removeEventListener(t, fn) {
      const a = this._handlers[t] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    }
    fire(t, extra) {
      const ev = Object.assign({ type: t, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} }, extra || {});
      for (const fn of (this._handlers[t] || []).slice()) fn(ev);
      return ev;
    }
    handlerCount(t) { return (this._handlers[t] || []).length; }
    focus() {
      document.activeElement = this;
      this.focused = true;
    }
    querySelectorAll(sel) { return this._find(String(sel)); }
    querySelector(sel) { return this._find(String(sel))[0] || null; }
    _find(sel) {
      const key = sel.replace(/^\./, "");
      const out = [];
      const src = [this._qa.a, this._qa[sel], this._qa[sel.replace(/^\./, "")], this._qa[key]];
      for (const list of src) if (list) for (const el of list) if (out.indexOf(el) < 0) out.push(el);
      return out;
    }
    _indexIds() {
      const html = this._html;
      let m;
      const idRe = /\bid="([\w-]+)"/g;
      while ((m = idRe.exec(html))) {
        if (api.byId[m[1]] && api.byId[m[1]]._owner === this) continue;
        if (protectedIds.has(m[1])) continue;
        const stub = new El("div", m[1]);
        stub._owner = this;
        api.byId[m[1]] = stub;
        this._generated.push(stub);
      }
      const classRe = /\bclass="([^"]*)"/g;
      while ((m = classRe.exec(html))) {
        for (const c of m[1].split(/\s+/)) {
          if (!c) continue;
          let stub = api.byId["__cls_" + c + "_" + this._generated.length + "_" + m.index];
          if (!stub) {
            stub = new El("div", "");
            stub.className = m[1];
            stub._owner = this;
            stub._classMatch = c;
          }
          (this._qa["." + c] = this._qa["." + c] || []).push(stub);
          (this._qa[c] = this._qa[c] || []).push(stub);
        }
      }
    }
  }

  /* ---------- 假全局 ---------- */
  function disposeStub(el, owner) {
    for (const c of el.children.slice()) disposeStub(c, owner);
    el.children = [];
    if (el.id && api.byId[el.id] === el) delete api.byId[el.id];
    if (document.activeElement === el) { document.activeElement = body; el.focused = false; }
  }
  const api = {
    byId: Object.create(null),
    requests: [],
    openCalls: [],
    errors: [],
    timers: [],
    nextTimerId: 1,
    now: 0,
  };

  const protectedIds = new Set(["view", "nav", "search", "rescan", "toast"]);

  const view = new El("main", "view");
  view._protected = true;
  const nav = new El("nav", "nav");
  const search = new El("input", "search");
  const rescan = new El("button", "rescan");
  const toastEl = new El("div", "toast");
  const body = new El("body", "");
  for (const name of ["home", "movies", "shows", "recs"]) {
    const a = new El("a", "");
    a.dataset.nav = name;
    a.parentNode = nav;
    nav.children.push(a);
    (nav._qa.a = nav._qa.a || []).push(a);
  }
  Object.assign(api.byId, { view, nav, search, rescan, toast: toastEl });

  const document = {
    getElementById(id) { return api.byId[id] || null; },
    createElement(tag) { return new El(tag); },
    addEventListener() {},
    activeElement: body,
    body,
    documentElement: new El("html", ""),
  };

  const windowHandlers = Object.create(null);
  const location = {
    _hash: initialHash,
    get hash() { return this._hash; },
    set hash(v) {
      if (v[0] !== "#") v = "#" + v;
      if (v === this._hash) return;
      this._hash = v;
      /* 真浏览器里 hashchange 是异步派发，这里走假时钟 */
      addTimer(() => fireWindow("hashchange"), 0);
    },
  };

  function fireWindow(type, ev) {
    for (const fn of (windowHandlers[type] || []).slice()) fn(Object.assign({ type }, ev || {}));
  }

  const window = {
    location,
    addEventListener(type, fn) { (windowHandlers[type] = windowHandlers[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const a = windowHandlers[type] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    },
    open(url, target, features) {
      api.openCalls.push({ url, target, features });
      return null;
    },
    document,
    setTimeout: (fn, ms) => addTimer(fn, ms),
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearTimeout: (id) => clearTimer(id),
    clearInterval: (id) => clearTimer(id),
  };

  class FakeXHR {
    constructor() { this.readyState = 0; this.status = 0; this.responseText = ""; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(k, v) { (this._headers = this._headers || {})[k] = v; }
    send(bodyObj) {
      this.body = bodyObj;
      api.requests.push(this);
      if (opts.autoRespond !== undefined) {
        flush().then(() => { if (!this._done) this.respond(opts.autoRespond.status, opts.autoRespond.body); });
      }
    }
    respond(status, payload) {
      this._done = true;
      this.status = status;
      this.responseText = typeof payload === "string" ? payload : JSON.stringify(payload);
      if (this.onload) this.onload();
    }
    networkError() { this._done = true; if (this.onerror) this.onerror(); }
  }

  /* ---------- 假时钟 ---------- */
  function addTimer(fn, ms, repeat) {
    const t = {
      id: api.nextTimerId++,
      at: api.now + (Number(ms) || 0),
      step: Number(ms) || 0,
      fn,
      repeat: !!repeat,
    };
    api.timers.push(t);
    return t.id;
  }
  function clearTimer(id) {
    const i = api.timers.findIndex((t) => t.id === id);
    if (i >= 0) api.timers.splice(i, 1);
  }
  function flush() {
    return new Promise((r) => REAL_SET_TIMEOUT(r, 0));
  }
  async function tick(ms) {
    const target = ms === undefined ? Infinity : api.now + ms;
    for (let guard = 0; guard < 100000; guard++) {
      const due = api.timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      api.now = Math.max(api.now, due.at);
      if (due.repeat) due.at = api.now + due.step;
      else clearTimer(due.id);
      /* 不 await 回调：浏览器也不会等 async listener。
         回调里的 await apiCall 需要测试稍后手动 respond，await 会死锁。 */
      try {
        Promise.resolve().then(() => due.fn()).catch((e) => { api.errors.push(e); });
      } catch (e) { api.errors.push(e); throw e; }
      await flush();
    }
    if (target !== Infinity) api.now = Math.max(api.now, target);
    return api.timers.length;
  }

  /* ---------- 加载 app.js ---------- */
  const src = fs.readFileSync(appJsPath, "utf8");
  const sandbox = {
    window, document, XMLHttpRequest: FakeXHR, URLSearchParams, console,
    setTimeout: (fn, ms) => addTimer(fn, ms),
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearTimeout: clearTimer,
    clearInterval: clearTimer,
    __diag: {},
  };
  const patched = src + "\n;Object.assign(globalThis.__diag, {" +
    "state: state, route: route, renderHome: renderHome, renderList: renderList," +
    "renderMovie: renderMovie, renderShow: renderShow, renderRecs: renderRecs," +
    "cardHTML: cardHTML, continueCardHTML: continueCardHTML, recCardHTML: recCardHTML," +
    "epRowHTML: epRowHTML, posterHTML: posterHTML, barHTML: barHTML, esc: esc, fmt: fmt," +
    "fmtTime: fmtTime, imgUrl: imgUrl, apiCall: apiCall, playItem: playItem," +
    "startRecRefresh: startRecRefresh, bindRecEvents: bindRecEvents, toast: toast," +
    "errorHTML: errorHTML, loadingHTML: loadingHTML, inEmbeddedApp: inEmbeddedApp});\n";
  const context = vm.createContext(sandbox);
  /* 看门狗：假时钟不会挂住 Node 事件循环，脚本若卡在 await 上会静默 exit 0。
     这个真实定时器既保住事件循环，也把「没走到显式 process.exit」暴露成 exit 3。 */
  REAL_SET_TIMEOUT(() => {
    console.log("HARNESS GUARD: 20s 内没有走到脚本末尾的 process.exit（很可能卡在 await 上）");
    process.exit(3);
  }, 20000);
  try {
    vm.runInContext(patched, context, { filename: "app.js" });
  } catch (e) {
    e.message = "app.js 在假 DOM 下加载失败：" + e.message;
    throw e;
  }

  const app = {
    ...sandbox,
    api,
    view, nav, search, rescan, toastEl, body, window, document, location,
    byId: api.byId,
    reqs: () => api.requests.slice(),
    lastRequest: () => api.requests[api.requests.length - 1],
    requestAt: (i) => api.requests[i],
    requestsFor: (sub) => api.requests.filter((r) => r.url.includes(sub)),
    setHash(h) { location.hash = h; },
    tick,
    flush,
    pending: () => api.timers.length,
    el(id) { return api.byId[id] || null; },
    fakeTarget(elOrObj, dataset) {
      /* 构造一个事件 target：closest(sel) 返回指定桩 */
      const card = elOrObj;
      return { target: Object.assign({ closest(sel) { return card && sel.includes(String(card._closestFor || "")) ? card : null; } }, { dataset: dataset || {} }) };
    },
  };
  return app;
}

/* ---------- 断言小工具：默认「断言正确行为」，在当前代码上失败 ---------- */
function assert(cond, msg, got) {
  if (!cond) {
    console.log("  FAIL " + msg);
    if (got !== undefined) console.log("  实际输出: " + JSON.stringify(got));
    return false;
  }
  console.log("  PASS " + msg);
  return true;
}

/* ---------- 后端数据桩（字段取自 server.py / db.py 的真实 payload） ---------- */
const HOME = {
  continue_watching: [
    {
      type: "movie", id: 7, title: "继续看这部", year: 2004,
      poster: "m7.jpg", backdrop: "b7.jpg", position_sec: 400, duration_sec: 1000,
      progress: 0.4, updated_at: "2026-10-01T00:00:00.000000+00:00", href: "#/movie/7",
    },
  ],
  recently_added: [
    { type: "movie", id: 7, title: "新来的电影", year: 2020, poster: null, added_at: "x", href: "#/movie/7" },
  ],
  movies: [
    { type: "movie", id: 1, href: "#/movie/1", title: "电影一", year: 2001, status: "matched", poster: null, backdrop: null, added_at: "x", watched: false },
  ],
  shows: [
    { type: "show", id: 2, href: "#/show/2", title: "剧集一", year: 2002, status: "matched", poster: null, backdrop: null, added_at: "x", episode_count: 6, watched_count: 1 },
  ],
};

const MOVIE = {
  type: "movie", id: 1, href: "#/movie/1", title: "电影一", year: 2001, status: "matched",
  poster: "p.jpg", backdrop: "b.jpg", added_at: "x", watched: false, overview: "简介",
  genres: ["动作"], rating: 7.5, runtime_minutes: 120, position_sec: 600,
  duration_sec: 1200, progress: 0.5,
};

const SHOW = {
  type: "show", id: 2, href: "#/show/2", title: "剧集一", year: 2002, status: "matched",
  poster: null, backdrop: null, added_at: "x", episode_count: 3, watched_count: 0,
  overview: "剧情简介", genres: ["剧情"], rating: 8.1, runtime_minutes: 45,
  seasons: [
    { season_number: 1, name: "第 1 季", episodes: [EP(1, 1), EP(1, 2)] },
    { season_number: 2, name: "第 2 季", episodes: [EP(2, 1)] },
  ],
  next_episode: EP(2, 1),
};

function EP(sn, en) {
  return {
    type: "episode", id: sn * 100 + en, show_id: 2, season_number: sn, episode_number: en,
    title: "S" + sn + "E" + en + " 集名", still: null, runtime_minutes: 45, air_date: null,
    missing: false, watched: false, position_sec: 100, duration_sec: 1000, progress: 0.1,
  };
}

const RECS = {
  movies: [{ kind: "movie", tmdb_id: 550, title: "推荐电影", year: 1999, overview: "", poster: null, rating: 8.4, because: ["搏击俱乐部"], url: "https://www.themoviedb.org/movie/550" }],
  shows: [],
  generated_at: "2026-10-01T00:00:00.000000+00:00",
};

function list(kind, marker) {
  const arr = [];
  for (let i = 0; i < 2; i++) {
    arr.push(kind === "shows"
      ? { type: "show", id: 10 + i, href: "#/show/" + (10 + i), title: marker + "剧集" + i, year: 2010, status: "matched", poster: null, backdrop: null, added_at: "x", episode_count: 4, watched_count: 0 }
      : { type: "movie", id: 20 + i, href: "#/movie/" + (20 + i), title: marker + "电影" + i, year: 2010, status: "matched", poster: null, backdrop: null, added_at: "x", watched: false });
  }
  return arr;
}

module.exports = { makeApp, assert, HOME, MOVIE, SHOW, RECS, EP, list, APP_JS };
