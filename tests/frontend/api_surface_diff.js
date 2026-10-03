"use strict";
/* API 面一致性核对（不是某条发现的复现，是审计要求的「逐个对」证据）：
   用正则从 app.js 抽出前端请求（方法/路径/查询参数），从 server.py 抽出后端路由与函数签名，打印差集。
   退出码：0 = 无差集（本仓库当前就是这样），1 = 出现前端调用了不存在的路由/参数不一致。 */

const fs = require("fs");
const path = require("path");

const JS = fs.readFileSync(path.resolve(__dirname, "../../homecinema/web/app.js"), "utf8");
const PY = fs.readFileSync(path.resolve(__dirname, "../../homecinema/server.py"), "utf8");
const HTML = fs.readFileSync(path.resolve(__dirname, "../../homecinema/web/index.html"), "utf8");

/* ---------- 后端路由 ---------- */
const routes = [];
const routeRe = /@app\.(get|post|put|delete)\("([^"]+)"\)\s*\n\s*def\s+(\w+)\(([^)]*)\)/g;
let m;
while ((m = routeRe.exec(PY))) {
  const params = m[4].split(",").map((s) => s.trim()).filter(Boolean)
    .filter((s) => s !== "self" && !/^(request|body|background)/.test(s))
    .map((s) => s.split(":")[0].trim());
  routes.push({ method: m[1].toUpperCase(), path: m[2], fn: m[3], params });
}
routes.push({ method: "GET", path: "/static/{rest}", fn: "mount(StaticFiles)", params: [] });

/* ---------- 前端调用 ---------- */
const calls = [];
const callRe = /apiCall\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*(`[^`]+`|"[^"]+")/g;
while ((m = callRe.exec(JS))) {
  const url = m[2].replace(/^[`"]|[`"]$/g, "").trim();
  calls.push({ method: m[1], raw: url, line: JS.slice(0, m.index).split("\n").length });
}
/* index.html 里的静态资源 */
for (const tag of HTML.match(/(?:href|src)="\/[^"]*"/g) || []) {
  calls.push({ method: "GET", raw: tag.split('="')[1].replace(/"/g, ""), line: 0, static: true });
}

/* 模板串 /api/${kind} 展开为 renderList 里实际可能取的两个值 */
const kindValues = ["movies", "shows"];
const expanded = [];
for (const c of calls) {
  if (c.raw.includes("${kind}")) {
    for (const k of kindValues) expanded.push(Object.assign({}, c, { raw: c.raw.replace("${kind}", k) }));
  } else expanded.push(c);
}
/* 去掉 query，记下 query 参数名 */
const front = expanded.map((c) => {
  const [p, qs] = c.raw.split("?");
  return Object.assign({}, c, {
    path: p,
    query: qs ? qs.split(/[&,]/).map((s) => s.split("=")[0]).filter((s) => !/^\$\{/.test(s) || s === "params") : [],
    hasTemplate: /\$\{/.test(c.raw),
  });
});
/* renderList 用 URLSearchParams 组装的查询参数 */
const usp = [...JS.matchAll(/params\.set\("([^"]+)"/g)].map((x) => x[1]);
const listCall = front.find((f) => /\/api\/(movies|shows)$/.test(f.path));
if (listCall) front.filter((f) => /\/api\/(movies|shows)$/.test(f.path)).forEach((f) => { f.query = usp; });

/* 图片不走 apiCall，但同样是拼出来的 URL（app.js:44-46 imgUrl） */
const imgLine = JS.split("\n").findIndex((l) => l.includes('"/img/"')) + 1;
if (imgLine > 0) front.push({ method: "GET", path: "/img/", raw: '"/img/" + encodeURIComponent(name)', query: [], line: imgLine, note: "HTML <img src>" });

/* ---------- 匹配 ---------- */
function routeMatches(routePath, callPath) {
  const rest = /\{rest\}$/.test(routePath);
  const rp = routePath.split("/").filter(Boolean).filter((s) => s !== "{rest}");
  const cp = callPath.split("/").filter(Boolean);
  const head = (a, b) => a.every((s, i) => s.startsWith("{") || s === b[i]);
  if (rest) return cp.length >= rp.length && head(rp, cp);
  if (cp.length === rp.length) return head(rp, cp);
  /* 前端把 id 拼在字符串外（"/api/movie/" + id）-> 末段占位符落在 call 串之外 */
  if (cp.length === rp.length - 1 && rp[rp.length - 1].startsWith("{")) return head(rp.slice(0, cp.length), cp);
  return false;
}

console.log("=== 后端路由（server.py）===");
for (const r of routes) console.log("  " + r.method.padEnd(5) + r.path + (r.params.length ? "   params: " + r.params.join(",") : ""));
console.log("\n=== 前端调用（app.js / index.html）===");
for (const f of front) console.log("  " + f.method.padEnd(5) + f.path +
  (f.query.length ? "?" + f.query.join("&") : "") + "   (app.js:" + f.line + ")");

console.log("\n=== 差集 A：前端调用了、后端没有的路由 ===");
const missing = front.filter((f) => !routes.some((r) => r.method === f.method && routeMatches(r.path, f.path)));
if (!missing.length) console.log("  （空）");
missing.forEach((f) => console.log("  " + f.method + " " + f.path));

console.log("\n=== 差集 B：后端有、前端没用的路由（仅信息）===");
const unused = routes.filter((r) => !front.some((f) => f.method === r.method && routeMatches(r.path, f.path)));
if (!unused.length) console.log("  （空）");
unused.forEach((r) => console.log("  " + r.method + " " + r.path));

console.log("\n=== 查询参数核对 ===");
let paramBad = 0;
for (const f of front.filter((x) => x.query.length)) {
  const r = routes.find((rt) => rt.method === f.method && routeMatches(rt.path, f.path));
  const q = f.query.filter((s) => s !== "params");
  const bad = r ? q.filter((s) => !r.params.includes(s)) : q;
  if (bad.length) paramBad++;
  console.log("  " + f.path + " 前端参数 " + JSON.stringify(q) +
    " | 后端签名 " + JSON.stringify(r ? r.params : null) +
    " -> " + (bad.length ? "不匹配: " + bad.join(",") : "OK"));
}

/* 路径里的 id：前端一律 parseInt 后才拼（app.js:174-176、231-233），后端是 int 路径参数 */
const idConcat = [...JS.matchAll(/"(\/api\/[a-z\/]+)"\s*\+\s*([A-Za-z_.]+)/g)].map((x) => x[1] + " + " + x[2]);
console.log("\n=== 路径拼接的 id 变量 ===");
console.log("  " + idConcat.join("\n  "));

console.log("\n结论: 差集 A " + (missing.length ? "非空 -> 有不存在的接口" : "为空") +
  "；查询参数不一致 " + paramBad + " 处");
process.exit(missing.length || paramBad ? 1 : 0);
