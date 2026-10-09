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
    if not isinstance(data, dict):
        return None
    stamp = data.get("fetched_at")
    try:  # 缺写入时间或写入时间读不懂的缓存一律按过期处理（_older_than_days 对空值是「不过期」）
        datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if not stamp or _older_than_days(stamp, CACHE_TTL_DAYS):
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
        "fit": cand.get("fit"),
        "generated_at": generated_at,
    }


# ---------- 按口味重排（可选第二步，只作用于电影） ----------

def _movie_directors(client, cache_dir: Path, tmdb_id: int, refresh: bool) -> list[str]:
    """候选导演名列表：GET /movie/{id}/credits 的 crew 里 job==Director。

    缓存走 recs/credits_{id}.json（同 recommendations 的 7 天缓存方式）；
    空结果不缓存（网络失败与真无导演无法区分，缓存空会让该候选 7 天拿不到导演）。
    任何失败都返回空列表，不影响重排。
    """
    path = _cache_path(cache_dir, "credits", tmdb_id)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            return [str(n) for n in cached if n]
    try:
        credits = client.movie_credits(int(tmdb_id))
    except Exception:
        return []
    if not isinstance(credits, dict):
        return []
    names: list[str] = []
    for member in credits.get("crew") or []:
        if isinstance(member, dict) and member.get("job") == "Director":
            name = member.get("name")
            if name and name not in names:
                names.append(str(name))
    if names:
        _write_cache(path, names)
    return names


def _rerank_movie_picked(db: Library, picked: list[dict], llm_call,
                         client, cache_dir: Path, refresh: bool) -> list[dict]:
    """二段重排：最终分写回 score，fit 单独落列，why 追加在推荐理由之后。"""
    from . import rerank as rerank_mod
    profile = rerank_mod.taste_profile(db)
    candidates = []
    for cand in picked:
        item = cand["item"]
        candidates.append({"tmdb_id": int(item["id"]),
                           "title": _title(item, "movie"),
                           "original_title": _original_title(item, "movie"),
                           "year": _year(item, "movie"),
                           "overview": item.get("overview") or "",
                           "directors": _movie_directors(client, cache_dir,
                                                         int(item["id"]), refresh),
                           "score": cand["score"],
                           "cand": cand})
    ranked = rerank_mod.rerank_movies(candidates, profile,
                                      llm_call or rerank_mod.default_llm_call)
    out: list[dict] = []
    for entry in ranked:
        cand = entry["cand"]
        cand["score"] = entry["final_score"]
        cand["fit"] = entry["fit"]
        if entry["why"]:
            cand["because"].append(entry["why"])
        out.append(cand)
    return out


# ---------- 入口 ----------

def generate(db: Library, client, cache_dir, images_dir, refresh: bool = False,
             progress=None, rerank: bool = False, llm_call=None) -> dict:
    """汇总来源推荐 → 打分过滤（电影可选按口味重排）→ 整批落库。

    返回 {"movies", "shows", "sources"}。rerank=True 时电影候选二段重排
    （见 rerank.py）；剧集不受影响，默认关闭时行为与旧版完全一致。
    llm_call 供测试注入假实现，默认走 free_llm.chat。
    """
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
            if not isinstance(item, dict):
                continue  # Kimi-E-3：缓存文件可能被手工改坏/混入非对象，跳过，不中止整轮推荐
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
        if rerank and kind == "movie" and picked:
            picked = _rerank_movie_picked(db, picked, llm_call, client,
                                          cache_dir, refresh)
        for cand in picked:
            poster = None
            if cand["item"].get("poster_path"):
                poster = client.download_image(cand["item"]["poster_path"], images_dir, "poster")
            rows.append(_row(kind, cand, poster, now))
        counts[kind] = len(picked)

    db.save_recommendations(rows)
    return {"movies": counts["movie"], "shows": counts["tv"], "sources": total}
