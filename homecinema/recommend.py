"""推荐计算：汇总 TMDB 推荐接口结果，结合观看记录加权、过滤后落库。

来源权重、名次衰减、过滤规则见下方常量与函数；缓存按来源条目存
`<cache_dir>/recs/{kind}_{tmdb_id}.json`，7 天内直接复用。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Library, utcnow

CACHE_TTL_DAYS = 7          # 缓存有效期
MIN_VOTE_COUNT = 150        # 候选最少投票数
MIN_VOTE_AVERAGE = 7.0      # 候选最低评分（6.5 时电影前排容易被热门流行片占满）
MOVIE_LIMIT = 30            # 电影推荐条数上限
SHOW_LIMIT = 20             # 剧集推荐条数上限
RANK_SPAN = 40              # 名次衰减：第 r 名贡献 w × (1 - r/40)
BECAUSE_LIMIT = 3           # 推荐理由保留的来源条数
ABANDON_DAYS = 7            # 放弃判定：最后播放超过 7 天
ABANDON_MIN_SECONDS = 30    # 放弃判定：至少看过 30 秒
ABANDON_MAX_PROGRESS = 0.10  # 放弃判定：进度 <10%
IN_PROGRESS_MIN_PROGRESS = 0.30  # 在看判定：进度 ≥30%


# ---------- 权重 ----------

def movie_weight(row) -> float:
    """来源电影的权重：已看 3、看到 30% 以上未看完 2、放弃 0.3，其余（含没播放过）1。"""
    watched = bool(row["watched"])
    position = row["position_sec"]
    duration = row["duration_sec"]
    progress = (position / duration) if (position is not None and duration) else None
    if watched:
        return 3.0
    if progress is not None and progress >= IN_PROGRESS_MIN_PROGRESS:
        return 2.0
    if (position is not None and position >= ABANDON_MIN_SECONDS
            and progress is not None and progress < ABANDON_MAX_PROGRESS
            and _older_than_days(row["played_at"], ABANDON_DAYS)):
        return 0.3
    return 1.0


def show_weight(watched_count: int) -> float:
    """来源剧集的权重：已看 ≥3 集 3、已看 1–2 集 2、没看过 1。"""
    if watched_count >= 3:
        return 3.0
    if watched_count >= 1:
        return 2.0
    return 1.0


def _older_than_days(stamp, days: int) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > timedelta(days=days)


# ---------- 来源 ----------

def _collect_sources(db: Library) -> list[dict]:
    """只用 missing=0 且有 tmdb_id 的条目作为来源。"""
    sources: list[dict] = []
    for row in db.movies_for_metadata(include_matched=True):
        if row["tmdb_id"] is None:
            continue
        full = db.get_movie(row["id"])
        if full is None:
            continue
        sources.append({"kind": "movie", "tmdb_id": int(row["tmdb_id"]),
                        "title": row["title"], "weight": movie_weight(full)})
    for row in db.list_shows():
        if row["tmdb_id"] is None:
            continue
        sources.append({"kind": "tv", "tmdb_id": int(row["tmdb_id"]),
                        "title": row["title"],
                        "weight": show_weight(int(row["watched_count"] or 0))})
    return sources


# ---------- 缓存 ----------

def _cache_path(cache_dir: Path, kind: str, tmdb_id: int) -> Path:
    return Path(cache_dir) / "recs" / f"{kind}_{int(tmdb_id)}.json"


def _read_cache(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or _older_than_days(data.get("fetched_at"), CACHE_TTL_DAYS):
        return None
    results = data.get("results")
    return results if isinstance(results, list) else None


def _write_cache(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": utcnow(), "results": results},
                               ensure_ascii=False), encoding="utf-8")


def _recommendations(client, cache_dir: Path, kind: str, tmdb_id: int, refresh: bool) -> list[dict]:
    path = _cache_path(cache_dir, kind, tmdb_id)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            return cached
    results = client.recommendations(kind, tmdb_id)
    # 空结果不缓存：client 在限速/网络失败时也返回 []，缓存下来会让这个来源 7 天不再重拉
    if results:
        _write_cache(path, results)
    return results


# ---------- 打分/过滤 ----------

def _top_titles(contributions: list[tuple[float, str]]) -> list[str]:
    ranked = sorted(contributions, key=lambda pair: pair[0], reverse=True)
    titles: list[str] = []
    for _score, title in ranked:
        if title not in titles:
            titles.append(title)
        if len(titles) >= BECAUSE_LIMIT:
            break
    return titles


def _title(item: dict, kind: str) -> str:
    if kind == "tv":
        return item.get("name") or item.get("original_name") or ""
    return item.get("title") or item.get("original_title") or ""


def _original_title(item: dict, kind: str):
    return item.get("original_name") if kind == "tv" else item.get("original_title")


def _year(item: dict, kind: str) -> int | None:
    date = item.get("first_air_date") if kind == "tv" else item.get("release_date")
    y = str(date or "")[:4]
    return int(y) if y.isdigit() else None


def _finalize(bucket: dict, owned: set[int], dismissed: set[tuple[str, int]],
              kind: str) -> list[dict]:
    out: list[dict] = []
    for cid, entry in bucket.items():
        if cid in owned or (kind, cid) in dismissed:
            continue
        item = entry["item"]
        if item.get("adult"):
            continue
        if (item.get("vote_count") or 0) < MIN_VOTE_COUNT:
            continue
        average = item.get("vote_average")
        if average is None or average < MIN_VOTE_AVERAGE:
            continue
        out.append({"score": entry["score"], "item": item,
                    "because": _top_titles(entry["because"])})
    out.sort(key=lambda c: (-c["score"], _title(c["item"], kind)))
    return out


def _row(kind: str, cand: dict, poster: str | None, generated_at: str) -> dict:
    item = cand["item"]
    return {
        "kind": kind, "tmdb_id": int(item["id"]),
        "title": _title(item, kind), "original_title": _original_title(item, kind),
        "year": _year(item, kind), "overview": item.get("overview") or "",
        "poster_cached": poster, "vote_average": item.get("vote_average"),
        "vote_count": item.get("vote_count"), "score": cand["score"],
        "because": json.dumps(cand["because"], ensure_ascii=False),
        "generated_at": generated_at,
    }


# ---------- 入口 ----------

def generate(db: Library, client, cache_dir, images_dir, refresh: bool = False,
             progress=None) -> dict:
    """汇总来源推荐 → 打分过滤 → 整批落库；返回 {"movies", "shows", "sources"}。"""
    cache_dir = Path(cache_dir)
    images_dir = Path(images_dir)
    sources = _collect_sources(db)
    owned = db.all_tmdb_ids()
    dismissed = db.dismissed_recommendations()
    buckets: dict[str, dict[int, dict]] = {"movie": {}, "tv": {}}

    total = len(sources)
    for done, src in enumerate(sources, start=1):
        kind = src["kind"]
        results = _recommendations(client, cache_dir, kind, src["tmdb_id"], refresh)
        bucket = buckets[kind]
        for rank, item in enumerate(results):
            cid = item.get("id")
            if cid is None:
                continue
            contribution = src["weight"] * (1 - rank / RANK_SPAN)
            entry = bucket.setdefault(cid, {"item": item, "score": 0.0, "because": []})
            entry["score"] += contribution
            entry["because"].append((contribution, src["title"]))
        if progress:
            progress(done, total, f"{src['title']}（{len(results)} 条）")

    now = utcnow()
    rows: list[dict] = []
    counts: dict[str, int] = {}
    for kind, limit in (("movie", MOVIE_LIMIT), ("tv", SHOW_LIMIT)):
        picked = _finalize(buckets[kind], owned[kind], dismissed, kind)[:limit]
        for cand in picked:
            poster = None
            if cand["item"].get("poster_path"):
                poster = client.download_image(cand["item"]["poster_path"], images_dir, "poster")
            rows.append(_row(kind, cand, poster, now))
        counts[kind] = len(picked)

    db.save_recommendations(rows)
    return {"movies": counts["movie"], "shows": counts["tv"], "sources": total}
