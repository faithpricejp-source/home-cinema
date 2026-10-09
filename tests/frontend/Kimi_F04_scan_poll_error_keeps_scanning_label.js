"use strict";
/* Kimi-F-4 · 扫描状态轮询遇到网络失败：定时器被清掉但按钮文字停在「扫描中…」，且没有任何提示
   位置：project/homecinema/web/app.js:891 pollScan() 的 catch 分支
         （对比正常结束分支 app.js:875-885：会复位文字并 toast 扫描结果）
   触发：点「重新扫描」→ POST /api/scan 成功 → pollScan 每 1s GET /api/scan/status
         → 某一次请求本身失败（服务刚重启、浏览器休眠唤醒）→ apiCall 走 xhr.onerror → reject
         → catch 里 clearInterval + $rescan.disabled = false，但不复位 textContent、不 toast。
   正确行为：轮询失败时按钮文字回到「重新扫描」，并 toast 一句「扫描状态获取失败…后台扫描仍在进行」，
             不能让用户以为扫描已经结束或已被取消。
   断言正确行为 —— 修复前应当失败（exit 1）。 */

const { makeApp, assert, HOME } = require("./lib/harness.js");

(async () => {
  const app = makeApp({ hash: "#/" });
  await app.tick();
  app.lastRequest().respond(200, HOME);
  await app.flush();

  /* 1. 点「重新扫描」，POST /api/scan 成功 */
  app.rescan.fire("click");
  await app.flush();
  const scanPost = app.requestsFor("/api/scan").filter((r) => r.method === "POST").pop();
  console.log("点击后请求:", scanPost && scanPost.method, scanPost && scanPost.url);
  scanPost.respond(200, { ok: true });
  await app.flush();
  console.log("按钮文字（扫描中）:", JSON.stringify(app.rescan.textContent),
              "disabled:", app.rescan.disabled);

  /* 2. 假时钟走 1s，轮询发出 GET /api/scan/status；先让它正常回一次「仍在扫描」 */
  await app.tick(1000);
  const st1 = app.requestsFor("/api/scan/status").pop();
  console.log("轮询请求:", st1 && st1.method, st1 && st1.url);
  st1.respond(200, { running: true, phase: "scan", movies: 0, episodes: 0,
                     metadata_done: 0, metadata_total: 0, error: null });
  await app.flush();
  console.log("按钮文字（第 1 轮后）:", JSON.stringify(app.rescan.textContent),
              "toast:", JSON.stringify(app.toastEl.textContent));

  /* 3. 第 2 轮轮询请求本身网络失败（xhr.onerror） */
  await app.tick(1000);
  const st2 = app.requestsFor("/api/scan/status").pop();
  console.log("第 2 轮请求 pending:", st2 && !st2._done);
  st2.networkError();
  await app.flush();

  console.log("网络失败后 按钮文字:", JSON.stringify(app.rescan.textContent));
  console.log("网络失败后 disabled:", app.rescan.disabled);
  console.log("网络失败后 toast   :", JSON.stringify(app.toastEl.textContent));
  console.log("网络失败后剩余定时器:", app.pending());

  const labelOk = assert(app.rescan.textContent === "重新扫描",
    "轮询失败后按钮文字应回到「重新扫描」（实际停在：" + app.rescan.textContent + "）");
  const toastOk = assert(/扫描状态获取失败/.test(app.toastEl.textContent),
    "轮询失败应 toast 一句状态获取失败、后台扫描仍在进行");
  const enabledOk = assert(app.rescan.disabled === false, "按钮应恢复可用（这条已成立）");

  const bad = !(labelOk && toastOk && enabledOk);
  console.log(bad ? "RESULT: BUG REPRODUCED (exit 1) — 静默停轮询 + 文字停在「扫描中…」"
                  : "RESULT: 未发现失败（此复现不成立）");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("harness 异常:", e); process.exit(2); });
