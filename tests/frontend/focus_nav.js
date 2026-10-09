"use strict";
/* focus_nav · 10-foot 键盘焦点导航（方向键 + Enter/空格 + 返回落点）
 *
 * 覆盖：
 *   1) 网格内左右上下移动（上下取同一视觉行、按列对齐）
 *   2) 横排之间上下切换时保持水平位置最接近的那张
 *   3) Enter 在卡片上等同点击
 *   4) 搜索框获得焦点时方向键不被劫持（ESC 仍先让输入框失焦）
 *   5) 进入详情页焦点落在「播放」按钮
 *   6) 从详情页返回列表后焦点回到进入前那张卡片（按 href 记）
 *   7) 焦点卡片有可见的 .nav-focus 样式（app.css 里真的写了）
 *
 * 做法：复用 lib/harness.js 的假 DOM/XHR/假时钟，给 El 原型补上
 * getBoundingClientRect / getAttribute / scrollIntoView / matches / closest /
 * parentNode 这些浏览器里有、假 DOM 里缺的东西，再按「每行几张、每张多大」
 * 把卡片的视觉位置摆好——navStep() 只认 getBoundingClientRect，所以布局一旦
 * 摆对，上下左右怎么走就是被真代码算出来的。
 *
 * 成功 exit 0，任一断言失败 exit 1。只用 Node 内置模块。
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { makeApp, assert, HOME, MOVIE, SHOW, EP } = require("./lib/harness.js");
const { rules, readCss } = require("./lib/css.js");

const APP_JS = path.resolve(__dirname, "../../homecinema/web/app.js");

/* ---------- 1. 给 app.js 装上「能收键盘事件、能问布局」的沙盒 ---------- */

/* harness 的 document.addEventListener 是空实现，收不到 app.js 的 keydown。
   在 app.js 前面插一段 prelude 包一层，把监听器记下来供测试派发事件。 */
const PRELUDE = `
;(function () {
  var rec = (globalThis.__keylog = { doc: {}, win: {} });
  function wrap(obj, bag) {
    var orig = obj.addEventListener;
    obj.addEventListener = function (type, fn) {
      (bag[type] = bag[type] || []).push(fn);
      if (orig) return orig.call(obj, type, fn);
    };
  }
  wrap(document, rec.doc);
  wrap(window, rec.win);

  /* harness 的沙盒里没有 history，app.js 的 ESC 处理器要用 history.back()。
     这里按 hash 变化维护一个栈，back() 弹回上一条并重新触发 hashchange。 */
  var stack = [window.location.hash];
  var desc = Object.getOwnPropertyDescriptor(window.location, "hash");
  Object.defineProperty(window.location, "hash", {
    get: function () { return desc.get.call(window.location); },
    set: function (v) {
      var before = desc.get.call(window.location);
      desc.set.call(window.location, v);
      if (desc.get.call(window.location) !== before) stack.push(desc.get.call(window.location));
    },
  });
  globalThis.history = {
    back: function () {
      if (stack.length > 1) stack.pop();
      desc.set.call(window.location, stack[stack.length - 1]);
    },
  };
})();
`;

function makeFocusApp(opts) {
  const file = path.join(os.tmpdir(), "hc_focus_nav_app_" + process.pid + ".js");
  fs.writeFileSync(file, PRELUDE + fs.readFileSync(APP_JS, "utf8"));
  try {
    const app = makeApp(Object.assign({ appJsPath: file }, opts || {}));
    installDom(app);
    return app;
  } finally {
    fs.unlinkSync(file);
  }
}

/* 浏览器里有的、harness 的 El 上没有的那几个方法 + 一个可摆放的布局。
   注意：这些方法是在 VM 之外定义的，被 app.js 调用时 this 是 VM 里的桩元素，
   所以一律通过 app.document / app.window 拿全局，不能直接引用宿主的 document。 */
function installDom(app) {
  const P = Object.getPrototypeOf(app.view);
  const doc = app.document;
  const win = app.window;

  P.getBoundingClientRect = function () {
    return this._rect || { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  };
  P.scrollIntoView = function (arg) {
    this._scrolled = arg === undefined ? true : arg;
  };
  P.getAttribute = function (name) {
    return this._attrs && name in this._attrs ? this._attrs[name] : null;
  };
  P.setAttribute = function (name, value) {
    (this._attrs = this._attrs || {})[name] = String(value);
  };
  P.closest = function (sel) {
    for (let el = this; el; el = el.parentNode) {
      if (sel.split(",").some((s) => matchesSimple(el, s.trim()))) return el;
    }
    return null;
  };
  P.matches = function (sel) {
    return sel.split(",").some((s) => matchesSimple(this, s.trim()));
  };
  P.click = function () {
    this._clicked = (this._clicked || 0) + 1;
    /* 真实浏览器里点一个 <a>/<button> 会让它成为焦点元素 */
    doc.activeElement = this;
    this.focused = true;
    for (const fn of this._handlers.click || []) {
      fn({ type: "click", target: this, preventDefault() {}, stopPropagation() {} });
    }
    /* 浏览器里点 <a href="#/…"> 会换 hash。测试里允许关掉，
       便于在同一次渲染上连按好几次键而桩元素不被换掉。 */
    const href = this.getAttribute("href");
    if (href && href[0] === "#" && app.__allowNav !== false) win.location.hash = href;
  };
  P.blur = function () {
    if (doc.activeElement === this) doc.activeElement = doc.body;
    this.focused = false;
  };
  P.focus = function () {
    doc.activeElement = this;
    this.focused = true;
  };

  /* harness 的 El 只有空壳：没有属性表/tagName 恒为 DIV/parentNode 为空，
     而且 querySelectorAll 只在自己一层（不递归子元素）。下面把这些补齐，
     让 app.js 的方向键逻辑能按真实 DOM 的样子跑。
     每次 innerHTML 一被赋值就自动重建索引，这样测试不用管「什么时候刷」。 */

  /* harness 用 innerHTML 造桩：只有 class 名和 id，没有属性表，tagName 恒为 DIV，
     parentNode 也是空的。这里把 HTML 按标签流重新走一遍，把每个标签对应的桩找出来，
     补上：属性表（getAttribute("href") 才有值）、真实 tagName、dataset、以及父子关系
     （事件要能从 .ep-row 冒泡到 #ep-list，app.js 的委托 keydown 才收得到）。
     桩与 HTML 的对应关系和 harness 的 _indexIds() 一致：class 属性每出现一次建一个桩，
     同一个桩登记在该属性列出的每个类名下。
     root 一般是 view；剧集行是直接写进 #ep-list.innerHTML 的，桩挂在那个元素上。 */
  const VOID = new Set(["img", "input", "br", "hr", "meta", "link", "source"]);

  const syncContainer = function (root) {
    const html = root.innerHTML;
    if (!html) return;
    /* 每个类名一个队列：按 class 属性出现顺序取桩，正好复现 harness 的登记顺序 */
    const queues = Object.create(null);
    for (const cls of Object.keys(root._qa || {})) {
      const key = cls[0] === "." ? cls.slice(1) : cls;
      const seen = new Set();
      queues[key] = (root._qa[cls] || []).filter((el) =>
        (seen.has(el) ? false : (seen.add(el), true)));
    }
    const taken = Object.create(null);
    for (const c of Object.keys(queues)) taken[c] = queues[c].slice();
    const stack = [{ el: root, tag: "" }];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
    let m;
    while ((m = re.exec(html))) {
      const closing = m[1] === "/";
      const tag = m[2];
      const attrs = parseAttrs(m[3]);
      const lower = tag.toLowerCase();
      if (closing) {
        while (stack.length > 1 && stack[stack.length - 1].tag !== lower) stack.pop();
        if (stack.length > 1) stack.pop();
        continue;
      }
      /* 找这个标签对应的桩：有 id 用 byId，否则按 class 队列顺序取 */
      let stub = null;
      if (attrs.id) {
        const byId = app.byId[attrs.id];
        if (byId && byId._owner === root) stub = byId;
      }
      if (!stub) {
        for (const c of String(attrs.class || "").split(/\s+/).filter(Boolean)) {
          if (taken[c] && taken[c].length) { stub = taken[c].shift(); break; }
        }
      }
      const parent = stack[stack.length - 1].el;
      if (stub) {
        stub._attrs = attrs;
        stub.tagName = tag.toUpperCase();
        stub.parentNode = parent;
        /* harness 的 El 只有个空的 dataset 对象。app.js 的委托处理器读的是
           row.dataset.ver / row.dataset.ep，这里按 data-* 属性填上。 */
        const ds = Object.create(null);
        for (const k of Object.keys(attrs)) {
          if (k.startsWith("data-")) {
            ds[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = attrs[k];
          }
        }
        stub.dataset = ds;
        for (const c of String(attrs.class || "").split(/\s+/).filter(Boolean)) {
          if (taken[c] && taken[c][0] === stub) taken[c].shift();
        }
      }
      if (!VOID.has(lower) && !m[4]) stack.push({ el: stub || parent, tag: lower });
    }
  };

  /* 刷一遍所有会被整体替换 innerHTML 的容器 */
  app.syncAttrs = function () {
    syncContainer(app.view);
    for (const id of ["ep-list", "ver-list", "sub-box", "tabs"]) {
      const el = app.byId[id];
      if (el && el._owner === app.view) syncContainer(el);
    }
  };

  /* 让 querySelector/querySelectorAll 能递归到子容器的桩上（浏览器里就是这么工作的）。
     app.js 的 navItems() 用 $view.querySelectorAll(".ep-row") 找剧集行，
     而剧集行的桩其实挂在 #ep-list 上——不桥接就找不到。 */
  const rawFind = P._find;
  P._find = function (sel) {
    const out = rawFind.call(this, String(sel));
    if (this !== app.view) return out;
    for (const id of ["ep-list", "ver-list", "sub-box", "tabs"]) {
      const el = app.byId[id];
      if (!el || el._owner !== app.view) continue;
      for (const e of rawFind.call(el, String(sel))) if (out.indexOf(e) < 0) out.push(e);
    }
    return out;
  };

  /* innerHTML 一被赋值就重建索引（含属性/tagName/父子关系） */
  const desc = Object.getOwnPropertyDescriptor(P, "innerHTML");
  Object.defineProperty(P, "innerHTML", {
    get() { return desc.get.call(this); },
    set(v) {
      desc.set.call(this, v);
      app.syncAttrs();
    },
    configurable: true,
  });

  /* 容器内所有导航元素（.ep-row 挂在 #ep-list 上，.card 挂在 view 上） */
  app.navEls = function (sel) {
    const out = [];
    for (const root of [app.view, app.byId["ep-list"], app.byId["ver-list"]]) {
      if (!root) continue;
      for (const el of root.querySelectorAll(sel)) if (out.indexOf(el) < 0) out.push(el);
    }
    return out;
  };

  /* 摆布局：els 按 cols 列排布，起始 top 用来把不同横排/网格分到不同纵向位置。
     parent 决定事件冒泡到哪（真实浏览器里由 DOM 结构决定，这里手动接上）。 */
  app.layoutRow = function (els, cols, top, w, h, gapX, gapY, parent) {
    w = w || 160; h = h || 240; gapX = gapX === undefined ? 20 : gapX; gapY = gapY === undefined ? 30 : gapY;
    els.forEach((el, i) => {
      const c = i % cols, r = Math.floor(i / cols);
      const left = c * (w + gapX), y = top + r * (h + gapY);
      el._rect = { left, top: y, width: w, height: h, right: left + w, bottom: y + h };
      el.parentNode = parent || app.view;
    });
    return els;
  };
}

function parseAttrs(s) {
  const out = {};
  const re = /([\w:-]+)(?:="([^"]*)")?/g;
  let m;
  while ((m = re.exec(s))) out[m[1]] = m[2] === undefined ? "" : m[2];
  return out;
}

function matchesSimple(el, sel) {
  if (!sel) return false;
  const parts = sel.match(/^([a-zA-Z]*)((?:\.[\w-]+)*)(\[[^\]]*\])?$/);
  if (!parts) return false;
  if (parts[1] && el.tagName !== parts[1].toUpperCase()) return false;
  for (const c of (parts[2].match(/\.[\w-]+/g) || [])) {
    if (!el.classList.contains(c.slice(1))) return false;
  }
  if (parts[3]) {
    const name = parts[3].slice(1, -1).split("=")[0];
    if (!el._attrs || !(name in el._attrs)) return false;
  }
  return true;
}

/* ---------- 2. 派发键盘事件 ---------- */

/* 按真实 DOM 的行为派发：从 target 沿 parentNode 冒泡到 document，
   所以绑在 #ver-list / #ep-list 上的委托 keydown 也能收到（app.js 靠它们处理剧集行）。
   与浏览器一致地补上原生激活：<button> 上按 Enter/空格会自己派发 click，
   没被 preventDefault 阻止的话。 */
function key(app, k, target, extra) {
  const ev = Object.assign({
    type: "keydown", key: k, target: target || app.view,
    ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
  }, extra || {});

  for (let el = ev.target; el; el = el.parentNode) {
    if (el._stopped) break;
    for (const fn of ((el._handlers || {}).keydown || []).slice()) {
      fn(ev);
      if (ev._stopped) break;
    }
  }
  if (!ev._stopped) {
    for (const fn of (app.__keylog.doc.keydown || []).slice()) fn(ev);
  }
  /* 原生激活：真实浏览器里 <button> 按 Enter/空格会 click（除非被 preventDefault） */
  const t = ev.target;
  if (!ev.defaultPrevented && t && t.tagName === "BUTTON" && (k === "Enter" || k === " ")
      && typeof t.click === "function") {
    t.click();
  }
  return ev;
}

function focusedCard(app) {
  const el = app.document.activeElement;
  if (!el) return null;
  return el.classList && el.classList.contains("nav-focus") ? el : null;
}

function titleOf(el) {
  const t = el && el._attrs && el._attrs.href;
  return t || (el && el.className) || "(null)";
}

/* 回应最后一条还没回过的、URL 含 sub 的请求。
   不能用 requestsFor(sub)[0]：同一个 URL 可能已经请求过好几轮（比如返回列表页）。 */
function respondLast(app, sub, body) {
  const rs = app.requestsFor(sub);
  for (let i = rs.length - 1; i >= 0; i--) {
    if (!rs[i]._done) { rs[i].respond(200, body); return true; }
  }
  return false;
}

/* ---------- 3. 各项测试 ---------- */

(async () => {
  let ok = true;

  /* ============ 1) 网格内左右上下移动 ============ */
  {
    const app = makeFocusApp({ hash: "#/movies" });
    await app.tick();
    app.lastRequest().respond(200, MOVIE_LIST);
    await app.flush();
    app.syncAttrs();

    const grid = app.view.querySelector(".grid");
    const cards = app.view.querySelectorAll(".card");
    console.log("\n[1] 列表页网格卡片数:", cards.length);
    /* 3 列 × 4 行 */
    app.layoutRow(cards, 3, 100, 160, 240, 20, 30);

    /* 从左上角那张开始 */
    key(app, "ArrowRight", null);
    let cur = focusedCard(app);
    ok = assert(!!cur && cur === cards[0], "没有焦点时按右键落到网格左上角第一张", titleOf(cur)) && ok;

    key(app, "ArrowRight", cur);
    ok = assert(focusedCard(app) === cards[1], "→ 走到同一行右边一张") && ok;

    key(app, "ArrowRight", cards[1]);
    ok = assert(focusedCard(app) === cards[2], "→ 再右一张（本行最后一张）") && ok;

    const edge = key(app, "ArrowRight", cards[2]);
    ok = assert(focusedCard(app) === cards[2] && !edge.defaultPrevented,
      "本行最右端再按 → 不动、也不吞掉按键") && ok;

    key(app, "ArrowDown", cards[2]);
    ok = assert(focusedCard(app) === cards[5], "↓ 落到下一行同一列（第 2 列）", titleOf(focusedCard(app))) && ok;

    key(app, "ArrowDown", cards[5]);
    ok = assert(focusedCard(app) === cards[8], "↓ 再下一行仍是同一列") && ok;

    key(app, "ArrowLeft", cards[8]);
    ok = assert(focusedCard(app) === cards[7], "← 回到左边一张") && ok;

    key(app, "ArrowUp", cards[7]);
    ok = assert(focusedCard(app) === cards[4], "↑ 回到上一行同一列") && ok;

    key(app, "ArrowUp", cards[4]);
    ok = assert(focusedCard(app) === cards[1], "顶行再按 ↑ 不动", titleOf(focusedCard(app))) && ok;

    console.log("  焦点样式 class:", focusedCard(app).className);
    ok = assert(focusedCard(app).classList.contains("nav-focus"),
      "焦点卡片带 .nav-focus") && ok;
    ok = assert(!!cards[1]._scrolled, "移动焦点后调了 scrollIntoView", cards[1]._scrolled) && ok;
    ok = assert(cards[1]._scrolled && cards[1]._scrolled.block === "nearest"
      && cards[1]._scrolled.inline === "nearest",
    "scrollIntoView 用 {block:'nearest', inline:'nearest'}", cards[1]._scrolled) && ok;
    ok = assert(!!app.document.activeElement.focused, "焦点真的落在卡片元素上") && ok;
    ok = assert(grid && cards.length === 12, "网格里有 12 张卡片供导航") && ok;
  }

  /* ============ 2) 横排之间上下切换保持水平位置 ============ */
  {
    const app = makeFocusApp({ hash: "#/" });
    await app.tick();
    app.lastRequest().respond(200, HOME_TWO_ROWS);
    await app.flush();
    app.syncAttrs();

    const cws = app.view.querySelectorAll(".cw-card");
    const cards = app.view.querySelectorAll(".card");
    console.log("\n[2] 继续观看", cws.length, "张 / .card", cards.length, "张");

    /* 摆成真实布局：继续观看(2 张宽卡) / 最近添加(横排 4 张) / 电影(网格 4 列 × 2 行)。
       .card 里前 4 张属于「最近添加」横排，后 8 张属于电影网格——它们同属 .card，
       只能靠 getBoundingClientRect 区分，这也正是 navRows/navStep 依赖的东西。 */
    app.layoutRow(cws, 2, 0, 400, 200, 20, 30);
    app.layoutRow(cards.slice(0, 4), 4, 320, 160, 240, 20, 30);
    app.layoutRow(cards.slice(4), 4, 620, 160, 240, 20, 30);

    const cx = (el) => Math.round(el._rect.left + el._rect.width / 2);
    console.log("  继续观看中心 x:", cws.map(cx).join(","),
      "最近添加:", cards.slice(0, 4).map(cx).join(","),
      "电影网格第 1 行:", cards.slice(4, 8).map(cx).join(","));

    /* 从「继续观看」第 2 张（中心 x=620）往下 → 最近添加横排里中心最近的那张（第 4 张） */
    key(app, "ArrowDown", cws[1]);
    let cur = focusedCard(app);
    ok = assert(cur === cards[3],
      "横排往下切到下一横排时落在水平位置最近的那张（cx=620 → 第 4 张，不是第 1 张）",
      cur && titleOf(cur)) && ok;

    /* 再往上回到继续观看：中心 620 对齐回第 2 张 */
    key(app, "ArrowUp", cur);
    ok = assert(focusedCard(app) === cws[1],
      "从横排往上回到水平位置对齐的继续观看卡片（往返一致）") && ok;

    /* 中心 x=200 不在任何列中心上：应取最近的 cx=260（第 2 张），而不是行首 */
    key(app, "ArrowDown", cws[0]);
    cur = focusedCard(app);
    console.log("  继续观看第 1 张 cx:", cx(cws[0]), "→ 落到:", titleOf(cur), "cx:", cur && cx(cur));
    ok = assert(cur === cards[1],
      "落点是最近的列（cx=200 → cx=260 那张），不是行首", cur && titleOf(cur)) && ok;

    /* 电影网格内部：第 1 张往下 → 网格第 1 列下一行 */
    key(app, "ArrowDown", cards[4]);
    ok = assert(focusedCard(app) === cards[8], "电影网格内 ↓ 保持同一列",
      titleOf(focusedCard(app))) && ok;

    /* 横排内左右：两张宽卡之间移动 */
    key(app, "ArrowRight", cws[0]);
    ok = assert(focusedCard(app) === cws[1],
      "横排内 → 从第 1 张走到第 2 张", titleOf(focusedCard(app))) && ok;
    key(app, "ArrowLeft", cws[1]);
    ok = assert(focusedCard(app) === cws[0], "横排内 ← 走回第 1 张") && ok;
    key(app, "ArrowLeft", cws[0]);
    ok = assert(focusedCard(app) === cws[0], "本排最左再按 ← 不动") && ok;

    /* 最后一排再往下没有邻居了：先把焦点落到最后一排，再按 ↓ */
    key(app, "ArrowUp", cards[10]);
    ok = assert(focusedCard(app) === cards[6], "从最后一行 ↑ 回到上一行同一列",
      titleOf(focusedCard(app))) && ok;
    key(app, "ArrowDown", cards[6]);
    ok = assert(focusedCard(app) === cards[10], "↓ 又回到最后一行", titleOf(focusedCard(app))) && ok;
    const before = focusedCard(app);
    key(app, "ArrowDown", before);
    ok = assert(focusedCard(app) === before && before === cards[10],
      "最后一行再按 ↓ 不动（没有下一行了）", titleOf(focusedCard(app))) && ok;
  }

  /* ============ 3) Enter / 空格在卡片上等同点击 ============ */
  {
    const app = makeFocusApp({ hash: "#/movies" });
    app.__allowNav = false;   /* 别让点击真的换 hash，否则桩元素会被换掉、焦点也就断了 */
    await app.tick();
    app.lastRequest().respond(200, MOVIE_LIST);
    await app.flush();
    app.syncAttrs();
    const cards = app.view.querySelectorAll(".card");
    app.layoutRow(cards, 3, 100, 160, 240, 20, 30);

    let clicked = 0;
    for (const c of cards) {
      const orig = c.click.bind(c);
      c.click = function () { clicked++; orig(); };
    }

    key(app, "Enter", cards[0]);
    ok = assert(clicked === 1, "卡片上按 Enter 触发一次点击", clicked) && ok;
    ok = assert(!!cards[0].focused, "卡片本身持有焦点（不是只加了样式）") && ok;

    key(app, " ", cards[0]);
    ok = assert(clicked === 2, "卡片上按空格同样触发点击", clicked) && ok;

    const ev = key(app, "Enter", cards[0]);
    ok = assert(clicked === 3, "Enter 每次都触发点击", clicked) && ok;
    ok = assert(ev.defaultPrevented,
      "Enter 被 preventDefault（否则浏览器会原生再激活一次链接，等于点两次）") && ok;

    /* 焦点在卡片内部的按钮上时不代劳（那是「不感兴趣」这类控件自己的语义） */
    const inner = {
      tagName: "BUTTON", className: "rec-dismiss", parentNode: cards[0],
      _handlers: {}, _attrs: {},
      classList: { contains: () => false },
      matches: () => false, closest: () => null,
    };
    key(app, "Enter", inner);
    ok = assert(clicked === 3, "焦点在卡片里的按钮上时不触发卡片点击", clicked) && ok;

    /* 剧集行交给它自己的 keydown 处理，不被通用逻辑抢走 */
    const app2 = makeFocusApp({ hash: "#/movie/1" });
    await app2.tick();
    app2.lastRequest().respond(200, MOVIE_VERSIONS);
    await app2.flush();
    app2.syncAttrs();
    const vrows = app2.navEls(".ep-row");
    app2.layoutRow(vrows, 1, 900, 900, 90, 0, 12, app2.el("ver-list"));
    app2.requestsFor("/api/play").length = 0;
    key(app2, "Enter", vrows[0]);
    await app2.tick();
    ok = assert(app2.requestsFor("/api/play").length === 1,
      "版本行上 Enter 只走它自己的处理，不被通用逻辑重复触发",
      app2.requestsFor("/api/play").length) && ok;
    for (const r of app2.requestsFor("/api/play")) r.respond(200, { path: "/x", start_at: 0, title: "t" });
    await app2.flush();
  }

  /* ============ 4) 搜索框里方向键不被劫持 ============ */
  {
    const app = makeFocusApp({ hash: "#/movies" });
    await app.tick();
    app.lastRequest().respond(200, MOVIE_LIST);
    await app.flush();
    app.syncAttrs();
    const cards = app.view.querySelectorAll(".card");
    app.layoutRow(cards, 3, 100, 160, 240, 20, 30);

    const search = app.search;
    search._attrs = { type: "search" };
    search.focus();
    ok = assert(app.document.activeElement === search, "搜索框已获得焦点") && ok;

    const before = app.document.activeElement;
    const ev = key(app, "ArrowDown", search);
    ok = assert(app.document.activeElement === before && before === search,
      "搜索框里按 ↓ 不移动卡片焦点") && ok;
    ok = assert(!ev.defaultPrevented, "搜索框里方向键不 preventDefault（光标能正常移动）") && ok;
    ok = assert(app.view.querySelectorAll(".card").every((c) => !c.classList.contains("nav-focus")),
      "搜索框有焦点时卡片上没有 .nav-focus") && ok;

    /* ESC 先让输入框失焦（已有行为，焦点导航不能破坏它） */
    key(app, "Escape", search);
    ok = assert(app.document.activeElement !== search, "ESC 让搜索框失焦") && ok;

    /* 失焦之后方向键又归导航管 */
    key(app, "ArrowRight", app.view);
    ok = assert(!!focusedCard(app), "输入框失焦后方向键恢复焦点导航") && ok;
  }

  /* ============ 5) 进入详情页焦点落在「播放」 ============ */
  {
    const app = makeFocusApp({ hash: "#/movies" });
    await app.tick();
    app.lastRequest().respond(200, MOVIE_LIST);
    await app.flush();
    app.syncAttrs();
    const cards = app.view.querySelectorAll(".card");
    app.layoutRow(cards, 3, 100, 160, 240, 20, 30);

    /* 从第 3 张卡片进去（记住这张） */
    key(app, "ArrowRight", cards[0]);
    key(app, "ArrowRight", cards[1]);
    ok = assert(focusedCard(app) === cards[2], "当前焦点在第 3 张卡片") && ok;

    app.setHash(cards[2]._attrs.href);
    await app.tick();
    respondLast(app, "/api/movie/", Object.assign({}, MOVIE, { id: 22, title: "电影二十二" }));
    await app.flush();
    app.syncAttrs();

    const play = app.el("play");
    ok = assert(!!play, "详情页有「播放」按钮") && ok;
    ok = assert(app.document.activeElement === play, "进入详情页焦点落在「播放」按钮",
      app.document.activeElement && app.document.activeElement.id) && ok;
    ok = assert(!!play.focused, "「播放」按钮是真实焦点 owner") && ok;

    /* Enter 触发播放：「播放」是 <button>，Enter 的原生激活就该发请求 */
    respondLast(app, "/api/play", { path: "/x.mkv", start_at: 0, title: "t" });
    const playsBefore = app.requestsFor("/api/play").length;
    key(app, "Enter", play);
    await app.tick();
    const playReqs = app.requestsFor("/api/play");
    ok = assert(playReqs.length === playsBefore + 1, "「播放」按钮上按 Enter 发起播放请求",
      playReqs.length) && ok;
    respondLast(app, "/api/play", { path: "/x.mkv", start_at: 0, title: "t" });
    await app.flush();

    /* 剧集详情：焦点落在「继续：S2E1」 */
    const app2 = makeFocusApp({ hash: "#/show/2" });
    await app2.tick();
    app2.lastRequest().respond(200, SHOW);
    await app2.flush();
    ok = assert(app2.document.activeElement === app2.el("play-next"),
      "剧集详情页焦点落在「继续播放」按钮",
      app2.document.activeElement && app2.document.activeElement.id) && ok;
  }

  /* ============ 6) 返回列表后焦点回到原来那张卡片 ============ */
  {
    const app = makeFocusApp({ hash: "#/movies" });
    await app.tick();
    app.lastRequest().respond(200, MOVIE_LIST);
    await app.flush();
    app.syncAttrs();
    const cards = app.view.querySelectorAll(".card");
    app.layoutRow(cards, 3, 100, 160, 240, 20, 30);
    const target = cards[7];
    const targetHref = target._attrs.href;
    console.log("\n[6] 进入详情的那张卡片 href:", targetHref);

    /* 走到第 8 张再进去 */
    key(app, "ArrowRight", cards[0]);
    for (let i = 1; i < 7; i++) key(app, "ArrowRight", cards[i]);
    ok = assert(focusedCard(app) === target, "先移动焦点到第 8 张卡片") && ok;

    app.setHash(targetHref);
    await app.tick();
    respondLast(app, "/api/movie/", MOVIE);
    await app.flush();
    ok = assert(app.document.activeElement === app.el("play"), "详情页焦点在「播放」") && ok;

    /* ESC 返回列表：ESC 走 navStack 回退，列表页重新请求并渲染 */
    key(app, "Escape", app.document.activeElement);
    await app.tick();
    respondLast(app, "/api/movies", MOVIE_LIST);
    await app.flush();
    app.syncAttrs();

    const back = app.view.querySelectorAll(".card");
    console.log("  返回后 hash:", app.location.hash, " 卡片数:", back.length);
    ok = assert(app.location.hash === "#/movies", "ESC 回到列表页") && ok;
    ok = assert(back.length === 12, "列表页重新渲染出 12 张卡片", back.length) && ok;
    const landed = focusedCard(app);
    ok = assert(!!landed, "返回后仍有卡片持有焦点") && ok;
    ok = assert(landed && landed._attrs.href === targetHref,
      "返回后焦点回到进入前那张卡片（按 href 匹配）", landed && titleOf(landed)) && ok;
    ok = assert(landed === back[7], "就是新渲染结果里的同一个 DOM 元素（不是旧桩）") && ok;

    /* 焦点落点用的是新渲染的桩，getBoundingClientRect 也得摆上（真实浏览器里布局已就绪） */
    app.layoutRow(back, 3, 100, 160, 240, 20, 30);
    key(app, "ArrowRight", landed);
    ok = assert(focusedCard(app) === back[8], "返回后方向键继续可用（从原卡片向右一张）",
      titleOf(focusedCard(app))) && ok;
  }

  /* ============ 7) 详情页剧集行/版本行：方向键 + Enter/Space 复用已有处理 ============ */
  {
    const app = makeFocusApp({ hash: "#/show/2" });
    await app.tick();
    app.lastRequest().respond(200, SHOW);
    await app.flush();
    app.syncAttrs();
    /* 剧集行是直接写进 #ep-list.innerHTML 的，桩挂在那个元素上；
       parent 也要设成 #ep-list，Enter 才能冒泡到 app.js 的委托 keydown。 */
    const epList = app.el("ep-list");
    const rows = app.navEls(".ep-row");
    console.log("\n[7] 剧集行数:", rows.length);
    app.layoutRow(rows, 1, 600, 900, 90, 0, 12, epList);
    ok = assert(rows.length === 1, "当前季渲染出 1 行（S2 只有 E1）", rows.length) && ok;

    key(app, "ArrowDown", app.el("play-next"));
    ok = assert(focusedCard(app) === rows[0], "从「继续播放」按 ↓ 落到剧集行") && ok;

    /* 剧集行已有自己的委托 keydown（Enter/空格播放）。通用逻辑不能重复触发，
       否则会发两次 /api/play、播放器被调两次。 */
    let playsBefore = app.requestsFor("/api/play").length;
    key(app, "Enter", rows[0]);
    await app.tick();
    ok = assert(app.requestsFor("/api/play").length === playsBefore + 1,
      "剧集行上按 Enter 播放该集（复用已有 keydown 处理，只发一次）",
      app.requestsFor("/api/play").length - playsBefore) && ok;
    respondLast(app, "/api/play", { path: "/e.mkv", start_at: 0, title: "e" });
    await app.flush();

    /* 版本行（电影详情多版本） */
    const app2 = makeFocusApp({ hash: "#/movie/1" });
    await app2.tick();
    app2.lastRequest().respond(200, MOVIE_VERSIONS);
    await app2.flush();
    app2.syncAttrs();
    const vrows = app2.navEls(".ep-row");
    app2.layoutRow(vrows, 1, 900, 900, 90, 0, 12, app2.el("ver-list"));
    console.log("  版本行数:", vrows.length);
    ok = assert(vrows.length === 2, "两个版本各一行") && ok;

    key(app2, "ArrowDown", app2.el("play"));
    ok = assert(focusedCard(app2) === vrows[0], "从「播放」按 ↓ 落到第一个版本行") && ok;
    key(app2, "ArrowDown", vrows[0]);
    ok = assert(focusedCard(app2) === vrows[1], "↓ 到第二个版本行") && ok;
    playsBefore = app2.requestsFor("/api/play").length;
    key(app2, "Enter", vrows[1]);
    await app2.tick();
    const vreqs = app2.requestsFor("/api/play");
    ok = assert(vreqs.length === playsBefore + 1,
      "版本行上按 Enter 播放该版本（同样只发一次）", vreqs.length - playsBefore) && ok;
    if (vreqs.length > playsBefore) {
      const body = JSON.parse(vreqs[vreqs.length - 1].body);
      ok = assert(body.id === 2, "播的是被聚焦的那个版本（data-ver=2）", body) && ok;
    }
    respondLast(app2, "/api/play", { path: "/v.mkv", start_at: 0, title: "v" });
    await app2.flush();
  }

  /* ============ 8) app.css 里真的有可见的焦点样式 ============ */
  {
    const rs = rules(readCss());
    const navRules = rs.filter((r) => /\.nav-focus/.test(r.sel));
    console.log("\n[8] .nav-focus 相关 CSS 规则:", navRules.length, "条");
    for (const r of navRules) console.log("   ", r.media + r.sel, "{", r.decl.slice(0, 70) + "}");
    ok = assert(navRules.length > 0, "app.css 写了 .nav-focus 样式") && ok;
    const ring = navRules.filter((r) => /outline:\s*2px solid var\(--accent\)/.test(r.decl)
      || /border-color:\s*var\(--accent\)/.test(r.decl) || /transform:/.test(r.decl));
    ok = assert(ring.length >= 3, "焦点样式包含描边/放大，不是只有颜色变化") && ok;
    const cardRing = rs.some((r) => /\.card\.nav-focus/.test(r.sel) && /transform:/.test(r.decl));
    ok = assert(cardRing, "海报卡片聚焦时放大（transform）") && ok;
    const posterRing = rs.some((r) => /\.card\.nav-focus \.poster/.test(r.sel)
      && (/outline:/.test(r.decl) || /border-color:/.test(r.decl)));
    ok = assert(posterRing, "海报本体有描边（卡片本身无背景，描边落在 .poster 上）") && ok;
    const focusVisible = rs.filter((r) => /:focus-visible/.test(r.sel) && /nav|\.card|\.ep-row/.test(r.sel));
    ok = assert(focusVisible.length > 0, "另有 :focus-visible 兜底（鼠标点击后的焦点也看得见）") && ok;
    const reduced = rs.filter((r) => /prefers-reduced-motion/.test(r.media));
    ok = assert(reduced.length > 0, "尊重 prefers-reduced-motion（仍描边但不位移）") && ok;
  }

  console.log(ok ? "\nRESULT: OK" : "\nRESULT: FAILED (exit 1)");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });

/* ---------- 数据桩 ---------- */

function MOVIE_ITEM(i) {
  return {
    type: "movie", id: 20 + i, href: "#/movie/" + (20 + i), title: "电影" + i, year: 2000 + i,
    status: "matched", poster: null, added_at: "x", watched: false, genres: [], rating: 7,
  };
}

/* 列表页：12 张卡（3 列 × 4 行）。/api/movies 直接返回数组。 */
const MOVIE_LIST = Array.from({ length: 12 }, (_, i) => MOVIE_ITEM(i));

/* 首页：继续观看 2 张 + 最近添加 4 张（横排）+ 电影 8 张（网格） */
const HOME_TWO_ROWS = {
  continue_watching: [
    { type: "movie", id: 7, title: "继续看一", year: 2004, poster: null, backdrop: null,
      position_sec: 400, duration_sec: 1000, progress: 0.4, href: "#/movie/7" },
    { type: "movie", id: 8, title: "继续看二", year: 2005, poster: null, backdrop: null,
      position_sec: 100, duration_sec: 1000, progress: 0.1, href: "#/movie/8" },
  ],
  recently_added: [0, 1, 2, 3].map((i) => Object.assign(MOVIE_ITEM(30 + i), { added_at: "x" })),
  movies: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => MOVIE_ITEM(40 + i)),
  shows: [],
};

/* 电影详情：两个版本 */
const MOVIE_VERSIONS = Object.assign({}, MOVIE, {
  id: 1, version_count: 2, default_version_id: 2,
  versions: [
    { id: 7, name: "导演剪辑版", video_height: 1080, ext: "mp4", size: 2147483648,
      watched: false, position_sec: 600, progress: 0.08, is_default: false },
    { id: 2, name: "加长版", video_height: 2160, ext: "mkv", size: 4294967296,
      watched: false, position_sec: 0, progress: null, is_default: true },
  ],
});