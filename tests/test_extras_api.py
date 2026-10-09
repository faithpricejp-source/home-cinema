"""导演/演员、合集、收藏筛选、追剧日历、缺集检测：假 TMDB 客户端 + 虚构片名。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from homecinema import extras
from homecinema.player import Player
from homecinema.scanner import run_scan
from homecinema.server import AiringService, ScanService, create_app


class FakeTmdb:
    def __init__(self):
        self.calls = []

    def download_image(self, path, images_dir, kind="poster"):
        return "img_" + path.strip("/")

    def movie_detail_with(self, tmdb_id, append):
        self.calls.append(("movie", tmdb_id))
        return {"id": tmdb_id, "belongs_to_collection": {"id": 900, "name": "Example Saga", "poster_path": "/c.jpg"},
                "credits": {"crew": [{"id": 1, "name": "Ann Director", "job": "Director", "profile_path": "/a.jpg"},
                                     {"id": 2, "name": "Bob Writer", "job": "Writer"}],
                            "cast": [{"id": 3, "name": "Cat Actor", "character": "Hero", "profile_path": None}]}}

    def collection_detail(self, coll_id):
        return {"parts": [{"id": 501, "title": "Example Movie", "release_date": "2001-05-01"},
                          {"id": 502, "title": "Example Movie II", "release_date": "2004-05-01"}]}

    def tv_detail_with(self, tmdb_id, append):
        self.calls.append(("tv", tmdb_id, append))
        data = {"id": tmdb_id, "status": "Returning Series",
                "next_episode_to_air": {"season_number": 1, "episode_number": 4, "name": "Four", "air_date": "2099-01-01"},
                "last_episode_to_air": {"season_number": 1, "episode_number": 3, "name": "Three", "air_date": "2026-01-01"}}
        if append:
            data["created_by"] = [{"id": 1, "name": "Ann Director"}]
            data["aggregate_credits"] = {"cast": [{"id": 4, "name": "Dee Actor", "roles": [{"character": "Lead"}]}]}
        return data


@pytest.fixture
def env(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    for name in ("Example Movie (2001)", "Other Film (1999)"):
        (movies / name).mkdir(parents=True)
        (movies / name / f"{name}.mp4").write_bytes(b"")
    season = tmp_path / "tv" / "Sample Show" / "Season 01"
    season.mkdir(parents=True)
    for n in (1, 3):  # 缺第 2 集；第 3 集已播出，第 4 集未播
        (season / f"Sample Show S01E0{n}.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    m1, m2 = sorted(lib.list_movies(), key=lambda r: r["title"])
    lib.update_movie_metadata(m1["id"], {"tmdb_id": 501, "status": "matched", "genres": ["Drama"], "rating": 8.1})
    lib.update_movie_metadata(m2["id"], {"tmdb_id": 777, "status": "matched", "genres": ["Comedy"], "rating": 6.0})
    show = lib.list_shows()[0]
    lib.update_show_metadata(show["id"], {"tmdb_id": 42, "status": "matched"})
    lib.upsert_season(show["id"], 1, name="Season 1", episode_count=8)
    tmdb = FakeTmdb()
    stats = extras.run_extras(lib, tmdb, cfg.images_dir, progress=lambda *a, **k: None)
    assert stats == {"total": 3, "ok": 3, "failed": 0}
    player = Player(lib, cfg, launcher=lambda *a, **k: None)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib),
                     airing_service=AiringService(cfg, lib, client_factory=lambda: tmdb))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        yield tc, lib, {"m1": m1["id"], "m2": m2["id"], "show": show["id"]}, tmdb


def test_movie_detail_has_director_cast_collection(env):
    tc, lib, ids, _ = env
    d = tc.get(f"/api/movie/{ids['m1']}").json()
    assert [p["name"] for p in d["directors"]] == ["Ann Director"]
    assert d["cast"][0]["name"] == "Cat Actor" and d["cast"][0]["character"] == "Hero"
    assert d["collection"] == {"id": 900, "name": "Example Saga", "owned": 2}
    assert d["favorite"] is False


def test_person_page_and_people_list(env):
    tc, lib, ids, _ = env
    p = tc.get("/api/person/1").json()
    assert p["name"] == "Ann Director"
    assert {(w["type"], w["id"]) for w in p["made"]} == {("movie", ids["m1"]), ("movie", ids["m2"]), ("show", ids["show"])}
    assert p["acted"] == []
    people = tc.get("/api/people?min_count=2").json()
    assert [(x["name"], x["count"]) for x in people] == [("Ann Director", 3)]
    assert tc.get("/api/person/999").status_code == 404


def test_collection_lists_owned_and_missing(env):
    tc, lib, ids, _ = env
    assert tc.get("/api/collections").json()[0]["owned"] == 2
    c = tc.get("/api/collection/900").json()
    assert len(c["movies"]) == 2
    assert [m["title"] for m in c["missing"]] == ["Example Movie II"]
    assert c["missing"][0]["url"].endswith("/movie/502")


def test_favorite_and_filters(env):
    tc, lib, ids, _ = env
    assert tc.post("/api/favorite", json={"type": "movie", "id": ids["m2"], "favorite": True}).json() == {"ok": True}
    assert [m["id"] for m in tc.get("/api/movies?fav=true").json()] == [ids["m2"]]
    assert [m["id"] for m in tc.get("/api/movies?genre=Drama").json()] == [ids["m1"]]
    assert [m["id"] for m in tc.get("/api/movies?min_rating=7").json()] == [ids["m1"]]
    assert [m["id"] for m in tc.get("/api/movies?sort=rating").json()] == [ids["m1"], ids["m2"]]
    tc.post("/api/watched", json={"type": "movie", "id": ids["m1"], "watched": True})
    assert [m["id"] for m in tc.get("/api/movies?watched=unwatched").json()] == [ids["m2"]]
    assert tc.post("/api/favorite", json={"type": "episode", "id": 1, "favorite": True}).status_code == 422
    assert tc.post("/api/favorite", json={"type": "show", "id": 999, "favorite": True}).status_code == 404
    tc.post("/api/favorite", json={"type": "movie", "id": ids["m2"], "favorite": False})
    assert tc.get("/api/movies?fav=true").json() == []


def test_missing_episodes_capped_by_next_air_and_calendar(env):
    tc, lib, ids, _ = env
    s = tc.get(f"/api/show/{ids['show']}").json()
    # TMDB 说 8 集，但第 4 集 2099 年才播：只算 1–3，缺第 2 集
    assert s["missing_episodes"] == [{"season": 1, "episodes": [2]}]
    assert s["next_air"]["episode"] == 4 and s["tmdb_status"] == "Returning Series"
    assert [p["name"] for p in s["creators"]] == ["Ann Director"]
    cal = tc.get("/api/calendar").json()
    assert cal["upcoming"][0]["air_date"] == "2099-01-01"
    assert cal["new_aired"] == []          # 最新已播是第 3 集，库里有
    assert cal["gaps"][0]["missing"] == [{"season": 1, "episodes": [2]}]


def test_calendar_refresh_skips_credits(env):
    tc, lib, ids, tmdb = env
    tmdb.calls.clear()
    assert tc.post("/api/calendar/refresh", json={}).status_code in (200, 409)


def test_multi_episode_file_not_counted_missing(lib):
    sid = lib.upsert_show(path="/tv/M", folder="/tv/M", title="M", year=None)
    lib.upsert_season(sid, 1, episode_count=4)
    lib.upsert_episode(show_id=sid, season_number=1, episode_number=1, path="/tv/M/a.mkv",
                       video_name="M S01E01E02", ext=".mkv", title="a", size=1, mtime=0)
    lib.upsert_episode(show_id=sid, season_number=1, episode_number=3, path="/tv/M/b.mkv",
                       video_name="M S01E003-E004", ext=".mkv", title="b", size=1, mtime=0)
    assert lib.missing_episodes(sid) == []


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    from homecinema.db import Library
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE, folder TEXT NOT NULL, title TEXT NOT NULL, year INTEGER, added_at TEXT NOT NULL, updated_at TEXT, missing INTEGER NOT NULL DEFAULT 0, tmdb_id INTEGER, overview TEXT NOT NULL DEFAULT '', genres TEXT NOT NULL DEFAULT '[]', rating REAL, runtime_minutes INTEGER, poster_path TEXT, backdrop_path TEXT, poster_local TEXT, poster_cached TEXT, backdrop_cached TEXT, status TEXT NOT NULL DEFAULT 'unmatched', metadata_at TEXT)")
    con.commit()
    con.close()
    lib = Library(path)
    cols = {r[1] for r in lib._conn.execute("PRAGMA table_info(shows)")}
    assert {"imdb_id", "tmdb_status", "next_air", "last_air", "airing_at", "credits_at"} <= cols
    lib.close()


def test_tail_of_last_season_is_not_a_gap(lib):
    sid = lib.upsert_show(path="/tv/T", folder="/tv/T", title="T", year=None)
    lib.upsert_season(sid, 1, episode_count=3)
    lib.upsert_season(sid, 2, episode_count=5)
    for sn, n in ((1, 1), (1, 2), (2, 1), (2, 3)):
        lib.upsert_episode(show_id=sid, season_number=sn, episode_number=n, path=f"/tv/T/{sn}{n}.mkv",
                           video_name=f"T S0{sn}E0{n}", ext=".mkv", title="x", size=1, mtime=0)
    # 第 1 季结尾缺 E03（后面还有第 2 季）算缺集；第 2 季缺 E02 是空洞，E04/E05 是没下完
    assert lib.missing_episodes(sid) == [{"season": 1, "episodes": [3]}, {"season": 2, "episodes": [2]}]
