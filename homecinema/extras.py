"""TMDB 补充数据：导演/主创/演员、电影所属合集、剧集播出信息（追剧日历用）。

一部片一次请求：电影 /movie/{id}?append_to_response=credits（顺带 belongs_to_collection），
剧集 /tv/{id}?append_to_response=aggregate_credits（顺带 status / next_episode_to_air /
last_episode_to_air）。头像只下导演/主创和前 CAST_LIMIT 位演员。
"""

from __future__ import annotations

import json

from .db import Library

CAST_LIMIT = 10
ENDED = ("Ended", "Canceled")


def _people(client, images_dir, items: list[dict], role: str, limit: int | None,
            character_key: str | None = None) -> list[dict]:
    out = []
    for i, p in enumerate(items[:limit] if limit else items):
        if p.get("id") is None or not p.get("name"):
            continue
        cached = None
        if p.get("profile_path"):
            cached = client.download_image(p["profile_path"], images_dir, "profile")
        character = None
        if character_key == "roles":  # aggregate_credits：roles 列表
            roles = p.get("roles") or []
            character = roles[0].get("character") if roles else None
        elif character_key:
            character = p.get(character_key)
        out.append({"id": int(p["id"]), "name": p["name"], "profile_path": p.get("profile_path"),
                    "profile_cached": cached, "role": role, "character": character or None,
                    "ord": i})
    return out


def _parts(cdata: dict | None) -> list[dict] | None:
    """/collection/{id} 的成员 → [{tmdb_id, title, year}]；拉取失败返回 None。"""
    if not cdata or not isinstance(cdata.get("parts"), list):
        return None
    out = []
    for p in cdata["parts"]:
        if p.get("id"):
            y = (p.get("release_date") or "")[:4]
            out.append({"tmdb_id": p["id"], "title": p.get("title") or "",
                        "year": int(y) if y.isdigit() else None})
    return out


def fetch_movie_extras(db: Library, client, images_dir, movie) -> bool:
    data = client.movie_detail_with(movie["tmdb_id"], "credits")
    if not data:
        return False
    credits = data.get("credits") or {}
    crew = credits.get("crew") or []
    directors = [c for c in crew if c.get("job") == "Director"]
    people = (_people(client, images_dir, directors, "director", None)
              + _people(client, images_dir, credits.get("cast") or [], "cast", CAST_LIMIT, "character"))
    db.save_credits("movie", movie["id"], people)
    coll = data.get("belongs_to_collection") or None
    coll_id = None
    if coll and coll.get("id"):
        coll_id = int(coll["id"])
        poster = client.download_image(coll["poster_path"], images_dir) if coll.get("poster_path") else None
        parts = None
        if not db.collection_has_parts(coll_id):  # 合集成员一个合集只拉一次
            cdata = client.collection_detail(coll_id)
            parts = _parts(cdata)
        db.upsert_collection(coll_id, coll.get("name") or "", poster, parts)
    db.set_movie_extras(movie["id"], coll_id)
    return True


def _air(ep: dict | None) -> str | None:
    if not ep or not ep.get("air_date"):
        return None
    return json.dumps({"season": ep.get("season_number"), "episode": ep.get("episode_number"),
                       "name": ep.get("name") or "", "air_date": ep["air_date"]},
                      ensure_ascii=False)


def fetch_show_extras(db: Library, client, images_dir, show, with_credits: bool = True) -> bool:
    data = client.tv_detail_with(show["tmdb_id"], "aggregate_credits" if with_credits else None)
    if not data:
        return False
    if with_credits:
        agg = data.get("aggregate_credits") or {}
        people = (_people(client, images_dir, data.get("created_by") or [], "creator", None)
                  + _people(client, images_dir, agg.get("cast") or [], "cast", CAST_LIMIT, "roles"))
        db.save_credits("show", show["id"], people)
    db.set_show_airing(show["id"], data.get("status"), _air(data.get("next_episode_to_air")),
                       _air(data.get("last_episode_to_air")), credits_done=with_credits)
    return True


def run_extras(db: Library, client, images_dir, refresh: bool = False, progress=print) -> dict:
    """补还没拉过的（credits_at 为空）；refresh=True 全部重拉。每部拉完立即落库，可续跑。"""
    movies, shows = db.items_needing_extras(refresh)
    total = len(movies) + len(shows)
    stats = {"total": total, "ok": 0, "failed": 0}
    jobs = [(fetch_movie_extras, m) for m in movies] + [(fetch_show_extras, s) for s in shows]
    for i, (fn, row) in enumerate(jobs, 1):
        try:
            ok = fn(db, client, images_dir, row)
        except Exception as exc:  # 单条出错不中断
            ok = False
            progress(f"[{i}/{total}] {row['title']} 出错：{type(exc).__name__}: {exc}", flush=True)
        stats["ok" if ok else "failed"] += 1
        if ok:
            progress(f"[{i}/{total}] {row['title']}", flush=True)
    # 之前没拉到成员的合集补拉一次
    with db._lock:
        pending = db._conn.execute("SELECT id, name, poster_cached FROM collections WHERE parts IS NULL").fetchall()
    for c in pending:
        cdata = client.collection_detail(c["id"])
        parts = _parts(cdata)
        if parts is not None:
            db.upsert_collection(c["id"], c["name"], c["poster_cached"], parts)
    return stats


def person_aka(detail: dict | None) -> list[str] | None:
    """英文常用名 + also_known_as，去空白、去两端引号、去重保序；拉取失败返回 None。"""
    if not isinstance(detail, dict) or not detail.get("id"):
        return None
    out: list[str] = []
    for raw in [detail.get("name")] + list(detail.get("also_known_as") or []):
        name = str(raw or "").strip().strip("'\"").strip()
        if name and name not in out:
            out.append(name)
    return out


def run_people_aka(db: Library, client, refresh: bool = False, progress=print) -> dict:
    """10-07：给演职员补英文名和别名（原名、中文译名），一人一次请求、每人立即落库，可续跑。"""
    ids = db.people_needing_aka(refresh)
    stats = {"total": len(ids), "ok": 0, "failed": 0}
    for i, pid in enumerate(ids, 1):
        try:
            names = person_aka(client.person_detail(pid))
        except Exception:  # 单人出错不中断
            names = None
        if names is None:
            stats["failed"] += 1  # 不写 aka_at，下次重跑再补
            continue
        db.set_person_aka(pid, names)
        stats["ok"] += 1
        if progress and (i % 200 == 0 or i == len(ids)):
            progress(f"[人名 {i}/{len(ids)}]", flush=True)
    return stats


def refresh_airing(db: Library, client, images_dir, progress=None) -> int:
    """追剧日历刷新：只重拉没完结（或没拉过状态）的剧的播出信息，不重拉演员。"""
    shows = db.shows_for_airing()
    for i, s in enumerate(shows, 1):
        try:
            fetch_show_extras(db, client, images_dir, s, with_credits=False)
        except Exception:
            pass  # 单部失败下次再刷
        if progress:
            progress(i, len(shows), s["title"])
    return len(shows)


def probe_heights(db: Library, progress=print) -> int:
    """电影分辨率（筛选用）：ffprobe 只读文件头，取第一条视频流的高度。只补还没有的。"""
    import subprocess
    with db._lock:
        rows = db._conn.execute("SELECT id, path, title FROM movies WHERE missing=0 AND video_height IS NULL ORDER BY id").fetchall()
    for i, r in enumerate(rows, 1):
        height = None
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=height", "-of", "csv=p=0", r["path"]],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
            height = int(out.splitlines()[0]) if out else None
        except (subprocess.SubprocessError, ValueError, IndexError, OSError):
            height = None
        db.set_video_height(r["id"], height)
        progress(f"[{i}/{len(rows)}] {r['title']} {height}", flush=True)
    return len(rows)
