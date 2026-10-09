"""一次性：给存量 ocr_tail 缓存补源文件指纹（Kimi-E-2 后续，2026-10-07 用户拍板）。

550c77c3 起新写的缓存带 "file": {size, mtime}，换源后会失效重算；此前写的约 3670 份
没有指纹，按旧格式规则照旧被沿用。本脚本只 stat 源文件、不重新截帧，把「当前文件」的
指纹写回缓存。这等于断言「缓存内容对应当前文件」，所以：

- 源文件 mtime 或 ctime 晚于缓存文件自身的写入时间（mtime）→ 缓存写完后源文件被改过/
  被替换过（cp -p、rsync -a 会保留 mtime，但替换必刷新 ctime），断言不成立 → 可疑，不补，单列。
  ctime 也会被 chmod/rename 刷新，会有误报，误报只落到「不补」这边，安全。
- 源文件 mtime、ctime 都早于缓存写入时间 → 缓存截帧时读到的就是当前这个文件 → 补。
- 源不存在 / 在未挂载的外置卷上 / 缓存的集 id 已不在库里 → 不动，分桶计数。
- 已有指纹 → 不动（与当前文件不符的也不覆盖，留给 load_or_extract 下次重算）。

写回为原子写（同目录临时文件 + os.replace），并保留缓存文件原 mtime（留作下次判可疑的证据）。
对外置卷只读（只 os.stat），只写缓存目录。

用法（默认 dry-run，只报数）：
  .venv/bin/python tools/backfill_ocr_fingerprint.py [--report out.json]
  .venv/bin/python tools/backfill_ocr_fingerprint.py --apply [--report out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homecinema.ocr_credits import cache_matches, fingerprint  # noqa: E402

DB = os.path.expanduser("~/Library/Application Support/HomeCinema/library.db")
CACHE = os.path.expanduser("~/Library/Caches/HomeCinema/ocr_tail")
NAME = re.compile(r"^(\d+)\.json$")


def _unmounted_volume(path: str) -> str | None:
    """路径在 /Volumes/<卷> 下且该卷没挂载 → 返回卷根，否则 None。"""
    parts = path.split(os.sep)
    if len(parts) > 2 and parts[0] == "" and parts[1] == "Volumes":
        root = os.sep + os.path.join("Volumes", parts[2])
        if not os.path.ismount(root):
            return root
    return None


def classify(cache_file: str, src_path: str | None) -> tuple[str, dict, os.stat_result | None]:
    """返回 (桶, 明细, 源 stat)。桶：bad/orphan/unmounted/missing/has_ok/has_mismatch/
    has_nosrc/suspicious/fill。"""
    try:
        with open(cache_file, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError) as exc:
        return "bad", {"error": str(exc)}, None
    if not isinstance(cached, dict):
        return "bad", {"error": f"不是对象：{type(cached).__name__}"}, None
    if src_path is None:
        return "orphan", {}, None
    vol = _unmounted_volume(src_path)
    try:
        st = os.stat(src_path)
    except OSError as exc:
        st = None
        err = str(exc)
    if cached.get("file"):
        if st is None:
            return "has_nosrc", {}, None
        return ("has_ok" if cache_matches(cached, st) else "has_mismatch"), {}, st
    if st is None:
        return ("unmounted", {"volume": vol}, None) if vol else ("missing", {"error": err}, None)
    cache_mtime = os.stat(cache_file).st_mtime
    reasons = []
    if st.st_mtime > cache_mtime:
        reasons.append("src_mtime>cache_mtime")
    if st.st_ctime > cache_mtime:
        reasons.append("src_ctime>cache_mtime")
    if reasons:
        return "suspicious", {"reasons": reasons, "cache_mtime": cache_mtime,
                              "src_mtime": st.st_mtime, "src_ctime": st.st_ctime}, st
    return "fill", {}, st


def write_fingerprint(cache_file: str, st: os.stat_result) -> bool:
    """原子写回 "file" 指纹，保留缓存文件原 atime/mtime。写前缓存被别人改过 → 放弃返回 False。"""
    before = os.stat(cache_file)
    with open(cache_file, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("file"):
        return False
    data["file"] = fingerprint(st)
    tmp = cache_file + ".fpfill.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
    if os.stat(cache_file).st_mtime_ns != before.st_mtime_ns:
        os.unlink(tmp)
        return False
    os.replace(tmp, cache_file)
    return True


def run(cache_dir: str, db_path: str, apply: bool) -> tuple[Counter, dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    paths = dict(con.execute("SELECT id, path FROM episodes"))
    con.close()
    counts: Counter = Counter()
    detail: dict = {"suspicious": {}, "missing": {}, "unmounted": {}, "orphan": [],
                    "bad": {}, "has_mismatch": [], "concurrent": []}
    for name in sorted(os.listdir(cache_dir)):
        m = NAME.match(name)
        if not m:
            continue
        eid = int(m.group(1))
        cache_file = os.path.join(cache_dir, name)
        bucket, info, st = classify(cache_file, paths.get(eid))
        counts["total"] += 1
        counts[bucket] += 1
        if bucket in ("suspicious", "missing", "unmounted", "bad"):
            detail[bucket][eid] = {"path": paths.get(eid), **info}
        elif bucket in ("orphan", "has_mismatch"):
            detail[bucket].append(eid)
        if bucket == "fill" and apply:
            if write_fingerprint(cache_file, st):
                counts["written"] += 1
            else:
                counts["concurrent"] += 1
                detail["concurrent"].append(eid)
    return counts, detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run 只报数）")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--report", help="可疑/源缺失等明细写到这个 JSON")
    args = ap.parse_args()
    counts, detail = run(args.cache, args.db, args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    has = counts["has_ok"] + counts["has_mismatch"] + counts["has_nosrc"]
    print(f"[{mode}] 缓存总数 {counts['total']}")
    print(f"  会补指纹 {counts['fill']}" + (f"（实写 {counts['written']}，写前被改放弃 {counts['concurrent']}）"
                                       if args.apply else ""))
    print(f"  可疑不补 {counts['suspicious']}")
    print(f"  源缺失 {counts['missing']}  未挂载卷 {counts['unmounted']}  库里无此集 {counts['orphan']}")
    print(f"  已有指纹 {has}（与源一致 {counts['has_ok']}，不一致 {counts['has_mismatch']}，源缺失 {counts['has_nosrc']}）")
    print(f"  缓存损坏 {counts['bad']}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode, "counts": dict(counts), "detail": detail}, fh,
                      ensure_ascii=False, indent=1)
        print(f"明细：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
