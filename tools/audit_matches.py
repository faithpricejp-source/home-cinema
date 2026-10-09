"""独立核对 TMDB 匹配：拿库里的 tmdb_id 回查英文片名和年份，跟文件夹名比，列出可疑的。

用法：.venv/bin/python tools/audit_matches.py > logs/match-audit.tsv
输出 TSV：kind, id, 文件夹片名, 文件夹年份, TMDB 英文名, TMDB 原名, TMDB 年份, 判定
判定：ok / suspicious（片名不像且年份对不上，或片名不像且没有年份）/ unmatched
     / error（TMDB 没回可比对的片名：key 失效、限流、5xx——不是匹配错了，重跑即可）
有 error 时退出码为 1，stderr 给出条数。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homecinema.config import load_config  # noqa: E402


def norm(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def main():
    cfg = load_config()
    key = cfg.read_tmdb_key()
    db = sqlite3.connect(cfg.db_file, timeout=30)
    rows = [("movie", *r) for r in db.execute("SELECT id, title, year, tmdb_id FROM movies WHERE missing=0")]
    rows += [("tv", *r) for r in db.execute("SELECT id, title, year, tmdb_id FROM shows WHERE missing=0")]
    http = httpx.Client(timeout=20)
    print("kind\tid\tfolder_title\tfolder_year\ttmdb_title\ttmdb_original\ttmdb_year\tverdict")
    errors = 0
    for kind, rid, title, year, tmdb_id in rows:
        if not tmdb_id:
            print(f"{kind}\t{rid}\t{title}\t{year or ''}\t\t\t\tunmatched", flush=True)
            continue
        d = {}
        for _ in range(3):
            try:
                resp = http.get(f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
                                params={"api_key": key, "language": "en-US"})
                if getattr(resp, "status_code", 200) != 200:
                    raise httpx.HTTPError(f"HTTP {resp.status_code}")
                d = resp.json()
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(2)
        name = d.get("title") or d.get("name") or ""
        orig = d.get("original_title") or d.get("original_name") or ""
        ty = (d.get("release_date") or d.get("first_air_date") or "")[:4]
        if not name:
            # 回查失败（key 失效、限流、5xx）时压根没有可比对的片名，
            # 「片名不像且年份对不上」不成立，不能混进可疑队列
            errors += 1
            verdict = "error"
        else:
            title_ok = max(sim(title, name), sim(title, orig)) >= 0.75
            year_ok = bool(year and ty.isdigit() and abs(int(ty) - int(year)) <= 1)
            verdict = "ok" if (title_ok and (year_ok or not year)) or (year_ok and title_ok) else "suspicious"
        print(f"{kind}\t{rid}\t{title}\t{year or ''}\t{name}\t{orig}\t{ty}\t{verdict}", flush=True)
        time.sleep(0.1)
    print(f"回查失败 {errors} 条（判定列 error，需重跑），共 {len(rows)} 条", file=sys.stderr, flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
