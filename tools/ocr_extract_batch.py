"""批量截帧+文字识别，特征缓存到 ~/Library/Caches/HomeCinema/ocr_tail/<集 id>.json。

顺序：先每季抽 1 集「已有片尾」的集当标准答案（调规则用），再跑所有「片尾为空」的集，
最后跑其余已有片尾的集（复核声纹结果）。
已缓存的集跳过，中断后重跑即续。用法：
  .venv/bin/python tools/ocr_extract_batch.py [--workers 4] [--gt-only]
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homecinema.detect import probe_duration  # noqa: E402
from homecinema.ocr_credits import load_or_extract  # noqa: E402

DB = os.path.expanduser("~/Library/Application Support/HomeCinema/library.db")
CACHE = os.path.expanduser("~/Library/Caches/HomeCinema/ocr_tail")


def targets(gt_only: bool) -> list[tuple[int, str, str]]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT e.id, e.path, e.show_id, e.season_number, s.credits_start IS NOT NULL "
        "FROM episodes e JOIN segments s ON s.episode_id = e.id WHERE e.missing = 0 "
        "ORDER BY e.show_id, e.season_number, e.episode_number").fetchall()
    by_season: dict = {}
    for eid, path, show, season, has in rows:
        if has:
            by_season.setdefault((show, season), []).append((eid, path))
    rng = random.Random(20261003)
    gt = [(eid, path, "gt") for eps in by_season.values() for eid, path in [rng.choice(eps)]]
    if gt_only:
        return gt
    todo = [(eid, path, "todo") for eid, path, _s, _n, has in rows if not has]
    picked = {eid for eid, _p, _k in gt}
    # 已有片尾的其余集也跑：声纹给的片尾起点有时偏早，会切掉结尾剧情，要用文字识别复核
    recheck = [(eid, path, "recheck") for eid, path, _s, _n, has in rows
               if has and eid not in picked]
    return gt + todo + recheck


def work(eid: int, path: str) -> tuple[int, float, str]:
    t0 = time.time()
    try:
        dur = probe_duration(path)
        if not dur:
            return eid, 0.0, "no-duration"
        # Kimi-E-2：缓存是否可用由 load_or_extract 校验源文件 size+mtime 后决定，
        # 这里不再「缓存文件存在就直接跳过」，否则换源后永远用旧帧特征
        data = load_or_extract(CACHE, eid, path, dur)
        return eid, time.time() - t0, f"ok frames={len(data['frames'])}"
    except Exception as exc:  # 单集失败记日志继续
        return eid, time.time() - t0, f"error {type(exc).__name__}: {exc}"[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--gt-only", action="store_true")
    args = ap.parse_args()
    items = targets(args.gt_only)
    kinds = {eid: kind for eid, _p, kind in items}
    print(f"共 {len(items)} 集（标准答案 {sum(k == 'gt' for k in kinds.values())}）", flush=True)
    done = 0
    with ProcessPoolExecutor(args.workers) as pool:
        futs = [pool.submit(work, eid, path) for eid, path, _k in items]
        for fut in as_completed(futs):
            eid, sec, msg = fut.result()
            done += 1
            print(f"[{done}/{len(items)}] {kinds[eid]} {eid} {sec:.1f}s {msg}", flush=True)
    print("截帧识别完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
